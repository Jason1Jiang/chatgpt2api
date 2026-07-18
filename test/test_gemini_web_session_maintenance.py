from __future__ import annotations

import json
import os
import time
import unittest
from typing import Any

from cryptography.fernet import Fernet

from services.account_service import AccountService
from services.gemini_web_backend import (
    AccountValidation,
    DownloadedImage,
    GeminiWebAccount,
    GeminiWebBackend,
    GeminiWebError,
    GeminiWebErrorCode,
    ImageRequest,
    TransportGeneration,
    TransportImageCandidate,
    TransportSession,
)
from services.gemini_web_credentials import (
    GeminiWebCookieVault,
    _decode_local_key,
    _encode_local_key,
    run_gemini_web_cookie_maintenance,
    start_gemini_web_cookie_maintainer,
)


OLD_COOKIE = "FAKE_OLD_COOKIE_MUST_NOT_BE_STORED"
ROTATED_COOKIE = "FAKE_ROTATED_COOKIE_MUST_NOT_BE_STORED"


class MemoryStorage:
    def __init__(self, accounts: list[dict[str, Any]] | None = None) -> None:
        self.accounts = list(accounts or [])

    def load_accounts(self) -> list[dict[str, Any]]:
        return list(self.accounts)

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self.accounts = list(accounts)

    def load_auth_keys(self) -> list[dict[str, Any]]:
        return []

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        return None


class ValidationBackend:
    def __init__(self) -> None:
        self.validated: list[str] = []
        self.failure: GeminiWebError | None = None

    def prepare_account(self, cookie_payload: Any) -> dict[str, Any]:
        return {
            "account_id": "gemini_web:fixture",
            "provider": "gemini_web",
            "type": "Gemini Web",
            "source_type": "cookie_json",
            "email": "fixture@example.invalid",
            "session_label": "gemini-session:fixture",
            "status": "正常",
            "credentials": {"cookies": dict(cookie_payload["cookies"])},
        }

    def validate_account(self, account: dict[str, Any]) -> AccountValidation:
        self.validated.append(str(account["account_id"]))
        if self.failure is not None:
            raise self.failure
        return AccountValidation(
            valid=True,
            email="fixture@example.invalid",
            session_label="gemini-session:fixture",
            refreshed_cookies=(("__Secure-1PSIDTS", ROTATED_COOKIE),),
        )


def account(account_id: str, *, status: str = "正常") -> dict[str, Any]:
    return {
        "account_id": account_id,
        "provider": "gemini_web",
        "type": "Gemini Web",
        "source_type": "cookie_json",
        "status": status,
        "credentials": {"cookies": {"__Secure-1PSID": OLD_COOKIE}},
    }


class FakeStopEvent:
    def __init__(self, stop_on_wait: int) -> None:
        self.stop_on_wait = stop_on_wait
        self.waits: list[float] = []

    def wait(self, delay: float) -> bool:
        self.waits.append(delay)
        return len(self.waits) >= self.stop_on_wait


class CapturingPool:
    def __init__(self) -> None:
        self.account = GeminiWebAccount(
            account_id="gemini_web:fixture",
            cookies={"__Secure-1PSID": OLD_COOKIE},
        )
        self.persisted: list[dict[str, str]] = []

    def acquire(self, **_: Any) -> GeminiWebAccount:
        return self.account

    def release(self, account: GeminiWebAccount) -> None:
        return None

    def mark_invalid(self, account: GeminiWebAccount) -> None:
        return None

    def persist_refreshed_cookies(
        self,
        account: GeminiWebAccount,
        cookies: dict[str, str],
    ) -> None:
        self.persisted.append(dict(cookies))


class RefreshedTransport:
    def initialize(
        self,
        account: GeminiWebAccount,
        timeout_seconds: float,
    ) -> TransportSession:
        return TransportSession(
            account=account,
            email=None,
            xsrf_token="fixture-xsrf",
            refreshed_cookies=(
                ("__Secure-1PSID", OLD_COOKIE),
                ("__Secure-1PSIDTS", ROTATED_COOKIE),
            ),
        )

    def upload(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("upload should not be called")

    def generate(self, *args: Any, **kwargs: Any) -> TransportGeneration:
        return TransportGeneration(
            images=(
                TransportImageCandidate(
                    kind="generated_image",
                    download_ref="fixture://generated",
                    mime_type="image/png",
                ),
            )
        )


class FixtureDownloader:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}

    def download(
        self,
        account: GeminiWebAccount,
        *args: Any,
        **kwargs: Any,
    ) -> DownloadedImage:
        self.cookies = dict(account.cookies)
        return DownloadedImage(content=b"fixture-image", mime_type="image/png")


class GeminiWebCookieVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vault = GeminiWebCookieVault.from_key(Fernet.generate_key())

    def test_encrypted_round_trip_never_contains_plain_cookie(self) -> None:
        protected = self.vault.seal({"__Secure-1PSID": OLD_COOKIE})

        self.assertEqual(protected["scheme"], "fernet-v1")
        self.assertNotIn(OLD_COOKIE, json.dumps(protected))
        self.assertEqual(
            self.vault.open(protected),
            {"__Secure-1PSID": OLD_COOKIE},
        )

    def test_local_key_uses_dpapi_on_windows(self) -> None:
        key = Fernet.generate_key()

        encoded = _encode_local_key(key)
        decoded, migrate = _decode_local_key(encoded)

        self.assertEqual(decoded, key)
        self.assertFalse(migrate)
        if os.name == "nt":
            self.assertTrue(encoded.startswith(b"dpapi-v1:"))
            self.assertNotIn(key, encoded)

            legacy_key, migrate = _decode_local_key(key)
            self.assertEqual(legacy_key, key)
            self.assertTrue(migrate)

    def test_import_encrypts_storage_and_process_reload_can_decrypt(self) -> None:
        storage = MemoryStorage()
        backend = ValidationBackend()
        service = AccountService(
            storage,
            gemini_web_backend=backend,  # type: ignore[arg-type]
            gemini_web_cookie_vault=self.vault,
        )

        service.import_gemini_web_account(
            {"cookies": {"__Secure-1PSID": OLD_COOKIE}}
        )

        serialized = json.dumps(storage.accounts)
        self.assertNotIn(OLD_COOKIE, serialized)
        self.assertIn("protected_cookies", serialized)
        reloaded = AccountService(
            storage,
            gemini_web_backend=backend,  # type: ignore[arg-type]
            gemini_web_cookie_vault=self.vault,
        )
        self.assertEqual(
            reloaded.get_account("gemini_web:fixture")["credentials"]["cookies"]
            ["__Secure-1PSID"],
            OLD_COOKIE,
        )

    def test_successful_validation_persists_rotation_but_failure_keeps_cookie(self) -> None:
        storage = MemoryStorage([account("gemini_web:fixture")])
        backend = ValidationBackend()
        service = AccountService(
            storage,
            gemini_web_backend=backend,  # type: ignore[arg-type]
            gemini_web_cookie_vault=self.vault,
        )

        service.validate_account("gemini_web:fixture")

        stored = service.get_account("gemini_web:fixture")
        self.assertEqual(
            stored["credentials"]["cookies"]["__Secure-1PSIDTS"],
            ROTATED_COOKIE,
        )
        encrypted_before_failure = storage.accounts[0]["credentials"][
            "protected_cookies"
        ]
        backend.failure = GeminiWebError(GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)
        with self.assertRaises(GeminiWebError):
            service.validate_account("gemini_web:fixture")
        self.assertEqual(
            service.get_account("gemini_web:fixture")["credentials"]["cookies"]
            ["__Secure-1PSIDTS"],
            ROTATED_COOKIE,
        )
        self.assertNotEqual(
            storage.accounts[0]["credentials"]["protected_cookies"],
            encrypted_before_failure,
        )

    def test_maintenance_skips_disabled_and_inflight_accounts(self) -> None:
        storage = MemoryStorage(
            [
                account("gemini_web:a"),
                account("gemini_web:b"),
                account("gemini_web:disabled", status="禁用"),
                account("gemini_web:abnormal", status="异常"),
                account("gemini_web:refreshing"),
            ]
        )
        backend = ValidationBackend()
        service = AccountService(
            storage,
            gemini_web_backend=backend,  # type: ignore[arg-type]
            gemini_web_cookie_vault=self.vault,
        )
        inflight = service.acquire_gemini_web_account(
            excluded_account_ids=set(),
            deadline=time.monotonic() + 1,
        )
        service._gemini_web_refreshing.add("gemini_web:refreshing")

        result = service.maintain_gemini_web_sessions()
        service.release_image_slot(inflight["account_id"])
        service._gemini_web_refreshing.discard("gemini_web:refreshing")

        self.assertEqual(result, {"refreshed": 1, "failed": 0, "skipped": 4})
        self.assertEqual(
            backend.validated,
            ["gemini_web:b" if inflight["account_id"] == "gemini_web:a" else "gemini_web:a"],
        )

    def test_transient_maintenance_failure_backs_off_without_disabling_account(self) -> None:
        storage = MemoryStorage([account("gemini_web:fixture")])
        backend = ValidationBackend()
        backend.failure = GeminiWebError(GeminiWebErrorCode.UPSTREAM_TIMEOUT)
        service = AccountService(
            storage,
            gemini_web_backend=backend,  # type: ignore[arg-type]
            gemini_web_cookie_vault=self.vault,
        )

        failed = service.maintain_gemini_web_sessions()
        backend.failure = None
        recovered = service.maintain_gemini_web_sessions()

        self.assertEqual(failed, {"refreshed": 0, "failed": 1, "skipped": 0})
        self.assertEqual(recovered, {"refreshed": 1, "failed": 0, "skipped": 0})
        self.assertEqual(
            service.get_account("gemini_web:fixture")["status"],
            "正常",
        )

    def test_reimport_replaces_invalid_cookie_and_restores_account(self) -> None:
        storage = MemoryStorage(
            [account("gemini_web:fixture", status="异常")]
        )
        backend = ValidationBackend()
        service = AccountService(
            storage,
            gemini_web_backend=backend,  # type: ignore[arg-type]
            gemini_web_cookie_vault=self.vault,
        )

        result = service.import_gemini_web_account(
            {"cookies": {"__Secure-1PSID": ROTATED_COOKIE}}
        )

        self.assertEqual(result["item"]["status"], "正常")
        self.assertEqual(
            service.get_account("gemini_web:fixture")["credentials"]["cookies"]
            ["__Secure-1PSID"],
            ROTATED_COOKIE,
        )
        self.assertNotIn(ROTATED_COOKIE, json.dumps(storage.accounts))

    def test_maintenance_worker_enforces_minimum_interval_and_backs_off(self) -> None:
        stop = FakeStopEvent(stop_on_wait=3)
        results = iter(
            [
                {"refreshed": 0, "failed": 1, "skipped": 0},
                {"refreshed": 1, "failed": 0, "skipped": 0},
            ]
        )

        run_gemini_web_cookie_maintenance(
            stop,  # type: ignore[arg-type]
            lambda: next(results),
            interval_seconds=1,
            maximum_backoff_seconds=300,
        )

        self.assertEqual(stop.waits, [60.0, 120.0, 60.0])

    def test_maintenance_worker_stops_cleanly_without_running_after_shutdown(self) -> None:
        from threading import Event

        stop = Event()
        calls: list[bool] = []
        thread = start_gemini_web_cookie_maintainer(
            stop,
            lambda: calls.append(True) or {"refreshed": 0, "failed": 0},
            interval_seconds=60,
        )

        stop.set()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [])

    def test_authenticated_generation_persists_refreshed_session_cookie(self) -> None:
        pool = CapturingPool()
        downloader = FixtureDownloader()
        backend = GeminiWebBackend(
            account_pool=pool,
            transport=RefreshedTransport(),
            downloader=downloader,
            timeout_seconds=30,
        )

        result = backend.generate(ImageRequest(prompt="fixture"))

        self.assertEqual(result.images[0].content, b"fixture-image")
        self.assertEqual(
            pool.persisted,
            [
                {
                    "__Secure-1PSID": OLD_COOKIE,
                    "__Secure-1PSIDTS": ROTATED_COOKIE,
                }
            ],
        )
        self.assertEqual(
            downloader.cookies,
            {
                "__Secure-1PSID": OLD_COOKIE,
                "__Secure-1PSIDTS": ROTATED_COOKIE,
            },
        )


if __name__ == "__main__":
    unittest.main()
