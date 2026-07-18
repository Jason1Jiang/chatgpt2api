from __future__ import annotations

import json
import unittest
from pathlib import Path

from services.gemini_web_backend import (
    GeminiWebBackend,
    GeminiWebAccount,
    GeminiWebError,
    GeminiWebErrorCode,
    GeminiWebTransport,
    HttpGeminiWebTransport,
    HttpResultDownloader,
    ImageReference,
    ImageRequest,
    ResultDownloader,
    TransportSession,
    parse_generation_stream,
)
from test.gemini_web_fixtures import (
    FixtureAccountPool,
    FixtureGeminiWebTransport,
    FixtureResultDownloader,
)


FIXTURE_DIR = Path(__file__).with_name("fixtures") / "gemini_web"


class ManualClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return 10.0


class _FakeUploadResponse:
    status_code = 200
    text = "/contrib_service/ttl_1d/fixture-upload-reference"


class _FakeUploadClient:
    def __init__(self) -> None:
        self.call: dict[str, object] | None = None

    def post(self, url: str, **kwargs: object) -> _FakeUploadResponse:
        headers = kwargs.get("headers")
        self.call = {
            "url": url,
            "headers": dict(headers) if isinstance(headers, dict) else {},
            "has_multipart": kwargs.get("multipart") is not None,
            "allow_redirects": kwargs.get("allow_redirects"),
            "timeout": kwargs.get("timeout"),
        }
        return _FakeUploadResponse()


class _FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


def _google_frame(parts: list[object]) -> str:
    frame = "\n" + json.dumps(parts, separators=(",", ":"))
    frame_length = len(frame.encode("utf-16-le")) // 2
    return ")]}'\n" + str(frame_length) + frame


def _current_generation_frame() -> str:
    entry = [
        [
            None,
            None,
            None,
            [None, None, None, "https://example.invalid/generated-preview"],
        ],
        ["generated-image-id"],
    ]
    candidate: list[object] = [None] * 13
    candidate[0] = "choice-id"
    candidate[1] = ["current revised prompt"]
    candidate[12] = [None] * 8
    candidate[12][7] = [[entry]]
    inner = json.dumps(
        [None, ["conversation-id", "response-id"], None, None, [candidate]],
        separators=(",", ":"),
    )
    return _google_frame([[None, None, inner]])


def _current_user_status_frame() -> str:
    body: list[object] = [None] * 18
    body[14] = 1000
    body[15] = [
        ["fixture-model-one", "Model One", "Fixture model"],
        ["fixture-model-two", "Model Two", "Fixture model"],
    ]
    body[16] = []
    body[17] = [115]
    return _google_frame([[None, None, json.dumps(body, separators=(",", ":"))]])


class _FakeInitializationClient:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.app_calls = 0

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append(("GET", url))
        if url == HttpGeminiWebTransport.GOOGLE_URL:
            return _FakeResponse()
        self.app_calls += 1
        token = '"SNlM0e":"fixture-xsrf",' if self.app_calls > 1 else ""
        return _FakeResponse(
            "{" + token
            + '"cfb2h":"fixture-build","FdrFJe":"fixture-session",'
            + '"TuX5cc":"en","qKIAYe":"fixture-push",'
            + '"email":"fixture.user@example.invalid"}'
        )

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append(("POST", url))
        if url == HttpGeminiWebTransport.BATCH_URL:
            return _FakeResponse(_current_user_status_frame())
        if url == HttpGeminiWebTransport.ROTATE_COOKIES_URL:
            self.cookies["__Secure-1PSIDTS"] = "fixture-rotated-cookie"
        return _FakeResponse()


class _FakeGenerationClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if url == HttpGeminiWebTransport.GENERATE_URL:
            return _FakeResponse(_current_generation_frame())
        params = kwargs.get("params")
        if isinstance(params, dict) and params.get("rpcids") == "ESY5D":
            return _FakeResponse()
        original = json.dumps(
            ["https://example.invalid/generated-original-reference"],
            separators=(",", ":"),
        )
        return _FakeResponse(_google_frame([[None, None, original]]))

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if url.endswith("=d-I?alr=yes"):
            return _FakeResponse("https://example.invalid/redirect-reference")
        return _FakeResponse("https://example.invalid/generated-original")


class _FakeImageResponse:
    status_code = 200
    headers = {"content-type": "image/png"}
    content = b"fixture-downloaded-image"


class _FakeDownloadClient:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.call: dict[str, object] | None = None

    def get(self, url: str, **kwargs: object) -> _FakeImageResponse:
        self.call = {"url": url, **kwargs}
        return _FakeImageResponse()


def load_account() -> dict[str, object]:
    return json.loads((FIXTURE_DIR / "account.json").read_text(encoding="utf-8"))


class GeminiWebBackendContractTests(unittest.TestCase):
    def test_http_downloader_uses_gemini_referer_and_validates_image_content(self) -> None:
        account = GeminiWebAccount(
            account_id="gemini_web:fixture-download",
            cookies=(("fixture", "fixture-cookie"),),
        )
        client = _FakeDownloadClient()
        downloader = HttpResultDownloader(
            session_factory=lambda **kwargs: client
        )

        downloaded = downloader.download(
            account,
            "https://example.invalid/final-image",
            15.0,
        )

        self.assertEqual(downloaded.content, b"fixture-downloaded-image")
        self.assertEqual(downloaded.mime_type, "image/png")
        self.assertEqual(client.cookies, {"fixture": "fixture-cookie"})
        self.assertEqual(client.call["headers"]["Origin"], "https://gemini.google.com")
        self.assertEqual(client.call["headers"]["Referer"], "https://gemini.google.com/")
        self.assertEqual(client.call["timeout"], 15.0)

    def test_http_initialize_rotates_stale_session_only_in_memory(self) -> None:
        account = GeminiWebAccount(
            account_id="gemini_web:fixture-init",
            cookies=(("fixture", "fixture"),),
        )
        client = _FakeInitializationClient()
        transport = HttpGeminiWebTransport(
            session_factory=lambda **kwargs: client
        )

        session = transport.initialize(account, 30.0)

        self.assertIs(session.http_client, client)
        self.assertEqual(session.email, "fixture.user@example.invalid")
        self.assertEqual(session.language, "en")
        self.assertEqual(
            client.calls,
            [
                ("GET", HttpGeminiWebTransport.GOOGLE_URL),
                ("GET", HttpGeminiWebTransport.APP_URL),
                ("POST", HttpGeminiWebTransport.ROTATE_COOKIES_URL),
                ("GET", HttpGeminiWebTransport.APP_URL),
                ("POST", HttpGeminiWebTransport.BATCH_URL),
            ],
        )
        self.assertEqual(transport.initialization_diagnostics["rotation_attempted"], 1)
        self.assertEqual(transport.initialization_diagnostics["rotation_yielded_token"], 1)
        self.assertEqual(transport.initialization_diagnostics["available_model_count"], 2)
        self.assertEqual(transport.initialization_diagnostics["model_header_selected"], 1)
        self.assertEqual(
            dict(session.model_headers)["x-goog-ext-525001261-jspb"],
            '[1,null,null,null,"fixture-model-one",null,null,0,[4],null,null,4]',
        )
        self.assertEqual(
            dict(session.refreshed_cookies)["__Secure-1PSIDTS"],
            "fixture-rotated-cookie",
        )
        self.assertNotIn("fixture-xsrf", repr(session))
        self.assertNotIn("fixture-rotated-cookie", repr(session))

    def test_http_generate_resolves_original_image_with_current_batch_rpc(self) -> None:
        account = GeminiWebAccount(
            account_id="gemini_web:fixture-generation",
            cookies=(("fixture", "fixture"),),
        )
        client = _FakeGenerationClient()
        session = TransportSession(
            account=account,
            email=None,
            xsrf_token="fixture-xsrf",
            build_label="fixture-build",
            session_id="fixture-session",
            language="en",
            model_headers=(
                (
                    "x-goog-ext-525001261-jspb",
                    '[1,null,null,null,"fixture-model",null,null,0,[4],null,null,1]',
                ),
            ),
            http_client=client,
        )

        generation = HttpGeminiWebTransport().generate(
            session,
            "fixture image prompt",
            (),
            30.0,
        )

        self.assertEqual(len(generation.images), 1)
        self.assertEqual(
            generation.images[0].download_ref,
            "https://example.invalid/generated-original",
        )
        self.assertEqual(
            [call["url"] for call in client.calls],
            [
                HttpGeminiWebTransport.BATCH_URL,
                HttpGeminiWebTransport.GENERATE_URL,
                HttpGeminiWebTransport.BATCH_URL,
                "https://example.invalid/generated-original-reference=d-I?alr=yes",
                "https://example.invalid/redirect-reference",
            ],
        )
        activity_call = client.calls[0]
        activity_outer = json.loads(activity_call["data"]["f.req"])
        self.assertEqual(activity_outer[0][0][0], "ESY5D")
        self.assertEqual(
            activity_outer[0][0][1],
            '[[["bard_activity_enabled"]]]',
        )
        generate_call = client.calls[1]
        self.assertIn("f.req", generate_call["data"])
        self.assertEqual(generate_call["params"]["rt"], "c")
        self.assertIn("x-goog-ext-525005358-jspb", generate_call["headers"])
        self.assertIn("fixture-model", generate_call["headers"]["x-goog-ext-525001261-jspb"])
        batch_call = client.calls[2]
        batch_outer = json.loads(batch_call["data"]["f.req"])
        rpc_record = batch_outer[0][0]
        self.assertEqual(rpc_record[0], "c8o8Fe")
        rpc_payload = json.loads(rpc_record[1])
        self.assertEqual(rpc_payload[0][1], ["generated-image-id", 0])
        self.assertEqual(
            rpc_payload[1],
            ["response-id", "choice-id", "conversation-id", None, ""],
        )

    def test_http_upload_uses_current_multipart_protocol_and_shared_session(self) -> None:
        account = GeminiWebAccount(
            account_id="gemini_web:fixture-upload",
            cookies=(("fixture", "fixture"),),
        )
        client = _FakeUploadClient()
        session = TransportSession(
            account=account,
            email=None,
            xsrf_token="fixture-xsrf",
            push_id="fixture-push-id",
            http_client=client,
        )

        uploaded = HttpGeminiWebTransport().upload(
            session,
            b"fixture-image",
            "image/png",
            12.0,
        )

        self.assertIsNotNone(client.call)
        self.assertEqual(client.call["url"], HttpGeminiWebTransport.UPLOAD_URL)
        self.assertTrue(client.call["has_multipart"])
        self.assertTrue(client.call["allow_redirects"])
        self.assertEqual(client.call["timeout"], 12.0)
        self.assertEqual(client.call["headers"]["X-Tenant-Id"], "bard-storage")
        self.assertEqual(client.call["headers"]["Push-ID"], "fixture-push-id")
        self.assertEqual(uploaded.mime_type, "image/png")
        self.assertTrue(uploaded.filename.endswith(".png"))
        self.assertNotIn(uploaded.token, repr(uploaded))

    def test_current_google_frame_extracts_only_generated_image_candidates(self) -> None:
        diagnostics: dict[str, int] = {}
        generation = parse_generation_stream(_current_generation_frame(), diagnostics)

        self.assertEqual(generation.revised_prompt, "current revised prompt")
        self.assertEqual(len(generation.images), 1)
        self.assertEqual(
            generation.images[0].download_ref,
            "https://example.invalid/generated-preview",
        )
        self.assertEqual(generation.images[0].image_id, "generated-image-id")
        self.assertEqual(generation.images[0].conversation_id, "conversation-id")
        self.assertEqual(generation.images[0].response_id, "response-id")
        self.assertEqual(generation.images[0].choice_id, "choice-id")
        self.assertEqual(diagnostics["decoded_parts"], 1)
        self.assertEqual(diagnostics["inner_payloads"], 1)
        self.assertEqual(diagnostics["candidate_records"], 1)
        self.assertEqual(diagnostics["generated_entries"], 1)
        self.assertEqual(diagnostics["preview_refs"], 1)

    def make_backend(
        self,
        *,
        failure: str | None = None,
        expected_upload_count: int = 0,
    ) -> tuple[GeminiWebBackend, FixtureAccountPool, ManualClock]:
        pool = FixtureAccountPool(load_account())
        clock = ManualClock()
        backend = GeminiWebBackend(
            account_pool=pool,
            transport=FixtureGeminiWebTransport(
                FIXTURE_DIR,
                failure=failure,
                expected_upload_count=expected_upload_count,
            ),
            downloader=FixtureResultDownloader(FIXTURE_DIR),
            clock=clock,
            timeout_seconds=30,
        )
        return backend, pool, clock

    def test_validate_account_returns_only_normalized_identity(self) -> None:
        backend, _, _ = self.make_backend()
        account = load_account()
        fake_cookie = str(account["credentials"]["cookies"]["__Secure-1PSID"])

        validation = backend.validate_account(account)

        self.assertTrue(validation.valid)
        self.assertEqual(validation.email, "fixture.user@example.invalid")
        self.assertTrue(validation.session_label.startswith("gemini-session:"))
        self.assertNotIn(fake_cookie, repr(validation))
        self.assertFalse(hasattr(validation, "cookies"))
        self.assertFalse(hasattr(validation, "rpc_id"))
        self.assertFalse(hasattr(validation, "xsrf_token"))

    def test_generate_returns_downloaded_image_without_transport_details(self) -> None:
        backend, pool, clock = self.make_backend()

        result = backend.generate(
            ImageRequest(
                prompt="fixture prompt",
                response_format="b64_json",
                size="1024x1024",
                quality="high",
            )
        )

        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.images[0].content, b"fixture-original-image")
        self.assertEqual(result.images[0].mime_type, "image/png")
        self.assertEqual(result.revised_prompt, "fixture revised prompt")
        self.assertFalse(hasattr(result.images[0], "url"))
        self.assertNotIn("fixture://", repr(result))
        self.assertEqual((pool.acquired, pool.released), (1, 1))
        self.assertGreater(clock.calls, 1)

    def test_edit_uploads_single_and_multiple_references_before_generation(self) -> None:
        references = (
            ImageReference(content=b"input-one", mime_type="image/png"),
            ImageReference(content=b"input-two", mime_type="image/jpeg"),
        )
        for reference_count in (1, 2):
            with self.subTest(reference_count=reference_count):
                backend, pool, _ = self.make_backend(
                    expected_upload_count=reference_count
                )
                result = backend.edit(
                    ImageRequest(
                        prompt="edit fixture",
                        references=references[:reference_count],
                    )
                )

                self.assertEqual(
                    [image.content for image in result.images],
                    [b"fixture-original-image"],
                )
                self.assertEqual((pool.acquired, pool.released), (1, 1))

    def test_missing_stable_account_id_is_rejected_without_echoing_cookie(self) -> None:
        backend, _, _ = self.make_backend()
        secret = "FAKE_FIXTURE_ROTATING_COOKIE"

        for account_id in (None, "", "   "):
            with self.subTest(account_id=account_id):
                account = {
                    "credentials": {"cookies": {"__Secure-1PSID": secret}},
                }
                if account_id is not None:
                    account["account_id"] = account_id

                with self.assertRaises(GeminiWebError) as raised:
                    backend.validate_account(account)

                self.assertEqual(
                    raised.exception.code,
                    GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT,
                )
                self.assertNotIn(secret, str(raised.exception))
                self.assertNotIn(secret, repr(raised.exception))

    def test_edit_requires_a_reference_image(self) -> None:
        backend, pool, _ = self.make_backend()

        with self.assertRaises(ValueError) as raised:
            backend.edit(ImageRequest(prompt="missing image"))

        self.assertEqual(str(raised.exception), "image is required")
        self.assertEqual((pool.acquired, pool.released), (0, 0))

    def test_fixture_failures_map_to_stable_safe_errors(self) -> None:
        account = load_account()
        fake_cookie = str(account["credentials"]["cookies"]["__Secure-1PSID"])
        cases = {
            "timeout": (GeminiWebErrorCode.UPSTREAM_TIMEOUT, 504),
            "rate_limit": (GeminiWebErrorCode.UPSTREAM_RATE_LIMITED, 429),
            "invalid_cookie": (GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT, 503),
            "content_policy": (GeminiWebErrorCode.CONTENT_POLICY_VIOLATION, 400),
            "protocol": (GeminiWebErrorCode.UPSTREAM_PROTOCOL_ERROR, 502),
        }

        for failure, (expected_code, expected_status) in cases.items():
            with self.subTest(failure=failure):
                backend, pool, _ = self.make_backend(failure=failure)
                with self.assertRaises(GeminiWebError) as raised:
                    backend.generate(ImageRequest(prompt="safe fixture prompt"))

                error = raised.exception
                self.assertEqual(error.code, expected_code)
                self.assertEqual(error.status_code, expected_status)
                self.assertNotIn(fake_cookie, str(error))
                self.assertNotIn(fake_cookie, repr(error))
                self.assertEqual((pool.acquired, pool.released), (1, 1))

    def test_invalid_account_shape_does_not_echo_credentials(self) -> None:
        backend, _, _ = self.make_backend()
        secret = "fixture-secret-that-must-not-escape"

        with self.assertRaises(GeminiWebError) as raised:
            backend.validate_account({"credentials": {"cookies": secret}})

        self.assertEqual(raised.exception.code, GeminiWebErrorCode.NO_AVAILABLE_ACCOUNT)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))

    def test_production_adapters_satisfy_internal_seams(self) -> None:
        self.assertIsInstance(HttpGeminiWebTransport(), GeminiWebTransport)
        self.assertIsInstance(HttpResultDownloader(), ResultDownloader)


if __name__ == "__main__":
    unittest.main()
