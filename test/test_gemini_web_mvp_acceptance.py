from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest import mock

from scripts.gemini_web_mvp_acceptance import main
from scripts.gemini_web_protocol_probe import REPO_ROOT, assert_scrubbed_fixture


class GeminiWebMvpAcceptanceTests(unittest.TestCase):
    def test_dry_run_is_fixture_only_and_does_not_require_openai_or_credentials(self) -> None:
        output = io.StringIO()

        with mock.patch("httpx.Client.request") as sync_request, mock.patch(
            "httpx.AsyncClient.request"
        ) as async_request:
            exit_code = main(["dry-run"], stdout=output)

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["fixture_only"])
        self.assertFalse(payload["credentials_read"])
        self.assertEqual(payload["network_requests"], 0)
        self.assertTrue(payload["protocol"]["image_edit"]["result_distinct_from_reference"])
        sync_request.assert_not_called()
        async_request.assert_not_called()
        assert_scrubbed_fixture(payload)

    def test_live_mode_requires_acknowledgement_before_executor_or_file_access(self) -> None:
        output = io.StringIO()
        executor = mock.AsyncMock()

        exit_code = main(
            [
                "live",
                "--cookie-file",
                str(Path("missing-cookie.json")),
                "--reference-image",
                str(Path("missing-reference.png")),
                "--output-dir",
                str(Path("missing-output")),
            ],
            stdout=output,
            live_executor=executor,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(output.getvalue())["error"],
            "live_probe_acknowledgement_required",
        )
        executor.assert_not_awaited()

    def test_live_mode_rejects_repository_inputs_before_optional_client_import(self) -> None:
        output = io.StringIO()

        exit_code = main(
            [
                "live",
                "--cookie-file",
                str(REPO_ROOT / "README.md"),
                "--reference-image",
                str(REPO_ROOT / "README.md"),
                "--output-dir",
                str(REPO_ROOT / ".tmp-acceptance"),
                "--acknowledge-live-probe",
            ],
            stdout=output,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "cookie_file_must_be_external")


if __name__ == "__main__":
    unittest.main()
