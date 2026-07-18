from __future__ import annotations

import copy
import time
import unittest
from dataclasses import dataclass
from typing import Any, Mapping
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import ai as ai_api
from api.errors import install_exception_handlers
from services.account_service import AccountService
from services.gemini_web_backend import (
    DownloadedImage,
    GeminiTransportFailure,
    GeminiTransportFailureKind,
    GeminiWebAccount,
    GeminiWebBackend,
    GeminiWebError,
    GeminiWebErrorCode,
    ImageReference,
    ImageRequest,
    PersistedGeminiWebAccountPool,
    TransportGeneration,
    TransportImageCandidate,
    TransportSession,
    UploadedReference,
    _raise_for_status,
)
from services.config import config
from services.protocol import openai_v1_image_edit, openai_v1_image_generations
from test.test_gemini_web_account_management import MemoryStorage


def _account(account_id: str, *, status: str = "正常", provider: str = "gemini_web") -> dict[str, Any]:
    return {
        "account_id": account_id,
        "provider": provider,
        "type": "Gemini Web",
        "source_type": "cookie_json",
        "status": status,
        "credentials": {"cookies": {"__Secure-1PSID": f"FAKE_{account_id}"}},
    }


class SequencePool:
    def __init__(self, accounts: list[GeminiWebAccount]) -> None:
        self.accounts = accounts
        self.cursor = 0
        self.acquired: list[str] = []
        self.deadlines: list[float] = []
        self.released: list[str] = []
        self.invalid: list[str] = []
        self.inflight: dict[str, int] = {}

    def acquire(
        self,
        *,
        excluded_account_ids: set[str],
        deadline: float,
    ) -> GeminiWebAccount:
        self.deadlines.append(deadline)
        for offset in range(len(self.accounts)):
            index = (self.cursor + offset) % len(self.accounts)
            account = self.accounts[index]
            if account.account_id in excluded_account_ids:
                continue
            self.cursor = index + 1
            self.acquired.append(account.account_id)
            self.inflight[account.account_id] = self.inflight.get(account.account_id, 0) + 1
            return account
        raise GeminiWebError(GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)

    def release(self, account: GeminiWebAccount) -> None:
        self.released.append(account.account_id)
        self.inflight[account.account_id] -= 1

    def mark_invalid(self, account: GeminiWebAccount) -> None:
        self.invalid.append(account.account_id)

    def persist_refreshed_cookies(
        self,
        account: GeminiWebAccount,
        cookies: Mapping[str, str],
    ) -> None:
        return None


class AdvancingClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.index = 0

    def __call__(self) -> float:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


class ScriptedTransport:
    def __init__(self, failures: dict[tuple[str, str], BaseException] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, str, float]] = []

    def _record(self, account_id: str, stage: str, timeout_seconds: float) -> None:
        self.calls.append((account_id, stage, timeout_seconds))
        failure = self.failures.get((account_id, stage))
        if failure is not None:
            raise failure

    def initialize(self, account: GeminiWebAccount, timeout_seconds: float) -> TransportSession:
        self._record(account.account_id, "initialize", timeout_seconds)
        return TransportSession(account=account, email=None, xsrf_token="xsrf", rpc_id="rpc")

    def upload(
        self,
        session: TransportSession,
        content: bytes,
        mime_type: str,
        timeout_seconds: float,
    ) -> UploadedReference:
        self._record(session.account.account_id, "upload", timeout_seconds)
        return UploadedReference(token="upload", mime_type=mime_type)

    def generate(
        self,
        session: TransportSession,
        prompt: str,
        references: tuple[UploadedReference, ...],
        timeout_seconds: float,
    ) -> TransportGeneration:
        self._record(session.account.account_id, "generate", timeout_seconds)
        return TransportGeneration(
            images=(
                TransportImageCandidate(
                    kind="generated_image",
                    download_ref=f"fixture://{session.account.account_id}",
                    mime_type="image/png",
                ),
            )
        )


class ScriptedDownloader:
    def __init__(self, failures: dict[str, BaseException] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, float]] = []

    def download(
        self,
        account: GeminiWebAccount,
        download_ref: str,
        timeout_seconds: float,
    ) -> DownloadedImage:
        self.calls.append((account.account_id, timeout_seconds))
        failure = self.failures.get(account.account_id)
        if failure is not None:
            raise failure
        return DownloadedImage(content=b"generated", mime_type="image/png")


def _normalized(account_id: str) -> GeminiWebAccount:
    return GeminiWebAccount.from_mapping(_account(account_id))


class GeminiWebBackendReliabilityTests(unittest.TestCase):
    def backend(
        self,
        pool: SequencePool,
        *,
        failures: dict[tuple[str, str], BaseException] | None = None,
        download_failures: dict[str, BaseException] | None = None,
        clock: Any | None = None,
    ) -> tuple[GeminiWebBackend, ScriptedTransport, ScriptedDownloader]:
        transport = ScriptedTransport(failures)
        downloader = ScriptedDownloader(download_failures)
        return (
            GeminiWebBackend(
                account_pool=pool,
                transport=transport,
                downloader=downloader,
                clock=clock or (lambda: 10.0),
                timeout_seconds=30,
            ),
            transport,
            downloader,
        )

    def test_invalid_cookie_marks_account_abnormal_then_retries_each_stable_id_once(self) -> None:
        pool = SequencePool([_normalized("gemini_web:a"), _normalized("gemini_web:b")])
        backend, _, _ = self.backend(
            pool,
            failures={
                ("gemini_web:a", "initialize"): GeminiTransportFailure(
                    GeminiTransportFailureKind.INVALID_COOKIE
                )
            },
        )

        result = backend.generate(ImageRequest(prompt="retry"))

        self.assertEqual(result.images[0].content, b"generated")
        self.assertEqual(pool.acquired, ["gemini_web:a", "gemini_web:b"])
        self.assertEqual(pool.released, ["gemini_web:a", "gemini_web:b"])
        self.assertEqual(pool.invalid, ["gemini_web:a"])
        self.assertEqual(pool.inflight, {"gemini_web:a": 0, "gemini_web:b": 0})

    def test_rate_limit_retries_edit_without_resetting_shared_deadline(self) -> None:
        pool = SequencePool([_normalized("gemini_web:a"), _normalized("gemini_web:b")])
        clock = AdvancingClock([100.0 + index for index in range(30)])
        backend, transport, downloader = self.backend(
            pool,
            failures={
                ("gemini_web:a", "generate"): GeminiTransportFailure(
                    GeminiTransportFailureKind.RATE_LIMIT
                )
            },
            clock=clock,
        )

        backend.edit(
            ImageRequest(
                prompt="retry edit",
                references=(ImageReference(content=b"ref", mime_type="image/png"),),
            )
        )

        budgets = [timeout for _, _, timeout in transport.calls] + [
            timeout for _, timeout in downloader.calls
        ]
        self.assertTrue(all(later < earlier for earlier, later in zip(budgets, budgets[1:])), budgets)
        self.assertEqual(pool.acquired, ["gemini_web:a", "gemini_web:b"])
        self.assertEqual(pool.released, ["gemini_web:a", "gemini_web:b"])
        self.assertEqual(len(set(pool.deadlines)), 1)

    def test_http_5xx_is_transient_and_rotates_while_protocol_failure_stays_terminal(self) -> None:
        class Response:
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

        with self.assertRaises(GeminiTransportFailure) as transient:
            _raise_for_status(Response(503))
        self.assertEqual(transient.exception.kind, GeminiTransportFailureKind.TEMPORARY)

        pool = SequencePool([_normalized("gemini_web:a"), _normalized("gemini_web:b")])
        backend, _, _ = self.backend(
            pool,
            failures={
                ("gemini_web:a", "initialize"): GeminiTransportFailure(
                    GeminiTransportFailureKind.TEMPORARY
                )
            },
        )
        result = backend.generate(ImageRequest(prompt="transient retry"))
        self.assertEqual(result.images[0].content, b"generated")
        self.assertEqual(pool.acquired, ["gemini_web:a", "gemini_web:b"])
        self.assertEqual(pool.released, pool.acquired)

        terminal_pool = SequencePool(
            [_normalized("gemini_web:a"), _normalized("gemini_web:b")]
        )
        terminal_backend, _, _ = self.backend(
            terminal_pool,
            failures={
                ("gemini_web:a", "generate"): GeminiTransportFailure(
                    GeminiTransportFailureKind.PROTOCOL
                )
            },
        )
        with self.assertRaises(GeminiWebError) as terminal:
            terminal_backend.generate(ImageRequest(prompt="protocol terminal"))
        self.assertEqual(terminal.exception.code, GeminiWebErrorCode.UPSTREAM_PROTOCOL_ERROR)
        self.assertEqual(terminal_pool.acquired, ["gemini_web:a"])
        self.assertEqual(terminal_pool.released, ["gemini_web:a"])

    def test_all_transient_accounts_exhaust_to_stable_no_available_account(self) -> None:
        pool = SequencePool([_normalized("gemini_web:a"), _normalized("gemini_web:b")])
        backend, _, _ = self.backend(
            pool,
            failures={
                (account.account_id, "initialize"): GeminiTransportFailure(
                    GeminiTransportFailureKind.TEMPORARY
                )
                for account in pool.accounts
            },
        )

        with self.assertRaises(GeminiWebError) as raised:
            backend.generate(ImageRequest(prompt="all transient"))

        self.assertEqual(raised.exception.code, GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(pool.acquired, ["gemini_web:a", "gemini_web:b"])
        self.assertEqual(pool.released, pool.acquired)
        self.assertTrue(all(value == 0 for value in pool.inflight.values()))

    def test_all_invalid_and_all_rate_limited_return_distinct_stable_errors(self) -> None:
        cases = (
            (GeminiTransportFailureKind.INVALID_COOKIE, GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT, 503),
            (GeminiTransportFailureKind.RATE_LIMIT, GeminiWebErrorCode.UPSTREAM_RATE_LIMITED, 429),
        )
        for kind, expected_code, expected_status in cases:
            with self.subTest(kind=kind):
                pool = SequencePool([_normalized("gemini_web:a"), _normalized("gemini_web:b")])
                backend, _, _ = self.backend(
                    pool,
                    failures={
                        (account.account_id, "initialize"): GeminiTransportFailure(kind)
                        for account in pool.accounts
                    },
                )
                with self.assertRaises(GeminiWebError) as raised:
                    backend.generate(ImageRequest(prompt="all fail"))
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(raised.exception.status_code, expected_status)
                self.assertEqual(pool.acquired, ["gemini_web:a", "gemini_web:b"])
                self.assertEqual(pool.released, pool.acquired)
                self.assertTrue(all(value == 0 for value in pool.inflight.values()))

    def test_content_policy_and_protocol_do_not_blindly_switch_accounts(self) -> None:
        for kind, expected_code in (
            (GeminiTransportFailureKind.CONTENT_POLICY, GeminiWebErrorCode.CONTENT_POLICY_VIOLATION),
            (GeminiTransportFailureKind.PROTOCOL, GeminiWebErrorCode.UPSTREAM_PROTOCOL_ERROR),
        ):
            with self.subTest(kind=kind):
                pool = SequencePool([_normalized("gemini_web:a"), _normalized("gemini_web:b")])
                backend, _, _ = self.backend(
                    pool,
                    failures={
                        ("gemini_web:a", "generate"): GeminiTransportFailure(kind)
                    },
                )
                with self.assertRaises(GeminiWebError) as raised:
                    backend.generate(ImageRequest(prompt="terminal"))
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(pool.acquired, ["gemini_web:a"])
                self.assertEqual(pool.released, ["gemini_web:a"])

    def test_upload_download_timeout_and_cancellation_all_release_slot(self) -> None:
        cases = (
            ("upload", RuntimeError("upload failed"), None, GeminiWebErrorCode.UPSTREAM_PROTOCOL_ERROR),
            ("download", None, RuntimeError("download failed"), GeminiWebErrorCode.UPSTREAM_PROTOCOL_ERROR),
            ("timeout", TimeoutError(), None, GeminiWebErrorCode.UPSTREAM_TIMEOUT),
            ("cancel", KeyboardInterrupt(), None, None),
        )
        for name, transport_failure, download_failure, expected_code in cases:
            with self.subTest(name=name):
                pool = SequencePool([_normalized("gemini_web:a")])
                failures = {}
                if transport_failure is not None:
                    failures[("gemini_web:a", "upload")] = transport_failure
                download_failures = (
                    {"gemini_web:a": download_failure} if download_failure is not None else None
                )
                backend, _, _ = self.backend(
                    pool,
                    failures=failures,
                    download_failures=download_failures,
                )
                expected_exception = GeminiWebError if expected_code is not None else KeyboardInterrupt
                with self.assertRaises(expected_exception) as raised:
                    backend.edit(
                        ImageRequest(
                            prompt=name,
                            references=(ImageReference(content=b"ref", mime_type="image/png"),),
                        )
                    )
                if expected_code is not None:
                    self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(pool.released, ["gemini_web:a"])
                self.assertEqual(pool.inflight["gemini_web:a"], 0)


class PersistedGeminiWebAccountPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage(
            [
                _account("gemini_web:a"),
                _account("gemini_web:b"),
                _account("gemini_web:disabled", status="禁用"),
                {
                    "access_token": "chatgpt-token",
                    "provider": "chatgpt",
                    "status": "正常",
                    "quota": 100,
                },
            ]
        )
        self.service = AccountService(self.storage)
        self.pool = PersistedGeminiWebAccountPool(
            acquire_account=self.service.acquire_gemini_web_account,
            release_slot=self.service.release_image_slot,
            mark_invalid_account=self.service.mark_gemini_web_account_invalid,
        )

    def test_filters_provider_and_status_round_robins_and_exposes_real_slot_counts(self) -> None:
        first = self.pool.acquire(excluded_account_ids=set(), deadline=time.monotonic() + 1)
        second = self.pool.acquire(
            excluded_account_ids={first.account_id},
            deadline=time.monotonic() + 1,
        )

        self.assertEqual({first.account_id, second.account_id}, {"gemini_web:a", "gemini_web:b"})
        listed = {item.get("account_id"): item for item in self.service.list_accounts()}
        self.assertEqual(listed[first.account_id]["image_inflight"], 1)
        self.assertEqual(listed[second.account_id]["image_inflight"], 1)
        self.assertEqual(listed["gemini_web:disabled"]["image_inflight"], 0)
        self.pool.release(first)
        self.pool.release(second)
        self.assertTrue(
            all(
                item["image_inflight"] == 0
                for item in self.service.list_accounts()
                if item.get("provider") == "gemini_web"
            )
        )

    def test_separate_calls_use_simple_round_robin(self) -> None:
        first = self.pool.acquire(excluded_account_ids=set(), deadline=time.monotonic() + 1)
        self.pool.release(first)
        second = self.pool.acquire(excluded_account_ids=set(), deadline=time.monotonic() + 1)
        self.pool.release(second)
        self.assertEqual(
            [first.account_id, second.account_id],
            ["gemini_web:a", "gemini_web:b"],
        )

    def test_invalid_cookie_persists_only_abnormal_status_not_quota_fields(self) -> None:
        account = self.pool.acquire(excluded_account_ids=set(), deadline=time.monotonic() + 1)
        self.pool.mark_invalid(account)
        self.pool.release(account)

        stored = self.service.get_account(account.account_id)
        self.assertEqual(stored["status"], "异常")
        self.assertNotIn("quota", stored)
        self.assertNotIn("restore_at", stored)
        self.assertNotIn("limits_progress", stored)

    def test_backend_invalid_cookie_marks_persisted_account_and_switches(self) -> None:
        backend = GeminiWebBackend(
            account_pool=self.pool,
            transport=ScriptedTransport(
                {
                    ("gemini_web:a", "initialize"): GeminiTransportFailure(
                        GeminiTransportFailureKind.INVALID_COOKIE
                    )
                }
            ),
            downloader=ScriptedDownloader(),
            timeout_seconds=30,
        )

        result = backend.generate(ImageRequest(prompt="persist and switch"))

        self.assertEqual(result.images[0].content, b"generated")
        self.assertEqual(self.service.get_account("gemini_web:a")["status"], "异常")
        self.assertEqual(self.service.get_account("gemini_web:b")["status"], "正常")
        listed = {
            item["account_id"]: item["image_inflight"]
            for item in self.service.list_accounts()
            if item.get("provider") == "gemini_web"
        }
        self.assertEqual(listed, {"gemini_web:a": 0, "gemini_web:b": 0, "gemini_web:disabled": 0})

    def test_slot_limit_times_out_then_allows_acquire_after_release(self) -> None:
        storage = MemoryStorage([_account("gemini_web:only")])
        service = AccountService(storage)
        pool = PersistedGeminiWebAccountPool(
            acquire_account=service.acquire_gemini_web_account,
            release_slot=service.release_image_slot,
            mark_invalid_account=service.mark_gemini_web_account_invalid,
        )
        with mock.patch.dict(config.data, {"image_account_concurrency": 1}):
            first = pool.acquire(excluded_account_ids=set(), deadline=time.monotonic() + 1)
            with self.assertRaises(TimeoutError):
                pool.acquire(excluded_account_ids=set(), deadline=time.monotonic())
            pool.release(first)
            second = pool.acquire(excluded_account_ids=set(), deadline=time.monotonic() + 1)
            pool.release(second)
        self.assertEqual(second.account_id, first.account_id)
        listed = service.list_accounts()
        self.assertEqual(listed[0]["image_inflight"], 0)

    def test_rate_limit_rotation_does_not_query_or_persist_quota_state(self) -> None:
        before = copy.deepcopy(self.storage.accounts)
        backend = GeminiWebBackend(
            account_pool=self.pool,
            transport=ScriptedTransport(
                {
                    (account_id, "initialize"): GeminiTransportFailure(
                        GeminiTransportFailureKind.RATE_LIMIT
                    )
                    for account_id in ("gemini_web:a", "gemini_web:b")
                }
            ),
            downloader=ScriptedDownloader(),
            timeout_seconds=30,
        )
        with mock.patch.object(self.service, "fetch_remote_info") as remote_query:
            with self.assertRaises(GeminiWebError) as raised:
                backend.generate(ImageRequest(prompt="no quota query"))
        self.assertEqual(raised.exception.code, GeminiWebErrorCode.UPSTREAM_RATE_LIMITED)
        remote_query.assert_not_called()
        self.assertEqual(self.storage.accounts, before)


@dataclass
class RaisingBackend:
    error: GeminiWebError

    def generate(self, request: ImageRequest) -> Any:
        raise self.error

    def edit(self, request: ImageRequest) -> Any:
        raise self.error


class GeminiWebPublicErrorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(ai_api.create_router())
        self.client = TestClient(app)

    def test_public_generation_preserves_401_before_backend_and_stable_error_envelope(self) -> None:
        with mock.patch.object(
            openai_v1_image_generations,
            "create_gemini_web_generation_backend",
        ) as factory:
            unauthorized = self.client.post(
                "/v1/images/generations",
                json={"model": "gemini-web-image", "prompt": "auth"},
            )
        self.assertEqual(unauthorized.status_code, 401)
        factory.assert_not_called()

        for code in GeminiWebErrorCode:
            with self.subTest(code=code), mock.patch.object(
                ai_api,
                "filter_or_log",
                mock.AsyncMock(),
            ), mock.patch.object(
                openai_v1_image_generations,
                "create_gemini_web_generation_backend",
                return_value=RaisingBackend(GeminiWebError(code)),
            ):
                response = self.client.post(
                    "/v1/images/generations",
                    headers={"Authorization": "Bearer chatgpt2api"},
                    json={"model": "gemini-web-image", "prompt": "mapped"},
                )
            error = response.json()["error"]
            self.assertEqual(response.status_code, GeminiWebError(code).status_code, response.text)
            self.assertEqual(error["code"], code.value)
            self.assertNotEqual(response.status_code, 401)

    def test_public_edit_uses_same_stable_error_envelope(self) -> None:
        error = GeminiWebError(GeminiWebErrorCode.UPSTREAM_TIMEOUT)
        with mock.patch.object(
            ai_api,
            "filter_or_log",
            mock.AsyncMock(),
        ), mock.patch.object(
            openai_v1_image_edit,
            "create_gemini_web_generation_backend",
            return_value=RaisingBackend(error),
        ):
            response = self.client.post(
                "/v1/images/edits",
                headers={"Authorization": "Bearer chatgpt2api"},
                json={
                    "model": "gemini-web-image",
                    "prompt": "mapped edit",
                    "image": "data:image/png;base64,aW1hZ2U=",
                },
            )
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["error"]["code"], "upstream_timeout")


if __name__ == "__main__":
    unittest.main()
