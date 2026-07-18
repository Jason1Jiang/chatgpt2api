from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import ai as ai_api
from api import image_inputs as image_inputs_api
from api.errors import install_exception_handlers
from services.account_service import AccountService
from services.gemini_web_backend import (
    DownloadedImage,
    FirstNormalGeminiWebAccountPool,
    GeminiWebBackend,
)
from services.image_storage_service import ImageStorageService
from services.protocol import openai_v1_image_edit
from services.protocol.conversation import ImageOutput
from test.gemini_web_fixtures import FixtureGeminiWebTransport
from test.test_gemini_web_account_management import MemoryStorage


AUTH_HEADERS = {"Authorization": "Bearer chatgpt2api"}
FIXTURE_DIR = Path(__file__).with_name("fixtures") / "gemini_web"
FAKE_COOKIE = "FAKE_EDIT_COOKIE_MUST_NOT_ESCAPE"
VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _data_url(content: bytes, mime_type: str = "image/png") -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _gemini_account() -> dict[str, object]:
    return {
        "account_id": "gemini_web:normal",
        "provider": "gemini_web",
        "type": "Gemini Web",
        "source_type": "cookie_json",
        "status": "正常",
        "credentials": {"cookies": {"__Secure-1PSID": FAKE_COOKIE}},
    }


class RecordingPngDownloader:
    def __init__(self) -> None:
        self.download_refs: list[str] = []

    def download(
        self,
        account: object,
        download_ref: str,
        timeout_seconds: float,
    ) -> DownloadedImage:
        self.download_refs.append(download_ref)
        return DownloadedImage(content=VALID_PNG, mime_type="image/png")


class GeminiWebImageEditsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account_service = AccountService(MemoryStorage([_gemini_account()]))
        self.pool = FirstNormalGeminiWebAccountPool(
            list_accounts=self.account_service.list_accounts,
            get_account=self.account_service.get_account,
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
            mock.patch.object(ai_api, "filter_or_log", mock.AsyncMock()),
            mock.patch("services.image_storage_service.config", self.image_config),
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

    def backend(
        self,
        expected_upload_count: int,
    ) -> tuple[GeminiWebBackend, FixtureGeminiWebTransport, RecordingPngDownloader]:
        transport = FixtureGeminiWebTransport(
            FIXTURE_DIR,
            expected_upload_count=expected_upload_count,
        )
        downloader = RecordingPngDownloader()
        backend = GeminiWebBackend(
            account_pool=self.pool,
            transport=transport,
            downloader=downloader,
            timeout_seconds=30,
        )
        return backend, transport, downloader

    def test_public_multipart_single_edit_uploads_raw_image_and_returns_b64(self) -> None:
        backend, transport, downloader = self.backend(expected_upload_count=1)
        with mock.patch.object(
            openai_v1_image_edit,
            "create_gemini_web_generation_backend",
            return_value=backend,
        ):
            response = self.client.post(
                "/v1/images/edits",
                headers=AUTH_HEADERS,
                data={
                    "model": "gemini-web-image",
                    "prompt": "fixture edit",
                    "n": "1",
                    "response_format": "b64_json",
                },
                files=[
                    ("image", ("input.png", b"single-reference", "image/png")),
                    ("mask", ("mask.png", b"compatible-but-ignored-mask", "image/png")),
                ],
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            base64.b64decode(response.json()["data"][0]["b64_json"]),
            VALID_PNG,
        )
        self.assertEqual(transport.upload_calls, [(b"single-reference", "image/png")])
        self.assertEqual(len(transport.generated_references), 1)
        self.assertEqual(downloader.download_refs, ["fixture://generated/original"])
        self.assertNotIn("fixture://", response.text)
        self.assertNotIn(FAKE_COOKIE, response.text)

    def test_public_json_images_upload_data_url_and_remote_url_in_order(self) -> None:
        backend, transport, downloader = self.backend(expected_upload_count=2)
        remote_image = (b"remote-reference", "remote.jpg", "image/jpeg")
        download_image_url = image_inputs_api._download_image_url
        with (
            mock.patch.object(
                openai_v1_image_edit,
                "create_gemini_web_generation_backend",
                return_value=backend,
            ),
            mock.patch(
                "api.image_inputs._download_image_url",
                side_effect=lambda url: (
                    download_image_url(url) if url.startswith("data:") else remote_image
                ),
            ),
        ):
            response = self.client.post(
                "/v1/images/edits",
                headers=AUTH_HEADERS,
                json={
                    "model": "gemini-web-image",
                    "prompt": "two references",
                    "images": [
                        _data_url(b"inline-reference"),
                        {"image_url": "https://example.invalid/reference.jpg"},
                    ],
                    "response_format": "b64_json",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            transport.upload_calls,
            [
                (b"inline-reference", "image/png"),
                (b"remote-reference", "image/jpeg"),
            ],
        )
        self.assertEqual(len(transport.generated_references), 2)
        self.assertEqual(downloader.download_refs, ["fixture://generated/original"])

    def test_public_top_level_json_image_url_returns_project_hosted_url(self) -> None:
        backend, transport, downloader = self.backend(expected_upload_count=1)
        remote_image = (b"remote-reference", "remote.png", "image/png")
        with (
            mock.patch.object(
                openai_v1_image_edit,
                "create_gemini_web_generation_backend",
                return_value=backend,
            ),
            mock.patch("api.image_inputs._download_image_url", return_value=remote_image),
        ):
            response = self.client.post(
                "/v1/images/edits",
                headers=AUTH_HEADERS,
                json={
                    "model": "gemini-web-image",
                    "prompt": "url reference",
                    "image_url": "https://example.invalid/reference.png",
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
        self.assertEqual(transport.upload_calls, [(b"remote-reference", "image/png")])
        self.assertEqual(downloader.download_refs, ["fixture://generated/original"])

    def test_public_multipart_repeated_image_url_fields_upload_every_reference(self) -> None:
        backend, transport, _ = self.backend(expected_upload_count=2)
        remote_image = (b"remote-reference", "remote.jpg", "image/jpeg")
        download_image_url = image_inputs_api._download_image_url
        with (
            mock.patch.object(
                openai_v1_image_edit,
                "create_gemini_web_generation_backend",
                return_value=backend,
            ),
            mock.patch(
                "api.image_inputs._download_image_url",
                side_effect=lambda url: (
                    download_image_url(url) if url.startswith("data:") else remote_image
                ),
            ),
        ):
            response = self.client.post(
                "/v1/images/edits",
                headers=AUTH_HEADERS,
                data={"model": "gemini-web-image", "prompt": "form references"},
                files=[
                    ("image_url", (None, _data_url(b"form-inline"))),
                    ("image_url", (None, "https://example.invalid/form.jpg")),
                ],
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            transport.upload_calls,
            [(b"form-inline", "image/png"), (b"remote-reference", "image/jpeg")],
        )
        self.assertEqual(len(transport.generated_references), 2)

    def test_missing_image_keeps_image_is_required_error(self) -> None:
        with mock.patch.object(
            openai_v1_image_edit,
            "create_gemini_web_generation_backend",
        ) as backend_factory:
            response = self.client.post(
                "/v1/images/edits",
                headers=AUTH_HEADERS,
                json={"model": "gemini-web-image", "prompt": "missing image"},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["message"], "image is required")
        backend_factory.assert_not_called()

    def test_unsupported_parameters_are_rejected_before_account_acquire(self) -> None:
        cases = (
            ({"n": 2}, "n"),
            ({"stream": True}, "stream"),
            ({"response_format": "temporary_url"}, "response_format"),
        )
        for extra, expected_param in cases:
            with self.subTest(param=expected_param), mock.patch.object(
                openai_v1_image_edit,
                "create_gemini_web_generation_backend",
            ) as backend_factory, mock.patch.object(
                self.pool,
                "acquire",
                wraps=self.pool.acquire,
            ) as acquire:
                response = self.client.post(
                    "/v1/images/edits",
                    headers=AUTH_HEADERS,
                    json={
                        "model": "gemini-web-image",
                        "prompt": "unsupported",
                        "image": _data_url(b"reference"),
                        **extra,
                    },
                )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["code"], "unsupported_parameter")
            self.assertEqual(response.json()["error"]["param"], expected_param)
            backend_factory.assert_not_called()
            acquire.assert_not_called()

    def test_protocol_failure_keeps_openai_error_envelope(self) -> None:
        failing_backend = GeminiWebBackend(
            account_pool=self.pool,
            transport=FixtureGeminiWebTransport(
                FIXTURE_DIR,
                failure="protocol",
                expected_upload_count=1,
            ),
            downloader=RecordingPngDownloader(),
            timeout_seconds=30,
        )
        with mock.patch.object(
            openai_v1_image_edit,
            "create_gemini_web_generation_backend",
            return_value=failing_backend,
        ):
            response = self.client.post(
                "/v1/images/edits",
                headers=AUTH_HEADERS,
                json={
                    "model": "gemini-web-image",
                    "prompt": "safe failure",
                    "image": _data_url(b"reference"),
                },
            )

        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["code"], "upstream_protocol_error")
        self.assertNotIn(FAKE_COOKIE, response.text)

    def test_existing_edit_models_keep_existing_dispatch(self) -> None:
        existing_data = [{"b64_json": base64.b64encode(b"existing").decode("ascii")}]
        for model in ("gpt-image-2", "codex-gpt-image-2", "auto"):
            with self.subTest(model=model), mock.patch.object(
                openai_v1_image_edit,
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
                openai_v1_image_edit,
                "create_gemini_web_generation_backend",
            ) as gemini_factory:
                response = self.client.post(
                    "/v1/images/edits",
                    headers=AUTH_HEADERS,
                    json={
                        "model": model,
                        "prompt": "existing path",
                        "image": _data_url(b"existing-input"),
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            existing_dispatch.assert_called_once()
            gemini_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
