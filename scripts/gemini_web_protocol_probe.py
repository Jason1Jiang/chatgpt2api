from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.gemini_web_backend import (
    DownloadedImage,
    GeminiWebAccount,
    GeminiWebBackend,
    GeminiWebError,
    GeminiWebErrorCode,
    GeminiTransportFailure,
    HttpGeminiWebTransport,
    HttpResultDownloader,
    ImageReference,
    ImageRequest,
    ImageResult,
    TransportGeneration,
    TransportSession,
    UploadedReference,
)


FIXTURE_DIR = REPO_ROOT / "test" / "fixtures" / "gemini_web"
SAFE_CAPTURE_NAME = "gemini-web-protocol-summary.scrubbed.json"
SAFE_DIAGNOSTIC_NAME = "gemini-web-protocol-diagnostic.scrubbed.json"
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "set-cookie",
    "account_id",
    "email",
    "xsrf",
    "xsrf_token",
    "rpc_id",
    "token",
    "upload_token",
    "download_ref",
}
_NORMALIZED_SENSITIVE_KEYS = {item.replace("-", "_") for item in _SENSITIVE_KEYS}


class ProbeSafetyError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Gemini Web protocol probe safety check failed")


@dataclass(frozen=True)
class LiveProbeConfig:
    cookie_file: Path
    reference_image: Path
    output_dir: Path
    timeout_seconds: float


class _SingleAccountPool:
    def __init__(self, account: Mapping[str, Any]) -> None:
        self._account = GeminiWebAccount.from_mapping(account)

    def acquire(
        self,
        *,
        excluded_account_ids: set[str],
        deadline: float,
    ) -> GeminiWebAccount:
        if self._account.account_id in excluded_account_ids:
            raise GeminiWebError(GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)
        return self._account

    def release(self, account: GeminiWebAccount) -> None:
        return None

    def mark_invalid(self, account: GeminiWebAccount) -> None:
        return None

    def persist_refreshed_cookies(
        self,
        account: GeminiWebAccount,
        cookies: Mapping[str, str],
    ) -> None:
        return None


class _ProtocolDiagnosticRecorder:
    def __init__(self) -> None:
        self.completed_stages: list[str] = []
        self.failure_stage: str | None = None
        self.failure_kind: str | None = None
        self.initialization_shape: dict[str, int] = {}
        self.generation_shape: dict[str, int] = {}

    def success(self, stage: str) -> None:
        self.completed_stages.append(stage)

    def failure(self, stage: str, kind: str) -> None:
        if self.failure_stage is None:
            self.failure_stage = stage
            self.failure_kind = kind

    def record_generation_shape(self, values: Mapping[str, int]) -> None:
        allowed = {
            "legacy_chunks",
            "decoded_parts",
            "inner_payloads",
            "candidate_records",
            "candidate_len_max",
            "candidate_nonempty_mask",
            "slot12_len_max",
            "slot12_nonempty_mask",
            "generated_entries",
            "edited_entries",
            "web_entries",
            "preview_refs",
            "image_ids",
        }
        self.generation_shape = {
            key: int(value)
            for key, value in values.items()
            if key in allowed and isinstance(value, int) and value >= 0
        }

    def record_initialization_shape(self, values: Mapping[str, int]) -> None:
        allowed = {
            "http_status_class",
            "xsrf_present",
            "build_label_present",
            "session_id_present",
            "language_present",
            "push_id_present",
            "email_present",
            "rotation_attempted",
            "rotation_status_class",
            "rotation_yielded_token",
        }
        self.initialization_shape = {
            key: int(value)
            for key, value in values.items()
            if key in allowed and isinstance(value, int) and value >= 0
        }

    def summary(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "mode": "live-diagnostic",
            "status": "error" if self.failure_stage else "ok",
            "completed_stages": list(self.completed_stages),
            "failure_stage": self.failure_stage,
            "failure_kind": self.failure_kind,
            "initialization_shape": dict(self.initialization_shape),
            "generation_shape": dict(self.generation_shape),
            "capture_boundary": {
                "raw_headers_stored": False,
                "raw_responses_stored": False,
                "credentials_stored": False,
                "account_identifier_stored": False,
            },
        }
        assert_scrubbed_fixture(payload)
        return payload


class _DiagnosticTransport:
    def __init__(
        self,
        inner: HttpGeminiWebTransport,
        recorder: _ProtocolDiagnosticRecorder,
    ) -> None:
        self._inner = inner
        self._recorder = recorder

    def initialize(
        self,
        account: GeminiWebAccount,
        timeout_seconds: float,
    ) -> TransportSession:
        try:
            return self._call(
                "initialize",
                self._inner.initialize,
                account,
                timeout_seconds,
            )
        finally:
            self._recorder.record_initialization_shape(
                self._inner.initialization_diagnostics
            )

    def upload(
        self,
        session: TransportSession,
        content: bytes,
        mime_type: str,
        timeout_seconds: float,
    ) -> UploadedReference:
        return self._call(
            "upload",
            self._inner.upload,
            session,
            content,
            mime_type,
            timeout_seconds,
        )

    def generate(
        self,
        session: TransportSession,
        prompt: str,
        references: tuple[UploadedReference, ...],
        timeout_seconds: float,
    ) -> TransportGeneration:
        try:
            return self._call(
                "generate",
                self._inner.generate,
                session,
                prompt,
                references,
                timeout_seconds,
            )
        finally:
            self._recorder.record_generation_shape(
                self._inner.generation_diagnostics
            )

    def _call(self, stage: str, call: Callable[..., Any], *args: Any) -> Any:
        try:
            result = call(*args)
        except GeminiTransportFailure as error:
            self._recorder.failure(stage, error.kind.value)
            raise
        except BaseException:
            self._recorder.failure(stage, "unexpected")
            raise
        self._recorder.success(stage)
        return result


class _DiagnosticDownloader:
    def __init__(
        self,
        inner: HttpResultDownloader,
        recorder: _ProtocolDiagnosticRecorder,
    ) -> None:
        self._inner = inner
        self._recorder = recorder

    def download(
        self,
        account: GeminiWebAccount,
        download_ref: str,
        timeout_seconds: float,
    ) -> DownloadedImage:
        try:
            result = self._inner.download(account, download_ref, timeout_seconds)
        except GeminiTransportFailure as error:
            self._recorder.failure("download", error.kind.value)
            raise
        except BaseException:
            self._recorder.failure("download", "unexpected")
            raise
        self._recorder.success("download")
        return result


def _is_within_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return False
    return True


def _external_input(path: Path, *, code: str) -> Path:
    resolved = path.expanduser().resolve()
    if _is_within_repo(resolved):
        raise ProbeSafetyError(code)
    if not resolved.is_file():
        raise ProbeSafetyError(code)
    return resolved


def _external_output_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if _is_within_repo(resolved):
        raise ProbeSafetyError("output_must_be_outside_repo")
    return resolved


def _cookie_mapping(payload: Any) -> dict[str, str]:
    source = payload.get("cookies", payload) if isinstance(payload, Mapping) else payload
    cookies: dict[str, str] = {}
    if isinstance(source, Mapping):
        for name, value in source.items():
            if isinstance(name, str) and name.strip() and isinstance(value, str) and value:
                cookies[name] = value
    elif isinstance(source, list):
        for item in source:
            if not isinstance(item, Mapping):
                continue
            domain = str(item.get("domain") or "").strip().lower().lstrip(".")
            if domain and domain != "google.com" and not domain.endswith(".google.com"):
                continue
            name = item.get("name")
            value = item.get("value")
            if isinstance(name, str) and name.strip() and isinstance(value, str) and value:
                cookies[name] = value
    if not cookies:
        raise ProbeSafetyError("invalid_cookie_file")
    return cookies


def _load_probe_account(path: Path) -> dict[str, Any]:
    cookie_file = _external_input(path, code="cookie_file_must_be_external")
    try:
        payload = json.loads(cookie_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProbeSafetyError("invalid_cookie_file") from None
    cookies = _cookie_mapping(payload)
    return {
        "account_id": "gemini_web:protocol-probe",
        "credentials": {"cookies": cookies},
    }


def _read_reference(path: Path) -> ImageReference:
    reference_path = _external_input(path, code="reference_image_must_be_external")
    try:
        content = reference_path.read_bytes()
    except OSError:
        raise ProbeSafetyError("invalid_reference_image") from None
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
    elif content.startswith((b"GIF87a", b"GIF89a")):
        mime_type = "image/gif"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        mime_type = "image/webp"
    else:
        raise ProbeSafetyError("invalid_reference_image")
    return ImageReference(content=content, mime_type=mime_type)


def _safe_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        value.startswith("FAKE_FIXTURE_")
        or value.startswith("fixture://")
        or value.startswith("gemini_web:fixture-")
        or value == "<redacted>"
        or lowered.endswith(".invalid")
    )


def assert_scrubbed_fixture(payload: Any) -> None:
    """Reject values that look like live identity, headers, or protocol secrets."""

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized_key = str(key).strip().lower().replace("-", "_")
                if normalized_key in _NORMALIZED_SENSITIVE_KEYS:
                    if isinstance(child, str) and not _safe_placeholder(child):
                        raise ProbeSafetyError("capture_contains_sensitive_data")
                    if isinstance(child, list):
                        raise ProbeSafetyError("capture_contains_sensitive_data")
                    if isinstance(child, Mapping):
                        for nested in child.values():
                            if not isinstance(nested, str) or not _safe_placeholder(nested):
                                raise ProbeSafetyError("capture_contains_sensitive_data")
                walk(child)
            return
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if isinstance(value, str):
            email_match = _EMAIL_RE.search(value)
            if email_match and not email_match.group(0).lower().endswith(".invalid"):
                raise ProbeSafetyError("capture_contains_sensitive_data")
            if "googleusercontent.com" in value.lower() and "?" in value:
                raise ProbeSafetyError("capture_contains_sensitive_data")

    walk(payload)


def _mime_suffix(mime_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime_type, ".img")


def _write_images(output_dir: Path, stem: str, result: ImageResult) -> None:
    for index, image in enumerate(result.images, start=1):
        (output_dir / f"{stem}-{index}{_mime_suffix(image.mime_type)}").write_bytes(
            image.content
        )


def _build_backend(account: Mapping[str, Any], timeout_seconds: float) -> GeminiWebBackend:
    return GeminiWebBackend(
        account_pool=_SingleAccountPool(account),
        transport=HttpGeminiWebTransport(),
        downloader=HttpResultDownloader(),
        timeout_seconds=timeout_seconds,
    )


def _build_diagnostic_backend(
    account: Mapping[str, Any],
    timeout_seconds: float,
    recorder: _ProtocolDiagnosticRecorder,
) -> GeminiWebBackend:
    return GeminiWebBackend(
        account_pool=_SingleAccountPool(account),
        transport=_DiagnosticTransport(HttpGeminiWebTransport(), recorder),
        downloader=_DiagnosticDownloader(HttpResultDownloader(), recorder),
        timeout_seconds=timeout_seconds,
    )


def _safe_summary(
    *,
    mode: str,
    validation_email_present: bool,
    text_result: ImageResult,
    edit_result: ImageResult,
    reference: ImageReference,
) -> dict[str, Any]:
    reference_digest = hashlib.sha256(reference.content).digest()
    edit_is_distinct = all(
        hashlib.sha256(image.content).digest() != reference_digest
        for image in edit_result.images
    )
    summary = {
        "schema_version": 1,
        "mode": mode,
        "status": "ok",
        "same_account_for_all_stages": True,
        "account_validation": {
            "valid": True,
            "email_present": validation_email_present,
        },
        "text_generation": {
            "downloaded_original": bool(text_result.images),
            "generated_image_count": len(text_result.images),
            "mime_types": [image.mime_type for image in text_result.images],
        },
        "image_edit": {
            "reference_count": 1,
            "downloaded_original": bool(edit_result.images),
            "generated_image_count": len(edit_result.images),
            "mime_types": [image.mime_type for image in edit_result.images],
            "result_distinct_from_reference": edit_is_distinct,
            "backend_generated_kind_filter_applied": True,
        },
        "capture_boundary": {
            "raw_headers_stored": False,
            "raw_responses_stored": False,
            "credentials_stored": False,
            "account_identifier_stored": False,
        },
    }
    assert_scrubbed_fixture(summary)
    return summary


def execute_live_probe(
    config: LiveProbeConfig,
    *,
    backend_factory: Callable[[Mapping[str, Any], float], GeminiWebBackend] = _build_backend,
) -> dict[str, Any]:
    account = _load_probe_account(config.cookie_file)
    reference = _read_reference(config.reference_image)
    output_dir = _external_output_dir(config.output_dir)
    backend = backend_factory(account, config.timeout_seconds)

    validation = backend.validate_account(account)
    if not validation.valid:
        raise ProbeSafetyError("account_validation_failed")
    text_result = backend.generate(
        ImageRequest(prompt="Generate one simple geometric still-life image.")
    )
    edit_result = backend.edit(
        ImageRequest(
            prompt="Generate a visibly different stylized edit of the reference image.",
            references=(reference,),
        )
    )
    if not text_result.images or not edit_result.images:
        raise ProbeSafetyError("missing_generated_image")

    summary = _safe_summary(
        mode="live",
        validation_email_present=bool(validation.email),
        text_result=text_result,
        edit_result=edit_result,
        reference=reference,
    )
    if not summary["image_edit"]["result_distinct_from_reference"]:
        raise ProbeSafetyError("edit_result_matches_reference")

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_images(output_dir, "text-generation", text_result)
    _write_images(output_dir, "image-edit", edit_result)
    capture_path = output_dir / SAFE_CAPTURE_NAME
    capture_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def execute_live_diagnostic(config: LiveProbeConfig) -> dict[str, Any]:
    account = _load_probe_account(config.cookie_file)
    reference = _read_reference(config.reference_image)
    output_dir = _external_output_dir(config.output_dir)
    recorder = _ProtocolDiagnosticRecorder()
    backend = _build_diagnostic_backend(account, config.timeout_seconds, recorder)

    active_stage = "validate_result"
    try:
        backend.validate_account(account)
        active_stage = "generate_result"
        backend.generate(ImageRequest(prompt="Generate one simple geometric still-life image."))
        active_stage = "edit_result"
        backend.edit(
            ImageRequest(
                prompt="Generate a visibly different stylized edit of the reference image.",
                references=(reference,),
            )
        )
    except GeminiWebError as error:
        if recorder.failure_stage is None:
            recorder.failure(active_stage, error.code.value)
    except ProbeSafetyError:
        if recorder.failure_stage is None:
            recorder.failure(active_stage, "probe_safety")
    except BaseException:
        if recorder.failure_stage is None:
            recorder.failure("probe", "unexpected")

    summary = recorder.summary()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / SAFE_DIAGNOSTIC_NAME).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def execute_dry_run() -> dict[str, Any]:
    from test.gemini_web_fixtures import (
        FixtureAccountPool,
        FixtureGeminiWebTransport,
        FixtureResultDownloader,
    )

    account = json.loads((FIXTURE_DIR / "account.json").read_text(encoding="utf-8"))
    backend = GeminiWebBackend(
        account_pool=FixtureAccountPool(account),
        transport=FixtureGeminiWebTransport(FIXTURE_DIR, expected_upload_count=0),
        downloader=FixtureResultDownloader(FIXTURE_DIR),
        timeout_seconds=30,
    )
    validation = backend.validate_account(account)
    text_result = backend.generate(ImageRequest(prompt="fixture text probe"))

    edit_backend = GeminiWebBackend(
        account_pool=FixtureAccountPool(account),
        transport=FixtureGeminiWebTransport(FIXTURE_DIR, expected_upload_count=1),
        downloader=FixtureResultDownloader(FIXTURE_DIR),
        timeout_seconds=30,
    )
    reference = ImageReference(content=b"fixture-input-image", mime_type="image/png")
    edit_result = edit_backend.edit(
        ImageRequest(prompt="fixture edit probe", references=(reference,))
    )
    return _safe_summary(
        mode="dry-run",
        validation_email_present=bool(validation.email),
        text_result=text_result,
        edit_result=edit_result,
        reference=reference,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safety-bounded Gemini Web protocol probe; never reads browser state."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("dry-run", help="Use repository fixtures only.")
    live = subparsers.add_parser("live", help="Run the explicit, credentialed probe.")
    live.add_argument("--cookie-file", type=Path, required=True)
    live.add_argument("--reference-image", type=Path, required=True)
    live.add_argument("--output-dir", type=Path, required=True)
    live.add_argument("--timeout-seconds", type=float, default=300.0)
    live.add_argument(
        "--acknowledge-live-probe",
        action="store_true",
        help="Required explicit acknowledgement; no implicit live mode exists.",
    )
    live.add_argument(
        "--diagnose-scrubbed-protocol",
        action="store_true",
        help="Write only stage-level scrubbed diagnostics; never raw responses.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    live_executor: Callable[[LiveProbeConfig], dict[str, Any]] = execute_live_probe,
    diagnostic_executor: Callable[[LiveProbeConfig], dict[str, Any]] = execute_live_diagnostic,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "dry-run":
            summary = execute_dry_run()
        else:
            if not args.acknowledge_live_probe:
                raise ProbeSafetyError("live_acknowledgement_required")
            if args.timeout_seconds <= 0:
                raise ProbeSafetyError("invalid_timeout")
            config = LiveProbeConfig(
                cookie_file=_external_input(
                    args.cookie_file,
                    code="cookie_file_must_be_external",
                ),
                reference_image=_external_input(
                    args.reference_image,
                    code="reference_image_must_be_external",
                ),
                output_dir=_external_output_dir(args.output_dir),
                timeout_seconds=float(args.timeout_seconds),
            )
            summary = (
                diagnostic_executor(config)
                if args.diagnose_scrubbed_protocol
                else live_executor(config)
            )
        stdout.write(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 0
    except ProbeSafetyError as error:
        stdout.write(
            json.dumps(
                {"mode": args.mode, "status": "error", "code": error.code},
                separators=(",", ":"),
            )
            + "\n"
        )
        return 2
    except GeminiWebError as error:
        stdout.write(
            json.dumps(
                {"mode": args.mode, "status": "error", "code": error.code.value},
                separators=(",", ":"),
            )
            + "\n"
        )
        return 3
    except Exception:
        stdout.write(
            json.dumps(
                {"mode": args.mode, "status": "error", "code": "probe_failed"},
                separators=(",", ":"),
            )
            + "\n"
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
