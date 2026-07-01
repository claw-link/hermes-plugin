"""Unit tests for the ClawLink Hermes config writer."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Make the plugin importable when running `python -m unittest` from the repo
# root.
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from bootstrap import (  # noqa: E402
    BootstrapError,
    _clear_pending_session,
    _get_resumable_session,
    _load_pending_session,
    _pending_session_path,
    _poll_for_approval,
    _redact_clawlink_secrets,
    _run_mcp_test,
    _save_pending_session,
    run_begin,
    run_finish,
    upsert_clawlink_config,
)


SAMPLE_INSTALL = {
    "url": "https://claw-link.dev/api/mcp",
    "headers": {"x-clawlink-api-key": "ck_test_123"},
    "timeout": 180,
    "connect_timeout": 60,
}


def _sample_session(expires_at: str = "2999-01-01T00:00:00.000Z") -> dict:
    return {
        "session_id": "hbs_test",
        "approval_url": "https://claw-link.dev/hermes/approve/hbs_test",
        "poll_url": "https://claw-link.dev/api/hermes/bootstrap-sessions/hbs_test",
        "expires_at": expires_at,
    }


class ConfigWriterTests(unittest.TestCase):
    def test_empty_config_creates_section(self) -> None:
        result = upsert_clawlink_config("", SAMPLE_INSTALL)
        self.assertIn("mcp_servers:", result)
        self.assertIn("  clawlink:", result)
        self.assertIn(
            'url: "https://claw-link.dev/api/mcp"',
            result,
        )
        self.assertIn(
            'x-clawlink-api-key: "ck_test_123"',
            result,
        )
        self.assertIn("timeout: 180", result)
        self.assertIn("connect_timeout: 60", result)

    def test_no_mcp_section_appends_section(self) -> None:
        config = "model:\n  name: claude\n"
        result = upsert_clawlink_config(config, SAMPLE_INSTALL)
        self.assertIn("model:\n  name: claude", result)
        self.assertIn("mcp_servers:", result)
        self.assertIn("  clawlink:", result)

    def test_empty_mcp_servers_dict_is_replaced(self) -> None:
        config = "mcp_servers: {}\n"
        result = upsert_clawlink_config(config, SAMPLE_INSTALL)
        self.assertNotIn("mcp_servers: {}", result)
        self.assertIn("mcp_servers:", result)
        self.assertIn("  clawlink:", result)

    def test_existing_clawlink_block_is_replaced(self) -> None:
        config = (
            "mcp_servers:\n"
            "  clawlink:\n"
            '    url: "https://old.example.com/api/mcp"\n'
            "    headers:\n"
            '      x-clawlink-api-key: "ck_old_token"\n'
            "    timeout: 60\n"
            "    connect_timeout: 30\n"
        )
        result = upsert_clawlink_config(config, SAMPLE_INSTALL)
        self.assertNotIn("ck_old_token", result)
        self.assertNotIn("https://old.example.com", result)
        self.assertIn("ck_test_123", result)
        self.assertIn("https://claw-link.dev/api/mcp", result)
        self.assertEqual(result.count("clawlink:"), 1)

    def test_other_servers_are_preserved(self) -> None:
        config = (
            "mcp_servers:\n"
            "  github:\n"
            "    command: github-mcp\n"
            '    args: ["--token", "ghp_test"]\n'
        )
        result = upsert_clawlink_config(config, SAMPLE_INSTALL)
        self.assertIn("  github:", result)
        self.assertIn("command: github-mcp", result)
        self.assertIn("  clawlink:", result)

    def test_missing_api_key_raises(self) -> None:
        bad_install = {"headers": {}}
        with self.assertRaises(Exception):
            upsert_clawlink_config("", bad_install)

    def test_special_chars_in_token_are_quoted(self) -> None:
        install = {
            "url": "https://claw-link.dev/api/mcp",
            "headers": {"x-clawlink-api-key": 'ck_with "quotes" and \\ slash'},
            "timeout": 180,
            "connect_timeout": 60,
        }
        result = upsert_clawlink_config("", install)
        # Embedded quotes/backslashes are escaped.
        self.assertIn(r'\"quotes\"', result)
        self.assertIn(r"\\ slash", result)


class PendingSessionTests(unittest.TestCase):
    def test_pending_session_round_trips_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            session = {
                "session_id": "hbs_test",
                "approval_url": "https://claw-link.dev/hermes/approve/hbs_test",
                "poll_url": "https://claw-link.dev/api/hermes/bootstrap-sessions/hbs_test",
                "expires_at": "2999-01-01T00:00:00.000Z",
                "install": {"headers": {"x-clawlink-api-key": "cllk_live_secret"}},
            }

            _save_pending_session(hermes_home, "https://claw-link.dev", session)

            path = _pending_session_path(hermes_home)
            self.assertTrue(path.exists())
            self.assertNotIn("cllk_live_secret", path.read_text())

            loaded = _load_pending_session(hermes_home, "https://claw-link.dev")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["session_id"], "hbs_test")
            self.assertEqual(loaded["poll_url"], session["poll_url"])

    def test_expired_pending_session_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            _save_pending_session(
                hermes_home,
                "https://claw-link.dev",
                {
                    "session_id": "hbs_old",
                    "approval_url": "https://claw-link.dev/hermes/approve/hbs_old",
                    "poll_url": "https://claw-link.dev/api/hermes/bootstrap-sessions/hbs_old",
                    "expires_at": "2000-01-01T00:00:00.000Z",
                },
            )

            self.assertIsNone(_load_pending_session(hermes_home, "https://claw-link.dev"))
            self.assertFalse(_pending_session_path(hermes_home).exists())

    def test_base_url_mismatch_clears_pending_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            _save_pending_session(
                hermes_home,
                "https://staging.claw-link.dev",
                {
                    "session_id": "hbs_stage",
                    "approval_url": "https://staging.claw-link.dev/hermes/approve/hbs_stage",
                    "poll_url": "https://staging.claw-link.dev/api/hermes/bootstrap-sessions/hbs_stage",
                    "expires_at": "2999-01-01T00:00:00.000Z",
                },
            )

            self.assertIsNone(_load_pending_session(hermes_home, "https://claw-link.dev"))
            self.assertFalse(_pending_session_path(hermes_home).exists())
            _clear_pending_session(hermes_home)

    def test_retryable_resume_probe_keeps_pending_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            session = {
                "session_id": "hbs_retry",
                "approval_url": "https://claw-link.dev/hermes/approve/hbs_retry",
                "poll_url": "https://claw-link.dev/api/hermes/bootstrap-sessions/hbs_retry",
                "expires_at": "2999-01-01T00:00:00.000Z",
            }
            _save_pending_session(hermes_home, "https://claw-link.dev", session)

            with patch(
                "bootstrap._request_json",
                side_effect=BootstrapError("temporary network error", retryable=True),
            ):
                resumed = _get_resumable_session(hermes_home, "https://claw-link.dev")

            self.assertIsNotNone(resumed)
            self.assertTrue(resumed["resumed"])
            self.assertTrue(_pending_session_path(hermes_home).exists())

    def test_poll_retries_after_retryable_network_error(self) -> None:
        with patch(
            "bootstrap._request_json",
            side_effect=[
                BootstrapError("temporary network error", retryable=True),
                {"status": "approved", "install": SAMPLE_INSTALL},
            ],
        ), patch("bootstrap.time.sleep", return_value=None):
            install = _poll_for_approval(
                "https://claw-link.dev/api/hermes/bootstrap-sessions/hbs_retry"
            )

        self.assertEqual(install, SAMPLE_INSTALL)


class SecretRedactionTests(unittest.TestCase):
    def test_redactor_scrubs_header_bearer_and_raw_token(self) -> None:
        raw = (
            'x-clawlink-api-key: "cllk_live_secret123"\n'
            "authorization: Bearer cllk_live_secret123\n"
            "raw token cllk_live_secret123"
        )

        redacted = _redact_clawlink_secrets(raw)

        self.assertNotIn("cllk_live_secret123", redacted)
        self.assertIn('x-clawlink-api-key: "[REDACTED]"', redacted)
        self.assertIn("authorization: Bearer [REDACTED]", redacted)
        self.assertIn("raw token cllk_live_[REDACTED]", redacted)

    def test_mcp_test_failure_logs_redacted_output(self) -> None:
        result = SimpleNamespace(
            returncode=1,
            stdout='headers:\n  x-clawlink-api-key: "cllk_live_secret123"\n',
            stderr="",
        )

        with patch("bootstrap.subprocess.run", return_value=result):
            output = io.StringIO()
            with redirect_stdout(output):
                passed = _run_mcp_test("/usr/bin/hermes")

        self.assertFalse(passed)
        rendered = output.getvalue()
        self.assertIn("MCP test failed", rendered)
        self.assertIn('x-clawlink-api-key: "[REDACTED]"', rendered)
        self.assertNotIn("cllk_live_secret123", rendered)


class RunBeginTests(unittest.TestCase):
    def test_begin_saves_session_prints_link_and_does_not_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            with patch("bootstrap._find_hermes", return_value="/usr/bin/hermes"), \
                patch("bootstrap._hermes_home", return_value=hermes_home), \
                patch("bootstrap._ensure_mcp_importable", return_value=None), \
                patch(
                    "bootstrap._request_json", return_value=_sample_session()
                ), \
                patch("bootstrap._poll_for_approval") as poll:
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_begin()

            self.assertEqual(code, 0)
            poll.assert_not_called()
            self.assertTrue(_pending_session_path(hermes_home).exists())
            rendered = output.getvalue()
            self.assertIn("hbs_test", rendered)
            self.assertIn("finish", rendered)


class RunFinishTests(unittest.TestCase):
    def _save(self, hermes_home: Path) -> None:
        _save_pending_session(hermes_home, "https://claw-link.dev", _sample_session())

    def test_finish_completes_when_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            self._save(hermes_home)
            with patch("bootstrap._find_hermes", return_value="/usr/bin/hermes"), \
                patch("bootstrap._hermes_home", return_value=hermes_home), \
                patch(
                    "bootstrap._request_json",
                    return_value={"status": "approved", "install": SAMPLE_INSTALL},
                ), \
                patch("bootstrap._consume_session", return_value=None), \
                patch("bootstrap._run_mcp_test", return_value=True):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_finish()

            self.assertEqual(code, 0)
            config = (hermes_home / "config.yaml").read_text()
            self.assertIn("clawlink:", config)
            self.assertIn("ck_test_123", config)
            # Completed sessions are cleared so they can't be reused.
            self.assertFalse(_pending_session_path(hermes_home).exists())

    def test_finish_while_pending_keeps_session_and_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            self._save(hermes_home)
            with patch("bootstrap._find_hermes", return_value="/usr/bin/hermes"), \
                patch("bootstrap._hermes_home", return_value=hermes_home), \
                patch(
                    "bootstrap._request_json",
                    return_value={"status": "pending_approval"},
                ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_finish()

            self.assertEqual(code, 1)
            # Saved session is preserved so a later finish on the same link works.
            self.assertTrue(_pending_session_path(hermes_home).exists())
            self.assertFalse((hermes_home / "config.yaml").exists())
            self.assertIn("approve", output.getvalue().lower())

    def test_finish_clears_session_on_terminal_status(self) -> None:
        for status in ("expired", "rejected", "consumed"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmp:
                    hermes_home = Path(tmp)
                    self._save(hermes_home)
                    with patch(
                        "bootstrap._find_hermes", return_value="/usr/bin/hermes"
                    ), \
                        patch("bootstrap._hermes_home", return_value=hermes_home), \
                        patch(
                            "bootstrap._request_json",
                            return_value={"status": status},
                        ):
                        output = io.StringIO()
                        with redirect_stdout(output):
                            code = run_finish()

                    self.assertEqual(code, 1)
                    self.assertFalse(_pending_session_path(hermes_home).exists())

    def test_finish_without_pending_session_points_to_begin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            with patch("bootstrap._find_hermes", return_value="/usr/bin/hermes"), \
                patch("bootstrap._hermes_home", return_value=hermes_home), \
                patch("bootstrap._request_json") as req:
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_finish()

            self.assertEqual(code, 1)
            req.assert_not_called()
            self.assertIn("begin", output.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
