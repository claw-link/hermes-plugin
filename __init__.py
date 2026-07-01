"""ClawLink Hermes plugin entry point.

Registers:
  * `hermes clawlink {begin|finish|setup|test|repair|status}` — CLI subcommands.
  * `/clawlink {begin|finish|setup|test|status}` — in-session slash command.
  * A bundled routing skill (``clawlink:clawlink``) that teaches the agent
    when to prefer ClawLink for third-party app interactions.

`begin` + `finish` are the non-blocking, agent-driven pairing path: `begin`
prints the approval link and returns; once the user approves and says they're
done, the agent runs `finish` to complete. `setup` is the legacy blocking path
kept for terminal users. The actual logic lives in ``bootstrap.py``.
"""

from __future__ import annotations

from pathlib import Path

from . import bootstrap

PLUGIN_NAME = "clawlink"
PLUGIN_VERSION = bootstrap.PLUGIN_VERSION


def _slash_help() -> str:
    return (
        "Usage:\n"
        "  /clawlink begin    Start pairing: print the approval link (no wait).\n"
        "  /clawlink finish   Finish pairing after you approve in the browser.\n"
        "  /clawlink setup    Pair in one blocking step (terminal use).\n"
        "  /clawlink test     Run `hermes mcp test clawlink`.\n"
        "  /clawlink status   Show whether ClawLink is configured.\n"
        "  /clawlink repair   Rotate the local ClawLink token."
    )


def register(ctx) -> None:
    """Plugin entry point. Called once at Hermes startup."""

    # ---- CLI subcommand: hermes clawlink <action> ------------------------
    def _setup_cli(parser) -> None:
        sub = parser.add_subparsers(dest="action")
        sub.add_parser(
            "begin",
            help="Start pairing: print the approval link and exit (no wait)",
        )
        sub.add_parser(
            "finish",
            help="Finish pairing after you approve in the browser",
        )
        sub.add_parser(
            "setup", help="Pair Hermes with your ClawLink account (blocking)"
        )
        sub.add_parser(
            "test",
            help="Run `hermes mcp test clawlink` against the current config",
        )
        repair = sub.add_parser(
            "repair",
            help="Rotate the local ClawLink token and rewrite Hermes config",
        )
        repair.set_defaults(action="repair")
        sub.add_parser(
            "status", help="Show whether ClawLink is configured"
        )

    def _handle_cli(args) -> int:
        action = getattr(args, "action", None) or "setup"
        if action == "begin":
            return bootstrap.run_begin()
        if action == "finish":
            return bootstrap.run_finish()
        if action == "setup":
            return bootstrap.run_setup()
        if action == "repair":
            return bootstrap.run_setup(repair=True)
        if action == "test":
            return bootstrap.run_test()
        if action == "status":
            return bootstrap.run_status()
        print(f"[clawlink] Unknown action: {action}")
        return 2

    ctx.register_cli_command(
        name=PLUGIN_NAME,
        help="Manage ClawLink integration (begin, finish, pair, test, repair)",
        setup_fn=_setup_cli,
        handler_fn=_handle_cli,
    )

    # ---- In-session slash command: /clawlink <action> --------------------
    def _handle_slash(raw_args: str) -> str:
        action = (raw_args or "").strip().split()
        verb = action[0] if action else "setup"

        if verb in {"help", "-h", "--help", "?"}:
            return _slash_help()
        if verb == "begin":
            code = bootstrap.run_begin()
            return (
                "Approval link printed above. Open it, approve in your browser, "
                "then tell me you're done and I'll run `/clawlink finish`."
                if code == 0
                else "Could not start ClawLink pairing. See the log lines above."
            )
        if verb == "finish":
            code = bootstrap.run_finish()
            return (
                "ClawLink setup complete. Start a new chat or run "
                "/reload-mcp to use ClawLink tools."
                if code == 0
                else "ClawLink isn't ready yet. If you haven't approved the "
                "link, approve it in your browser, then run `/clawlink finish` "
                "again."
            )
        if verb == "setup":
            code = bootstrap.run_setup()
            return (
                "ClawLink setup complete. Start a new chat or run "
                "/reload-mcp to use ClawLink tools."
                if code == 0
                else "ClawLink setup did not finish. See the log lines above "
                "for details, then try `/clawlink setup` again."
            )
        if verb == "repair":
            code = bootstrap.run_setup(repair=True)
            return (
                "ClawLink token rotated and config rewritten."
                if code == 0
                else "Repair failed. See log lines above."
            )
        if verb == "test":
            code = bootstrap.run_test()
            return (
                "ClawLink MCP test passed."
                if code == 0
                else "ClawLink MCP test failed. See log lines above."
            )
        if verb == "status":
            bootstrap.run_status()
            return ""
        return f"Unknown action: {verb}\n\n{_slash_help()}"

    ctx.register_command(
        PLUGIN_NAME,
        handler=_handle_slash,
        description="Set up, test, or repair ClawLink",
    )

    # ---- Bundled routing skill ------------------------------------------
    skill_path = Path(__file__).parent / "skills" / "clawlink.md"
    if skill_path.exists():
        try:
            ctx.register_skill(name=PLUGIN_NAME, path=str(skill_path))
        except Exception:
            # Older Hermes versions may not expose register_skill yet — the
            # plugin still works without the bundled skill.
            pass
