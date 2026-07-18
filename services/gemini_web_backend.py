from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Mapping, MutableMapping, Protocol, runtime_checkable

from curl_cffi import CurlMime, requests


GEMINI_WEB_IMAGE_MODEL = "gemini-web-image"


@dataclass(frozen=True)
class ImageReference:
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    references: tuple[ImageReference, ...] = ()
    response_format: str = "b64_json"
    size: str | None = None
    quality: str | None = None


@dataclass(frozen=True)
class ImageOutput:
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class ImageResult:
    images: tuple[ImageOutput, ...]
    revised_prompt: str | None = None


@dataclass(frozen=True)
class AccountValidation:
    valid: bool
    email: str | None
    session_label: str
    refreshed_cookies: tuple[tuple[str, str], ...] = field(default=(), repr=False)


class GeminiWebErrorCode(StrEnum):
    NO_AVAILABLE_ACCOUNT = "no_available_account"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    CONTENT_POLICY_VIOLATION = "content_policy_violation"
    UPSTREAM_PROTOCOL_ERROR = "upstream_protocol_error"
    UPSTREAM_TIMEOUT = "upstream_timeout"


_ERROR_DETAILS = {
    GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT: (503, "no available Gemini Web account"),
    GeminiWebErrorCode.UPSTREAM_RATE_LIMITED: (429, "Gemini Web request was rate limited"),
    GeminiWebErrorCode.CONTENT_POLICY_VIOLATION: (400, "image request was rejected by content policy"),
    GeminiWebErrorCode.UPSTREAM_PROTOCOL_ERROR: (502, "Gemini Web returned an unsupported response"),
    GeminiWebErrorCode.UPSTREAM_TIMEOUT: (504, "Gemini Web request timed out"),
}


class GeminiWebError(RuntimeError):
    def __init__(self, code: GeminiWebErrorCode) -> None:
        self.code = code
        self.status_code, message = _ERROR_DETAILS[code]
        super().__init__(message)


@dataclass(frozen=True)
class GeminiWebAccount:
    account_id: str
    cookies: Mapping[str, str] = field(repr=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> GeminiWebAccount:
        credentials = value.get("credentials")
        cookies = credentials.get("cookies") if isinstance(credentials, Mapping) else None
        if not isinstance(cookies, Mapping) or not cookies:
            raise GeminiWebError(GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)
        normalized_cookies = {
            str(name): str(cookie_value)
            for name, cookie_value in cookies.items()
            if str(name).strip() and isinstance(cookie_value, str) and cookie_value
        }
        if not normalized_cookies:
            raise GeminiWebError(GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)
        account_id = str(value.get("account_id") or "").strip()
        if not account_id:
            raise GeminiWebError(GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)
        return cls(account_id=account_id, cookies=normalized_cookies)

    @property
    def session_label(self) -> str:
        fingerprint = hashlib.sha256(self.account_id.encode("utf-8")).hexdigest()[:10]
        return f"gemini-session:{fingerprint}"


@runtime_checkable
class GeminiWebAccountPool(Protocol):
    def acquire(
        self,
        *,
        excluded_account_ids: set[str],
        deadline: float,
    ) -> GeminiWebAccount: ...

    def release(self, account: GeminiWebAccount) -> None: ...

    def mark_invalid(self, account: GeminiWebAccount) -> None: ...

    def persist_refreshed_cookies(
        self,
        account: GeminiWebAccount,
        cookies: Mapping[str, str],
    ) -> None: ...


@dataclass(frozen=True)
class TransportSession:
    account: GeminiWebAccount = field(repr=False)
    email: str | None
    xsrf_token: str = field(repr=False)
    rpc_id: str | None = field(default=None, repr=False)
    build_label: str | None = field(default=None, repr=False)
    session_id: str | None = field(default=None, repr=False)
    language: str = "en"
    push_id: str = field(default="feeds/mcudyrk2a4khkz", repr=False)
    model_headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    refreshed_cookies: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    http_client: Any | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class UploadedReference:
    token: str = field(repr=False)
    mime_type: str
    filename: str = "input.png"


@dataclass(frozen=True)
class TransportImageCandidate:
    kind: str
    download_ref: str = field(repr=False)
    mime_type: str
    image_id: str | None = field(default=None, repr=False)
    conversation_id: str | None = field(default=None, repr=False)
    response_id: str | None = field(default=None, repr=False)
    choice_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class TransportGeneration:
    images: tuple[TransportImageCandidate, ...]
    revised_prompt: str | None = None


@dataclass(frozen=True)
class DownloadedImage:
    content: bytes = field(repr=False)
    mime_type: str


class GeminiTransportFailureKind(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    INVALID_COOKIE = "invalid_cookie"
    CONTENT_POLICY = "content_policy"
    PROTOCOL = "protocol"


class GeminiTransportFailure(RuntimeError):
    def __init__(self, kind: GeminiTransportFailureKind) -> None:
        self.kind = kind
        super().__init__("Gemini Web transport failed")


@runtime_checkable
class GeminiWebTransport(Protocol):
    def initialize(
        self,
        account: GeminiWebAccount,
        timeout_seconds: float,
    ) -> TransportSession: ...

    def upload(
        self,
        session: TransportSession,
        content: bytes,
        mime_type: str,
        timeout_seconds: float,
    ) -> UploadedReference: ...

    def generate(
        self,
        session: TransportSession,
        prompt: str,
        references: tuple[UploadedReference, ...],
        timeout_seconds: float,
    ) -> TransportGeneration: ...


@runtime_checkable
class ResultDownloader(Protocol):
    def download(
        self,
        account: GeminiWebAccount,
        download_ref: str,
        timeout_seconds: float,
    ) -> DownloadedImage: ...


def parse_initialization_payload(
    payload: Mapping[str, Any],
    *,
    account: GeminiWebAccount | None = None,
    http_client: Any | None = None,
) -> TransportSession:
    account_payload = payload.get("account")
    session_payload = payload.get("session")
    if not isinstance(account_payload, Mapping) or not isinstance(session_payload, Mapping):
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
    email_value = account_payload.get("email")
    email = str(email_value) if isinstance(email_value, str) and email_value else None
    xsrf_token = session_payload.get("xsrf")
    if not isinstance(xsrf_token, str) or not xsrf_token:
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
    if account is None:
        account = GeminiWebAccount(
            account_id="gemini_web:fixture",
            cookies={"fixture": "fixture"},
        )
    return TransportSession(
        account=account,
        email=email,
        xsrf_token=xsrf_token,
        rpc_id=_optional_string(session_payload.get("rpc_id")),
        build_label=_optional_string(session_payload.get("build_label")),
        session_id=_optional_string(session_payload.get("session_id")),
        language=_optional_string(session_payload.get("language")) or "en",
        push_id=(
            _optional_string(session_payload.get("push_id"))
            or "feeds/mcudyrk2a4khkz"
        ),
        http_client=http_client,
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_upload_payload(payload: Mapping[str, Any]) -> UploadedReference:
    upload = payload.get("upload")
    if not isinstance(upload, Mapping):
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
    token = upload.get("token")
    mime_type = upload.get("mime_type")
    if not isinstance(token, str) or not token:
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
    if not isinstance(mime_type, str) or not mime_type:
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
    return UploadedReference(token=token, mime_type=mime_type)


def parse_generation_payload(payload: Mapping[str, Any]) -> TransportGeneration:
    image_payloads = payload.get("images")
    if not isinstance(image_payloads, list):
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
    images: list[TransportImageCandidate] = []
    for image_payload in image_payloads:
        if not isinstance(image_payload, Mapping):
            continue
        kind = image_payload.get("kind")
        download_ref = image_payload.get("download_ref")
        mime_type = image_payload.get("mime_type")
        if not all(isinstance(value, str) and value for value in (kind, download_ref, mime_type)):
            continue
        images.append(
            TransportImageCandidate(
                kind=kind,
                download_ref=download_ref,
                mime_type=mime_type,
            )
        )
    revised_prompt_value = payload.get("revised_prompt")
    revised_prompt = (
        revised_prompt_value
        if isinstance(revised_prompt_value, str) and revised_prompt_value
        else None
    )
    return TransportGeneration(images=tuple(images), revised_prompt=revised_prompt)


def parse_generation_stream(
    payload: str,
    diagnostics: MutableMapping[str, int] | None = None,
) -> TransportGeneration:
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics["legacy_chunks"] = 0
    images: list[Any] = []
    revised_prompt: str | None = None
    parsed_chunk = False
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line or line == ")]}'" or line.isdigit():
            continue
        try:
            chunk = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(chunk, Mapping):
            continue
        parsed_chunk = True
        if diagnostics is not None:
            diagnostics["legacy_chunks"] += 1
        chunk_images = chunk.get("images")
        if isinstance(chunk_images, list):
            images.extend(chunk_images)
        chunk_prompt = chunk.get("revised_prompt")
        if isinstance(chunk_prompt, str) and chunk_prompt:
            revised_prompt = chunk_prompt
    if parsed_chunk:
        return parse_generation_payload(
            {"images": images, "revised_prompt": revised_prompt}
        )
    return _parse_current_generation_stream(payload, diagnostics)


def _nested(value: Any, path: tuple[int | str, ...], default: Any = None) -> Any:
    current = value
    for key in path:
        if isinstance(key, int) and isinstance(current, list):
            if -len(current) <= key < len(current):
                current = current[key]
                continue
        elif isinstance(key, str) and isinstance(current, Mapping) and key in current:
            current = current[key]
            continue
        return default
    return default if current is None else current


def _take_utf16_units(value: str, start: int, units: int) -> tuple[str, int] | None:
    used = 0
    cursor = start
    while cursor < len(value) and used < units:
        used += 2 if ord(value[cursor]) > 0xFFFF else 1
        cursor += 1
    if used != units:
        return None
    return value[start:cursor], cursor


def _nonempty_list_mask(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    mask = 0
    for index, item in enumerate(value[:63]):
        if item is not None and item != "" and item != [] and item != {}:
            mask |= 1 << index
    return mask


def _decode_google_frames(payload: str) -> list[Any]:
    content = str(payload or "")
    if content.startswith(")]}'"):
        content = content[4:]
    content = content.lstrip()
    frames: list[Any] = []
    cursor = 0
    while cursor < len(content):
        while cursor < len(content) and content[cursor].isspace():
            cursor += 1
        marker = re.match(r"(\d+)\n", content[cursor:])
        if marker is None:
            break
        length_text = marker.group(1)
        start = cursor + len(length_text)
        taken = _take_utf16_units(content, start, int(length_text))
        if taken is None:
            break
        raw_frame, cursor = taken
        try:
            decoded = json.loads(raw_frame.strip())
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, list):
            frames.extend(decoded)
        else:
            frames.append(decoded)
    if frames:
        return frames

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.isdigit():
            continue
        try:
            decoded = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, list):
            frames.extend(decoded)
        else:
            frames.append(decoded)
    return frames


def _parse_current_generation_stream(
    payload: str,
    diagnostics: MutableMapping[str, int] | None = None,
) -> TransportGeneration:
    images: list[TransportImageCandidate] = []
    revised_prompt: str | None = None
    seen: set[str] = set()
    parsed_inner = False
    parts = _decode_google_frames(payload)
    if diagnostics is not None:
        diagnostics.update(
            {
                "decoded_parts": len(parts),
                "inner_payloads": 0,
                "candidate_records": 0,
                "candidate_len_max": 0,
                "candidate_nonempty_mask": 0,
                "slot12_len_max": 0,
                "slot12_nonempty_mask": 0,
                "generated_entries": 0,
                "edited_entries": 0,
                "web_entries": 0,
                "preview_refs": 0,
                "image_ids": 0,
            }
        )
    for part in parts:
        error_code = _nested(part, (5, 2, 0, 1, 0))
        if error_code in {1037, 1060}:
            raise GeminiTransportFailure(GeminiTransportFailureKind.RATE_LIMIT)
        inner_json = _nested(part, (2,))
        if not isinstance(inner_json, str) or not inner_json:
            continue
        try:
            body = json.loads(inner_json)
        except (TypeError, ValueError):
            continue
        parsed_inner = True
        if diagnostics is not None:
            diagnostics["inner_payloads"] += 1
        metadata = _nested(body, (1,), [])
        conversation_id = _optional_string(_nested(metadata, (0,)))
        response_id = _optional_string(_nested(metadata, (1,)))
        candidates = _nested(body, (4,), [])
        if not isinstance(candidates, list):
            continue
        if diagnostics is not None:
            diagnostics["candidate_records"] += len(candidates)
        for candidate in candidates:
            if diagnostics is not None and isinstance(candidate, list):
                diagnostics["candidate_len_max"] = max(
                    diagnostics["candidate_len_max"], len(candidate)
                )
                diagnostics["candidate_nonempty_mask"] |= _nonempty_list_mask(
                    candidate
                )
                slot12 = _nested(candidate, (12,))
                if isinstance(slot12, list):
                    diagnostics["slot12_len_max"] = max(
                        diagnostics["slot12_len_max"], len(slot12)
                    )
                    diagnostics["slot12_nonempty_mask"] |= _nonempty_list_mask(
                        slot12
                    )
            choice_id = _optional_string(_nested(candidate, (0,)))
            candidate_text = _optional_string(_nested(candidate, (1, 0)))
            if candidate_text:
                revised_prompt = candidate_text
            generated = _nested(candidate, (12, 7, 0), [])
            edited = _nested(candidate, (12, 0, "8", 0), [])
            web = _nested(candidate, (12, 1), [])
            if diagnostics is not None:
                diagnostics["generated_entries"] += (
                    len(generated) if isinstance(generated, list) else 0
                )
                diagnostics["edited_entries"] += (
                    len(edited) if isinstance(edited, list) else 0
                )
                diagnostics["web_entries"] += len(web) if isinstance(web, list) else 0
            entries = [
                item
                for group in (generated, edited)
                if isinstance(group, list)
                for item in group
            ]
            for entry in entries:
                preview_url = _optional_string(_nested(entry, (0, 3, 3)))
                image_id = _optional_string(_nested(entry, (1, 0)))
                if diagnostics is not None:
                    diagnostics["preview_refs"] += int(bool(preview_url))
                    diagnostics["image_ids"] += int(bool(image_id))
                identity = image_id or preview_url
                if not preview_url or not identity or identity in seen:
                    continue
                seen.add(identity)
                images.append(
                    TransportImageCandidate(
                        kind="generated_image",
                        download_ref=preview_url,
                        mime_type="image/png",
                        image_id=image_id,
                        conversation_id=conversation_id,
                        response_id=response_id,
                        choice_id=choice_id,
                    )
                )
    if not parsed_inner:
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
    return TransportGeneration(images=tuple(images), revised_prompt=revised_prompt)


_TRANSPORT_ERROR_MAP = {
    GeminiTransportFailureKind.TIMEOUT: GeminiWebErrorCode.UPSTREAM_TIMEOUT,
    GeminiTransportFailureKind.RATE_LIMIT: GeminiWebErrorCode.UPSTREAM_RATE_LIMITED,
    GeminiTransportFailureKind.INVALID_COOKIE: GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT,
    GeminiTransportFailureKind.CONTENT_POLICY: GeminiWebErrorCode.CONTENT_POLICY_VIOLATION,
    GeminiTransportFailureKind.PROTOCOL: GeminiWebErrorCode.UPSTREAM_PROTOCOL_ERROR,
}


class GeminiWebBackend:
    def __init__(
        self,
        *,
        account_pool: GeminiWebAccountPool,
        transport: GeminiWebTransport,
        downloader: ResultDownloader,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._account_pool = account_pool
        self._transport = transport
        self._downloader = downloader
        self._clock = clock
        self._timeout_seconds = float(timeout_seconds)

    def validate_account(self, account: Mapping[str, Any]) -> AccountValidation:
        normalized_account = GeminiWebAccount.from_mapping(account)
        deadline = self._clock() + self._timeout_seconds
        try:
            session = self._transport.initialize(
                normalized_account,
                self._remaining(deadline),
            )
        except GeminiTransportFailure as error:
            raise self._public_error(error) from None
        except TimeoutError:
            raise GeminiWebError(GeminiWebErrorCode.UPSTREAM_TIMEOUT) from None
        return AccountValidation(
            valid=True,
            email=session.email,
            session_label=normalized_account.session_label,
            refreshed_cookies=session.refreshed_cookies,
        )

    def generate(self, request: ImageRequest) -> ImageResult:
        return self._run(request, upload_references=False)

    def edit(self, request: ImageRequest) -> ImageResult:
        if not request.references:
            raise ValueError("image is required")
        return self._run(request, upload_references=True)

    def _run(self, request: ImageRequest, *, upload_references: bool) -> ImageResult:
        deadline = self._clock() + self._timeout_seconds
        attempted_account_ids: set[str] = set()
        retry_failures: list[GeminiTransportFailureKind] = []
        while True:
            account: GeminiWebAccount | None = None
            try:
                account = self._account_pool.acquire(
                    excluded_account_ids=attempted_account_ids,
                    deadline=deadline,
                )
                if account.account_id in attempted_account_ids:
                    raise GeminiWebError(GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)
                attempted_account_ids.add(account.account_id)
                session = self._transport.initialize(account, self._remaining(deadline))
                active_account = account
                if session.refreshed_cookies:
                    active_account = GeminiWebAccount(
                        account_id=account.account_id,
                        cookies=dict(session.refreshed_cookies),
                    )
                    self._account_pool.persist_refreshed_cookies(
                        account,
                        active_account.cookies,
                    )
                uploaded: list[UploadedReference] = []
                if upload_references:
                    for reference in request.references:
                        uploaded.append(
                            self._transport.upload(
                                session,
                                reference.content,
                                reference.mime_type,
                                self._remaining(deadline),
                            )
                        )
                generation = self._transport.generate(
                    session,
                    request.prompt,
                    tuple(uploaded),
                    self._remaining(deadline),
                )
                generated = [
                    candidate
                    for candidate in generation.images
                    if candidate.kind == "generated_image"
                ]
                if not generated:
                    raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
                images = tuple(
                    self._download_output(active_account, candidate, deadline)
                    for candidate in generated
                )
                return ImageResult(images=images, revised_prompt=generation.revised_prompt)
            except GeminiTransportFailure as error:
                if error.kind == GeminiTransportFailureKind.INVALID_COOKIE:
                    if account is not None:
                        try:
                            self._account_pool.mark_invalid(account)
                        except Exception:
                            raise GeminiWebError(
                                GeminiWebErrorCode.UPSTREAM_PROTOCOL_ERROR
                            ) from None
                    retry_failures.append(error.kind)
                    continue
                if error.kind == GeminiTransportFailureKind.RATE_LIMIT:
                    retry_failures.append(error.kind)
                    continue
                raise self._public_error(error) from None
            except GeminiWebError as error:
                if (
                    error.code == GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT
                    and retry_failures
                    and all(
                        kind == GeminiTransportFailureKind.RATE_LIMIT
                        for kind in retry_failures
                    )
                ):
                    raise GeminiWebError(GeminiWebErrorCode.UPSTREAM_RATE_LIMITED) from None
                raise
            except TimeoutError:
                raise GeminiWebError(GeminiWebErrorCode.UPSTREAM_TIMEOUT) from None
            except Exception:
                raise GeminiWebError(GeminiWebErrorCode.UPSTREAM_PROTOCOL_ERROR) from None
            finally:
                if account is not None:
                    self._account_pool.release(account)

    def _download_output(
        self,
        account: GeminiWebAccount,
        candidate: TransportImageCandidate,
        deadline: float,
    ) -> ImageOutput:
        downloaded = self._downloader.download(
            account,
            candidate.download_ref,
            self._remaining(deadline),
        )
        if not downloaded.content or not downloaded.mime_type.startswith("image/"):
            raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
        return ImageOutput(content=downloaded.content, mime_type=downloaded.mime_type)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise GeminiWebError(GeminiWebErrorCode.UPSTREAM_TIMEOUT)
        return remaining

    @staticmethod
    def _public_error(error: GeminiTransportFailure) -> GeminiWebError:
        return GeminiWebError(_TRANSPORT_ERROR_MAP[error.kind])


def _response_json(response: Any) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL) from None
    if not isinstance(payload, Mapping):
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
    return payload


def _raise_for_status(response: Any) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {401, 403}:
        raise GeminiTransportFailure(GeminiTransportFailureKind.INVALID_COOKIE)
    if status_code == 429:
        raise GeminiTransportFailure(GeminiTransportFailureKind.RATE_LIMIT)
    if status_code < 200 or status_code >= 300:
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)


class HttpGeminiWebTransport:
    """Current consumer-Web codec behind the internal Gemini transport seam.

    All volatile endpoint fields and response indexes stay in this adapter.
    Synthetic protocol tests cover its request and parsing shapes; ticket #3's
    scrubbed live probe remains the delivery gate for end-to-end acceptance.
    """

    APP_URL = "https://gemini.google.com/app"
    GOOGLE_URL = "https://www.google.com"
    ROTATE_COOKIES_URL = "https://accounts.google.com/RotateCookies"
    UPLOAD_URL = "https://content-push.googleapis.com/upload"
    GENERATE_URL = (
        "https://gemini.google.com/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
    )
    BATCH_URL = "https://gemini.google.com/_/BardChatUi/data/batchexecute"

    def __init__(self, session_factory: Callable[..., Any] = requests.Session) -> None:
        self._session_factory = session_factory
        self._initialization_diagnostics: dict[str, int] = {}
        self._generation_diagnostics: dict[str, int] = {}

    @property
    def initialization_diagnostics(self) -> Mapping[str, int]:
        return dict(self._initialization_diagnostics)

    @property
    def generation_diagnostics(self) -> Mapping[str, int]:
        return dict(self._generation_diagnostics)

    def _session(self, account: GeminiWebAccount) -> Any:
        session = self._session_factory(impersonate="chrome", verify=True)
        session.cookies.update(dict(account.cookies))
        return session

    def initialize(
        self,
        account: GeminiWebAccount,
        timeout_seconds: float,
    ) -> TransportSession:
        self._initialization_diagnostics.clear()
        deadline = time.monotonic() + timeout_seconds
        client = self._session(account)
        try:
            client.get(
                self.GOOGLE_URL,
                timeout=self._remaining_timeout(deadline),
            )
            response = client.get(
                self.APP_URL,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                    "Origin": "https://gemini.google.com",
                    "Referer": "https://gemini.google.com/",
                },
                timeout=self._remaining_timeout(deadline),
            )
        except Exception as error:
            self._raise_network_failure(error)
        status_code = int(getattr(response, "status_code", 0) or 0)
        self._initialization_diagnostics["http_status_class"] = status_code // 100
        _raise_for_status(response)
        text = str(getattr(response, "text", ""))
        xsrf_match = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', text)
        build_match = re.search(r'"cfb2h"\s*:\s*"([^"]+)"', text)
        session_match = re.search(r'"FdrFJe"\s*:\s*"([^"]+)"', text)
        language_match = re.search(r'"TuX5cc"\s*:\s*"([^"]+)"', text)
        push_match = re.search(r'"qKIAYe"\s*:\s*"([^"]+)"', text)
        email_match = re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', text)
        if xsrf_match is None:
            self._initialization_diagnostics["rotation_attempted"] = 1
            try:
                rotation_response = client.post(
                    self.ROTATE_COOKIES_URL,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://accounts.google.com",
                    },
                    data='[000,"-0000000000000000000"]',
                    timeout=self._remaining_timeout(deadline),
                )
            except Exception as error:
                self._raise_network_failure(error)
            rotation_status = int(
                getattr(rotation_response, "status_code", 0) or 0
            )
            self._initialization_diagnostics["rotation_status_class"] = (
                rotation_status // 100
            )
            _raise_for_status(rotation_response)
            try:
                response = client.get(
                    self.APP_URL,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                        "Origin": "https://gemini.google.com",
                        "Referer": "https://gemini.google.com/",
                    },
                    timeout=self._remaining_timeout(deadline),
                )
            except Exception as error:
                self._raise_network_failure(error)
            _raise_for_status(response)
            text = str(getattr(response, "text", ""))
            xsrf_match = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', text)
            build_match = re.search(r'"cfb2h"\s*:\s*"([^"]+)"', text)
            session_match = re.search(r'"FdrFJe"\s*:\s*"([^"]+)"', text)
            language_match = re.search(r'"TuX5cc"\s*:\s*"([^"]+)"', text)
            push_match = re.search(r'"qKIAYe"\s*:\s*"([^"]+)"', text)
            email_match = re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', text)
            self._initialization_diagnostics["rotation_yielded_token"] = int(
                xsrf_match is not None
            )
        self._initialization_diagnostics.update(
            {
                "xsrf_present": int(xsrf_match is not None),
                "build_label_present": int(build_match is not None),
                "session_id_present": int(session_match is not None),
                "language_present": int(language_match is not None),
                "push_id_present": int(push_match is not None),
                "email_present": int(email_match is not None),
            }
        )
        payload = {
            "account": {"email": email_match.group(0) if email_match else None},
            "session": {
                "xsrf": xsrf_match.group(1) if xsrf_match else None,
                "build_label": build_match.group(1) if build_match else None,
                "session_id": session_match.group(1) if session_match else None,
                "language": language_match.group(1) if language_match else None,
                "push_id": push_match.group(1) if push_match else None,
            },
        }
        session = parse_initialization_payload(
            payload,
            account=account,
            http_client=client,
        )
        model_headers, available_model_count = self._discover_model_headers(
            client,
            session,
            deadline,
        )
        self._initialization_diagnostics["available_model_count"] = (
            available_model_count
        )
        self._initialization_diagnostics["model_header_selected"] = int(
            bool(model_headers)
        )
        refreshed_cookies = self._session_cookie_mapping(client, account.cookies)
        return replace(
            session,
            model_headers=tuple(model_headers.items()),
            refreshed_cookies=tuple(sorted(refreshed_cookies.items())),
        )

    @staticmethod
    def _session_cookie_mapping(
        client: Any,
        fallback: Mapping[str, str],
    ) -> dict[str, str]:
        merged = dict(fallback)
        cookie_store = getattr(client, "cookies", None)
        candidate: Any = cookie_store
        get_dict = getattr(cookie_store, "get_dict", None)
        if callable(get_dict):
            try:
                candidate = get_dict()
            except Exception:
                candidate = None
        if isinstance(candidate, Mapping):
            merged.update(
                {
                    str(name): value
                    for name, value in candidate.items()
                    if str(name).strip() and isinstance(value, str) and value
                }
            )
        return merged

    def _discover_model_headers(
        self,
        client: Any,
        session: TransportSession,
        deadline: float,
    ) -> tuple[dict[str, str], int]:
        params: dict[str, Any] = {
            "rpcids": "otAQ7b",
            "hl": session.language,
            "_reqid": int(time.time() * 1000) % 90000 + 10000,
            "rt": "c",
            "source-path": "/app",
        }
        if session.build_label:
            params["bl"] = session.build_label
        if session.session_id:
            params["f.sid"] = session.session_id
        try:
            response = client.post(
                self.BATCH_URL,
                params=params,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                    "Origin": "https://gemini.google.com",
                    "Referer": "https://gemini.google.com/",
                    "X-Same-Domain": "1",
                    "x-goog-ext-525001261-jspb": "[1,null,null,null,null,null,null,null,[4]]",
                    "x-goog-ext-73010989-jspb": "[0]",
                },
                data={
                    "at": session.xsrf_token,
                    "f.req": json.dumps(
                        [[["otAQ7b", "[]", None, "generic"]]],
                        separators=(",", ":"),
                    ),
                },
                timeout=self._remaining_timeout(deadline),
            )
        except Exception as error:
            self._raise_network_failure(error)
        _raise_for_status(response)

        for part in _decode_google_frames(str(getattr(response, "text", ""))):
            inner_json = _nested(part, (2,))
            if not isinstance(inner_json, str) or not inner_json:
                continue
            try:
                body = json.loads(inner_json)
            except (TypeError, ValueError):
                continue
            status_code = _nested(body, (14,))
            if status_code not in {None, 1000}:
                raise GeminiTransportFailure(
                    GeminiTransportFailureKind.INVALID_COOKIE
                )
            models = _nested(body, (15,), [])
            if not isinstance(models, list) or not models:
                continue
            tier_flags = _nested(body, (16,), [])
            capability_flags = _nested(body, (17,), [])
            tier_flags = tier_flags if isinstance(tier_flags, list) else []
            capability_flags = (
                capability_flags if isinstance(capability_flags, list) else []
            )
            capacity, capacity_field = self._model_capacity(
                tier_flags,
                capability_flags,
            )
            model_ids = [
                model_id
                for model in models
                if isinstance(model, list)
                for model_id in [_optional_string(_nested(model, (0,)))]
                if model_id
            ]
            if not model_ids:
                continue
            tail = f"null,{capacity}" if capacity_field == 13 else str(capacity)
            return (
                {
                    "x-goog-ext-525001261-jspb": (
                        f'[1,null,null,null,"{model_ids[0]}",null,null,0,'
                        f"[4],null,null,{tail}]"
                    ),
                    "x-goog-ext-73010989-jspb": "[0]",
                    "x-goog-ext-73010990-jspb": "[0]",
                },
                len(model_ids),
            )
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)

    @staticmethod
    def _model_capacity(
        tier_flags: list[Any],
        capability_flags: list[Any],
    ) -> tuple[int, int]:
        if 21 in tier_flags:
            return 1, 13
        if 22 in tier_flags:
            return 2, 13
        if 115 in capability_flags:
            return 4, 12
        if 16 in tier_flags or 106 in capability_flags:
            return 3, 12
        if 8 in tier_flags or (
            106 not in capability_flags and 19 in capability_flags
        ):
            return 2, 12
        return 1, 12

    def upload(
        self,
        session: TransportSession,
        content: bytes,
        mime_type: str,
        timeout_seconds: float,
    ) -> UploadedReference:
        suffix = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(mime_type, ".bin")
        filename = f"input_{uuid.uuid4().hex[:12]}{suffix}"
        multipart = CurlMime()
        multipart.addpart(
            name="file",
            content_type=mime_type,
            filename=filename,
            data=content,
        )
        client = session.http_client or self._session(session.account)
        try:
            response = client.post(
                self.UPLOAD_URL,
                headers={
                    "Origin": "https://gemini.google.com",
                    "Referer": "https://gemini.google.com/",
                    "X-Tenant-Id": "bard-storage",
                    "Push-ID": session.push_id or "",
                },
                multipart=multipart,
                allow_redirects=True,
                timeout=timeout_seconds,
            )
        except Exception as error:
            self._raise_network_failure(error)
        finally:
            multipart.close()
        _raise_for_status(response)
        token = str(getattr(response, "text", "")).strip()
        if not token:
            raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
        return UploadedReference(
            token=token,
            mime_type=mime_type,
            filename=filename,
        )

    def generate(
        self,
        session: TransportSession,
        prompt: str,
        references: tuple[UploadedReference, ...],
        timeout_seconds: float,
    ) -> TransportGeneration:
        deadline = time.monotonic() + timeout_seconds
        client = session.http_client or self._session(session.account)
        self._send_bard_activity(client, session, deadline)
        request_id = int(time.time() * 1000) % 90000 + 10000
        request_uuid = str(uuid.uuid4()).upper()
        file_data = [
            [[reference.token], reference.filename]
            for reference in references
        ] or None
        message_content = [prompt, 0, None, file_data, None, None, 0]
        inner_request: list[Any] = [None] * 69
        inner_request[0] = message_content
        inner_request[1] = [session.language]
        inner_request[2] = ["", "", "", None, None, None, None, None, None, ""]
        inner_request[6] = [1]
        inner_request[7] = 1
        inner_request[10] = 1
        inner_request[11] = 0
        inner_request[17] = [[0]]
        inner_request[18] = 0
        inner_request[27] = 1
        inner_request[30] = [4]
        inner_request[41] = [1]
        inner_request[53] = 0
        inner_request[55] = [[1]]
        inner_request[59] = request_uuid
        inner_request[61] = []
        inner_request[68] = 2
        params: dict[str, Any] = {
            "hl": session.language,
            "_reqid": request_id,
            "rt": "c",
        }
        if session.build_label:
            params["bl"] = session.build_label
        if session.session_id:
            params["f.sid"] = session.session_id
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/",
            "X-Same-Domain": "1",
            "x-goog-ext-525005358-jspb": f'["{request_uuid}",1]',
        }
        headers.update(dict(session.model_headers))
        try:
            response = client.post(
                self.GENERATE_URL,
                params=params,
                headers=headers,
                data={
                    "at": session.xsrf_token,
                    "f.req": json.dumps(
                        [None, json.dumps(inner_request, ensure_ascii=False)],
                        ensure_ascii=False,
                    ),
                },
                timeout=self._remaining_timeout(deadline),
            )
        except Exception as error:
            self._raise_network_failure(error)
        _raise_for_status(response)
        generation = parse_generation_stream(
            str(getattr(response, "text", "")),
            self._generation_diagnostics,
        )
        resolved_images = tuple(
            replace(
                candidate,
                download_ref=self._resolve_original_url(
                    client,
                    session,
                    candidate,
                    deadline,
                ),
            )
            for candidate in generation.images
        )
        return TransportGeneration(
            images=resolved_images,
            revised_prompt=generation.revised_prompt,
        )

    def _send_bard_activity(
        self,
        client: Any,
        session: TransportSession,
        deadline: float,
    ) -> None:
        params: dict[str, Any] = {
            "rpcids": "ESY5D",
            "hl": session.language,
            "_reqid": int(time.time() * 1000) % 90000 + 10000,
            "rt": "c",
            "source-path": "/app",
        }
        if session.build_label:
            params["bl"] = session.build_label
        if session.session_id:
            params["f.sid"] = session.session_id
        try:
            response = client.post(
                self.BATCH_URL,
                params=params,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                    "Origin": "https://gemini.google.com",
                    "Referer": "https://gemini.google.com/",
                    "X-Same-Domain": "1",
                    "x-goog-ext-525001261-jspb": "[1,null,null,null,null,null,null,null,[4]]",
                    "x-goog-ext-73010989-jspb": "[0]",
                },
                data={
                    "at": session.xsrf_token,
                    "f.req": json.dumps(
                        [
                            [
                                [
                                    "ESY5D",
                                    '[[["bard_activity_enabled"]]]',
                                    None,
                                    "generic",
                                ]
                            ]
                        ]
                    ),
                },
                timeout=self._remaining_timeout(deadline),
            )
        except Exception as error:
            self._raise_network_failure(error)
        _raise_for_status(response)

    def _resolve_original_url(
        self,
        client: Any,
        session: TransportSession,
        candidate: TransportImageCandidate,
        deadline: float,
    ) -> str:
        if not all(
            (
                candidate.image_id,
                candidate.conversation_id,
                candidate.response_id,
                candidate.choice_id,
            )
        ):
            return candidate.download_ref
        payload = [
            [
                [
                    None,
                    None,
                    None,
                    [None, None, None, None, None, ""],
                ],
                [candidate.image_id, 0],
                None,
                [19, ""],
                None,
                None,
                None,
                None,
                None,
                "",
            ],
            [
                candidate.response_id,
                candidate.choice_id,
                candidate.conversation_id,
                None,
                "",
            ],
            1,
            0,
            1,
        ]
        params: dict[str, Any] = {
            "rpcids": "c8o8Fe",
            "hl": session.language,
            "_reqid": int(time.time() * 1000) % 90000 + 10000,
            "rt": "c",
            "source-path": "/app",
        }
        if session.build_label:
            params["bl"] = session.build_label
        if session.session_id:
            params["f.sid"] = session.session_id
        try:
            response = client.post(
                self.BATCH_URL,
                params=params,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                    "Origin": "https://gemini.google.com",
                    "Referer": "https://gemini.google.com/",
                    "X-Same-Domain": "1",
                    "x-goog-ext-525001261-jspb": "[1,null,null,null,null,null,null,null,[4]]",
                    "x-goog-ext-73010989-jspb": "[0]",
                },
                data={
                    "at": session.xsrf_token,
                    "f.req": json.dumps(
                        [[["c8o8Fe", json.dumps(payload), None, "generic"]]]
                    ),
                },
                timeout=self._remaining_timeout(deadline),
            )
        except Exception as error:
            self._raise_network_failure(error)
        _raise_for_status(response)
        for part in _decode_google_frames(str(getattr(response, "text", ""))):
            inner_json = _nested(part, (2,))
            if not isinstance(inner_json, str):
                continue
            try:
                decoded = json.loads(inner_json)
            except (TypeError, ValueError):
                continue
            original_url = _optional_string(_nested(decoded, (0,)))
            if original_url:
                return self._resolve_original_download_reference(
                    client,
                    original_url,
                    deadline,
                )
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)

    def _resolve_original_download_reference(
        self,
        client: Any,
        original_url: str,
        deadline: float,
    ) -> str:
        request_url = f"{original_url}=d-I?alr=yes"
        for _ in range(2):
            try:
                response = client.get(
                    request_url,
                    headers={
                        "Origin": "https://gemini.google.com",
                        "Referer": "https://gemini.google.com/",
                    },
                    timeout=self._remaining_timeout(deadline),
                )
            except Exception as error:
                self._raise_network_failure(error)
            _raise_for_status(response)
            request_url = str(getattr(response, "text", "")).strip()
            if not request_url.startswith(("https://", "http://")):
                raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
        return request_url

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GeminiTransportFailure(GeminiTransportFailureKind.TIMEOUT)
        return remaining

    @staticmethod
    def _raise_network_failure(error: Exception) -> None:
        if "timeout" in type(error).__name__.lower():
            raise GeminiTransportFailure(GeminiTransportFailureKind.TIMEOUT) from None
        raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL) from None


class HttpResultDownloader:
    def __init__(self, session_factory: Callable[..., Any] = requests.Session) -> None:
        self._session_factory = session_factory

    def download(
        self,
        account: GeminiWebAccount,
        download_ref: str,
        timeout_seconds: float,
    ) -> DownloadedImage:
        session = self._session_factory(impersonate="chrome", verify=True)
        session.cookies.update(dict(account.cookies))
        try:
            response = session.get(
                download_ref,
                headers={
                    "Origin": "https://gemini.google.com",
                    "Referer": "https://gemini.google.com/",
                },
                timeout=timeout_seconds,
            )
        except Exception as error:
            HttpGeminiWebTransport._raise_network_failure(error)
        _raise_for_status(response)
        mime_type = str(getattr(response, "headers", {}).get("content-type", ""))
        mime_type = mime_type.split(";", 1)[0].strip().lower()
        content = bytes(getattr(response, "content", b""))
        if not mime_type.startswith("image/") or not content:
            raise GeminiTransportFailure(GeminiTransportFailureKind.PROTOCOL)
        return DownloadedImage(content=content, mime_type=mime_type)
