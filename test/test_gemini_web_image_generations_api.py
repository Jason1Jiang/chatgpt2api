from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import ai as ai_api
from api.errors import install_exception_handlers
from services.account_service import AccountService
from services.gemini_web_backend import (
    DownloadedImage,
    FirstNormalGeminiWebAccountPool,
    GeminiWebBackend,
)
from services.image_storage_service import ImageStorageService
from services.protocol import openai_v1_image_generations, openai_v1_models
from services.protocol.conversation import ImageOutput
from test.gemini_web_fixtures import (
    FixtureGeminiWebTransport,
)
from test.test_gemini_web_account_management import MemoryStorage


AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}
FIXTURE_DIR = Path(__file__).with_name("fixtures") / "gemini_web"
FAKE_COOKIE = "FAKE_GENERATION_COOKIE_MUST_NOT_ESCAPE"
VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ValidPngFixtureDownloader:
    def download(self, *args: object, **kwargs: object) -> DownloadedImage:
        return DownloadedImage(content=VALID_PNG, mime_type="image/png")


def _gemini_account(account_id: str, *, status: str) -> dict[str, object]:
    return {
        "account_id": account_id,
        "provider": "gemini_web",
        "type": "Gemini Web",
        "source_type": "cookie_json",
        "status": status,
        "credentials": {"cookies": {"__Secure-1PSID": FAKE_COOKIE}},
    }


class GeminiWebImageGenerationsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage(
            [
                _gemini_account("gemini_web:disabled", status="禁用"),
                _gemini_account("gemini_web:normal", status="正常"),
            ]
        )
        self.account_service = AccountService(self.storage)
        self.pool = FirstNormalGeminiWebAccountPool(
            list_accounts=self.account_service.list_accounts,
            get_account=self.account_service.get_account,
        )
        self.backend = GeminiWebBackend(
            account_pool=self.pool,
            transport=FixtureGeminiWebTransport(FIXTURE_DIR),
            downloader=ValidPngFixtureDownloader(),
            timeout_seconds=30,
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.image_config = SimpleNamespace(
            images_dir=root / "images",
            base_url="",
            cleanup_old_images=lambda: 0,
            get_image_storage_settings=lambda: {
                "enabled": False,
                "mode": "local",
                "public_base_url": "",
            },
        )
        self.image_config.images_dir.mkdir(parents=True)
        self.image_storage = ImageStorageService(root / "image_index.json")

        patches = (
            mock.patch.object(
                ai_api,
                "require_identity",
                return_value={"id": "test-user", "role": "admin"},
            ),
            mock.patch.object(ai_api, "filter_or_log", mock.AsyncMock()),
            mock.patch.object(
                openai_v1_image_generations,
                "create_gemini_web_generation_backend",
                return_value=self.backend,
            ),
            mock.patch(
                "services.image_storage_service.config",
                self.image_config,
            ),
            mock.patch(
                "services.protocol.conversation.image_storage_service",
                self.image_storage,
            ),
            mock.patch("services.log_service.log_service.add"),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(ai_api.create_router())
        self.client = TestClient(app)

    def test_public_b64_json_generation_uses_fixture_backend_and_valid_bytes(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "model": "gemini-web-image",
                "prompt": "fixture prompt",
                "n": 1,
                "size": "1024x1024",
                "quality": "high",
                "response_format": "b64_json",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["data"][0]
        image_bytes = base64.b64decode(item["b64_json"])
        self.assertEqual(image_bytes, VALID_PNG)
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as image:
            self.assertEqual((image.format, image.size), ("PNG", (1, 1)))
        self.assertNotIn("fixture://", response.text)
        self.assertNotIn(FAKE_COOKIE, response.text)
        self.assertIn("usage", response.json())

    def test_public_url_generation_uses_project_storage_not_gemini_url(self) -> None:
        response = self.client.post(
            "/v1/images/generations",
            headers=AUTH_HEADERS,
            json={
                "model": "gemini-web-image",
                "prompt": "fixture url prompt",
                "n": 1,
                "response_format": "url",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["data"][0]
        self.assertNotIn("b64_json", item)
        self.assertTrue(item["url"].startswith("http://testserver/images/"), item)
        self.assertNotIn("fixture://", item["url"])
        rel = item["url"].split("/images/", 1)[1]
        self.assertEqual(self.image_storage.get_bytes(rel), VALID_PNG)

    def test_n_greater_than_one_is_rejected_without_acquiring_an_account(self) -> None:
        with mock.patch.object(self.pool, "acquire", wraps=self.pool.acquire) as acquire:
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={
                    "model": "gemini-web-image",
                    "prompt": "too many",
                    "n": 2,
                },
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "unsupported_parameter")
        acquire.assert_not_called()

    def test_stream_is_rejected_before_backend_factory_or_account_acquire(self) -> None:
        with (
            mock.patch.object(
                openai_v1_image_generations,
                "create_gemini_web_generation_backend",
            ) as backend_factory,
            mock.patch.object(self.pool, "acquire", wraps=self.pool.acquire) as acquire,
        ):
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={
                    "model": "gemini-web-image",
                    "prompt": "no stream",
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "unsupported_parameter")
        self.assertEqual(response.json()["error"]["param"], "stream")
        backend_factory.assert_not_called()
        acquire.assert_not_called()

    def test_unknown_response_format_is_rejected_before_backend_or_account(self) -> None:
        with (
            mock.patch.object(
                openai_v1_image_generations,
                "create_gemini_web_generation_backend",
            ) as backend_factory,
            mock.patch.object(self.pool, "acquire", wraps=self.pool.acquire) as acquire,
        ):
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={
                    "model": "gemini-web-image",
                    "prompt": "unknown format",
                    "response_format": "temporary_url",
                },
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "unsupported_parameter")
        self.assertEqual(response.json()["error"]["param"], "response_format")
        backend_factory.assert_not_called()
        acquire.assert_not_called()

    def test_gemini_error_keeps_openai_envelope_and_stable_code(self) -> None:
        failing_backend = GeminiWebBackend(
            account_pool=self.pool,
            transport=FixtureGeminiWebTransport(FIXTURE_DIR, failure="protocol"),
            downloader=ValidPngFixtureDownloader(),
            timeout_seconds=30,
        )
        with mock.patch.object(
            openai_v1_image_generations,
            "create_gemini_web_generation_backend",
            return_value=failing_backend,
        ):
            response = self.client.post(
                "/v1/images/generations",
                headers=AUTH_HEADERS,
                json={"model": "gemini-web-image", "prompt": "safe failure"},
            )

        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["code"], "upstream_protocol_error")
        self.assertNotIn(FAKE_COOKIE, response.text)

    def test_no_normal_account_returns_stable_public_error(self) -> None:
        for accounts in (
            [],
            [_gemini_account("gemini_web:disabled-only", status="禁用")],
            [_gemini_account("gemini_web:abnormal-only", status="异常")],
        ):
            with self.subTest(accounts=accounts):
                service = AccountService(MemoryStorage(accounts))
                backend = GeminiWebBackend(
                    account_pool=FirstNormalGeminiWebAccountPool(
                        list_accounts=service.list_accounts,
                        get_account=service.get_account,
                    ),
                    transport=FixtureGeminiWebTransport(FIXTURE_DIR),
                    downloader=ValidPngFixtureDownloader(),
                    timeout_seconds=30,
                )
                with mock.patch.object(
                    openai_v1_image_generations,
                    "create_gemini_web_generation_backend",
                    return_value=backend,
                ):
                    response = self.client.post(
                        "/v1/images/generations",
                        headers=AUTH_HEADERS,
                        json={"model": "gemini-web-image", "prompt": "safe prompt"},
                    )

                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(
                    response.json()["error"]["code"],
                    "no_available_account",
                )
                self.assertNotIn(FAKE_COOKIE, response.text)

    def test_existing_chatgpt_codex_and_auto_models_keep_existing_dispatch(self) -> None:
        existing_data = [{"b64_json": base64.b64encode(b"existing").decode("ascii")}]
        for model in ("gpt-image-2", "codex-gpt-image-2", "auto"):
            with self.subTest(model=model), mock.patch.object(
                openai_v1_image_generations,
                "stream_image_outputs_with_pool",
                return_value=iter(
                    [
                        ImageOutput(
                            kind="result",
                            model=model,
                            index=1,
                            total=1,
                            data=existing_data,
                        )
                    ]
                ),
            ) as existing_dispatch, mock.patch.object(
                openai_v1_image_generations,
                "create_gemini_web_generation_backend",
            ) as gemini_factory:
                response = self.client.post(
                    "/v1/images/generations",
                    headers=AUTH_HEADERS,
                    json={"model": model, "prompt": "existing path"},
                )

            self.assertEqual(response.status_code, 200, response.text)
            existing_dispatch.assert_called_once()
            gemini_factory.assert_not_called()

    def test_pool_selects_only_first_normal_persisted_gemini_account(self) -> None:
        selected = self.pool.acquire()

        self.assertEqual(selected.account_id, "gemini_web:normal")
        self.assertNotEqual(selected.account_id, "gemini_web:disabled")
        self.assertNotIn(FAKE_COOKIE, repr(selected))


class GeminiWebImageModelDiscoveryTests(unittest.TestCase):
    def test_public_alias_is_discoverable_without_internal_protocol_id(self) -> None:
        service = AccountService(
            MemoryStorage([_gemini_account("gemini_web:normal", status="正常")])
        )
        with (
            mock.patch.object(
                ai_api,
                "require_identity",
                return_value={"id": "test-user", "role": "admin"},
            ),
            mock.patch.object(
                openai_v1_models.OpenAIBackendAPI,
                "list_models",
                return_value={
                    "object": "list",
                    "data": [{"id": "gemini-web-image", "object": "model"}],
                },
            ),
            mock.patch.object(openai_v1_models, "account_service", service),
        ):
            app = FastAPI()
            install_exception_handlers(app)
            app.include_router(ai_api.create_router())
            response = TestClient(app).get("/v1/models", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        model_ids = {item["id"] for item in result["data"]}
        self.assertIn("gemini-web-image", model_ids)
        self.assertEqual(
            sum(item["id"] == "gemini-web-image" for item in result["data"]),
            1,
        )
        self.assertNotIn("W1_WEBSITE_INTERNAL_RPC_ID", json.dumps(result))
        self.assertNotIn("gpt-image-2", model_ids)

    def test_public_alias_is_hidden_without_a_normal_gemini_account(self) -> None:
        for accounts in (
            [],
            [_gemini_account("gemini_web:disabled-only", status="禁用")],
            [_gemini_account("gemini_web:abnormal-only", status="异常")],
        ):
            with self.subTest(accounts=accounts), mock.patch.object(
                openai_v1_models.OpenAIBackendAPI,
                "list_models",
                return_value={
                    "object": "list",
                    "data": [{"id": "gemini-web-image", "object": "model"}],
                },
            ), mock.patch.object(
                openai_v1_models,
                "account_service",
                AccountService(MemoryStorage(accounts)),
            ):
                result = openai_v1_models.list_models()

            self.assertNotIn(
                "gemini-web-image",
                {item["id"] for item in result["data"]},
            )


if __name__ == "__main__":
    unittest.main()
