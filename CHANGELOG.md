# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/); versioning is
semantic. Package version and protocol version are decoupled — protocol
changes are tracked in [docs/protocol-changelog.md](docs/protocol-changelog.md).

## [0.1.0] — 2026-10 (first public release)

Initial open-source release, rebuilt from the internal production system per a
file-level disposition table (see docs/adr/ADR-0001-strip-web-arm-mandate.md).

### Added
- Room-scoped messaging: rooms, dm (deterministic pairwise rooms), fan-out with
  per-member read state (`new → delivered → acked`), 30-minute redelivery of
  unacked deliveries, closed-room three-stage lifecycle (tail window → freeze
  after 24h of no poll/ack activity → archive after 30 days), soft cap with
  task-reference protection. Freeze activity is tracked via `delivered_at`
  (first delivery, never overwritten) plus the `message_reads.acked_at`
  additive column; old databases migrate the column idempotently.
- Identity resolution: ancestor-chain pid matching, tty fallback, explicit
  `--as human` / `AB_AS` override (source label now also reported for the
  ancestor path).
- Discovery daemon (`python -m agentbus.discover`): `/proc` scanning, stable
  `tool#N` numbering with 1-hour cooldown, notifier (tmux display-message / BEL).
- v3 task layer: create/get/ls, atomic claim (optimistic lock, `E_CONFLICT`),
  report → in_review, review (send-back only), block/cancel/reopen,
  parent/blocks/related relations (single-parent, cycle-safe), room-close gate,
  **human-only `task done`**.
- Protocol v3 spec (docs/protocol-v3.md) locked by tests, incl. a
  43-item conformance catalog with bidirectional spec↔catalog coverage checks.
- Execution profiles (`supervised` default | `trusted` opt-in): the profile is
  visible on the bus (`agentbus profile`) and rewritten into the protocol's
  execution-mode clauses; `task done` and `profile set` stay human-only
  (discipline, not enforcement — see the threat model).
- `agentbus doctor`: read-only installation health check (db, schema,
  daemon liveness, identity, profile, notifier) with fix-up hints.
- `agentbus-mcp`: MCP stdio server (19 tools) — native access from agent
  CLIs that speak MCP; identity resolves through the process ancestor chain,
  `env.AB_AS` as fallback; frame-hardened, pure stdlib.
- `agentbus-discover` console script + systemd user unit template
  (packaging/agentbus-discover.service, paths relative to `$HOME`).
- Packaging: `agent-bus` on PyPI, `agentbus` console script (+ `ab` alias),
  zero runtime dependencies (CI-asserted), Python ≥ 3.11.
- Gates: forbidden-word redaction scanner, zero-dependency assertion,
  ruff + progressive mypy, coverage gate (buslib ≥ 80%).

### Removed (relative to the internal production system)
- Web dashboard (HTTP API + React UI) — returns in v0.2 with the arm plane
  stripped.
- Internal task-execution layer (armlib) and its CLI/MCP surfaces — v0.2 will
  evaluate a generic executor SPI.
- Undocumented mandate (delegated batch-done) subsystem — returns in v0.2 as a
  protocol chapter first, then code; until then `task done` is human-only.

### Security
- Threat model published (docs/threat-model.md); message signing deliberately
  deferred to v0.2 with the full design-collapse argument (ADR-0002).
