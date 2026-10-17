"""mcp — agentbus MCP stdio server (19 tools, pure stdlib).

Transport: MCP stdio — JSON-RPC 2.0, one message per line on stdin/stdout.
stderr is the only logging channel; **stdout must stay frame-pure** (a single
stray print breaks the stream — asserted in tests).

Hardening (DESIGN-OSS-v2 section 1.1):
- lines over MAX_LINE_BYTES are rejected (-32700) and skipped;
- unparseable frames / deep-nested JSON (RecursionError) → -32x error, never a crash;
- every business method before `initialize` is rejected (-32600).

Identity: resolved per tool call via the ancestor-chain (the agent CLI spawns
this server, so the chain reaches the registered instance). Never cached at
initialize — the discovery daemon may not have registered a freshly started
agent yet; per-call resolution self-heals. Broken chains (containers,
double-fork wrappers) fail closed with E_NO_IDENTITY; `env.AB_AS` in the
mcpServers config is the official fallback. Remediation text is MCP-flavoured
(never the CLI `--pid` hint).

Discipline: `poll_messages` carries the consumption / dedup / profile
contracts in its description; the bus enforces neither — see the execution-mode
and consumption-discipline sections of docs/protocol-v3.md.
"""

import argparse
import json
import os
import sys

from . import PROTOCOL_VERSION, __version__
from .buslib import (
    BusError,
    ack_message,
    claim_task,
    close_room,
    connect,
    create_room,
    create_task,
    get_execution_profile,
    get_task,
    init_schema,
    list_rooms,
    list_tasks,
    poll,
    post_message,
    reply_message,
    resolve_identity,
    set_busy,
    show_room,
    task_block,
    task_report,
    task_review,
)

MCP_VERSIONS = {"2025-06-18"}
MAX_LINE_BYTES = 1024 * 1024

_POLL_CONTRACT = (
    "Take new messages for this agent (JSON by room; marks them delivered). "
    "CONTRACTS: (1) call only when the user explicitly asks — polling on your "
    "own initiative is forbidden (protocol section 12); (2) skip any message "
    "whose #<id> already appears in context — unacked deliveries re-inject "
    "after 30min (protocol section 5); (3) execution profile governs what you do "
    "next: supervised = summarize and wait for confirmation (protocol section 9)."
)

_TASK_NOTE = "Claim before work (protocol section 13)."

# MCP-flavoured remedies for identity errors: the CLI's `--pid` hint does not
# exist on the tool surface — tell the caller how to fix it from here.
MCP_REMEDIES = {
    "E_NO_IDENTITY": (
        "E_NO_IDENTITY: cannot resolve identity — start the discover daemon "
        "(agentbus-discover) and retry after ~3s, or set env.AB_AS in the "
        "mcpServers config (headless / container / macOS)"
    ),
    "E_AMBIGUOUS": ("E_AMBIGUOUS: identity is ambiguous — set env.AB_AS in the mcpServers config"),
}


def _s(description, properties, required=None):
    return {
        "name": None,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


def _str(desc):
    return {"type": "string", "description": desc}


def _int(desc):
    return {"type": "integer", "description": desc}


def _bool(desc):
    return {"type": "boolean", "description": desc}


# (name, description, inputSchema properties, required)
_TOOL_DEFS = [
    ("whoami", "Report this agent's bus identity and how it was resolved.", {}, None),
    ("list_instances", "List online registered instances.", {}, None),
    (
        "set_busy",
        "Self-report busy (reason) or idle when cleared. Claiming a "
        "task should be accompanied by a busy report (protocol §13).",
        {
            "reason": _str("what you are working on"),
            "clear": _bool("set idle instead (ignores reason)"),
        },
        None,
    ),
    (
        "list_rooms",
        "List rooms this identity belongs to (unread counts included; stable order by room id).",
        {
            "limit": _int("max rooms to return"),
            "offset": _int("rooms to skip (for paging)"),
        },
        None,
    ),
    (
        "create_room",
        "Create a shared room; creator joins automatically.",
        {
            "topic": _str("room topic"),
            "members": _str("comma-separated member ids, e.g. codex#1,claude#3"),
            "wild": _bool("allow wildcard members like claude:* (default off)"),
        },
        ["topic"],
    ),
    (
        "show_room",
        "Room details; optionally full readable history.",
        {"room_id": _int("room id"), "history": _bool("include all messages")},
        ["room_id"],
    ),
    (
        "close_room",
        "Close a room (read-only afterwards). Refuses while "
        "non-terminal tasks exist unless force=true (then cancels them).",
        {"room_id": _int("room id"), "force": _bool("auto-cancel open tasks and close")},
        ["room_id"],
    ),
    (
        "post_message",
        "Post to a room, or DM via `to`. `at` directs delivery "
        "to mentioned members (message stays in room history).",
        {
            "room_id": _int("target room"),
            "to": _str("DM target id"),
            "body": _str("message body"),
            "at": _str("comma-separated @members"),
        },
        ["body"],
    ),
    (
        "poll_messages",
        _POLL_CONTRACT,
        {
            "since": _int("revision from the previous poll; O(1) empty fast-path when unchanged"),
            "only": _str(
                "receiver-side filter, e.g. "
                "mention+task — @me + my task events + human/system channel"
            ),
        },
        None,
    ),
    (
        "ack",
        "Acknowledge a message (stops redelivery, confirms receipt).",
        {"message_id": _int("message id")},
        ["message_id"],
    ),
    (
        "reply",
        "Reply to a message within its room.",
        {"message_id": _int("message id"), "body": _str("reply body")},
        ["message_id", "body"],
    ),
    (
        "create_task",
        "Create a task in a room; a system message with task:<id> refs is posted automatically.",
        {
            "room_id": _int("room id"),
            "title": _str("task title"),
            "body": _str("requirements/acceptance"),
            "priority": _str("urgent|high|medium|low (default medium)"),
            "label": _str("comma-separated labels"),
            "due": _str("YYYY-MM-DD"),
        },
        ["room_id", "title"],
    ),
    (
        "list_tasks",
        "List tasks in a room (state-machine order).",
        {"room_id": _int("room id"), "status": _str("filter by status")},
        ["room_id"],
    ),
    (
        "get_task",
        "Full task record: fields, relations, comments.",
        {"task_id": _int("task id")},
        ["task_id"],
    ),
    (
        "claim_task",
        "Atomic claim todo→in_progress; E_CONFLICT when raced (re-read, retry once). " + _TASK_NOTE,
        {"task_id": _int("task id")},
        ["task_id"],
    ),
    (
        "report_task",
        "Report completion: in_progress→in_review. NEVER done — done is human-only "
        "(protocol section 13).",
        {"task_id": _int("task id"), "body": _str("changes/verification/risks")},
        ["task_id", "body"],
    ),
    (
        "task_block",
        "Mark blocked (assignee only); reason recorded as comment.",
        {"task_id": _int("task id"), "reason": _str("what blocks it")},
        ["task_id", "reason"],
    ),
    (
        "review_task",
        "Send a task back: in_review→in_progress. No approve branch exists — only "
        "the human reaches done (protocol section 13).",
        {"task_id": _int("task id"), "body": _str("rework requirements")},
        ["task_id", "body"],
    ),
    (
        "execution_profile",
        "Read the execution profile governing what agents "
        "do with received messages: supervised (default; confirm before acting) "
        "or trusted (auto-execute). Read-only — setting is human-only via CLI.",
        {},
        None,
    ),
]

TOOLS = []
for _name, _desc, _props, _req in _TOOL_DEFS:
    _spec = _s(_desc, _props, _req)
    _spec["name"] = _name
    TOOLS.append(_spec)
TOOL_NAMES = {t["name"] for t in TOOLS}
assert len(TOOLS) == 19, len(TOOLS)


def _identity(conn):
    return resolve_identity(conn)


def _mentions(raw):
    return [m.strip() for m in (raw or "").split(",") if m.strip()] or None


class Handlers:
    """Tool implementations; each returns a JSON-serialisable payload.
    BusError propagates to the dispatcher and becomes isError results."""

    def __init__(self, conn):
        self.conn = conn

    def whoami(self, args):
        ident, source = _identity(self.conn)
        return {"identity": ident, "source": source}

    def list_instances(self, args):
        rows = self.conn.execute(
            "SELECT agent_id, kind, tty, cwd, busy FROM instances "
            "WHERE state='online' AND agent_id IS NOT NULL "
            "ORDER BY kind, agent_id"
        ).fetchall()
        return {"instances": [dict(r) for r in rows]}

    def set_busy(self, args):
        ident, _ = _identity(self.conn)
        reason = (args.get("reason") or "").strip()
        if args.get("clear") or not reason:
            set_busy(self.conn, ident, None)
            return {"identity": ident, "busy": None}  # mirrors the db state
        set_busy(self.conn, ident, reason)
        return {"identity": ident, "busy": reason}

    def list_rooms(self, args):
        ident, _ = _identity(self.conn)
        return {
            "rooms": list_rooms(
                self.conn, ident, limit=args.get("limit"), offset=args.get("offset", 0)
            )
        }

    def create_room(self, args):
        ident, _ = _identity(self.conn)
        members = _mentions(args.get("members")) or []
        rid = create_room(self.conn, args["topic"], ident, members, wild=args.get("wild", False))
        return {"room_id": rid}

    def show_room(self, args):
        ident, _ = _identity(self.conn)
        return show_room(self.conn, args["room_id"], ident, history=args.get("history", False))

    def close_room(self, args):
        ident, _ = _identity(self.conn)
        close_room(self.conn, args["room_id"], ident, force=args.get("force", False))
        return {"closed": args["room_id"]}

    def post_message(self, args):
        ident, _ = _identity(self.conn)
        if args.get("room_id") is not None:
            mid = post_message(
                self.conn,
                args["body"],
                ident,
                room_id=args["room_id"],
                mentions=_mentions(args.get("at")),
            )
        else:
            mid = post_message(self.conn, args["body"], ident, to=args.get("to"))
        return {"message_id": mid}

    def poll_messages(self, args):
        ident, _ = _identity(self.conn)
        return poll(self.conn, ident, since=args.get("since"), only=args.get("only"))

    def ack(self, args):
        ident, _ = _identity(self.conn)
        ack_message(self.conn, args["message_id"], ident)
        return {"acked": args["message_id"]}

    def reply(self, args):
        ident, _ = _identity(self.conn)
        mid = reply_message(self.conn, args["message_id"], args["body"], ident)
        return {"message_id": mid}

    def create_task(self, args):
        ident, _ = _identity(self.conn)
        labels = _mentions(args.get("label"))
        tid = create_task(
            self.conn,
            args["room_id"],
            args["title"],
            ident,
            body=args.get("body", ""),
            priority=args.get("priority", "medium"),
            labels=labels,
            due_date=args.get("due"),
        )
        return {"task_id": tid}

    def list_tasks(self, args):
        ident, _ = _identity(self.conn)
        return {"tasks": list_tasks(self.conn, args["room_id"], ident, status=args.get("status"))}

    def get_task(self, args):
        ident, _ = _identity(self.conn)
        return get_task(self.conn, args["task_id"], ident)

    def claim_task(self, args):
        ident, _ = _identity(self.conn)
        task = claim_task(self.conn, args["task_id"], ident)
        return {
            "task_id": task["id"],
            "status": task["status"],
            "assignee": task["assignee"],
            "claim_version": task["claim_version"],
        }

    def report_task(self, args):
        ident, _ = _identity(self.conn)
        task = task_report(self.conn, args["task_id"], ident, args["body"])
        return {"task_id": task["id"], "status": task["status"]}

    def task_block(self, args):
        ident, _ = _identity(self.conn)
        task = task_block(self.conn, args["task_id"], ident, args["reason"])
        return {"task_id": task["id"], "status": task["status"]}

    def review_task(self, args):
        ident, _ = _identity(self.conn)
        task = task_review(self.conn, args["task_id"], ident, args["body"])
        return {"task_id": task["id"], "status": task["status"]}

    def execution_profile(self, args):
        return get_execution_profile(self.conn)


HANDLERS = {name: getattr(Handlers, name) for name, *_ in _TOOL_DEFS}


def _rpc_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


class Server:
    def __init__(self, conn, stdout=None):
        self.conn = conn
        self.stdout = stdout or sys.stdout
        self.handlers = Handlers(conn)
        self.initialized = False

    def send(self, obj):
        self.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.stdout.flush()

    def handle(self, msg):
        """Handle one decoded message; sends the response when one is due.

        Notifications (no `id` member, per JSON-RPC 2.0) never get a reply.
        `initialize` opens the negotiation gate; business methods before it
        are rejected.
        """
        if not isinstance(msg, dict):
            self.send(_rpc_error(None, -32600, "invalid request"))
            return
        method = msg.get("method")
        if method is None:
            if "result" in msg or "error" in msg:
                return  # a response to a server request — we never send any
            if "id" in msg:
                self.send(_rpc_error(msg.get("id"), -32600, "invalid request: missing method"))
            return
        has_id = "id" in msg  # spec: only a missing id member means notification
        msg_id = msg.get("id")
        if method == "initialize":
            params = msg.get("params")
            if params is not None and not isinstance(params, dict):
                if has_id:
                    self.send(_rpc_error(msg_id, -32602, "invalid params"))
                return
            requested = (params or {}).get("protocolVersion")
            version = (
                requested
                if isinstance(requested, str) and requested in MCP_VERSIONS
                else max(MCP_VERSIONS)
            )
            self.initialized = True
            if has_id:
                self.send(
                    _rpc_result(
                        msg_id,
                        {
                            "protocolVersion": version,
                            "capabilities": {"tools": {}},
                            "serverInfo": {
                                "name": "agent-bus",
                                "version": __version__,
                                "protocolVersion": PROTOCOL_VERSION,
                            },
                        },
                    )
                )
            return
        if method == "notifications/initialized":
            return  # client-side ack; the gate opened at initialize
        if not self.initialized:
            if has_id:
                self.send(_rpc_error(msg_id, -32600, "not initialized — call initialize first"))
            return
        if method == "ping":
            if has_id:
                self.send(_rpc_result(msg_id, {}))
            return
        if method == "tools/list":
            if has_id:
                self.send(_rpc_result(msg_id, {"tools": TOOLS}))
            return
        if method == "tools/call":
            params = msg.get("params")
            if params is not None and not isinstance(params, dict):
                if has_id:
                    self.send(_rpc_error(msg_id, -32602, "invalid params"))
                return
            params = params or {}
            name = params.get("name")
            args = params.get("arguments")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                if has_id:
                    self.send(
                        _rpc_error(msg_id, -32602, "invalid params: arguments must be an object")
                    )
                return
            if not has_id:
                return  # notification-form tool call: no reply slot, not executed
            if name not in HANDLERS:
                self.send(
                    _rpc_result(
                        msg_id,
                        {
                            "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                            "isError": True,
                        },
                    )
                )
                return
            try:
                payload = HANDLERS[name](self.handlers, args)
            except BusError as exc:
                text = MCP_REMEDIES.get(exc.code) or f"{exc.code}: {exc.message}"
                self.send(
                    _rpc_result(
                        msg_id,
                        {"content": [{"type": "text", "text": text}], "isError": True},
                    )
                )
                return
            except (KeyError, TypeError, ValueError) as exc:
                # 参数类失败：可自纠，归 E_INVALID（在通用兜底之前）
                self.send(
                    _rpc_result(
                        msg_id,
                        {
                            "content": [
                                {"type": "text", "text": f"E_INVALID: bad arguments ({exc!r})"}
                            ],
                            "isError": True,
                        },
                    )
                )
                return
            except Exception as exc:  # noqa: BLE001 — a handler crash must not
                # kill the server loop: it becomes an isError result instead.
                print(f"agentbus-mcp: tool {name} failed: {exc!r}", file=sys.stderr, flush=True)
                self.send(
                    _rpc_result(
                        msg_id,
                        {
                            "content": [
                                {"type": "text", "text": f"E_INTERNAL: tool call failed ({exc})"}
                            ],
                            "isError": True,
                        },
                    )
                )
                return
            self.send(
                _rpc_result(
                    msg_id,
                    {
                        "content": [
                            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
                        ]
                    },
                )
            )
            return
        if has_id:
            self.send(_rpc_error(msg_id, -32601, f"unknown method: {method}"))

    def serve(self, stdin):
        """Frame loop; returns when stdin is exhausted.

        Bounded reading: never buffers more than MAX_LINE_BYTES + 1 bytes of
        any single frame. An oversized frame is drained to its newline (bytes
        discarded, not kept) and answered with -32700; the stream survives.
        Decoding errors are answered the same way instead of killing the
        process. Pure-ASCII stdout is asserted by tests.
        """
        for raw, oversized in _iter_frames(stdin, MAX_LINE_BYTES):
            if oversized:
                self.send(_rpc_error(None, -32700, "frame too large (max 1 MiB)"))
                continue
            if isinstance(raw, bytes):
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    self.send(_rpc_error(None, -32700, "parse error (invalid utf-8)"))
                    continue
            else:
                line = raw
            try:
                msg = json.loads(line)
            except RecursionError:
                self.send(_rpc_error(None, -32600, "frame nested too deeply"))
                continue
            except ValueError:
                self.send(_rpc_error(None, -32700, "parse error"))
                continue
            self.handle(msg)


def _payload_len(chunk: bytes | str) -> int:
    """Frame length in bytes, excluding a trailing LF/CRLF."""
    raw = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8", "replace")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    return len(raw)


def _ends_line(chunk: bytes | str) -> bool:
    if isinstance(chunk, bytes):
        return chunk.endswith(b"\n")
    return chunk.endswith("\n")


def _iter_frames(stream, limit):
    """Yield (raw, oversized) per line without ever buffering past limit+1.

    Oversize is "frame bytes excluding the trailing newline exceed `limit`" —
    identical for the streaming and fallback paths, so the limit / limit+1
    boundary and CRLF endings behave the same everywhere. Prefers a real
    `readline(limit + 1)`; falls back to plain iteration for line-iterables
    without readline (test doubles). Oversized lines are drained
    chunk-by-chunk to the next newline and yielded once.
    """
    readline = getattr(stream, "readline", None)
    if readline is None:
        for line in stream:
            yield line, _payload_len(line) > limit
        return

    while True:
        # limit + 2: room for payload(limit) + CRLF before the newline check
        chunk = readline(limit + 2)
        if not chunk:
            return
        raw = len(chunk) if isinstance(chunk, bytes) else len(chunk.encode("utf-8", "replace"))
        # Same yardstick on both paths: payload bytes minus a trailing CR/LF.
        # A bare trailing CR (no LF) is stripped here too, so streaming and
        # fallback agree at the boundary in every ending style.
        if _ends_line(chunk) or raw - 1 <= limit or _payload_len(chunk) <= limit:
            yield chunk, _payload_len(chunk) > limit
            continue
        while True:  # oversized: discard the remainder of the line
            more = readline(limit + 2)
            if not more or _ends_line(more):
                break
        yield chunk, True


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="agentbus-mcp", description="agent-bus MCP stdio server (JSON-RPC on stdin/stdout)"
    )
    parser.add_argument(
        "--db", default=None, help="bus db path (default: AB_DB env or ~/.agent-bus/bus.db)"
    )
    args = parser.parse_args(argv)
    if args.db:
        os.environ["AB_DB"] = args.db
    conn = connect()
    init_schema(conn)
    server = Server(conn)
    broken_pipe = False
    try:
        server.serve(sys.stdin.buffer)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        broken_pipe = True
    finally:
        if broken_pipe:
            # 只在断管场景把 fd1 指向 /dev/null，防解释器终刷二次抛错
            # （正常退出不动 stdout——库内调用 main() 不应吞掉调用者输出）
            _silence_stdout()
        conn.close()
    return 0


def _silence_stdout():
    """Point fd 1 at /dev/null before interpreter exit so a broken pipe is
    not re-raised during final flush (would surface as exit 120 + noise)."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
