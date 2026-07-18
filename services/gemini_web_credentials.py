from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable, Mapping, Protocol

from cryptography.fernet import Fernet, InvalidToken


class StopEvent(Protocol):
    def wait(self, timeout: float) -> bool: ...


class GeminiWebCookieVault:
    """Encrypt and decrypt one Gemini Web cookie mapping behind a small seam."""

    SCHEME = "fernet-v1"

    def __init__(self, key_provider: Callable[[], bytes]) -> None:
        self._key_provider = key_provider
        self._fernet: Fernet | None = None
        self._lock = Lock()

    @classmethod
    def from_key(cls, key: bytes) -> GeminiWebCookieVault:
        Fernet(key)
        return cls(lambda: key)

    def _cipher(self) -> Fernet:
        with self._lock:
            if self._fernet is None:
                try:
                    self._fernet = Fernet(self._key_provider())
                except Exception:
                    raise RuntimeError(
                        "Gemini Web cookie protection is not configured"
                    ) from None
            return self._fernet

    def seal(self, cookies: Mapping[str, str]) -> dict[str, str]:
        normalized = {
            str(name): value
            for name, value in cookies.items()
            if str(name).strip() and isinstance(value, str) and value
        }
        if not normalized:
            raise ValueError("Gemini Web cookies are empty")
        plaintext = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "scheme": self.SCHEME,
            "payload": self._cipher().encrypt(plaintext).decode("ascii"),
        }

    def open(self, protected: Mapping[str, Any]) -> dict[str, str]:
        if protected.get("scheme") != self.SCHEME:
            raise ValueError("unsupported Gemini Web cookie protection")
        payload = protected.get("payload")
        if not isinstance(payload, str) or not payload:
            raise ValueError("invalid protected Gemini Web cookies")
        try:
            decoded = self._cipher().decrypt(payload.encode("ascii"))
            raw = json.loads(decoded.decode("utf-8"))
        except (InvalidToken, UnicodeError, ValueError, TypeError):
            raise ValueError("invalid protected Gemini Web cookies") from None
        if not isinstance(raw, Mapping):
            raise ValueError("invalid protected Gemini Web cookies")
        cookies = {
            str(name): value
            for name, value in raw.items()
            if str(name).strip() and isinstance(value, str) and value
        }
        if not cookies:
            raise ValueError("invalid protected Gemini Web cookies")
        return cookies


def _default_key_path() -> Path:
    configured = str(os.getenv("GEMINI_WEB_COOKIE_KEY_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".chatgpt2api" / "gemini_web_cookie.key"


def _windows_dpapi_transform(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        return data
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    buffer = ctypes.create_string_buffer(data)
    input_blob = DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    if not function(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.data, ctypes.c_void_p))


def _encode_local_key(key: bytes) -> bytes:
    if os.name != "nt":
        return key
    protected = _windows_dpapi_transform(key, protect=True)
    return b"dpapi-v1:" + base64.urlsafe_b64encode(protected)


def _decode_local_key(stored: bytes) -> tuple[bytes, bool]:
    marker = b"dpapi-v1:"
    if stored.startswith(marker):
        if os.name != "nt":
            raise RuntimeError("Windows-protected Gemini Web cookie key is unavailable")
        try:
            protected = base64.urlsafe_b64decode(stored[len(marker) :])
            return _windows_dpapi_transform(protected, protect=False), False
        except Exception:
            raise RuntimeError(
                "Gemini Web cookie protection is not configured"
            ) from None
    return stored, os.name == "nt"


def _replace_key_file(path: Path, stored: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(stored + b"\n")
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_or_create_cookie_key() -> bytes:
    configured = str(os.getenv("GEMINI_WEB_COOKIE_KEY") or "").strip()
    if configured:
        return configured.encode("ascii")

    path = _default_key_path()
    try:
        stored = path.read_bytes().strip()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        generated = Fernet.generate_key()
        encoded = _encode_local_key(generated)
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            stored = path.read_bytes().strip()
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded + b"\n")
            try:
                path.chmod(0o600)
            except OSError:
                pass
            stored = encoded
    key, migrate = _decode_local_key(stored)
    if migrate:
        _replace_key_file(path, _encode_local_key(key))
    if not key:
        raise RuntimeError("Gemini Web cookie protection is not configured")
    return key


_default_vault: GeminiWebCookieVault | None = None
_default_vault_lock = Lock()


def default_gemini_web_cookie_vault() -> GeminiWebCookieVault:
    global _default_vault
    with _default_vault_lock:
        if _default_vault is None:
            _default_vault = GeminiWebCookieVault(_load_or_create_cookie_key)
        return _default_vault


def run_gemini_web_cookie_maintenance(
    stop_event: StopEvent,
    maintain_once: Callable[[], Mapping[str, int]],
    *,
    interval_seconds: float = 600,
    maximum_backoff_seconds: float = 3600,
) -> None:
    interval = max(60.0, float(interval_seconds))
    maximum = max(interval, float(maximum_backoff_seconds))
    delay = interval
    while not stop_event.wait(delay):
        try:
            result = maintain_once()
            failed = max(0, int(result.get("failed") or 0))
        except Exception:
            failed = 1
        delay = min(maximum, delay * 2) if failed else interval


def start_gemini_web_cookie_maintainer(
    stop_event: StopEvent,
    maintain_once: Callable[[], Mapping[str, int]],
    *,
    interval_seconds: float = 600,
    maximum_backoff_seconds: float = 3600,
) -> Thread:
    thread = Thread(
        target=run_gemini_web_cookie_maintenance,
        args=(stop_event, maintain_once),
        kwargs={
            "interval_seconds": interval_seconds,
            "maximum_backoff_seconds": maximum_backoff_seconds,
        },
        daemon=True,
        name="gemini-web-cookie-maintainer",
    )
    thread.start()
    return thread
