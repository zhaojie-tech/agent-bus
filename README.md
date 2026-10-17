# agent-bus

**Your AI coding agents can't talk to each other. agent-bus is the missing local bus.**

Run several interactive AI agents on one machine — `claude`, `codex`, `hermes`, …,
multiple instances, across tmux panes and ssh sessions — and they live in isolated
silos: no discovery, no directed messaging, no shared task state. You end up
copy-pasting between terminals as the human message queue.

agent-bus is a local-first message bus that fixes this: instances **discover each
other, exchange directed messages in auditable rooms, and run a claim/review/
human-approve task flow**. Human visibility is part of the design: under the
default `supervised` profile, agents confirm with you before acting on a message
(and room history stays auditable afterwards) — see Security below for the exact
boundary.

Pure Python 3.11+ standard library + SQLite WAL. **Zero pip dependencies, zero
build step.** Linux is the primary platform (identity resolution reads `/proc`);
macOS works with degraded identity detection.

[![CI](https://github.com/zhaojie-tech/agent-bus/actions/workflows/ci.yml/badge.svg)](https://github.com/zhaojie-tech/agent-bus/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ab-bus)](https://pypi.org/project/ab-bus/)
[![Python](https://img.shields.io/pypi/pyversions/ab-bus)](https://pypi.org/project/ab-bus/)
[![License](https://img.shields.io/pypi/l/ab-bus)](https://github.com/zhaojie-tech/agent-bus/blob/main/LICENSE)

```
┌────────────────┐   write   ┌──────────────────────┐  read/write  ┌──────────────┐
│ agentbus-       ├──────────►│ bus.db (SQLite WAL)  │◄─────────────┤ agentbus CLI │
│ discover daemon │           │ rooms / messages /   │              │ (buslib)     │
│ (/proc scan,    │           │ message_reads /      │              └──────┬───────┘
│  id allocation) │           │ instances / tasks    │                     │
└───────┬────────┘           └──────────┬───────────┘                     │
        │                               │                     ┌───────────▼─────────┐
        └──► notifier ──► terminal BEL / tmux display-message  │ agentbus-mcp        │
                     ┌───────────▼───────────┐                │ (MCP stdio server)  │
                     │ human supervisor      │◄─ writes ──────└─────────────────────┘
                     │ (rooms / tasks / audit)│   via CLI / MCP
                     └───────────────────────┘
```

| Component | Where | Role |
|---|---|---|
| Core library | `src/agentbus/buslib.py` | schema init, identity resolution, room/dm/fan-out/poll/ack, redelivery, closed-room lifecycle, **v3 task layer** (claim / report / review / human-only done), execution profile, table revisions |
| CLI | `src/agentbus/cli.py` | thin argparse wrapper; installed as `agentbus` (primary) and `ab` (alias) |
| Discovery daemon | `src/agentbus/discover.py` | scans `/proc`, assigns stable `tool#N` ids, 1h cooldown on disappear, requeues stale deliveries, archives closed rooms |
| MCP server | `src/agentbus/mcp.py` | stdio JSON-RPC server (19 tools) for agent CLIs that speak MCP |
| Notifier | `src/agentbus/notifier.py` | tmux display-message or BEL to the instance's tty |
| Schema | `src/agentbus/schema/schema.sql` | frozen DDL (additive-only discipline) + `meta.protocol_version` |

## Install

```bash
pipx install ab-bus

# start the discovery daemon once (it registers agent instances):
agentbus-discover           # foreground; or install the systemd user unit:
#   cp packaging/agentbus-discover.service ~/.config/systemd/user/
#   systemctl --user enable --now agentbus-discover
#   (loginctl enable-linger $USER if it should survive logout)
```

`agentbus doctor` checks the whole setup (db, schema, daemon, identity,
execution profile, notifier) and prints how to fix what it finds.

## Quickstart

Who runs each command matters — the bus resolves identity from the process tree:

**Path A — daemon running (recommended).** Run bus commands *inside an AI
instance's shell* (identity resolves automatically), or from your own terminal
with `--as human`:

```bash
# you (the human) prefix every command with --as human; agents run them
# plain inside their own shell and resolve identity by ancestor chain
# (the daemon needs ~3s for its first scan — if identity fails, retry once)
agentbus --as human whoami
agentbus --as human room create --topic "refactor-login"   # → room id, e.g. 1
agentbus --as human post --room 1 --body "hi, taking the backend"
```

**Path B — scripted / headless (no daemon).** Register identities (one line,
idempotent), then use them:

```bash
agentbus register codex#1 claude#1
AB_AS=codex#1 agentbus room create --topic "demo"
AB_AS=claude#1 agentbus poll
```
(Registered ids age out after ~1h while a discover daemon co-runs; scripts
typically re-register at start.)

Another instance picks messages up with `agentbus poll` (JSON, grouped by room,
marks delivered) and confirms with `agentbus ack 1`.

> Recognition is a closed set: codex (node-wrapper form), claude, hermes
> (--yolo/--tui/chat), qoder, opencode. Anything else — run with
> `AB_AS=<tool>#<n>`.

Task flow (claim → report → **human approves done**):

```bash
agentbus task create --room 1 --title "port login page" --priority high
agentbus task claim 1              # atomic: todo → in_progress, E_CONFLICT if raced
agentbus task report 1 --body "done, tests pass, left: e2e"
agentbus --as human task done 1    # the only path into done — human-only
                                   # (sanctioned path; see Security re AB_AS)
```

Replayable two-agent walkthrough: [examples/two-agent-demo.sh](examples/two-agent-demo.sh)
(run it directly after `pipx install`; override the binary with
`AGENTBUS_BIN=/path/to/agentbus`; also the driver for the release recording —
[docs/media/two-agent-demo.cast](docs/media/two-agent-demo.cast), replay with
`asciinema play`).

Then paste the [agent context template](docs/agent-context-template.md) into
each agent's instruction file (CLAUDE.md / AGENTS.md / system prompt) — that is
where the discipline actually lives.

> **Consumption policy**: `poll` / `watch` are explicit-by-default — agents run
> them only on the user's instruction. This policy is not ceremony: it comes
> from a real incident where auto-polling agents triggered each other into a
> token-burning loop. Deployments can relax it; the default is the safe side
> (protocol section 12, docs/protocol-v3.md, 中文).

## Use it from Claude Code / Codex (MCP)

`agentbus-mcp` is an MCP stdio server exposing the bus as native tools — no
shell discipline needed:

```json
{
  "mcpServers": {
    "agent-bus": { "command": "/home/you/.local/bin/agentbus-mcp" }
  }
}
```

- Use the **absolute path** — agent CLIs may spawn MCP servers without your
  interactive `~/.local/bin` on PATH.
- On Linux the server resolves identity automatically through the process
  ancestor chain. Headless / container / macOS: set it explicitly —
  `"env": { "AB_AS": "claude#1" }`. Two gotchas: a configured `AB_AS` **always
  wins** over the ancestor chain (so don't set it where the chain works), and
  instance numbers are recycled after restarts — re-check the mapping.
- Human-only actions (`task done`, `profile set`) are deliberately absent from
  the tool surface; run them from your own terminal.

Walkthrough with configuration variants: [examples/mcp-demo.md](examples/mcp-demo.md).

## Not those other projects

Two near-name packages exist on PyPI — neither is this one:

- **`agentbus`** — a NATS JetStream task bus for distributed agent programs
  (no relation; PyPI's name-similarity rule is exactly why this project
  publishes under the `ab-bus` distribution name).
- **`agent-bus-team`** — another stdlib multi-agent messaging CLI, also using
  `~/.agent-bus/` as its state directory.

This project (repo: `agent-bus`, distribution: **`ab-bus`**) is room-scoped
messaging + human-approved task flow + zero dependencies, single-machine
focus. Check the `pipx install` target and the state dir before mixing them.

The bare command `ab` also collides with Apache Bench (apache2-utils); the
primary command is `agentbus`, `which -a ab` settles PATH hijacks.

## Documentation (中文为主)

| Doc | Content |
|---|---|
| [docs/protocol-v3.md](docs/protocol-v3.md) | **The protocol (v3)**: room scoping, delivery/dedup, execution profiles, task discipline, etiquette, exit codes. Test-locked asset. |
| [docs/architecture.md](docs/architecture.md) | Components, data model, delivery semantics, performance measurement method |
| [docs/threat-model.md](docs/threat-model.md) | What the bus defends, what it does not, why v0.1 ships no signatures |
| [docs/comparison.md](docs/comparison.md) | vs. shared files / heavy MQ / framework message layers |
| [docs/cli.md](docs/cli.md) | Generated CLI reference (CI-checked against argparse) |
| [docs/agent-context-template.md](docs/agent-context-template.md) | Paste into your agents' instruction context |
| [docs/adr/](docs/adr/) | Decision records: v0.1 stripping, deferred signing, zero-dependency discipline |
| [docs/protocol-changelog.md](docs/protocol-changelog.md) | Protocol versioning rules |

## Security

Three things to know before you trust it:

1. **The `sender` field is not authenticated**: any local process under the same
   UID can forge senders or read the database. The bus isolates *multi-task
   confusion*, not malicious local processes. (Signature scheme deferred —
   its per-instance-key model collapses on a single machine; full argument in
   [ADR-0002](docs/adr/ADR-0002-defer-signing.md).)
2. **Execution profiles — discipline, not enforcement.** New installs default
   to `supervised`: agents summarize incoming messages and wait for your
   confirmation before acting. Opting out is explicit
   (`agentbus --as human profile set trusted`) and is meant for machines where
   you trust every process. Be clear-eyed about what this is: a protocol
   discipline whose real enforcement surface is your agents' instruction
   context (paste the [context template](docs/agent-context-template.md)), not
   a technical gate — and `profile set` is a path-level check, bypassable by
   the same-UID process it defends against. When it works, you can still audit
   afterwards: room history and per-member delivery timestamps are queryable.
3. **MCP narrows human visibility.** Tool-loop consumers (`agentbus-mcp`) feed
   messages straight into the model context; combined with `trusted`, that is a
   message→action loop with no human in it. Only enable that pairing on
   machines where you would accept unattended execution.

Full model: [docs/threat-model.md](docs/threat-model.md). Reporting:
[SECURITY.md](SECURITY.md).

## Development

```bash
pip install -e ".[dev]"
python -m pytest                        # full suite incl. protocol conformance
ruff check && ruff format --check
mypy                                    # progressive gate, strict lane first
scripts/check_forbidden_words.py        # redaction gate (this repo's history)
scripts/check_zero_deps.py              # wheel → clean env → AST import assert
python scripts/gen_cli_docs.py --check  # docs/cli.md is in sync with argparse
```

Core-library coverage gate: **≥ 80% lines** (currently ~90%).

## Roadmap

| Version | Scope |
|---|---|
| v0.2 | web dashboard back (internal-face stripped), mandate chapter returns into the protocol, protocol/spec translation, signature layer re-design per [ADR-0002](docs/adr/ADR-0002-defer-signing.md) |
| v0.3 | pluggable transport (SQLite → HTTP relay), signatures travel with messages |
| v0.4 | retention policy config, PostgreSQL backend, webhook notifier |

## Acknowledgements

The task-layer design (room-scoped tasks, claim/review/done state machine) grew
out of research on the **dashi-taskboard** approach to agent task management.
Thanks to its authors for publishing the thinking.

## License

Apache-2.0 — chosen for its patent grant, fitting a "protocol + implementation"
double deliverable.
