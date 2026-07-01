"""ClawLink bootstrap — pair Hermes with a ClawLink account and install the
ClawLink MCP server entry into ``~/.hermes/config.yaml``.

This module is the plugin equivalent of the standalone ``install.py`` hosted at
https://claw-link.dev/hermes/install.py. It is invoked by the Hermes plugin's
``register(ctx)`` entry point — never executed as a standalone script — so it
assumes it is running inside the Hermes Python interpreter and that the
``mcp`` package is already importable.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Tuple

__all__ = [
    "DEFAULT_BASE_URL",
    "PLUGIN_VERSION",
    "USER_AGENT",
    "run_begin",
    "run_finish",
    "run_setup",
    "run_test",
    "run_status",
    "upsert_clawlink_config",
]


PLUGIN_VERSION = "0.1.3"
DEFAULT_BASE_URL = "https://claw-link.dev"
MIN_EXPECTED_TOOLS = 10
POLL_TIMEOUT_SECONDS = 15 * 60
POLL_INTERVAL_SECONDS = 3
PENDING_SESSION_FILENAME = "clawlink-bootstrap-session.json"
USER_AGENT = f"clawlink-hermes-plugin/{PLUGIN_VERSION}"
REDACTED_SECRET = "[REDACTED]"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _redact_clawlink_secrets(text: str) -> str:
    redacted = re.sub(
        r'(?i)(x-clawlink-api-key\s*[:=]\s*["\'])[^"\r\n]+(["\'])',
        rf"\1{REDACTED_SECRET}\2",
        text,
    )
    redacted = re.sub(
        r'(?i)(x-clawlink-api-key\s*[:=]\s*)(?!["\'])[^\s,]+',
        rf"\1{REDACTED_SECRET}",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s*)cllk_live_[A-Za-z0-9._-]+",
        rf"\1{REDACTED_SECRET}",
        redacted,
    )
    redacted = re.sub(
        r"\bcllk_live_[A-Za-z0-9._-]+\b",
        "cllk_live_[REDACTED]",
        redacted,
    )
    return redacted


def _log(message: str) -> None:
    print(f"[clawlink] {_redact_clawlink_secrets(message)}", flush=True)


class BootstrapError(RuntimeError):
    """Raised when bootstrap cannot continue. Surfaces a clean message to the
    Hermes CLI / chat session instead of a Python traceback."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Hermes discovery
# ---------------------------------------------------------------------------


def _find_hermes() -> str:
    """Locate the ``hermes`` CLI on PATH so we can invoke ``hermes mcp test``.

    The plugin itself runs inside Hermes's interpreter, but ``hermes mcp test``
    is a separate subprocess invocation and needs the launcher script.
    """
    hermes = shutil.which("hermes")
    if not hermes:
        raise BootstrapError(
            "Hermes CLI was not found on PATH. The plugin is loaded but "
            "cannot run `hermes mcp test clawlink` for verification."
        )
    return hermes


def _hermes_version(hermes: str) -> str | None:
    try:
        result = subprocess.run(
            [hermes, "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0][:80] if output else None


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _ensure_mcp_importable() -> None:
    """Make sure the Hermes interpreter has the ``mcp`` package available.

    The plugin cannot install packages into Hermes's environment safely. If
    ``mcp`` is missing, surface a clear, copy-pasteable instruction.
    """
    try:
        import mcp  # noqa: F401  (probe import only)
    except ImportError as error:
        raise BootstrapError(
            "The `mcp` Python package is not installed in the Hermes "
            "environment. Install it with:\n"
            f"    {sys.executable} -m pip install --upgrade mcp\n"
            "Then rerun `hermes clawlink setup`."
        ) from error


# ---------------------------------------------------------------------------
# Bootstrap-session API
# ---------------------------------------------------------------------------


def _request_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {
        "accept": "application/json",
        "user-agent": USER_AGENT,
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"

    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("error") or body
        except Exception:
            message = body
        raise BootstrapError(
            f"{method} {url} failed: {message}",
            retryable=error.code in {408, 429, 500, 502, 503, 504},
        ) from error
    except urllib.error.URLError as error:
        raise BootstrapError(
            f"Could not reach ClawLink at {url}: {error.reason}. "
            "Check your network connection and try again.",
            retryable=True,
        ) from error


def _create_bootstrap_session(base_url: str, hermes: str | None) -> dict:
    payload = {
        "agent_family": "hermes",
        "agent_version": _hermes_version(hermes) if hermes else None,
        "client_label": "Hermes Plugin",
        "hostname": socket.gethostname(),
        "platform": platform.system().lower(),
        "approval_return_hint": "chat",
        "requested_transport": "mcp_http_header",
    }
    session = _request_json(
        "POST", f"{base_url}/api/hermes/bootstrap-sessions", payload
    )
    if not session.get("approval_url"):
        raise BootstrapError(
            "Bootstrap session response did not include an approval URL."
        )
    return session


def _pending_session_path(hermes_home: Path) -> Path:
    return hermes_home / PENDING_SESSION_FILENAME


def _parse_iso_datetime(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _pending_session_is_expired(session: dict) -> bool:
    expires_at = _parse_iso_datetime(session.get("expires_at"))
    if expires_at is None:
        return True
    return expires_at <= dt.datetime.now(dt.timezone.utc)


def _save_pending_session(hermes_home: Path, base_url: str, session: dict) -> None:
    session_id = session.get("session_id")
    approval_url = session.get("approval_url")
    poll_url = session.get("poll_url")
    expires_at = session.get("expires_at")
    required_values = (session_id, approval_url, poll_url, expires_at)
    if not all(isinstance(value, str) and value for value in required_values):
        return

    hermes_home.mkdir(parents=True, exist_ok=True)
    path = _pending_session_path(hermes_home)
    payload = {
        "base_url": base_url,
        "session_id": session_id,
        "approval_url": approval_url,
        "poll_url": poll_url,
        "expires_at": expires_at,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp_path.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _clear_pending_session(hermes_home: Path) -> None:
    try:
        _pending_session_path(hermes_home).unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        _log(f"Could not clear pending ClawLink approval cache: {error}")


def _load_pending_session(hermes_home: Path, base_url: str) -> dict | None:
    path = _pending_session_path(hermes_home)
    try:
        session = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        _clear_pending_session(hermes_home)
        return None

    if not isinstance(session, dict):
        _clear_pending_session(hermes_home)
        return None

    required_fields = ("session_id", "approval_url", "poll_url", "expires_at")
    if session.get("base_url") != base_url or not all(
        isinstance(session.get(field), str) and session.get(field)
        for field in required_fields
    ):
        _clear_pending_session(hermes_home)
        return None

    if _pending_session_is_expired(session):
        _clear_pending_session(hermes_home)
        return None

    return session


def _get_resumable_session(hermes_home: Path, base_url: str) -> dict | None:
    session = _load_pending_session(hermes_home, base_url)
    if not session:
        return None

    try:
        data = _request_json("GET", session["poll_url"])
    except BootstrapError as error:
        if error.retryable:
            _log(
                "Could not verify the saved ClawLink approval right now. "
                "Retrying the same saved session."
            )
            session["resumed"] = True
            return session
        _clear_pending_session(hermes_home)
        return None

    status = data.get("status")
    if status in {"pending_approval", "approved"}:
        session["resumed"] = True
        return session

    _clear_pending_session(hermes_home)
    return None


def _poll_for_approval(poll_url: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last_progress = 0.0
    last_retry_notice = 0.0
    _log("Waiting for approval...")

    while time.monotonic() < deadline:
        try:
            data = _request_json("GET", poll_url)
        except BootstrapError as error:
            if not error.retryable:
                raise

            now = time.monotonic()
            if now - last_retry_notice >= 6:
                _log(
                    "Could not reach ClawLink while waiting for approval. "
                    "Retrying..."
                )
                last_retry_notice = now
                last_progress = now
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        status = data.get("status")

        if status == "approved" and data.get("install"):
            _log("Approval received")
            return data["install"]
        if status == "expired":
            raise BootstrapError(
                "Approval expired. Run `hermes clawlink setup` again to "
                "generate a new link."
            )
        if status == "rejected":
            raise BootstrapError(
                "Approval was canceled. Run `hermes clawlink setup` again "
                "when you are ready."
            )
        if status == "consumed":
            raise BootstrapError(
                "This approval was already used. Run "
                "`hermes clawlink repair` to generate a fresh link."
            )

        now = time.monotonic()
        if now - last_progress >= 6:
            _log("Waiting for approval...")
            last_progress = now
        time.sleep(POLL_INTERVAL_SECONDS)

    raise BootstrapError(
        "Approval timed out. Run `hermes clawlink setup` again to generate "
        "a new link."
    )


def _consume_session(base_url: str, session_id: str) -> None:
    try:
        _request_json(
            "POST", f"{base_url}/api/hermes/bootstrap-sessions/{session_id}/consume"
        )
    except Exception as error:  # noqa: BLE001 — non-fatal
        _log(f"Could not mark bootstrap session consumed: {error}")


def _describe_expiry(session: dict) -> str | None:
    expires_at = _parse_iso_datetime(session.get("expires_at"))
    if expires_at is None:
        return None
    remaining = expires_at - dt.datetime.now(dt.timezone.utc)
    minutes = max(0, round(remaining.total_seconds() / 60))
    if minutes <= 0:
        return "less than 1 minute"
    if minutes == 1:
        return "about 1 minute"
    return f"about {minutes} minutes"


# ---------------------------------------------------------------------------
# config.yaml writing
# ---------------------------------------------------------------------------


def _yaml_quote(value: object) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_clawlink_block(install: dict, *, include_parent: bool) -> list[str]:
    headers = install.get("headers") or {}
    api_key = headers.get("x-clawlink-api-key")
    if not api_key:
        raise BootstrapError(
            "Approved install payload did not include a ClawLink API key."
        )

    prefix = ["mcp_servers:\n"] if include_parent else []
    return prefix + [
        "  clawlink:\n",
        f"    url: {_yaml_quote(install.get('url', 'https://claw-link.dev/api/mcp'))}\n",
        "    headers:\n",
        f"      x-clawlink-api-key: {_yaml_quote(api_key)}\n",
        f"    timeout: {int(install.get('timeout', 180))}\n",
        f"    connect_timeout: {int(install.get('connect_timeout', 60))}\n",
    ]


def _indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_top_level_section(
    lines: list[str], name: str
) -> tuple[int, int] | None:
    # Match plain section header (`mcp_servers:`) or the flow-style empty-dict
    # form (`mcp_servers: {}`). Flow-style with content (`mcp_servers: {a: 1}`)
    # is intentionally not matched — the caller falls back to "append a fresh
    # section" rather than risk corrupting in-line data.
    pattern = re.compile(
        rf"^{re.escape(name)}:\s*(?:\{{\s*\}}|\[\s*\])?\s*(?:#.*)?$"
    )
    for start, line in enumerate(lines):
        if pattern.match(line.rstrip("\n")):
            end = len(lines)
            for idx in range(start + 1, len(lines)):
                stripped = lines[idx].strip()
                if (
                    stripped
                    and not lines[idx].startswith((" ", "\t"))
                    and not stripped.startswith("#")
                ):
                    end = idx
                    break
            return start, end
    return None


def upsert_clawlink_config(config_text: str, install: dict) -> str:
    """Insert or replace the ``mcp_servers.clawlink`` block in a Hermes config.

    Pure-string transformation — exposed for unit tests.
    """
    lines = config_text.splitlines(keepends=True)
    if not lines:
        return "".join(_render_clawlink_block(install, include_parent=True))

    section = _find_top_level_section(lines, "mcp_servers")
    if section is None:
        if lines[-1] and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend(_render_clawlink_block(install, include_parent=True))
        return "".join(lines)

    start, end = section
    # If the section is in flow-empty form (`mcp_servers: {}` or
    # `mcp_servers: []`), replace the whole line with a proper nested block.
    if re.match(
        r"^mcp_servers:\s*(?:\{\s*\}|\[\s*\])\s*(?:#.*)?$",
        lines[start].rstrip("\n"),
    ):
        lines[start : start + 1] = _render_clawlink_block(install, include_parent=True)
        return "".join(lines)

    child_start = None
    child_indent = None
    for idx in range(start + 1, end):
        if re.match(r"^  clawlink:\s*(?:#.*)?$", lines[idx].rstrip("\n")):
            child_start = idx
            child_indent = _indentation(lines[idx])
            break

    child_block = _render_clawlink_block(install, include_parent=False)
    if child_start is None:
        insert_at = end
        lines[insert_at:insert_at] = child_block
        return "".join(lines)

    child_end = end
    for idx in range(child_start + 1, end):
        stripped = lines[idx].strip()
        if stripped and _indentation(lines[idx]) <= child_indent:
            child_end = idx
            break

    lines[child_start:child_end] = child_block
    return "".join(lines)


def _config_has_clawlink(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    return bool(
        re.search(
            r"(?m)^mcp_servers:\s*$[\s\S]*?^  clawlink:\s*$",
            config_path.read_text(),
        )
    )


def _backup_config(config_path: Path) -> Path | None:
    if not config_path.exists():
        return None
    timestamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = config_path.with_name(f"{config_path.name}.bak.{timestamp}")
    shutil.copy2(config_path, backup_path)
    return backup_path


def _write_config(hermes_home: Path, install: dict) -> Path:
    config_path = hermes_home / "config.yaml"
    hermes_home.mkdir(parents=True, exist_ok=True)
    backup_path = _backup_config(config_path)
    original = config_path.read_text() if config_path.exists() else ""
    updated = upsert_clawlink_config(original, install)
    temp_path = config_path.with_suffix(".yaml.tmp")

    try:
        temp_path.write_text(updated)
        temp_path.replace(config_path)
    except Exception:
        if backup_path and backup_path.exists():
            shutil.copy2(backup_path, config_path)
            _log(f"Config write failed; restored backup {backup_path}")
        raise

    if backup_path:
        _log(f"Backed up config to {backup_path}")
    _log(f"Updated {config_path}")
    return config_path


# ---------------------------------------------------------------------------
# MCP verification
# ---------------------------------------------------------------------------


def _mcp_test_passed(output: str) -> Tuple[bool, str | None]:
    counts = [
        int(match)
        for match in re.findall(r"(\d+)\s+(?:available\s+)?tools?", output, re.I)
    ]
    if counts and max(counts) < MIN_EXPECTED_TOOLS:
        return False, (
            f"discovered only {max(counts)} tools; expected at least "
            f"{MIN_EXPECTED_TOOLS}"
        )
    return True, None


def _run_mcp_test(hermes: str) -> bool:
    try:
        result = subprocess.run(
            [hermes, "mcp", "test", "clawlink"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        _log(f"Could not invoke `{hermes} mcp test clawlink`: {error}")
        return False

    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    if result.returncode != 0:
        _log("MCP test failed")
        if output:
            _log(output[-1600:])
        return False

    passed, reason = _mcp_test_passed(output)
    if not passed:
        _log(f"MCP test failed: {reason}")
        if output:
            _log(output[-1600:])
        return False

    _log("MCP test passed")
    return True


# ---------------------------------------------------------------------------
# Public entry points (called by __init__.register(ctx))
# ---------------------------------------------------------------------------


def _resolved_base_url() -> str:
    return os.environ.get("CLAWLINK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _existing_config_ok(hermes: str, config_path: Path, *, repair: bool) -> bool:
    """True when a healthy ClawLink config already exists and pairing can stop.

    Shared by the blocking ``setup`` path and the non-blocking ``begin`` path so
    neither re-pairs a device that is already working (unless ``repair``).
    """
    if repair or not _config_has_clawlink(config_path):
        return False
    _log("Existing ClawLink config found; validating")
    if _run_mcp_test(hermes):
        _log(
            "ClawLink is already installed. Run "
            "`hermes clawlink repair` to rotate the token."
        )
        return True
    _log("Existing config did not pass validation; repairing")
    return False


def _emit_begin(
    hermes_home: Path, base_url: str, hermes: str | None, *, repair: bool
) -> dict:
    """Create or resume a bootstrap session, persist it, and print the link.

    Returns the session dict. Does NOT poll — both ``begin`` (returns straight
    after) and ``setup`` (polls next) build on this.
    """
    session = None if repair else _get_resumable_session(hermes_home, base_url)
    if session:
        _log("Resuming pending ClawLink approval")
    else:
        session = _create_bootstrap_session(base_url, hermes)
        _save_pending_session(hermes_home, base_url, session)

    _log(f"Approval required: {session['approval_url']}")
    expires_in = _describe_expiry(session)
    if expires_in:
        _log(
            "This link is reusable until it expires. If pairing is interrupted, "
            f"rerun within {expires_in} to resume."
        )
    return session


def _complete_from_install(
    hermes: str,
    hermes_home: Path,
    base_url: str,
    session: dict,
    install: dict,
) -> int:
    """Write the config, consume the session, clear the pending file, and test.

    Shared completion step for ``setup`` (after its poll) and ``finish`` (after
    its single status fetch). Returns a process-style exit code.
    """
    try:
        _write_config(hermes_home, install)
    except Exception as error:
        raise BootstrapError(f"Config write failed: {error}") from error

    _consume_session(base_url, session["session_id"])
    _clear_pending_session(hermes_home)

    if not _run_mcp_test(hermes):
        _log(
            "Setup wrote the config but MCP verification failed. "
            "Run `hermes clawlink repair` after checking the error above."
        )
        return 1

    _log(
        "Run /reload-mcp in active Hermes chats, or start a new Hermes "
        "session, to use ClawLink."
    )
    _log("Done")
    return 0


def run_begin(*, repair: bool = False) -> int:
    """Start pairing without blocking: create the session, print the approval
    link, and return immediately. The user approves in the browser, then the
    agent runs ``run_finish`` to complete — so nothing has to stay alive across
    the human approval delay.

    Returns a process-style exit code (0 on success, non-zero on failure).
    """
    base_url = _resolved_base_url()
    try:
        hermes = _find_hermes()
        hermes_home = _hermes_home()
        config_path = hermes_home / "config.yaml"

        if _existing_config_ok(hermes, config_path, repair=repair):
            return 0

        _ensure_mcp_importable()

        _emit_begin(hermes_home, base_url, hermes, repair=repair)

        _log(
            "After you approve in the browser, come back to Hermes and tell it "
            "you're done. Hermes then runs `hermes clawlink finish` to complete "
            "setup."
        )
        return 0
    except BootstrapError as error:
        _log(str(error))
        return 1


def run_finish() -> int:
    """Complete a pairing started by ``run_begin``. Does a single status fetch
    (no long poll). If approved, writes the config and verifies; if still
    waiting, asks the user to approve first and leaves the saved session intact
    so a retry works.

    Returns a process-style exit code (0 on success, non-zero otherwise).
    """
    base_url = _resolved_base_url()
    try:
        hermes = _find_hermes()
        hermes_home = _hermes_home()

        session = _load_pending_session(hermes_home, base_url)
        if not session:
            raise BootstrapError(
                "No pending ClawLink approval found. Run `hermes clawlink begin` "
                "to start pairing."
            )

        # Single fetch against the installer route (carries the install payload
        # with the raw key once approved) — never the browser status route.
        data = _request_json("GET", session["poll_url"])
        status = data.get("status")

        if status == "approved" and data.get("install"):
            _log("Approval received")
            return _complete_from_install(
                hermes, hermes_home, base_url, session, data["install"]
            )

        if status in {"pending_approval", "approved"}:
            # Still waiting (or approved but the install payload hasn't landed
            # yet). Keep the saved session so the next `finish` succeeds.
            _log(
                "Not approved yet. Open the approval link, approve in the "
                "browser, then tell Hermes you're done."
            )
            _log(f"Approval link: {session['approval_url']}")
            return 1

        if status == "expired":
            _clear_pending_session(hermes_home)
            raise BootstrapError(
                "Approval expired. Run `hermes clawlink begin` again to "
                "generate a new link."
            )
        if status == "rejected":
            _clear_pending_session(hermes_home)
            raise BootstrapError(
                "Approval was canceled. Run `hermes clawlink begin` again "
                "when you are ready."
            )
        if status == "consumed":
            _clear_pending_session(hermes_home)
            raise BootstrapError(
                "This approval was already used. Run "
                "`hermes clawlink repair` to generate a fresh link."
            )

        _log(f"Unexpected approval status: {status}. Try again in a moment.")
        return 1
    except BootstrapError as error:
        _log(str(error))
        return 1


def run_setup(*, repair: bool = False) -> int:
    """Pair Hermes with a ClawLink account and write the MCP config.

    Blocking, terminal-oriented path: prints the link and then polls until the
    user approves. Retained for real terminal users; agent-driven pairing should
    use the non-blocking ``run_begin`` / ``run_finish`` pair instead.

    Returns a process-style exit code (0 on success, non-zero on failure) so
    the CLI handler can propagate it to ``sys.exit``.
    """
    base_url = _resolved_base_url()
    try:
        hermes = _find_hermes()
        hermes_home = _hermes_home()
        config_path = hermes_home / "config.yaml"

        if _existing_config_ok(hermes, config_path, repair=repair):
            return 0

        _ensure_mcp_importable()

        session = _emit_begin(hermes_home, base_url, hermes, repair=repair)

        try:
            install = _poll_for_approval(session["poll_url"])
        except BootstrapError as error:
            message = str(error).lower()
            terminal_words = ("expired", "timed out", "canceled", "already used")
            if any(word in message for word in terminal_words):
                _clear_pending_session(hermes_home)
            raise

        return _complete_from_install(hermes, hermes_home, base_url, session, install)
    except BootstrapError as error:
        _log(str(error))
        return 1


def run_test() -> int:
    """Run ``hermes mcp test clawlink`` and report the result."""
    try:
        hermes = _find_hermes()
    except BootstrapError as error:
        _log(str(error))
        return 1
    return 0 if _run_mcp_test(hermes) else 1


def run_status() -> int:
    """Print whether the ClawLink MCP server is configured."""
    config_path = _hermes_home() / "config.yaml"
    if not config_path.exists():
        _log(f"No Hermes config at {config_path} yet.")
        return 1
    if _config_has_clawlink(config_path):
        _log(f"ClawLink MCP server is configured in {config_path}.")
        return 0
    _log(
        f"ClawLink is not configured in {config_path}. Run "
        "`hermes clawlink setup` to pair this device."
    )
    return 1
