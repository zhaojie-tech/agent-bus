"""cli — agent-bus 命令行客户端（argparse 薄封装 buslib）。

命令面与退出码分级是冻结契约（规范见 docs/protocol-v3.md）。所有读写自动携带
身份；识别失败报 E_NO_IDENTITY。
退出码：0 成功；2 参数非法（E_INVALID）；3 身份解析失败/歧义
（E_NO_IDENTITY/E_AMBIGUOUS）；4 一般业务错误；5 认领乐观锁冲突（E_CONFLICT）。
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC

from . import PROTOCOL_VERSION, __version__, buslib
from .buslib import (
    IDENTITY_SOURCE_LABEL,
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
    invite_member,
    list_rooms,
    list_tasks,
    poll,
    post_message,
    register_identities,
    remove_member,
    reply_message,
    resolve_identity,
    set_busy,
    set_execution_profile,
    show_room,
    task_block,
    task_cancel,
    task_done,
    task_relation_add,
    task_relation_remove,
    task_reopen,
    task_report,
    task_review,
)
from .doctor import run_checks as _doctor_run_checks

_EXIT_CODES = {"E_INVALID": 2, "E_NO_IDENTITY": 3, "E_AMBIGUOUS": 3, "E_CONFLICT": 5}


def _exit_code_for(code: str) -> int:
    return _EXIT_CODES.get(code, 4)


def _build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--as",
        dest="as_human",
        choices=["human"],
        default=argparse.SUPPRESS,
        help="act as the human superuser identity (highest priority)",
    )
    common.add_argument(
        "--pid",
        type=int,
        metavar="PID",
        default=argparse.SUPPRESS,
        help="pid to start ancestor-chain resolution when ambiguous",
    )

    parser = argparse.ArgumentParser(
        prog="agentbus", parents=[common], description="agent-bus bus CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", parents=[common], help="print the current bus identity")
    sub.add_parser(
        "version", parents=[common], help="print the version two-tuple (package + protocol)"
    )
    sub.add_parser("doctor", parents=[common], help="installation health check (read-only)")
    reg = sub.add_parser(
        "register",
        parents=[common],
        help="register headless instance identities (idempotent; Path B)",
    )
    reg.add_argument(
        "ids",
        nargs="+",
        metavar="TOOL#N",
        help="e.g.: agentbus register codex#1 claude#1",
    )
    lsc = sub.add_parser("ls", parents=[common], help="list online instances")
    lsc.add_argument("--idle", action="store_true", help="only idle instances (busy IS NULL)")
    lsc.add_argument("--json", action="store_true", help="JSON output (for agent parsing)")

    b = sub.add_parser(
        "busy", parents=[common], help="self-report busy (set when taking work; reason optional)"
    )
    b.add_argument("reason", nargs="?", default="busy", help="busy reason")
    b.add_argument("--clear", action="store_true", help="clear busy (same as: free)")
    sub.add_parser("free", parents=[common], help="self-report idle (after finishing work)")

    room = sub.add_parser("room", parents=[common], help="room management")
    rsub = room.add_subparsers(dest="room_command", required=True)

    c = rsub.add_parser("create", parents=[common], help="create a shared room")
    c.add_argument("--topic", required=True, help="room topic")
    c.add_argument("--members", default="", help="comma-separated members (e.g. codex#1,claude#3)")
    c.add_argument(
        "--wild", action="store_true", help="allow wildcard members like claude:* (default off)"
    )

    rls = rsub.add_parser("ls", parents=[common], help="list rooms I belong to")
    rls.add_argument("--all", action="store_true", help="list all rooms (human only)")
    rls.add_argument(
        "--limit", type=int, default=None, metavar="N", help="show at most N rooms (default: all)"
    )
    rls.add_argument(
        "--offset",
        type=int,
        default=0,
        metavar="M",
        help="skip the first M rooms (use with --limit)",
    )

    i = rsub.add_parser("invite", parents=[common], help="invite a member")
    i.add_argument("room", type=int)
    i.add_argument("member")

    cl = rsub.add_parser("close", parents=[common], help="close the room (read-only archive)")
    cl.add_argument("room", type=int)
    cl.add_argument("--force", action="store_true", help="auto-cancel non-terminal tasks and close")

    rm = rsub.add_parser(
        "remove", parents=[common], help="remove a member (clears their unacked reads)"
    )
    rm.add_argument("room", type=int)
    rm.add_argument("member")
    lsc.add_argument(
        "--limit", type=int, default=None, metavar="N", help="show at most N rooms (default: all)"
    )
    lsc.add_argument("--offset", type=int, default=0, metavar="M", help="skip the first M rooms")

    s = rsub.add_parser("show", parents=[common], help="room details")
    s.add_argument("room", type=int)
    s.add_argument("--history", action="store_true", help="full read-only history for members")

    pg = sub.add_parser("post", parents=[common], help="post to a room, or DM")
    where = pg.add_mutually_exclusive_group(required=True)
    where.add_argument("--room", type=int, help="target room id")
    where.add_argument("--to", help="DM target id (auto dm room)")
    pg.add_argument("--body", required=True, help="message body")
    pg.add_argument(
        "--at", default="", help="comma-separated @members: delivery only to the mentioned"
    )

    pg = sub.add_parser("poll", parents=[common], help="take new messages across my rooms (JSON)")
    pg.add_argument(
        "--since",
        type=int,
        default=None,
        help="revision from the last poll; O(1) empty fast-path when unchanged",
    )
    pg.add_argument(
        "--only",
        default=None,
        help="receiver filter, e.g. mention+task: @me + my task events + human/system",
    )

    task = sub.add_parser("task", parents=[common], help="task management (v3)")
    tsub = task.add_subparsers(dest="task_command", required=True)
    tc = tsub.add_parser("create", parents=[common], help="create a task (posts a system message)")
    tc.add_argument("--room", type=int, required=True)
    tc.add_argument("--title", required=True)
    tc.add_argument("--body", default="", help="requirements/acceptance/context")
    tc.add_argument("--priority", default="medium", choices=["urgent", "high", "medium", "low"])
    tc.add_argument("--label", default="", help="comma-separated labels")
    tc.add_argument("--due", default=None, help="YYYY-MM-DD")
    tc.add_argument("--json", action="store_true")
    tl = tsub.add_parser("ls", parents=[common], help="list tasks in a room")
    tl.add_argument("--room", type=int, required=True)
    tl.add_argument("--status", default=None)
    tl.add_argument("--json", action="store_true")
    tg = tsub.add_parser("get", parents=[common], help="full task record (relations + comments)")
    tg.add_argument("task_id", type=int)
    tg.add_argument("--json", action="store_true")
    tcl = tsub.add_parser(
        "claim", parents=[common], help="atomic claim todo→in_progress (optimistic lock)"
    )
    tcl.add_argument("task_id", type=int)
    tcl.add_argument("--json", action="store_true")
    trp = tsub.add_parser(
        "report", parents=[common], help="report completion in_progress→in_review (never done)"
    )
    trp.add_argument("task_id", type=int)
    trp.add_argument("--body", required=True)
    trp.add_argument("--json", action="store_true")
    trv = tsub.add_parser(
        "review", parents=[common], help="send back in_review→in_progress (--back required)"
    )
    trv.add_argument("task_id", type=int)
    trv.add_argument("--back", action="store_true", required=True)
    trv.add_argument("--body", required=True, help="rework requirements")
    trv.add_argument("--json", action="store_true")
    tbl = tsub.add_parser(
        "block", parents=[common], help="mark blocked (assignee only; reason recorded)"
    )
    tbl.add_argument("task_id", type=int)
    tbl.add_argument("--reason", required=True)
    tbl.add_argument("--json", action="store_true")
    tdn = tsub.add_parser(
        "done", parents=[common], help="done (human only; the final-approval entry)"
    )
    tdn.add_argument("task_id", type=int)
    tdn.add_argument(
        "--if-version",
        type=int,
        default=None,
        help="optimistic check: stale claim_version → E_CONFLICT (exit 5)",
    )
    tdn.add_argument("--json", action="store_true")
    tcn = tsub.add_parser(
        "cancel", parents=[common], help="cancel (human only; supervisory fallback)"
    )
    tcn.add_argument("task_id", type=int)
    tcn.add_argument(
        "--if-version",
        type=int,
        default=None,
        help="optimistic check: stale claim_version → E_CONFLICT (exit 5)",
    )
    tcn.add_argument("--json", action="store_true")
    tro = tsub.add_parser(
        "reopen",
        parents=[common],
        help="reopen blocked/canceled→todo, clears assignee (human only)",
    )
    tro.add_argument("task_id", type=int)
    tro.add_argument("--json", action="store_true")
    tup = tsub.add_parser(
        "update", parents=[common], help="edit text fields (title/body/priority/labels/due)"
    )
    tup.add_argument("task_id", type=int)
    tup.add_argument("--title", default=None)
    tup.add_argument("--body", default=None)
    tup.add_argument("--priority", default=None, choices=["urgent", "high", "medium", "low"])
    tup.add_argument("--label", default=None, help="comma-separated labels (replaces existing)")
    tup.add_argument("--due", default=None, help="YYYY-MM-DD")
    tup.add_argument("--json", action="store_true")
    trel = tsub.add_parser(
        "relation",
        parents=[common],
        help="task relations (orchestrator creates, executors read-only)",
    )
    trsub = trel.add_subparsers(dest="relation_command", required=True)
    tra = trsub.add_parser("add", parents=[common])
    tra.add_argument("task_id", type=int)
    tra.add_argument("--type", required=True, choices=["parent", "blocks", "related"])
    tra.add_argument("--target", type=int, required=True)
    tra.add_argument("--json", action="store_true")
    trm = trsub.add_parser("rm", parents=[common])
    trm.add_argument("task_id", type=int)
    trm.add_argument("--type", required=True, choices=["parent", "blocks", "related"])
    trm.add_argument("--target", type=int, required=True)
    trm.add_argument("--json", action="store_true")

    a = sub.add_parser("ack", parents=[common], help="acknowledge a message")
    a.add_argument("message_id", type=int)

    r = sub.add_parser("reply", parents=[common], help="reply within the same room")
    r.add_argument("message_id", type=int)
    r.add_argument("--body", required=True)

    w = sub.add_parser("watch", parents=[common], help="tail my message stream in the foreground")
    w.add_argument("--all", action="store_true", help="all rooms (human only)")

    pf = sub.add_parser(
        "profile", parents=[common], help="execution profile: supervised (default) | trusted"
    )
    psub = pf.add_subparsers(dest="profile_command")
    pset = psub.add_parser("set", parents=[common], help="set the profile (human only; bus-global)")
    pset.add_argument("value", choices=["supervised", "trusted"])

    return parser


def _identity(args, conn):
    return resolve_identity(
        conn, as_human=getattr(args, "as_human", None) == "human", pid=getattr(args, "pid", None)
    )


def _uptime(started_at):
    """存活时长人读化（1h23m / 45s / 2d3h）。"""
    if not started_at:
        return "-"
    try:
        from datetime import datetime

        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ")
        started = started.replace(tzinfo=UTC)
    except ValueError:
        return "-"
    seconds = int(time.time() - started.timestamp())
    if seconds < 0:
        seconds = 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins}m"
    if mins:
        return f"{mins}m{secs}s"
    return f"{secs}s"


def _cmd_whoami(args, conn):
    ident, source = _identity(args, conn)
    label = IDENTITY_SOURCE_LABEL.get(source)
    print(ident if not label else f"{ident} ({label})")
    return 0


def _cmd_version(args, conn):
    print(f"agentbus {__version__} (protocol v{PROTOCOL_VERSION})")
    return 0


def _cmd_doctor(args, conn=None):
    as_human = getattr(args, "as_human", None) == "human"
    pid = getattr(args, "pid", None)
    checks, hard_fail = _doctor_run_checks(conn, as_human=as_human, pid=pid)
    marks = {"ok": "✓", "warn": "!", "skip": "-", "fail": "✗"}
    for c in checks:
        print(f"[{marks[c.status]}] {c.name:<10} {c.detail}")
    if hard_fail:
        print("doctor: FAIL", file=sys.stderr)
        return 1
    print(
        "doctor: OK"
        if not any(c.status == "warn" for c in checks)
        else "doctor: OK (with warnings)"
    )
    return 0


def _cmd_profile(args, conn):
    if getattr(args, "profile_command", None) == "set":
        # set requires the human identity; the profile itself is bus-global,
        # so reading it needs no identity at all.
        ident, _ = _identity(args, conn)
        prof = set_execution_profile(conn, args.value, ident)
        print(
            f"execution profile set to {prof['value']} "
            f"(by {prof['set_by']} at {prof['set_ts']}; bus-global)"
        )
        return 0
    prof = get_execution_profile(conn)
    if prof["default"]:
        print(
            "default: supervised; opt in to trusted with: agentbus --as human profile set trusted"
        )
    else:
        print(f"{prof['value']} (set by {prof['set_by']} at {prof['set_ts']})")
    return 0


def _cmd_ls(args, conn):
    sql = (
        "SELECT kind, agent_id, tty, cwd, started_at, busy "
        "FROM instances WHERE state='online' AND agent_id IS NOT NULL"
    )
    if getattr(args, "idle", False):
        sql += " AND busy IS NULL"
    rows = conn.execute(sql + " ORDER BY kind, agent_id").fetchall()
    if getattr(args, "json", False):
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
        return 0
    if not rows:
        print(
            "(no online instances)" if not getattr(args, "idle", False) else "(no idle instances)"
        )
        return 0
    for r in rows:
        busy = r["busy"] or "-"
        print(
            f"{r['agent_id']}\t{r['kind']}\tbusy={busy}\t{r['tty'] or '-'}\t"
            f"{r['cwd'] or '-'}\t{_uptime(r['started_at'])}"
        )
    return 0


def _cmd_busy(args, conn):
    ident, _ = _identity(args, conn)
    reason = None if getattr(args, "clear", False) else args.reason
    set_busy(conn, ident, reason)
    print(f"{ident} -> idle" if reason is None else f"{ident} -> busy ({reason})")
    return 0


def _cmd_free(args, conn):
    ident, _ = _identity(args, conn)
    set_busy(conn, ident, None)
    print(f"{ident} -> idle")
    return 0


def _cmd_register(args, conn):
    for r in register_identities(conn, args.ids):
        print(f"{r['agent_id']}: {r['status']}")
    print(
        "note: identities age out (~1h) while a discover daemon co-runs; "
        "re-register at script start",
        file=sys.stderr,
    )
    return 0


def _cmd_room_create(args, conn):
    ident, _ = _identity(args, conn)
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    rid = create_room(conn, args.topic, ident, members, wild=args.wild)
    print(rid)
    return 0


def _cmd_room_ls(args, conn):
    ident, _ = _identity(args, conn)
    limit = getattr(args, "limit", None)
    offset = getattr(args, "offset", 0) or 0
    total = len(list_rooms(conn, ident, all_rooms=args.all))
    items = list_rooms(conn, ident, all_rooms=args.all, limit=limit, offset=offset)
    if not items:
        print("(no rooms)")
        return 0
    for r in items:
        tags = []
        if r["state"] == "closed":
            tags.append("closed")
        if r["dm_key"]:
            tags.append("dm")
        if r["stale_dm"]:
            tags.append("stale-member")
        tag = f"[{'|'.join(tags)}] " if tags else ""
        names = ", ".join(m["member"] for m in r["members"])
        print(
            f"room#{r['id']} {tag}{r['topic']}  "
            f"members:{names}  msgs:{r['message_count']}  unread:{r['unread']}"
        )
    if limit is not None and offset + len(items) < total:
        print(
            f"(showing {offset + 1}–{offset + len(items)} of {total}; "
            f"next: --offset {offset + len(items)})"
        )
    return 0


def _cmd_room_invite(args, conn):
    ident, _ = _identity(args, conn)
    invite_member(conn, args.room, args.member, actor=ident)
    print(f"invited {args.member} to room#{args.room}")
    return 0


def _cmd_room_close(args, conn):
    ident, _ = _identity(args, conn)
    close_room(conn, args.room, actor=ident, force=getattr(args, "force", False))
    print(f"room#{args.room} closed (read-only archive)")
    return 0


def _cmd_room_remove(args, conn):
    ident, _ = _identity(args, conn)
    remove_member(conn, args.room, args.member, actor=ident)
    print(f"removed {args.member} from room#{args.room}")
    return 0


def _cmd_room_show(args, conn):
    ident, _ = _identity(args, conn)
    data = show_room(conn, args.room, ident, history=args.history)
    print(f"room#{data['id']} · {data['topic']}")
    print(
        f"state: {data['state']}  created_by: {data['created_by']}  "
        f"created_ts: {data['created_ts']}"
    )
    if data["closed_ts"]:
        print(f"closed_ts: {data['closed_ts']}")
    print("members:")
    for m in data["members"]:
        print(f"  - {m['member']} ({m['role']})")
    print(f"messages: {data['message_count']}  last_ts: {data['last_ts'] or '-'}")
    if args.history:
        print("--- history ---")
        for m in data.get("messages", []):
            reply = f"(reply to #{m['reply_to']})" if m.get("reply_to") else ""
            print(f"[#{m['id']} · {m['sender']} · {m['ts']}] {m['body']}{reply}")
    return 0


def _cmd_post(args, conn):
    ident, _ = _identity(args, conn)
    mentions = [m.strip() for m in (args.at or "").split(",") if m.strip()]
    if args.room is not None:
        mid = post_message(conn, args.body, ident, room_id=args.room, mentions=mentions or None)
    else:
        mid = post_message(conn, args.body, ident, to=args.to)
    print(mid)
    return 0


def _cmd_poll(args, conn):
    ident, _ = _identity(args, conn)
    print(json.dumps(poll(conn, ident, since=args.since, only=args.only), ensure_ascii=False))
    return 0


def _cmd_ack(args, conn):
    ident, _ = _identity(args, conn)
    ack_message(conn, args.message_id, ident)
    print(f"acknowledged #{args.message_id}")
    return 0


# ── v3 任务层命令 ────────────────────────────────────────────────────
def _task_out(args, conn, task_id: int, ident: str) -> None:
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"task": get_task(conn, task_id, ident), "schemaVersion": 1}, ensure_ascii=False
            )
        )
    else:
        task = get_task(conn, task_id, ident)
        print(
            f"task#{task['id']} [{task['status']}] {task['priority']} "
            f"assignee={task['assignee'] or '-'}  {task['title']}"
        )
        if task["relations"]:
            for rel in task["relations"]:
                print(
                    f"  {rel['type']} → #{rel['target_id']} "
                    f"[{rel['target_status']}] {rel['target_title']}"
                )


def _cmd_task_create(args, conn):
    ident, _ = _identity(args, conn)
    labels = [x.strip() for x in args.label.split(",") if x.strip()] or None
    tid = create_task(
        conn,
        args.room,
        args.title,
        ident,
        body=args.body,
        priority=args.priority,
        labels=labels,
        due_date=args.due,
    )
    _task_out(args, conn, tid, ident)
    return 0


def _cmd_task_ls(args, conn):
    ident, _ = _identity(args, conn)
    items = list_tasks(conn, args.room, ident, status=args.status)
    if getattr(args, "json", False):
        print(json.dumps({"tasks": items, "schemaVersion": 1}, ensure_ascii=False))
        return 0
    if not items:
        print("(no tasks in this room)")
        return 0
    for t in items:
        print(
            f"task#{t['id']} [{t['status']}] {t['priority']} "
            f"assignee={t['assignee'] or '-'}  {t['title']}"
        )
    return 0


def _cmd_task_get(args, conn):
    ident, _ = _identity(args, conn)
    _task_out(args, conn, args.task_id, ident)
    return 0


def _cmd_task_claim(args, conn):
    ident, _ = _identity(args, conn)
    task = claim_task(conn, args.task_id, ident)
    _task_out(args, conn, task["id"], ident)
    return 0


def _cmd_task_report(args, conn):
    ident, _ = _identity(args, conn)
    task = task_report(conn, args.task_id, ident, args.body)
    _task_out(args, conn, task["id"], ident)
    return 0


def _cmd_task_review(args, conn):
    ident, _ = _identity(args, conn)
    task = task_review(conn, args.task_id, ident, args.body)
    _task_out(args, conn, task["id"], ident)
    return 0


def _cmd_task_block(args, conn):
    ident, _ = _identity(args, conn)
    task = task_block(conn, args.task_id, ident, args.reason)
    _task_out(args, conn, task["id"], ident)
    return 0


def _cmd_task_done(args, conn):
    ident, _ = _identity(args, conn)
    task = task_done(conn, args.task_id, ident, expected_version=args.if_version)
    _task_out(args, conn, task["id"], ident)
    return 0


def _cmd_task_cancel(args, conn):
    ident, _ = _identity(args, conn)
    task = task_cancel(conn, args.task_id, ident, expected_version=args.if_version)
    _task_out(args, conn, task["id"], ident)
    return 0


def _cmd_task_reopen(args, conn):
    ident, _ = _identity(args, conn)
    task = task_reopen(conn, args.task_id, ident)
    _task_out(args, conn, task["id"], ident)
    return 0


def _cmd_task_update(args, conn):
    changes = {}
    for key in ("title", "body", "priority", "due"):
        if getattr(args, key, None) is not None:
            changes[key] = getattr(args, key)
    if getattr(args, "label", None) is not None:
        changes["labels"] = [x.strip() for x in args.label.split(",") if x.strip()]
    if not changes:
        raise BusError("E_INVALID", "update needs at least one field")
    ident, _ = _identity(args, conn)
    task = buslib.update_task(conn, args.task_id, changes, ident)
    _task_out(args, conn, task["id"], ident)
    return 0


def _cmd_task_relation(args, conn):
    ident, _ = _identity(args, conn)
    if args.relation_command == "add":
        task_relation_add(conn, args.task_id, args.type, args.target, ident)
        msg = f"relation added: task#{args.task_id} -{args.type}-> task#{args.target}"
    else:
        task_relation_remove(conn, args.task_id, args.type, args.target, ident)
        msg = f"relation removed: task#{args.task_id} -{args.type}-> task#{args.target}"
    if getattr(args, "json", False):
        print(json.dumps({"ok": True}, ensure_ascii=False))
    else:
        print(msg)
    return 0


def _cmd_reply(args, conn):
    ident, _ = _identity(args, conn)
    mid = reply_message(conn, args.message_id, args.body, ident)
    print(mid)
    return 0


def _format_message(room, m):
    reply = f"(reply to #{m['reply_to']})" if m.get("reply_to") else ""
    return (
        f"[room#{room['room_id']} · {room['topic']}] "
        f"[#{m['id']} · {m['sender']}] {m['body']}{reply}"
    )


def _cmd_watch(args, conn):
    ident, _ = _identity(args, conn)
    if args.all and ident != "human":
        raise BusError("E_NOT_MEMBER", "--all is human-only")
    print(f"watching as {ident} (Ctrl-C to stop)", file=sys.stderr)
    while True:
        for room in poll(conn, ident)["rooms"]:
            for m in room["messages"]:
                print(_format_message(room, m))
        time.sleep(2)


_DISPATCH = {
    "whoami": _cmd_whoami,
    "version": _cmd_version,
    "profile": _cmd_profile,
    "register": _cmd_register,
    "ls": _cmd_ls,
    "busy": _cmd_busy,
    "free": _cmd_free,
    "post": _cmd_post,
    "poll": _cmd_poll,
    "ack": _cmd_ack,
    "reply": _cmd_reply,
    "watch": _cmd_watch,
}

_TASK_DISPATCH = {
    "create": _cmd_task_create,
    "ls": _cmd_task_ls,
    "get": _cmd_task_get,
    "claim": _cmd_task_claim,
    "report": _cmd_task_report,
    "review": _cmd_task_review,
    "block": _cmd_task_block,
    "done": _cmd_task_done,
    "cancel": _cmd_task_cancel,
    "reopen": _cmd_task_reopen,
    "update": _cmd_task_update,
    "relation": _cmd_task_relation,
}

_ROOM_DISPATCH = {
    "create": _cmd_room_create,
    "ls": _cmd_room_ls,
    "invite": _cmd_room_invite,
    "close": _cmd_room_close,
    "remove": _cmd_room_remove,
    "show": _cmd_room_show,
}


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        # doctor is strictly read-only: no connect()/init_schema() here, so it
        # never creates the database or migrates the schema. run_checks()
        # reports a missing/absent db as a SKIP item instead.
        return _cmd_doctor(args, None)  # identity flags forwarded in _cmd_doctor
    conn = connect()
    try:
        init_schema(conn)
        if args.command == "room":
            return _ROOM_DISPATCH[args.room_command](args, conn)
        if args.command == "task":
            return _TASK_DISPATCH[args.task_command](args, conn)
        return _DISPATCH[args.command](args, conn)
    finally:
        conn.close()


def run():
    try:
        sys.exit(main(sys.argv[1:]))
    except BusError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.code in ("E_NO_IDENTITY", "E_AMBIGUOUS"):
            print(
                "hint: from your own terminal use --as human; agent shells "
                "resolve via the discover daemon (start agentbus-discover, "
                "retry after ~3s) or set AB_AS",
                file=sys.stderr,
            )
        sys.exit(_exit_code_for(exc.code))
    except sqlite3.Error as exc:
        # db-level failure (e.g. lock contention past busy_timeout) converges
        # into the documented exit-code family (4 = general business error)
        # instead of a bare traceback
        print(f"E_DB: {exc}", file=sys.stderr)
        sys.exit(4)
    except OverflowError:
        # ids beyond SQLite's 64-bit integer range — an argument problem,
        # not a crash (documented exit-code family: 2 = E_INVALID)
        print("E_INVALID: numeric argument out of range", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    run()
