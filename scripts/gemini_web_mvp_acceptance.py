from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Sequence, TextIO
from unittest import mock
from urllib.parse import unquote, urlparse

import httpx
from fastapi import FastAPI
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api import accounts as accounts_api
from api import ai as ai_api
from api.errors import install_exception_handlers
from scripts.gemini_web_protocol_probe import (
    ProbeSafetyError,
    _cookie_mapping,
    _external_input,
    _external_output_dir,
    _read_reference,
    assert_scrubbed_fixture,
    execute_dry_run as execute_protocol_dry_run,
)
from services import account_service as account_service_module
from services import image_storage_service as image_storage_module
from services.account_service import AccountService
from services.config import config
from services.gemini_web_backend import (
    GeminiWebBackend,
    HttpGeminiWebTransport,
    HttpResultDownloader,
    PersistedGeminiWebAccountPool,
    create_gemini_web_validation_backend,
)
from services.image_storage_service import ImageStorageService
from services.protocol import conversation, openai_v1_image_edit
from services.protocol import openai_v1_image_generations, openai_v1_models


SAFE_CAPTURE_NAME = "gemini-web-mvp-acceptance.scrubbed.json"
MODEL = "gemini-web-image"


class MemoryStorage:
    def __init__(self) -> None:
        self.accounts: list[dict[str, Any]] = []

    def load_accounts(self) -> list[dict[str, Any]]:
        return list(self.accounts)

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self.accounts = list(accounts)

    def load_auth_keys(self) -> list[dict[str, Any]]:
        return []

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        return None

    def health_check(self) -> dict[str, Any]:
        return {"ok": True}

    def get_backend_info(self) -> dict[str, Any]:
        return {"type": "memory"}


def _valid_image(payload: bytes) -> bool:
    if not payload:
        return False
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
    except Exception:
        return False
    return True


def _stored_url_bytes(url: str, storage: ImageStorageService) -> bytes:
    path = unquote(urlparse(url).path)
    marker = "/images/"
    if marker not in path:
        raise ProbeSafetyError("invalid_project_image_url")
    return storage.get_bytes(path.split(marker, 1)[1])


def _first_raw_item(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise ProbeSafetyError("public_image_call_failed")
    payload = response.json()
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ProbeSafetyError("invalid_openai_image_response")
    return items[0]


def _generation_backend(service: AccountService) -> GeminiWebBackend:
    return GeminiWebBackend(
        account_pool=PersistedGeminiWebAccountPool(
            acquire_account=service.acquire_gemini_web_account,
            release_slot=service.release_image_slot,
            mark_invalid_account=service.mark_gemini_web_account_invalid,
        ),
        transport=HttpGeminiWebTransport(),
        downloader=HttpResultDownloader(),
        timeout_seconds=config.image_poll_timeout_secs,
    )


async def execute(
    *,
    cookie_file: Path,
    reference_image: Path,
    output_dir: Path,
) -> dict[str, Any]:
    cookie_path = _external_input(cookie_file, code="cookie_file_must_be_external")
    reference = _read_reference(reference_image)
    output = _external_output_dir(output_dir)
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise ProbeSafetyError("openai_client_dependency_missing") from None
    output.mkdir(parents=True, exist_ok=True)
    try:
        cookie_payload = json.loads(cookie_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProbeSafetyError("invalid_cookie_file") from None
    _cookie_mapping(cookie_payload)

    image_root = output / "ticket-16-images"
    image_root.mkdir(parents=True, exist_ok=True)
    image_config = SimpleNamespace(
        images_dir=image_root,
        base_url="http://testserver",
        cleanup_old_images=lambda: 0,
        get_image_storage_settings=lambda: {
            "enabled": False,
            "mode": "local",
            "public_base_url": "",
        },
    )
    image_storage = ImageStorageService(output / "ticket-16-image-index.json")
    service = AccountService(
        MemoryStorage(),
        gemini_web_backend=create_gemini_web_validation_backend(),
    )
    api_key = "ticket-16-ephemeral-key"

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(accounts_api.create_router())
    app.include_router(ai_api.create_router())

    async def skip_filter_or_log(*_args: Any, **_kwargs: Any) -> None:
        return None

    def backend_factory() -> GeminiWebBackend:
        return _generation_backend(service)

    patches = (
        mock.patch.object(accounts_api, "account_service", service),
        mock.patch.object(account_service_module, "account_service", service),
        mock.patch.object(openai_v1_models, "account_service", service),
        mock.patch.object(conversation, "image_storage_service", image_storage),
        mock.patch.object(image_storage_module, "config", image_config),
        mock.patch.object(ai_api, "filter_or_log", skip_filter_or_log),
        mock.patch("services.log_service.log_service.add"),
        mock.patch.object(
            openai_v1_image_generations,
            "create_gemini_web_generation_backend",
            side_effect=backend_factory,
        ),
        mock.patch.object(
            openai_v1_image_edit,
            "create_gemini_web_generation_backend",
            side_effect=backend_factory,
        ),
        mock.patch.dict(config.data, {"auth-key": api_key}),
    )
    for patcher in patches:
        patcher.start()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            timeout=180,
        ) as raw:
            headers = {"Authorization": f"Bearer {api_key}"}
            imported = await raw.post(
                "/api/accounts/gemini-web",
                headers=headers,
                json={"cookie_json": cookie_payload},
            )
            if imported.status_code != 200:
                raise ProbeSafetyError("formal_account_import_failed")
            imported_item = imported.json().get("item")
            if not isinstance(imported_item, dict):
                raise ProbeSafetyError("formal_account_import_failed")
            identifier = str(imported_item.get("account_id") or "")
            if not identifier:
                raise ProbeSafetyError("formal_account_import_failed")

            listed = await raw.get("/api/accounts", headers=headers)
            listed_items = listed.json().get("items") if listed.status_code == 200 else None
            if not isinstance(listed_items, list) or len(listed_items) != 1:
                raise ProbeSafetyError("formal_account_list_failed")
            if any(
                key in listed_items[0]
                for key in ("credentials", "cookies", "access_token")
            ):
                raise ProbeSafetyError("public_account_leaked_credentials")

            validated = await raw.post(
                "/api/accounts/validate",
                headers=headers,
                json={"identifiers": [identifier]},
            )
            if validated.status_code != 200 or not validated.json().get("valid"):
                raise ProbeSafetyError("formal_account_validation_failed")

            disabled = await raw.post(
                "/api/accounts/update",
                headers=headers,
                json={"account_id": identifier, "status": "禁用"},
            )
            if disabled.status_code != 200:
                raise ProbeSafetyError("formal_account_disable_failed")
            unavailable = await raw.post(
                "/v1/images/generations",
                headers=headers,
                json={"model": MODEL, "prompt": "Generate a simple blue circle."},
            )
            if unavailable.status_code != 503:
                raise ProbeSafetyError("disabled_account_guard_failed")
            restored = await raw.post(
                "/api/accounts/update",
                headers=headers,
                json={"account_id": identifier, "status": "正常"},
            )
            if restored.status_code != 200:
                raise ProbeSafetyError("formal_account_restore_failed")

            raw_generation = await raw.post(
                "/v1/images/generations",
                headers=headers,
                json={
                    "model": MODEL,
                    "prompt": "Generate a minimal red paper-cut bird on a plain background.",
                    "response_format": "b64_json",
                },
            )
            raw_generation_item = _first_raw_item(raw_generation)
            raw_generation_bytes = base64.b64decode(
                str(raw_generation_item.get("b64_json") or ""),
                validate=True,
            )

            raw_edit = await raw.post(
                "/v1/images/edits",
                headers=headers,
                data={
                    "model": MODEL,
                    "prompt": "Transform the reference into a visibly different watercolor poster.",
                    "response_format": "url",
                },
                files={
                    "image": (
                        "reference.png",
                        reference.content,
                        reference.mime_type,
                    )
                },
            )
            raw_edit_item = _first_raw_item(raw_edit)
            raw_edit_bytes = _stored_url_bytes(
                str(raw_edit_item.get("url") or ""), image_storage
            )

            sdk_http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                timeout=180,
            )
            client = AsyncOpenAI(
                api_key=api_key,
                base_url="http://testserver/v1",
                http_client=sdk_http,
            )
            try:
                sdk_generation = await client.images.generate(
                    model=MODEL,
                    prompt="Generate a minimal green linocut fish on a plain background.",
                    response_format="url",
                )
                sdk_generation_item = sdk_generation.data[0]
                sdk_generation_bytes = _stored_url_bytes(
                    str(sdk_generation_item.url or ""), image_storage
                )
                sdk_edit = await client.images.edit(
                    model=MODEL,
                    prompt="Transform the reference into a visibly different ink illustration.",
                    image=("reference.png", reference.content, reference.mime_type),
                    response_format="b64_json",
                )
                sdk_edit_item = sdk_edit.data[0]
                sdk_edit_bytes = base64.b64decode(
                    str(sdk_edit_item.b64_json or ""), validate=True
                )
            finally:
                await client.close()

        valid_images = all(
            _valid_image(payload)
            for payload in (
                raw_generation_bytes,
                raw_edit_bytes,
                sdk_generation_bytes,
                sdk_edit_bytes,
            )
        )
        edit_distinct = all(
            payload != reference.content for payload in (raw_edit_bytes, sdk_edit_bytes)
        )
        if not valid_images or not edit_distinct:
            raise ProbeSafetyError("acceptance_image_validation_failed")

        summary = {
            "status": "ok",
            "formal_management": {
                "imported": True,
                "listed_without_credentials": True,
                "validated": True,
                "disabled": True,
                "restored": True,
            },
            "disabled_guard": {"rejected": True, "status_code": 503},
            "public_http": {
                "generation": {"response_format": "b64_json", "valid_image": True},
                "edit": {
                    "response_format": "url",
                    "valid_image": True,
                    "distinct_from_reference": True,
                },
            },
            "official_openai_client": {
                "generation": {"response_format": "url", "valid_image": True},
                "edit": {
                    "response_format": "b64_json",
                    "valid_image": True,
                    "distinct_from_reference": True,
                },
            },
            "real_calls": {"generation_count": 2, "edit_count": 2},
            "credential_persistence": "memory_only",
        }
        assert_scrubbed_fixture(summary)
        (output / SAFE_CAPTURE_NAME).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return summary
    finally:
        for patcher in reversed(patches):
            patcher.stop()


def execute_dry_run() -> dict[str, Any]:
    protocol = execute_protocol_dry_run()
    summary = {
        "schema_version": 1,
        "mode": "dry-run",
        "status": "ok",
        "fixture_only": True,
        "credentials_read": False,
        "network_requests": 0,
        "protocol": protocol,
    }
    assert_scrubbed_fixture(summary)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the scrubbed Gemini Web MVP end-to-end acceptance flow."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("dry-run", help="Use repository fixtures only; never read credentials.")
    live = subparsers.add_parser("live", help="Run the explicitly authorized live acceptance.")
    live.add_argument("--cookie-file", type=Path, required=True)
    live.add_argument("--reference-image", type=Path, required=True)
    live.add_argument("--output-dir", type=Path, required=True)
    live.add_argument("--acknowledge-live-probe", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    live_executor: Callable[..., Awaitable[dict[str, Any]]] = execute,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "dry-run":
            summary = execute_dry_run()
        else:
            if not args.acknowledge_live_probe:
                raise ProbeSafetyError("live_probe_acknowledgement_required")
            summary = asyncio.run(
                live_executor(
                    cookie_file=args.cookie_file,
                    reference_image=args.reference_image,
                    output_dir=args.output_dir,
                )
            )
    except ProbeSafetyError as error:
        stdout.write(
            json.dumps(
                {"mode": args.mode, "status": "error", "error": error.code},
                separators=(",", ":"),
            )
            + "\n"
        )
        return 2
    except Exception as error:
        stdout.write(
            json.dumps(
                {
                    "mode": args.mode,
                    "status": "error",
                    "error": error.__class__.__name__,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        return 1
    stdout.write(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
