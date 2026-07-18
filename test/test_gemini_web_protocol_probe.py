from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.gemini_web_protocol_probe import (
    FIXTURE_DIR,
    REPO_ROOT,
    LiveProbeConfig,
    ProbeSafetyError,
    _ProtocolDiagnosticRecorder,
    _SingleAccountPool,
    _load_probe_account,
    assert_scrubbed_fixture,
    execute_live_probe,
    main,
)
from services.gemini_web_backend import (
    AccountValidation,
    GeminiWebError,
    ImageOutput,
    ImageResult,
)


PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class _FakeLiveBackend:
    def __init__(self) -> None:
        self.account_objects: list[object] = []

    def validate_account(self, account: object) -> AccountValidation:
        self.account_objects.append(account)
        return AccountValidation(
            valid=True,
            email="live-account@example.test",
            session_label="must-not-be-captured",
        )

    def generate(self, request: object) -> ImageResult:
        return ImageResult(
            images=(ImageOutput(content=PNG_HEADER + b"generated", mime_type="image/png"),)
        )

    def edit(self, request: object) -> ImageResult:
        return ImageResult(
            images=(ImageOutput(content=PNG_HEADER + b"edited", mime_type="image/png"),)
        )


class GeminiWebProtocolProbeTests(unittest.TestCase):
    def test_protocol_diagnostic_records_only_stable_stage_metadata(self) -> None:
        recorder = _ProtocolDiagnosticRecorder()
        recorder.success("initialize")
        recorder.failure("generate", "protocol")
        recorder.record_generation_shape(
            {"candidate_records": 2, "account_id": 999}
        )

        payload = recorder.summary()

        self.assertEqual(payload["completed_stages"], ["initialize"])
        self.assertEqual(payload["failure_stage"], "generate")
        self.assertEqual(payload["failure_kind"], "protocol")
        self.assertEqual(payload["generation_shape"], {"candidate_records": 2})
        self.assertFalse(payload["capture_boundary"]["raw_responses_stored"])
        assert_scrubbed_fixture(payload)

    def test_live_single_account_pool_matches_runtime_retry_seam(self) -> None:
        pool = _SingleAccountPool(
            {
                "account_id": "gemini_web:fixture-probe",
                "credentials": {"cookies": {"fixture": "fixture"}},
            }
        )

        account = pool.acquire(excluded_account_ids=set(), deadline=1.0)

        self.assertEqual(account.account_id, "gemini_web:fixture-probe")
        pool.release(account)
        pool.mark_invalid(account)
        with self.assertRaises(GeminiWebError):
            pool.acquire(
                excluded_account_ids={"gemini_web:fixture-probe"},
                deadline=1.0,
            )

    def test_repository_protocol_fixtures_pass_sensitive_shape_scan(self) -> None:
        payloads = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in FIXTURE_DIR.glob("*.json")
        ]
        for stream_path in FIXTURE_DIR.glob("*.stream.txt"):
            for line in stream_path.read_text(encoding="utf-8").splitlines():
                if line.lstrip().startswith("{"):
                    payloads.append(json.loads(line))

        self.assertTrue(payloads)
        for payload in payloads:
            assert_scrubbed_fixture(payload)

    def test_dry_run_uses_only_fixtures_and_emits_scrubbed_summary(self) -> None:
        output = io.StringIO()

        exit_code = main(["dry-run"], stdout=output)

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["image_edit"]["result_distinct_from_reference"])
        assert_scrubbed_fixture(payload)

    def test_live_mode_requires_explicit_acknowledgement(self) -> None:
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            cookie_file = root / "cookies.json"
            reference = root / "reference.png"
            cookie_file.write_text('{"fixture":"value"}', encoding="utf-8")
            reference.write_bytes(PNG_HEADER + b"reference")
            exit_code = main(
                [
                    "live",
                    "--cookie-file",
                    str(cookie_file),
                    "--reference-image",
                    str(reference),
                    "--output-dir",
                    str(root / "output"),
                ],
                stdout=output,
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(output.getvalue())["code"],
            "live_acknowledgement_required",
        )

    def test_live_mode_rejects_inputs_and_outputs_inside_repository(self) -> None:
        cases = (
            ("--cookie-file", REPO_ROOT / "README.md", "cookie_file_must_be_external"),
            (
                "--reference-image",
                REPO_ROOT / "README.md",
                "reference_image_must_be_external",
            ),
            ("--output-dir", REPO_ROOT / ".tmp-probe", "output_must_be_outside_repo"),
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            cookie_file = root / "cookies.json"
            reference = root / "reference.png"
            cookie_file.write_text('{"fixture":"value"}', encoding="utf-8")
            reference.write_bytes(PNG_HEADER + b"reference")
            for option, unsafe_path, expected_code in cases:
                with self.subTest(option=option):
                    values = {
                        "--cookie-file": cookie_file,
                        "--reference-image": reference,
                        "--output-dir": root / "output",
                    }
                    values[option] = unsafe_path
                    output = io.StringIO()
                    exit_code = main(
                        [
                            "live",
                            "--cookie-file",
                            str(values["--cookie-file"]),
                            "--reference-image",
                            str(values["--reference-image"]),
                            "--output-dir",
                            str(values["--output-dir"]),
                            "--acknowledge-live-probe",
                        ],
                        stdout=output,
                    )
                    self.assertEqual(exit_code, 2)
                    self.assertEqual(json.loads(output.getvalue())["code"], expected_code)

    def test_arbitrary_live_failure_cannot_echo_cookie_or_paths(self) -> None:
        secret = "FAKE_TEST_REJECTED_COOKIE_123456"
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            cookie_file = root / "account-identifying-name.json"
            reference = root / "reference.png"
            cookie_file.write_text(json.dumps({"__Secure-1PSID": secret}), encoding="utf-8")
            reference.write_bytes(PNG_HEADER + b"reference")

            def fail_with_sensitive_detail(config: LiveProbeConfig) -> dict[str, object]:
                raw = config.cookie_file.read_text(encoding="utf-8")
                raise RuntimeError(f"{raw} {config.cookie_file}")

            exit_code = main(
                [
                    "live",
                    "--cookie-file",
                    str(cookie_file),
                    "--reference-image",
                    str(reference),
                    "--output-dir",
                    str(root / "output"),
                    "--acknowledge-live-probe",
                ],
                stdout=output,
                live_executor=fail_with_sensitive_detail,
            )

            rendered = output.getvalue()
            self.assertEqual(exit_code, 4)
            self.assertNotIn(secret, rendered)
            self.assertNotIn(str(cookie_file), rendered)
            self.assertEqual(json.loads(rendered)["code"], "probe_failed")

    def test_scrubber_rejects_live_credential_identity_and_download_shapes(self) -> None:
        unsafe_payloads = (
            {"cookies": {"__Secure-1PSID": "FAKE_TEST_REJECTED_VALUE"}},
            {
                "cookies": [
                    {"name": "__Secure-1PSID", "value": "FAKE_TEST_REJECTED_VALUE"}
                ]
            },
            {"authorization": "Bearer FAKE_TEST_REJECTED_TOKEN"},
            {"email": "person@example.com"},
            {
                "download_ref": (
                    "https://lh3.googleusercontent.com/image?token=FAKE_TEST_REJECTED"
                )
            },
        )
        for payload in unsafe_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ProbeSafetyError) as raised:
                    assert_scrubbed_fixture(payload)
                self.assertEqual(raised.exception.code, "capture_contains_sensitive_data")

    def test_live_executor_uses_one_account_and_writes_only_safe_capture(self) -> None:
        secret = "FAKE_TEST_REJECTED_COOKIE_987654"
        probe_account_id = "gemini_web:protocol-probe"
        backend = _FakeLiveBackend()
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            cookie_file = root / "cookies.json"
            other_cookie_file = root / "other-cookies.json"
            reference = root / "reference.png"
            output_dir = root / "output"
            cookie_file.write_text(json.dumps({"__Secure-1PSID": secret}), encoding="utf-8")
            other_cookie_file.write_text(
                json.dumps({"__Secure-1PSID": "FAKE_TEST_DIFFERENT_COOKIE"}),
                encoding="utf-8",
            )
            reference.write_bytes(PNG_HEADER + b"reference")

            first_account = _load_probe_account(cookie_file)
            second_account = _load_probe_account(other_cookie_file)
            self.assertEqual(first_account["account_id"], probe_account_id)
            self.assertEqual(second_account["account_id"], probe_account_id)
            self.assertNotEqual(
                first_account["credentials"],
                second_account["credentials"],
            )

            summary = execute_live_probe(
                LiveProbeConfig(cookie_file, reference, output_dir, 30),
                backend_factory=lambda account, timeout: backend,
            )

            capture = (output_dir / "gemini-web-protocol-summary.scrubbed.json").read_text(
                encoding="utf-8"
            )
            self.assertTrue(summary["same_account_for_all_stages"])
            self.assertEqual(len(backend.account_objects), 1)
            self.assertNotIn(secret, capture)
            self.assertNotIn(probe_account_id, capture)
            self.assertNotIn(probe_account_id, json.dumps(summary))
            self.assertNotIn("live-account@example.test", capture)
            self.assertNotIn("must-not-be-captured", capture)
            self.assertTrue((output_dir / "text-generation-1.png").is_file())
            self.assertTrue((output_dir / "image-edit-1.png").is_file())
            assert_scrubbed_fixture(json.loads(capture))


if __name__ == "__main__":
    unittest.main()
