from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import accounts as accounts_api
from services.account_service import AccountService, account_identifier
from services.gemini_web_backend import GeminiWebBackend, TransportSession
from test.gemini_web_fixtures import (
    FixtureAccountPool,
    FixtureGeminiWebTransport,
    FixtureResultDownloader,
)


FIXTURE_DIR = Path(__file__).with_name("fixtures") / "gemini_web"
FAKE_COOKIE = "FAKE_GEMINI_COOKIE_MUST_NOT_ESCAPE"


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
        pass

    def health_check(self) -> dict[str, Any]:
        return {"ok": True}

    def get_backend_info(self) -> dict[str, Any]:
        return {"type": "memory"}


def fixture_backend() -> GeminiWebBackend:
    fixture_account = json.loads(
        (FIXTURE_DIR / "account.json").read_text(encoding="utf-8")
    )
    return GeminiWebBackend(
        account_pool=FixtureAccountPool(fixture_account),
        transport=FixtureGeminiWebTransport(FIXTURE_DIR),
        downloader=FixtureResultDownloader(FIXTURE_DIR),
        timeout_seconds=30,
    )


class NoEmailFixtureTransport(FixtureGeminiWebTransport):
    def initialize(self, *args: Any, **kwargs: Any) -> TransportSession:
        return replace(super().initialize(*args, **kwargs), email=None)


def no_email_fixture_backend() -> GeminiWebBackend:
    fixture_account = json.loads(
        (FIXTURE_DIR / "account.json").read_text(encoding="utf-8")
    )
    return GeminiWebBackend(
        account_pool=FixtureAccountPool(fixture_account),
        transport=NoEmailFixtureTransport(FIXTURE_DIR),
        downloader=FixtureResultDownloader(FIXTURE_DIR),
        timeout_seconds=30,
    )


class GeminiWebCookieImportTests(unittest.TestCase):
    def test_prepare_account_accepts_dictionary_and_wrapped_cookie_formats(self) -> None:
        backend = fixture_backend()
        formats = (
            {"__Secure-1PSID": FAKE_COOKIE, "NID": "FAKE_NID"},
            {"cookies": {"__Secure-1PSID": FAKE_COOKIE, "NID": "FAKE_NID"}},
            json.dumps(
                {"cookies": {"__Secure-1PSID": FAKE_COOKIE, "NID": "FAKE_NID"}}
            ),
        )

        for payload in formats:
            with self.subTest(payload=payload):
                account = backend.prepare_account(payload)

                self.assertEqual(account["provider"], "gemini_web")
                self.assertEqual(account["type"], "Gemini Web")
                self.assertEqual(account["source_type"], "cookie_json")
                self.assertEqual(account["credentials"]["cookies"]["__Secure-1PSID"], FAKE_COOKIE)
                self.assertNotIn("access_token", account)

    def test_prepare_account_filters_browser_export_to_google_domains(self) -> None:
        backend = fixture_backend()

        account = backend.prepare_account(
            {
                "cookies": [
                    {"name": "__Secure-1PSID", "value": FAKE_COOKIE, "domain": ".google.com"},
                    {"name": "NID", "value": "FAKE_NID", "domain": "gemini.google.com"},
                    {"name": "SID", "value": "DO_NOT_IMPORT", "domain": ".example.com"},
                ]
            }
        )

        self.assertEqual(
            account["credentials"]["cookies"],
            {"__Secure-1PSID": FAKE_COOKIE, "NID": "FAKE_NID"},
        )

    def test_verified_email_produces_stable_id_independent_of_rotating_cookie(self) -> None:
        backend = fixture_backend()

        first = backend.prepare_account({"__Secure-1PSID": "FAKE_COOKIE_ONE"})
        second = backend.prepare_account({"__Secure-1PSID": "FAKE_COOKIE_TWO"})

        self.assertEqual(first["account_id"], second["account_id"])
        self.assertTrue(first["account_id"].startswith("gemini_web:"))
        self.assertEqual(first["email"], "fixture.user@example.invalid")
        self.assertNotEqual(first["account_id"], "gemini_web:fixture")

    def test_missing_email_uses_persistable_random_id_and_session_label(self) -> None:
        backend = no_email_fixture_backend()

        account = backend.prepare_account({"__Secure-1PSID": FAKE_COOKIE})

        self.assertIsNone(account["email"])
        self.assertTrue(account["account_id"].startswith("gemini_web:"))
        self.assertNotIn("pending", account["account_id"])
        self.assertTrue(account["session_label"].startswith("gemini-session:"))

    def test_rejected_non_google_browser_cookies_do_not_echo_values(self) -> None:
        backend = fixture_backend()

        with self.assertRaises(ValueError) as raised:
            backend.prepare_account(
                [{"name": "SID", "value": FAKE_COOKIE, "domain": ".example.com"}]
            )

        self.assertNotIn(FAKE_COOKIE, str(raised.exception))


class GeminiWebAccountServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.service = AccountService(
            self.storage,
            gemini_web_backend=fixture_backend(),
        )

    def import_account(self) -> dict[str, Any]:
        result = self.service.import_gemini_web_account(
            {"cookies": {"__Secure-1PSID": FAKE_COOKIE}}
        )
        self.assertEqual(result["added"], 1)
        return result["item"]

    def test_import_encrypts_credentials_and_public_results_are_sanitized(self) -> None:
        with mock.patch("services.account_service.log_service.add") as add_log:
            public = self.import_account()

        self.assertNotIn("credentials", public)
        self.assertNotIn("access_token", public)
        self.assertEqual(public["provider"], "gemini_web")
        self.assertTrue(public["session_label"].startswith("gemini-session:"))
        stored_credentials = self.storage.accounts[0]["credentials"]
        self.assertIn("protected_cookies", stored_credentials)
        self.assertNotIn("cookies", stored_credentials)
        self.assertNotIn(FAKE_COOKIE, json.dumps(self.storage.accounts))
        self.assertNotIn("quota", self.storage.accounts[0])
        self.assertNotIn("restore_at", self.storage.accounts[0])
        self.assertNotIn(FAKE_COOKIE, json.dumps(public))
        self.assertNotIn(FAKE_COOKIE, repr(add_log.call_args_list))

    def test_search_edit_validate_disable_and_delete_use_common_identifier(self) -> None:
        gemini = self.import_account()
        chatgpt = self.service.add_accounts(["legacy-access-token"])["items"][1]

        self.assertEqual(account_identifier(gemini), gemini["account_id"])
        self.assertEqual(account_identifier(chatgpt), "legacy-access-token")
        self.assertEqual(
            [item["account_id"] for item in self.service.search_accounts("fixture.user")],
            [gemini["account_id"]],
        )

        edited = self.service.update_account(gemini["account_id"], {"proxy": "http://proxy.invalid"})
        self.assertEqual(edited["proxy"], "http://proxy.invalid")

        validation = self.service.validate_account(gemini["account_id"])
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["item"]["status"], "正常")

        disabled = self.service.update_account(gemini["account_id"], {"status": "禁用"})
        self.assertEqual(disabled["status"], "禁用")

        removed = self.service.delete_accounts([gemini["account_id"], "legacy-access-token"])
        self.assertEqual(removed["removed"], 2)
        self.assertEqual(removed["items"], [])

    def test_token_exports_exclude_gemini_credentials(self) -> None:
        gemini = self.import_account()
        self.service.add_account_items(
            [
                {
                    "access_token": "legacy-access-token",
                    "refresh_token": "legacy-refresh-token",
                    "id_token": "legacy-id-token",
                }
            ]
        )

        self.assertEqual(self.service.build_export_items([gemini["account_id"]]), [])
        exported = self.service.build_export_items()
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["access_token"], "legacy-access-token")
        self.assertNotIn(FAKE_COOKIE, json.dumps(exported))

    def test_no_email_random_identifier_survives_storage_reload(self) -> None:
        service = AccountService(
            self.storage,
            gemini_web_backend=no_email_fixture_backend(),
        )
        imported = service.import_gemini_web_account(
            {"__Secure-1PSID": FAKE_COOKIE}
        )["item"]

        reloaded = AccountService(
            self.storage,
            gemini_web_backend=no_email_fixture_backend(),
        )

        self.assertEqual(
            reloaded.list_accounts()[0]["account_id"],
            imported["account_id"],
        )
        self.assertEqual(
            reloaded.list_accounts()[0]["session_label"],
            imported["session_label"],
        )

    def test_generic_account_import_cannot_bypass_gemini_validation(self) -> None:
        records = (
            {
                "account_id": "gemini_web:unvalidated",
                "provider": "gemini_web",
                "type": "Gemini Web",
                "source_type": "cookie_json",
                "credentials": {"cookies": {"__Secure-1PSID": FAKE_COOKIE}},
            },
            {
                "access_token": "disguised-token",
                "credentials": {"cookies": {"__Secure-1PSID": FAKE_COOKIE}},
            },
        )

        for raw_record in records:
            with self.subTest(provider=raw_record.get("provider")):
                result = self.service.add_account_items([raw_record])
                self.assertEqual(result["added"], 0)
                self.assertEqual(result["items"], [])
                self.assertEqual(self.storage.accounts, [])
                self.assertNotIn(FAKE_COOKIE, json.dumps(result))


class GeminiWebAccountApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.service = AccountService(
            self.storage,
            gemini_web_backend=fixture_backend(),
        )
        self.service_patch = mock.patch.object(accounts_api, "account_service", self.service)
        self.auth_patch = mock.patch.object(
            accounts_api,
            "require_admin",
            lambda _authorization: {"role": "admin"},
        )
        self.service_patch.start()
        self.auth_patch.start()
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.auth_patch.stop()
        self.service_patch.stop()

    def test_api_import_search_update_validate_and_delete_never_return_cookie(self) -> None:
        imported = self.client.post(
            "/api/accounts/gemini-web",
            json={"cookie_json": {"cookies": {"__Secure-1PSID": FAKE_COOKIE}}},
        )
        self.assertEqual(imported.status_code, 200)
        body = imported.json()
        account_id = body["item"]["account_id"]
        self.assertNotIn(FAKE_COOKIE, imported.text)
        self.assertNotIn("credentials", imported.text)

        searched = self.client.get("/api/accounts", params={"q": "fixture.user"})
        self.assertEqual([item["account_id"] for item in searched.json()["items"]], [account_id])
        self.assertNotIn(FAKE_COOKIE, searched.text)

        updated = self.client.post(
            "/api/accounts/update",
            json={"account_id": account_id, "status": "禁用"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["item"]["status"], "禁用")
        self.assertNotIn(FAKE_COOKIE, updated.text)

        validated = self.client.post(
            "/api/accounts/validate",
            json={"identifiers": [account_id]},
        )
        self.assertEqual(validated.status_code, 200)
        self.assertTrue(validated.json()["valid"])
        self.assertNotIn(FAKE_COOKIE, validated.text)

        deleted = self.client.request(
            "DELETE",
            "/api/accounts",
            json={"identifiers": [account_id]},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["removed"], 1)
        self.assertNotIn(FAKE_COOKIE, deleted.text)

    def test_legacy_account_api_keeps_access_token_fallback(self) -> None:
        self.service.add_account_items(
            [{"access_token": "legacy-token", "account_id": "chatgpt-account-id"}]
        )

        updated_by_id = self.client.post(
            "/api/accounts/update",
            json={"account_id": "chatgpt-account-id", "status": "禁用"},
        )
        self.assertEqual(updated_by_id.status_code, 200)
        self.assertEqual(updated_by_id.json()["item"]["status"], "禁用")

        deleted_by_token = self.client.request(
            "DELETE",
            "/api/accounts",
            json={"tokens": ["legacy-token"]},
        )
        self.assertEqual(deleted_by_token.status_code, 200)
        self.assertEqual(deleted_by_token.json()["removed"], 1)

    def test_generic_accounts_api_rejects_unvalidated_gemini_record_safely(self) -> None:
        response = self.client.post(
            "/api/accounts",
            json={
                "tokens": [],
                "accounts": [
                    {
                        "account_id": "gemini_web:unvalidated",
                        "access_token": "must-not-be-used-as-token",
                        "credentials": {"cookies": {"__Secure-1PSID": FAKE_COOKIE}},
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("validated import endpoint", response.text)
        self.assertNotIn(FAKE_COOKIE, response.text)
        self.assertEqual(self.storage.accounts, [])


if __name__ == "__main__":
    unittest.main()
