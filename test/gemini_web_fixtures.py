from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Mapping

from services.gemini_web_backend import (
    DownloadedImage,
    GeminiTransportFailure,
    GeminiTransportFailureKind,
    GeminiWebAccount,
    GeminiWebError,
    GeminiWebErrorCode,
    GeminiWebTransport,
    ResultDownloader,
    TransportGeneration,
    TransportSession,
    UploadedReference,
    parse_generation_stream,
    parse_initialization_payload,
    parse_upload_payload,
)


class FixtureAccountPool:
    def __init__(self, account: Mapping[str, Any]) -> None:
        self.account = GeminiWebAccount.from_mapping(account)
        self.acquired = 0
        self.released = 0

    def acquire(
        self,
        *,
        excluded_account_ids: set[str],
        deadline: float,
    ) -> GeminiWebAccount:
        if self.account.account_id in excluded_account_ids:
            raise GeminiWebError(GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)
        self.acquired += 1
        return self.account

    def release(self, account: GeminiWebAccount) -> None:
        if account == self.account:
            self.released += 1

    def mark_invalid(self, account: GeminiWebAccount) -> None:
        return None

    def persist_refreshed_cookies(
        self,
        account: GeminiWebAccount,
        cookies: Mapping[str, str],
    ) -> None:
        return None


class FixtureGeminiWebTransport:
    def __init__(
        self,
        fixture_dir: Path,
        failure: str | None = None,
        expected_upload_count: int = 0,
    ) -> None:
        self.fixture_dir = fixture_dir
        self.failure = failure
        self.expected_upload_count = expected_upload_count
        self._uploaded: list[UploadedReference] = []
        self.upload_calls: list[tuple[bytes, str]] = []
        self.generated_references: tuple[UploadedReference, ...] = ()

    def _load(self, name: str) -> dict[str, Any]:
        return json.loads((self.fixture_dir / name).read_text(encoding="utf-8"))

    def _raise_failure(self, stage: str) -> None:
        if self.failure is None:
            return
        payload = self._load(f"error_{self.failure}.json")
        if payload["stage"] == stage:
            raise GeminiTransportFailure(GeminiTransportFailureKind(payload["failure"]))

    def initialize(
        self,
        account: GeminiWebAccount,
        timeout_seconds: float,
    ) -> TransportSession:
        self._raise_failure("initialize")
        return parse_initialization_payload(self._load("account_valid.json"), account=account)

    def upload(
        self,
        session: TransportSession,
        content: bytes,
        mime_type: str,
        timeout_seconds: float,
    ) -> UploadedReference:
        self._raise_failure("upload")
        parsed = parse_upload_payload(self._load("upload.json"))
        uploaded = UploadedReference(
            token=f"{parsed.token}:{len(self._uploaded) + 1}",
            mime_type=mime_type,
        )
        self.upload_calls.append((content, mime_type))
        self._uploaded.append(uploaded)
        return uploaded

    def generate(
        self,
        session: TransportSession,
        prompt: str,
        references: tuple[UploadedReference, ...],
        timeout_seconds: float,
    ) -> TransportGeneration:
        self._raise_failure("generate")
        self.generated_references = references
        if len(references) != self.expected_upload_count or references != tuple(self._uploaded):
            raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
        if self.failure == "protocol":
            name = "generate_protocol_error.stream.txt"
        else:
            name = "generate.stream.txt"
        return parse_generation_stream((self.fixture_dir / name).read_text(encoding="utf-8"))


class FixtureResultDownloader:
    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = fixture_dir

    def download(
        self,
        account: GeminiWebAccount,
        download_ref: str,
        timeout_seconds: float,
    ) -> DownloadedImage:
        payload = json.loads(
            (self.fixture_dir / "original_image.json").read_text(encoding="utf-8")
        )
        return DownloadedImage(
            content=base64.b64decode(payload["content_base64"]),
            mime_type=payload["mime_type"],
        )


assert isinstance(FixtureGeminiWebTransport(Path(".")), GeminiWebTransport)
assert isinstance(FixtureResultDownloader(Path(".")), ResultDownloader)
