# clawlink-hermes-plugin

> [ClawLink](https://claw-link.dev) for [Hermes Agent](https://hermes-agent.nousresearch.com).
> Connect 100+ third-party apps — Gmail, Slack, Notion, GitHub, Stripe, and more — through one Hermes plugin.

ClawLink manages OAuth and API credentials for you. Install the plugin once, pair it with your ClawLink account in the browser, and Hermes can immediately call any app you connect in the [ClawLink dashboard](https://claw-link.dev/dashboard).

## Why this exists

Hermes connects to external apps through MCP servers, but the bootstrap is manual: editing `~/.hermes/config.yaml`, generating an API key, pasting headers. This plugin owns that setup so you never see the YAML.

The plugin is the recommended install path because it avoids `curl … | python3` patterns that Hermes's `tirith` security scanner flags.

## Install

Pairing is two non-blocking steps, so nothing has to stay alive while you approve in the browser:

```bash
hermes plugins install ClawLink-HQ/hermes-plugin --enable
hermes clawlink begin
# approve the printed link in your browser, then:
hermes clawlink finish
```

This is the recommended path when an agent runs the commands for you: `begin` prints the link and returns right away, you approve, then `finish` completes instantly. The browser and the Hermes host can be different machines — the link is portable, and `finish` completes on whichever host ran `begin` (e.g. Hermes on a cloud server, approval on your laptop or phone).

Pairing will:

1. Check whether ClawLink is already configured. If so, validate it and exit.
2. (`begin`) Create a one-time approval session and print a URL. If pairing is interrupted, rerun `hermes clawlink begin` before the link expires to resume the same approval session.
3. You approve the device in your browser (sign in to ClawLink if needed).
4. (`finish`) Write `mcp_servers.clawlink` into `~/.hermes/config.yaml` with a scoped MCP token and run `hermes mcp test clawlink` to verify.

After `finish`, run `/reload-mcp` in active Hermes chats — or start a fresh session — to pick up the new tools.

Prefer a single blocking command in a real terminal? `hermes clawlink setup` does begin + wait + finish in one call.

## Commands

CLI subcommands (run from your terminal):

| Command | What it does |
| --- | --- |
| `hermes clawlink begin` | Start pairing: print the approval link and exit (no wait) |
| `hermes clawlink finish` | Finish pairing after you approve in the browser |
| `hermes clawlink setup` | Pair in one blocking step (begin + wait + finish), for terminal use |
| `hermes clawlink test` | Run `hermes mcp test clawlink` against the current config |
| `hermes clawlink repair` | Rotate the ClawLink token and rewrite the config |
| `hermes clawlink status` | Show whether ClawLink is configured |

In-session slash commands (use these from inside a Hermes chat):

| Slash command | Equivalent |
| --- | --- |
| `/clawlink begin` | `hermes clawlink begin` |
| `/clawlink finish` | `hermes clawlink finish` |
| `/clawlink setup` | `hermes clawlink setup` |
| `/clawlink test` | `hermes clawlink test` |
| `/clawlink repair` | `hermes clawlink repair` |
| `/clawlink status` | `hermes clawlink status` |

## What the plugin writes

A single block under `mcp_servers` in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  clawlink:
    url: "https://claw-link.dev/api/mcp"
    headers:
      x-clawlink-api-key: "<scoped token issued by ClawLink>"
    timeout: 180
    connect_timeout: 60
```

Existing config is backed up to `config.yaml.bak.<timestamp>` before any change. If the write fails, the backup is restored automatically.

## Requirements

- Hermes Agent installed and on `PATH`.
- The Python `mcp` package available in Hermes's interpreter (Hermes installs this for you when MCP servers are configured; the plugin gives a clear `pip install` instruction if it is missing).
- A ClawLink account at [claw-link.dev](https://claw-link.dev).

## Troubleshooting

- **`Hermes CLI was not found on PATH`** — the plugin is loaded but cannot run `hermes mcp test clawlink`. Make sure the `hermes` launcher is on `PATH`, then rerun setup.
- **`The mcp Python package is not installed`** — install it into the Hermes interpreter with the command the plugin prints (`<python> -m pip install --upgrade mcp`), then rerun setup.
- **`finish` says the link isn't approved yet** — approve the printed link in your browser first, then run `hermes clawlink finish` again. The saved session is kept until it expires, so retrying works.
- **Approval timed out / expired / canceled** — the approval link has a 15-minute lifetime. Rerunning `hermes clawlink begin` before expiry resumes the same approval; after expiry it generates a fresh link.
- **Temporary network hiccup** — short-lived ClawLink/network errors do not discard the saved session. Rerun `hermes clawlink begin` (or `finish`) before expiry to resume the same approval link.
- **Tools do not appear after setup** — run `/reload-mcp` in your active chat, or start a new Hermes session so the tool catalog reloads.

## Development

This package mirrors the production code at [`ClawLink-HQ/clawlink`](https://github.com/ClawLink-HQ/clawlink) under `packages/clawlink-hermes-plugin/`. Releases are cut from this repo (`ClawLink-HQ/hermes-plugin`) so `hermes plugins install` can fetch them directly from GitHub.

To test locally without publishing:

```bash
hermes plugins install /absolute/path/to/clawlink-hermes-plugin --enable
hermes clawlink begin
# approve in the browser, then:
hermes clawlink finish
```

Run unit tests:

```bash
python3 -m pytest tests
```

## Security

- Tokens are stored only in `~/.hermes/config.yaml` and are sent only to `https://claw-link.dev` (or the `CLAWLINK_BASE_URL` you configure for self-hosted setups).
- Setup/test logging redacts ClawLink credential values before printing command output or error details to the terminal.
- The plugin makes no outbound network calls during normal Hermes operation — only during `begin`, `finish`, `setup`, `repair`, or `test`.
- See [`claw-link.dev/verify`](https://claw-link.dev/verify) for build provenance.

## License

GNU Affero General Public License v3.0 (AGPL-3.0)
