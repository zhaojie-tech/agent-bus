"""discover — agent-bus 发现守护。

职责：
- 每 3s 扫 /proc/*/comm + cmdline，识别交互实例：
  * codex = 双进程对：node 包装进程（cmdline 含 /bin/codex）为锚点，
    其原生 codex 子进程合并为同一实例，只编一个号；
  * claude = 单进程（comm=claude / claude.exe）；
  * hermes = python 进程，cmdline 含 hermes 且带 --yolo / --tui 等交互标志
    （gateway/cron/proxy 等非交互子命令排除）。
  * qoder = 单进程（comm=qoderclicn，cmdline[0] 再校验防 comm 伪装）。
  * opencode = 单进程（comm=opencode；run/serve 等非交互子命令排除）。
  排除 claude -p / codex exec / opencode run 等一次性进程、总线自身进程、subagent。
- 编号分配 <工具>#<序号>：按首次发现顺序持久化到 instances 表；pid 消失 →
  state=offline + offline_at，1h 冷却期内编号不回收，冷却期满释放（复用最小空号）。
- 只写 instances 表（自带 sqlite3 直连，不 import buslib，刻意解耦）；
  建表用冻结 DDL 的 instances 子集，CREATE TABLE IF NOT EXISTS 幂等。
- 顺带维护：delivered 超 30min 的 message_reads 退回 new（等价 SQL，权威实现
  在 buslib，合并后对齐）；有新消息的在线实例经 notifier.py 节流通知。

仅用 Python 标准库；守护主循环异常兜底不退出。
"""

import argparse
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .notifier import notify as _notify

# 运行时默认路径；测试一律 --db 指定临时库，严禁写真实 bus.db
BUS_DIR = os.path.expanduser("~/.agent-bus")
DB_PATH = os.path.join(BUS_DIR, "bus.db")

# 冻结 DDL 的 instances 段（与包内 schema/schema.sql 逐字一致，测试断言锁定；
# 幂等建表后由 buslib 接管完整迁移）
INSTANCES_DDL = """
CREATE TABLE IF NOT EXISTS instances(
  pid INTEGER PRIMARY KEY,                  -- 锚点 pid（codex=node 包装进程 pid）
  kind TEXT NOT NULL,                       -- claude / codex / hermes / qoder / opencode
  agent_id TEXT UNIQUE,                     -- codex#2
  tty TEXT, cwd TEXT, cmdline TEXT,
  started_at TEXT, last_seen TEXT,
  state TEXT NOT NULL DEFAULT 'online',     -- online / offline(冷却期)
  offline_at TEXT,
  busy TEXT                                 -- 忙碌自报：理由文本；NULL=空闲（编排者选人依据）
);
"""

# codex 非交互子命令（2026-08-13 实测 `node ~/.npm-global/bin/codex --help`）
CODEX_NON_INTERACTIVE = frozenset(
    {
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "plugin",
        "mcp-server",
        "app-server",
        "remote-control",
        "completion",
        "update",
        "doctor",
        "sandbox",
        "debug",
        "apply",
        "a",
        "archive",
        "delete",
        "unarchive",
        "cloud",
        "exec-server",
        "features",
        "help",
    }
)

# opencode 非交互子命令（`opencode --help` 实测；默认无子命令=TUI，
# run/serve 是 headless 通道、web/acp 等是服务面，均不算交互实例）
OPENCODE_NON_INTERACTIVE = frozenset(
    {
        "run",
        "serve",
        "web",
        "acp",
        "mcp",
        "attach",
        "models",
        "stats",
        "export",
        "import",
        "upgrade",
        "uninstall",
        "completion",
        "debug",
        "providers",
        "auth",
        "agent",
        "session",
        "plugin",
        "plug",
        "db",
        "github",
        "pr",
        "help",
    }
)

# hermes 非交互子命令（2026-08-13 实测 `hermes --help`；chat 是交互命令，不在列）
HERMES_NON_INTERACTIVE = frozenset(
    {
        "model",
        "moa",
        "fallback",
        "secrets",
        "egress",
        "migrate",
        "gateway",
        "proxy",
        "lsp",
        "setup",
        "whatsapp",
        "whatsapp-cloud",
        "slack",
        "send",
        "login",
        "logout",
        "auth",
        "status",
        "pause",
        "resume",
        "cron",
        "sync",
        "webhook",
        "portal",
        "kanban",
        "project",
        "hooks",
        "doctor",
        "verify",
        "security",
        "approvals",
        "dump",
        "debug",
        "backup",
        "checkpoints",
        "import",
        "import-agent",
        "config",
        "skin",
        "console",
        "pairing",
        "skills",
        "bundles",
        "plugins",
        "curator",
        "pets",
        "journey",
        "learning",
        "memory-graph",
        "memory",
        "tools",
        "computer-use",
        "mcp",
        "sessions",
        "insights",
        "monitoring",
        "claw",
        "version",
        "update",
        "uninstall",
        "acp",
        "profile",
        "completion",
        "dashboard",
        "serve",
        "desktop",
        "gui",
        "logs",
        "prompt-size",
    }
)


def utcnow() -> str:
    """当前 UTC 时间，ISO8601 格式（统一 %Y-%m-%dT%H:%M:%SZ）。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> datetime | None:
    """解析 ISO8601 UTC 时间串；空值返回 None。"""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def require_ts(value: str) -> datetime:
    """解析必有值的时间串（内部调用方约定 now 恒为有效串）。"""
    dt = parse_ts(value)
    assert dt is not None
    return dt


def format_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """打开总线 SQLite（WAL），幂等建 instances 表。测试传临时路径。"""
    if db_path != ":memory:":
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(INSTANCES_DDL)
    conn.commit()
    return conn


@dataclass
class Proc:
    """单进程快照（/proc 解析结果）。"""

    pid: int
    ppid: int
    comm: str
    cmdline: list[str]
    tty: str | None = None
    cwd: str | None = None


@dataclass
class Instance:
    """一个被识别的交互实例（codex 双进程已合并，pid 为锚点 pid）。"""

    pid: int
    kind: str
    tty: str | None
    cwd: str | None
    cmdline: str


def _read_file(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def read_comm(pid: int, proc_root: str = "/proc") -> str:
    data = _read_file(os.path.join(proc_root, str(pid), "comm"))
    return data.decode("utf-8", "replace").strip() if data else ""


def read_cmdline(pid: int, proc_root: str = "/proc") -> list[str]:
    data = _read_file(os.path.join(proc_root, str(pid), "cmdline"))
    if data is None:
        return []
    text = data.decode("utf-8", "replace").rstrip("\0")
    return text.split("\0") if text else []


def read_ppid(pid: int, proc_root: str = "/proc") -> int:
    data = _read_file(os.path.join(proc_root, str(pid), "stat"))
    if data is None:
        return 0
    text = data.decode("utf-8", "replace")
    rparen = text.rfind(")")
    if rparen < 0:
        return 0
    fields = text[rparen + 1 :].split()
    if len(fields) < 2:
        return 0
    try:
        return int(fields[1])  # fields[0]=state，fields[1]=ppid
    except (ValueError, IndexError):
        return 0


def read_tty(pid: int, proc_root: str = "/proc") -> str | None:
    """控制终端：readlink /proc/<pid>/fd/0（实测返回 /dev/pts/N）。"""
    try:
        target = os.readlink(os.path.join(proc_root, str(pid), "fd", "0"))
    except OSError:
        return None
    return target if target.startswith("/dev/") else None


def read_cwd(pid: int, proc_root: str = "/proc") -> str | None:
    try:
        return os.readlink(os.path.join(proc_root, str(pid), "cwd"))
    except OSError:
        return None


def scan_procs(proc_root: str = "/proc") -> dict[int, Proc]:
    """扫描 proc 根下所有数字 pid 目录，返回 pid -> Proc 映射。"""
    procs: dict[int, Proc] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return procs
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        cmdline = read_cmdline(pid, proc_root)
        comm = read_comm(pid, proc_root)
        if not comm and cmdline:
            comm = os.path.basename(cmdline[0])
        procs[pid] = Proc(
            pid=pid,
            ppid=read_ppid(pid, proc_root),
            comm=comm,
            cmdline=cmdline,
            tty=read_tty(pid, proc_root),
            cwd=read_cwd(pid, proc_root),
        )
    return procs


def _basename(path: str | None) -> str:
    return os.path.basename(path) if path else ""


def _is_ab_component(args: list[str]) -> bool:
    """排除总线自身进程（agentbus/ab CLI、发现守护、MCP 服务器），
    避免给工具自身编号。

    只按 argv 整 token 匹配（路径后缀或裸名/模块名）——不做子串包含
    判断：agent 的 cmdline 里恰好提到 "agentbus.mcp"（如让某实例解释代码）
    不得把该实例本身排除掉。
    """
    bus_names = (
        "agentbus.cli",
        "agentbus.discover",
        "agentbus.mcp",
        "agentbus-mcp",
        "agentbus-discover",
        "ab-discover",
        "agentbus",
        "ab",
    )
    if not args:
        return False
    first = args[0].rsplit("/", 1)[-1]
    # 只看启动位（argv[0]；解释器/包装器形态再看 argv[1:3]）——cli/cmd
    # 参数里恰好提到某个总线名（如 --add-dir /x/ab）不得排除该实例
    positions = args[1:3] if first.startswith(("python", "env", "nohup", "time")) else []
    for a in [args[0], *positions]:
        if a.rsplit("/", 1)[-1] in bus_names:
            return True
    return False


def identify(procs: dict[int, Proc]) -> list[Instance]:
    """识别交互实例：codex 双进程合并，排除一次性/总线自身/subagent。"""
    instances: list[Instance] = []

    # 1) codex 锚点：node 包装进程，argv[1] 是 codex bin
    codex_anchors: list[Proc] = []
    for proc in procs.values():
        args = proc.cmdline
        if _is_ab_component(args) or len(args) < 2:
            continue
        if _basename(args[0]) != "node" or not args[1].endswith("/bin/codex"):
            continue
        # argv[2] 若是非交互子命令（exec/review/...）则是一次性进程，排除
        sub = args[2] if len(args) > 2 else None
        if sub and not sub.startswith("-") and sub in CODEX_NON_INTERACTIVE:
            continue
        codex_anchors.append(proc)

    # 2) 原生 codex 子进程并入锚点（ppid 指向锚点，实测六对），只编一个号
    for anchor in codex_anchors:
        tty, cwd = anchor.tty, anchor.cwd
        for proc in procs.values():
            if proc.ppid != anchor.pid:
                continue
            is_native = (
                _basename(proc.cmdline[0]) == "codex" if proc.cmdline else proc.comm == "codex"
            )
            if is_native:
                tty = tty or proc.tty
                cwd = cwd or proc.cwd
        instances.append(Instance(anchor.pid, "codex", tty, cwd, " ".join(anchor.cmdline)))

    # 3) claude 单进程（comm=claude），排除 -p/--print 一次性
    claude_pids = set()
    for pid, proc in procs.items():
        if proc.comm not in ("claude", "claude.exe") or _is_ab_component(proc.cmdline):
            continue
        args = proc.cmdline
        # 实测：Claude Code 会把它派生的 shell 命令子进程的 comm 伪装成
        # claude.exe（如 ugrep），必须用 cmdline[0] 再校验，防误判
        if not args or _basename(args[0]) not in ("claude", "claude.exe"):
            continue
        if "-p" in args or any(a == "--print" or a.startswith("--print=") for a in args):
            continue
        claude_pids.add(pid)
    # subagent：父进程也是 claude 候选的子进程排除，保留父实例
    for pid in list(claude_pids):
        if procs[pid].ppid in claude_pids:
            claude_pids.discard(pid)
    for pid in sorted(claude_pids):
        proc = procs[pid]
        instances.append(Instance(pid, "claude", proc.tty, proc.cwd, " ".join(proc.cmdline)))

    # 4) hermes：python 进程 + cmdline 含 hermes + 交互标志（实测 --yolo）
    hermes_pids = set()
    for pid, proc in procs.items():
        args = proc.cmdline
        if not args or _is_ab_component(args):
            continue
        is_python = "python" in _basename(args[0]) or proc.comm in (
            "python",
            "python3",
            "hermes",
        )
        if not is_python or not any("hermes" in a for a in args):
            continue
        sub = args[2] if len(args) > 2 else None
        if sub and not sub.startswith("-") and sub in HERMES_NON_INTERACTIVE:
            continue
        if "--yolo" not in args and "--tui" not in args and sub != "chat":
            continue
        hermes_pids.add(pid)
    # subagent：父进程也是 hermes 候选的子进程排除，保留父实例
    for pid in list(hermes_pids):
        if procs[pid].ppid in hermes_pids:
            hermes_pids.discard(pid)
    for pid in sorted(hermes_pids):
        proc = procs[pid]
        instances.append(Instance(pid, "hermes", proc.tty, proc.cwd, " ".join(proc.cmdline)))

    # 5) qoder 单进程（comm=qoderclicn），cmdline[0] 再校验防 comm 伪装
    qoder_pids = set()
    for pid, proc in procs.items():
        if proc.comm != "qoderclicn" or _is_ab_component(proc.cmdline):
            continue
        args = proc.cmdline
        if not args or _basename(args[0]) != "qoderclicn":
            continue
        qoder_pids.add(pid)
    # subagent：父进程也是 qoder 候选的子进程排除，保留父实例
    for pid in list(qoder_pids):
        if procs[pid].ppid in qoder_pids:
            qoder_pids.discard(pid)
    for pid in sorted(qoder_pids):
        proc = procs[pid]
        instances.append(Instance(pid, "qoder", proc.tty, proc.cwd, " ".join(proc.cmdline)))

    # 6) opencode 单进程（comm=opencode：TUI 与 -s 会话恢复均为单进程形态），
    #    cmdline[0] 再校验防 comm 伪装；非交互子命令排除
    opencode_pids = set()
    for pid, proc in procs.items():
        if proc.comm != "opencode" or _is_ab_component(proc.cmdline):
            continue
        args = proc.cmdline
        if not args or _basename(args[0]) != "opencode":
            continue
        sub = args[1] if len(args) > 1 else None
        if sub and not sub.startswith("-") and sub in OPENCODE_NON_INTERACTIVE:
            continue
        opencode_pids.add(pid)
    # subagent：父进程也是 opencode 候选的子进程排除，保留父实例
    for pid in list(opencode_pids):
        if procs[pid].ppid in opencode_pids:
            opencode_pids.discard(pid)
    for pid in sorted(opencode_pids):
        proc = procs[pid]
        instances.append(Instance(pid, "opencode", proc.tty, proc.cwd, " ".join(proc.cmdline)))

    return instances


def allocate_agent_id(conn: sqlite3.Connection, kind: str, now: str, cooldown_hours: float) -> str:
    """为 kind 分配最小空闲序号；冷却期内 offline 编号视为占用（不回收）。

    冷却期满的判定：offline_at + cooldown <= now，即 offline_at <= cutoff。
    """
    cutoff = require_ts(now) - timedelta(hours=cooldown_hours)
    used = set()
    for row in conn.execute(
        "SELECT agent_id, state, offline_at FROM instances WHERE kind = ?", (kind,)
    ):
        if row["agent_id"] is None:
            continue
        if row["state"] == "online":
            used.add(row["agent_id"])
        elif row["state"] == "offline":
            offline_dt = parse_ts(row["offline_at"])
            if offline_dt is not None and offline_dt > cutoff:
                used.add(row["agent_id"])
    seq = 1
    while f"{kind}#{seq}" in used:
        seq += 1
    return f"{kind}#{seq}"


def update_instances(
    conn: sqlite3.Connection,
    discovered: list[Instance],
    now: str,
    cooldown_hours: float = 1.0,
) -> list[str]:
    """把本轮识别结果同步进 instances 表；返回事件描述列表。"""
    events: list[str] = []

    # 冷却期满的 offline 行删除，释放编号（1h 冷却期内不回收）
    cutoff_iso = format_ts(require_ts(now) - timedelta(hours=cooldown_hours))
    cur = conn.execute(
        "DELETE FROM instances "
        "WHERE state='offline' AND offline_at IS NOT NULL AND offline_at <= ?",
        (cutoff_iso,),
    )
    if cur.rowcount:
        events.append(f"released {cur.rowcount} id(s) (cooldown expired)")

    rows = {row["pid"]: row for row in conn.execute("SELECT * FROM instances")}
    seen_pids = {inst.pid for inst in discovered}

    for inst in discovered:
        row = rows.get(inst.pid)
        if row is None:
            agent_id = allocate_agent_id(conn, inst.kind, now, cooldown_hours)
            conn.execute(
                "INSERT INTO instances"
                "(pid, kind, agent_id, tty, cwd, cmdline, started_at, last_seen,"
                " state, offline_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'online', NULL)",
                (inst.pid, inst.kind, agent_id, inst.tty, inst.cwd, inst.cmdline, now, now),
            )
            events.append(f"new instance {agent_id} pid={inst.pid} tty={inst.tty or '-'}")
        elif row["state"] == "offline":
            if row["kind"] == inst.kind:
                # 同一实例复活：编号稳定，恢复 online
                conn.execute(
                    "UPDATE instances SET state='online', offline_at=NULL,"
                    " tty=?, cwd=?, cmdline=? WHERE pid=?",
                    (inst.tty, inst.cwd, inst.cmdline, inst.pid),
                )
                events.append(f"instance revived {row['agent_id']} pid={inst.pid}")
            else:
                # pid 被其他 kind 复用（极罕见，Linux pid 回绕）：旧行作废，
                # 新实例重新分配编号；旧编号随之释放
                conn.execute("DELETE FROM instances WHERE pid=?", (inst.pid,))
                agent_id = allocate_agent_id(conn, inst.kind, now, cooldown_hours)
                conn.execute(
                    "INSERT INTO instances"
                    "(pid, kind, agent_id, tty, cwd, cmdline, started_at, last_seen,"
                    " state, offline_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'online', NULL)",
                    (inst.pid, inst.kind, agent_id, inst.tty, inst.cwd, inst.cmdline, now, now),
                )
                events.append(f"pid reused: new instance {agent_id} pid={inst.pid}")
        else:
            if row["kind"] != inst.kind:
                # pid 被其他 kind 复用（极罕见）：旧行作废，新实例重新分配编号
                conn.execute("DELETE FROM instances WHERE pid=?", (inst.pid,))
                agent_id = allocate_agent_id(conn, inst.kind, now, cooldown_hours)
                conn.execute(
                    "INSERT INTO instances"
                    "(pid, kind, agent_id, tty, cwd, cmdline, started_at, last_seen,"
                    " state, offline_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'online', NULL)",
                    (inst.pid, inst.kind, agent_id, inst.tty, inst.cwd, inst.cmdline, now, now),
                )
                events.append(f"pid reused: new instance {agent_id} pid={inst.pid}")
            else:
                # 已在线：同步可能漂移的 tty/cwd/cmdline；last_seen 由 ab 心跳负责
                conn.execute(
                    "UPDATE instances SET tty=?, cwd=?, cmdline=? WHERE pid=?",
                    (inst.tty, inst.cwd, inst.cmdline, inst.pid),
                )

    # 本轮未见的 online 实例 → 离线，进入冷却期（编号保留 1h）
    for pid, row in rows.items():
        if row["state"] == "online" and pid not in seen_pids:
            conn.execute(
                "UPDATE instances SET state='offline', offline_at=? WHERE pid=?",
                (now, pid),
            )
            events.append(f"instance offline {row['agent_id']} pid={pid} (id kept during cooldown)")

    conn.commit()
    return events


# 重投跳过"冻结近似"：closed 且 24h 无活动的房，消息留在 delivered
# （权威判定在 buslib._room_frozen——含 poll 与 ack 两个活动信号；守护侧
#  用 closed 超 24h 的保守近似，不查 ack：宁可暂不重投，不可对已冻结房
#  反复打扰/把消息推进不可取状态。差异仅存在于"close 超 24h 但近 24h 有
#  新 ack"的角落——那类房里守护会保守跳过，buslib 侧 API 调用不受影响）
REQUEUE_SQL = (
    "UPDATE message_reads SET status='new', delivered_at=NULL "
    "WHERE status='delivered' AND delivered_at IS NOT NULL AND delivered_at < ? "
    "AND message_id NOT IN ("
    "  SELECT m.id FROM messages m JOIN rooms r ON r.id = m.room_id "
    "  WHERE r.state='closed' AND r.closed_ts IS NOT NULL AND r.closed_ts < ?)"
)


def requeue_stale_delivered(conn: sqlite3.Connection, now: str, minutes: float = 30.0) -> int:
    """delivered 超时（默认 30min）退回 new；message_reads 未建时静默跳过。

    权威实现在 buslib；此处为等价 SQL，合并后以 buslib 为准对齐。
    """
    cutoff = format_ts(require_ts(now) - timedelta(minutes=minutes))
    frozen_cutoff = format_ts(require_ts(now) - timedelta(hours=24))
    try:
        cur = conn.execute(REQUEUE_SQL, (cutoff, frozen_cutoff))
        conn.commit()
        return cur.rowcount
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise


ARCHIVE_DAYS = 30


def archive_stale_rooms(conn: sqlite3.Connection, now: str, days: int = ARCHIVE_DAYS) -> int:
    """物理归档 closed 超 N 天的房（等价于 buslib.archive_closed 的删除顺序；
    守护不 import buslib，保持解耦）。返回归档房数。"""
    cutoff = format_ts(require_ts(now) - timedelta(days=days))
    try:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM rooms WHERE state='closed' AND closed_ts IS NOT NULL "
                "AND closed_ts < ?",
                (cutoff,),
            ).fetchall()
        ]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    for rid in ids:
        for sql, args in (
            (
                "DELETE FROM task_relations WHERE source_id IN "
                "(SELECT id FROM tasks WHERE room_id=?) OR target_id IN "
                "(SELECT id FROM tasks WHERE room_id=?)",
                (rid, rid),
            ),
            ("DELETE FROM tasks WHERE room_id=?", (rid,)),
            ("DELETE FROM messages WHERE room_id=?", (rid,)),
            ("DELETE FROM room_members WHERE room_id=?", (rid,)),
            ("DELETE FROM rooms WHERE id=?", (rid,)),
        ):
            try:
                conn.execute(sql, args)
            except sqlite3.OperationalError as exc:
                # 部分表不存在（守护可先于 buslib 建表运行）——跳过该表
                if "no such table" not in str(exc):
                    raise
    if ids:
        conn.commit()
    return len(ids)


def collect_notifications(
    conn: sqlite3.Connection,
    now: str,
    notify_func: Callable[..., str | None],
    last_notify: dict[str, datetime],
    min_interval: float = 60.0,
) -> int:
    """给有新消息（message_reads.status=new）的在线实例发通知，按实例节流；
    冻结近似（closed 超 24h）的房不打扰（与 requeue 同口径）。"""
    frozen_cutoff = format_ts(require_ts(now) - timedelta(hours=24))
    try:
        agg_rows = conn.execute(
            "SELECT mr.member AS member, COUNT(*) AS n "
            "FROM message_reads mr "
            "JOIN messages m ON m.id = mr.message_id "
            "JOIN instances i ON i.agent_id = mr.member AND i.state = 'online' "
            "WHERE mr.status = 'new' "
            "AND NOT EXISTS (SELECT 1 FROM rooms r WHERE r.id = m.room_id "
            "  AND r.state='closed' AND r.closed_ts IS NOT NULL "
            "  AND r.closed_ts < ?) "
            "GROUP BY mr.member",
            (frozen_cutoff,),
        ).fetchall()
        room_rows = conn.execute(
            "SELECT mr.member AS member, m.room_id AS room_id, r.topic AS topic "
            "FROM message_reads mr "
            "JOIN messages m ON m.id = mr.message_id "
            "JOIN rooms r ON r.id = m.room_id "
            "JOIN instances i ON i.agent_id = mr.member AND i.state = 'online' "
            "WHERE mr.status = 'new' "
            "AND NOT EXISTS (SELECT 1 FROM rooms r2 WHERE r2.id = m.room_id "
            "  AND r2.state='closed' AND r2.closed_ts IS NOT NULL "
            "  AND r2.closed_ts < ?) "
            "GROUP BY mr.member, m.room_id",
            (frozen_cutoff,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise

    rooms_by_member: dict[str, list[str]] = {}
    for row in room_rows:
        rooms_by_member.setdefault(row["member"], []).append(
            f"room#{row['room_id']}[{row['topic'] or ''}]"
        )

    now_dt = require_ts(now)
    notified = 0
    for row in agg_rows:
        member = row["member"]
        last = last_notify.get(member)
        if last is not None and (now_dt - last).total_seconds() < min_interval:
            continue
        inst = conn.execute(
            "SELECT pid, tty, agent_id FROM instances WHERE agent_id = ? AND state = 'online'",
            (member,),
        ).fetchone()
        if inst is None:
            continue
        room_list = rooms_by_member.get(member, [])
        summary = f"[agent-bus] {member} has {row['n']} new message(s)"
        if room_list:
            summary += f" ({', '.join(room_list)})"
        notify_func(dict(inst), summary)
        last_notify[member] = now_dt
        notified += 1
    return notified


def scan_once(
    proc_root: str,
    db_path: str,
    now: str | None = None,
    cooldown_hours: float = 1.0,
    requeue_minutes: float = 30.0,
    notify_func: Callable[..., str | None] = _notify,
    notify_interval: float = 60.0,
    last_notify: dict[str, datetime] | None = None,
) -> dict:
    """执行一轮完整扫描：识别 → 更新 instances → 重投 → 通知。"""
    now = now or utcnow()
    conn = connect(db_path)
    try:
        procs = scan_procs(proc_root)
        discovered = identify(procs)
        events = update_instances(conn, discovered, now, cooldown_hours)
        requeued = requeue_stale_delivered(conn, now, requeue_minutes)
        if requeued:
            events.append(f"requeued {requeued} stale delivered")
        archived = archive_stale_rooms(conn, now)
        if archived:
            events.append(f"archived {archived} closed room(s)")
        notified = collect_notifications(conn, now, notify_func, last_notify or {}, notify_interval)
        if notified:
            events.append(f"notified {notified} instances")
        online = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM instances WHERE state='online' ORDER BY kind, agent_id"
            )
        ]
        cooling = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM instances WHERE state='offline' ORDER BY kind, agent_id"
            )
        ]
    finally:
        conn.close()
    return {
        "online": online,
        "cooling": cooling,
        "events": events,
        "requeued": requeued,
        "archived": archived,
        "notified": notified,
    }


def print_report(result: dict, out=sys.stdout) -> None:
    for row in result["online"]:
        out.write(
            f"[online] {row['agent_id']:<10} pid={row['pid']:<8} "
            f"tty={row['tty'] or '-'} cwd={row['cwd'] or '-'} "
            f"cmd={row['cmdline']}\n"
        )
    for row in result["cooling"]:
        out.write(
            f"[cooling] {row['agent_id']:<10} pid={row['pid']:<8} offline_at={row['offline_at']}\n"
        )
    out.write(
        f"total {len(result['online']) + len(result['cooling'])} instances "
        f"(online {len(result['online'])}, cooling {len(result['cooling'])})\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentbus-discover", description="agent-bus discovery daemon"
    )
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--db", default=DB_PATH, help=f"bus db path (default {DB_PATH})")
    parser.add_argument(
        "--proc", default="/proc", help="proc root (default /proc; injectable for tests)"
    )
    parser.add_argument(
        "--interval", type=float, default=3.0, help="scan interval seconds (default 3)"
    )
    parser.add_argument(
        "--cooldown-hours", type=float, default=1.0, help="id cooldown hours (default 1)"
    )
    parser.add_argument(
        "--requeue-minutes",
        type=float,
        default=30.0,
        help="delivered requeue threshold minutes (default 30)",
    )
    parser.add_argument(
        "--notify-interval",
        type=float,
        default=60.0,
        help="per-instance notify min interval seconds (default 60)",
    )
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be > 0")
    if args.cooldown_hours < 0:
        parser.error("--cooldown-hours must be >= 0")
    if args.requeue_minutes <= 0:
        parser.error("--requeue-minutes must be > 0")
    if args.notify_interval <= 0:
        parser.error("--notify-interval must be > 0")

    if args.once:
        result = scan_once(
            args.proc,
            args.db,
            cooldown_hours=args.cooldown_hours,
            requeue_minutes=args.requeue_minutes,
            notify_interval=args.notify_interval,
        )
        for event in result["events"]:
            print(f"[event] {event}")
        print_report(result, out=sys.stdout)
        return 0

    # 守护主循环：异常兜底不退出，间隔可配（默认 3s）
    last_notify: dict[str, datetime] = {}
    while True:
        try:
            result = scan_once(
                args.proc,
                args.db,
                cooldown_hours=args.cooldown_hours,
                requeue_minutes=args.requeue_minutes,
                notify_interval=args.notify_interval,
                last_notify=last_notify,
            )
            for event in result["events"]:
                print(f"[{utcnow()}] {event}", flush=True)
        except Exception as exc:  # noqa: BLE001 —— 守护永不因单轮异常退出
            print(f"[{utcnow()}] scan error: {exc!r}", file=sys.stderr, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
