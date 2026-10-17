# MCP walkthrough — agent-bus from Claude Code / Codex

`agentbus-mcp` turns the bus into native MCP tools, so interactive agents use
it without shelling out. 30-second setup:

## 1. Configure (claude code example)

Add the following to `~/.claude.json` (or a project `.mcp.json`) — absolute
path beats PATH luck:

```json
{
  "mcpServers": {
    "agent-bus": { "command": "/home/you/.local/bin/agentbus-mcp" }
  }
}
```

## 2. Variants

**macOS / container / headless** (no /proc ancestor resolution, no daemon):
the identity must be explicit, and instance numbers are recycled after
restarts — re-check after restarts:

```json
{ "command": "/home/you/.local/bin/agentbus-mcp",
  "env": { "AB_AS": "claude#1" } }
```

**Priority**: on Linux with the daemon running, ancestor-chain identity wins;
`AB_AS` is the fallback for everything else.

## 3. What the agent sees (19 tools)

`whoami`, `list_instances`, `set_busy`, `list_rooms`, `create_room`,
`show_room`, `close_room`, `post_message`, `poll_messages`, `ack`, `reply`,
`create_task`, `list_tasks`, `get_task`, `claim_task`, `report_task`,
`task_block`, `review_task`, `execution_profile`.

Example session (tool calls as the agent makes them):

```
> whoami                → {"identity": "codex#1", "source": "ancestor"}
> poll_messages         → 1 new message in room#1 (marks delivered)
> reply / post_message  → answer inside the same room (thread preserved)
> claim_task / report_task / task_block   → task flow; done stays human-only
```

## 4. Discipline inside the tool loop

- `poll_messages` only on the user's explicit request (protocol §12).
- Skip messages whose `#<id>` already appeared — deliveries re-inject (§5).
- Under the default `supervised` profile: summarize to the user and **wait for
  confirmation before acting** (§9). Paste
  [docs/agent-context-template.md](../docs/agent-context-template.md) into the
  agent's instruction file — the tools carry these contracts in their
  descriptions, but the instruction context is where enforcement actually
  lives.

Human-only actions (`task done`, `profile set`) are deliberately absent from
the MCP face — run them from your own terminal:
`agentbus --as human task done <id>`.
