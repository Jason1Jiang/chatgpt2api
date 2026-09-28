from __future__ import annotations

import threading
import time
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from curl_cffi import CurlOpt, requests

from services.config import config
from services.account_service import AccountService
from services.openai_backend_api import ImageContentPolicyError, ImageStreamTimeoutError, OpenAIBackendAPI
from services.protocol import conversation
from services.storage.json_storage import JSONStorageBackend


def backend_for_stream() -> OpenAIBackendAPI:
    backend = object.__new__(OpenAIBackendAPI)
    backend.access_token = "test-token"
    backend.progress_callback = None
    backend._bootstrap = mock.Mock()
    backend._get_chat_requirements = mock.Mock()
    backend._prepare_image_conversation = mock.Mock(return_value="test-conduit")
    backend._image_headers = mock.Mock(return_value={})
    return backend


class ImageStreamDeadlineTests(unittest.TestCase):
    def test_real_transport_deadline_covers_headers_silence_and_heartbeats(self) -> None:
        for mode in ("headers", "silent", "heartbeat_then_silent", "heartbeats"):
            with self.subTest(mode=mode):
                stop = threading.Event()

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_args):
                        pass

                    def do_POST(self):
                        self.rfile.read(int(self.headers.get("Content-Length", "0")))
                        if mode == "headers":
                            stop.wait(4)
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        self.wfile.flush()
                        try:
                            if mode == "heartbeat_then_silent":
                                self.wfile.write(b": heartbeat\n\n")
                                self.wfile.flush()
                            if mode == "heartbeats":
                                for _ in range(80):
                                    if stop.wait(0.05):
                                        break
                                    self.wfile.write(b": heartbeat\n\n")
                                    self.wfile.flush()
                            else:
                                stop.wait(4)
                        except (BrokenPipeError, ConnectionResetError):
                            pass

                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                backend = backend_for_stream()
                backend.base_url = f"http://127.0.0.1:{server.server_port}"
                backend.session = requests.Session(trust_env=False)
                previous_options = backend.session.curl_options
                try:
                    started = time.monotonic()
                    with mock.patch.dict(config.data, {"image_poll_timeout_secs": 1}):
                        with self.assertRaises(ImageStreamTimeoutError) as raised:
                            list(backend._stream_picture_conversation("draw", "gpt-image-2", []))
                    self.assertEqual(raised.exception.timeout_secs, 1)
                    self.assertLess(time.monotonic() - started, 2.5)
                    self.assertIs(backend.session.curl_options, previous_options)
                    self.assertNotIn(CurlOpt.TIMEOUT_MS, previous_options)
                finally:
                    stop.set()
                    backend.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_watchdog_clean_eof_is_still_a_timeout(self) -> None:
        backend = backend_for_stream()
        response = mock.Mock()
        response.iter_lines.return_value = iter([])
        backend._start_image_generation = mock.Mock(return_value=response)
        watchdog = mock.Mock()

        def start_timer(_seconds, callback):
            watchdog.start.side_effect = callback
            return watchdog

        with mock.patch("services.openai_backend_api.threading.Timer", side_effect=start_timer):
            with self.assertRaises(ImageStreamTimeoutError):
                list(backend._stream_picture_conversation("draw", "gpt-image-2", []))
        watchdog.cancel.assert_called_once()
        self.assertTrue(response.close.called)

    def test_normal_eof_and_transport_failure_both_cancel_watchdog(self) -> None:
        for error in (None, ValueError("broken stream")):
            with self.subTest(error=error):
                backend = backend_for_stream()
                response = mock.Mock()
                response.iter_lines.return_value = iter([b'data: {"ok":true}', b""])
                response.iter_lines.side_effect = error
                backend._start_image_generation = mock.Mock(return_value=response)
                with mock.patch("services.openai_backend_api.threading.Timer") as timer:
                    if error:
                        with self.assertRaisesRegex(ValueError, "broken stream"):
                            list(backend._stream_picture_conversation("draw", "gpt-image-2", []))
                    else:
                        self.assertEqual(
                            list(backend._stream_picture_conversation("draw", "gpt-image-2", [])),
                            ['{"ok":true}'],
                        )
                timer.return_value.cancel.assert_called_once()
                response.close.assert_called_once()

    def test_watchdog_only_receives_budget_remaining_after_request_start(self) -> None:
        backend = backend_for_stream()
        response = mock.Mock()
        response.iter_lines.return_value = iter([])
        backend._start_image_generation = mock.Mock(return_value=response)
        with (
            mock.patch.dict(config.data, {"image_poll_timeout_secs": 10}),
            mock.patch("services.openai_backend_api.time.monotonic", side_effect=[0, 7, 7, 8]),
            mock.patch("services.openai_backend_api.threading.Timer") as timer,
        ):
            list(backend._stream_picture_conversation("draw", "gpt-image-2", []))
        self.assertEqual(timer.call_args.args[0], 3)

    def test_success_rejection_and_timeout_preserve_cleanup_and_release_slot(self) -> None:
        for outcome in ("success", "rejection", "timeout"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                accounts = AccountService(JSONStorageBackend(Path(directory) / "accounts.json"))
                accounts.add_account_items([{"access_token": "test-token", "quota": 2}])
                accounts._image_inflight["test-token"] = 1
                backend = mock.Mock()

                def stream(*_args):
                    if outcome == "rejection":
                        raise ImageContentPolicyError("rejected", conversation_id="conv-1")
                    yield conversation.ImageOutput(
                        kind="result" if outcome == "success" else "progress",
                        model="gpt-image-2",
                        index=1,
                        total=1,
                        conversation_id="conv-1",
                    )
                    if outcome == "timeout":
                        raise ImageStreamTimeoutError(1)

                with (
                    mock.patch.object(conversation, "account_service", accounts),
                    mock.patch.object(accounts, "get_available_access_token", return_value="test-token") as select,
                    mock.patch.object(conversation, "OpenAIBackendAPI", return_value=backend),
                    mock.patch.object(conversation, "stream_image_outputs", side_effect=stream),
                    mock.patch.object(conversation, "_remove_image_conversation_later") as cleanup,
                ):
                    request = conversation.ConversationRequest(model="gpt-image-2", prompt="draw")
                    if outcome == "success":
                        conversation._generate_single_image(request, 1, 1)
                    else:
                        with self.assertRaises(conversation.ImageGenerationError) as raised:
                            conversation._generate_single_image(request, 1, 1)
                        self.assertEqual(raised.exception.status_code, 504 if outcome == "timeout" else 400)
                    cleanup.assert_called_once_with(backend, "conv-1", success=outcome == "success")
                    select.assert_called_once()
                self.assertEqual(accounts.list_accounts()[0]["image_inflight"], 0)
                backend.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
