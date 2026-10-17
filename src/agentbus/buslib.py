"""buslib — agent-bus 核心库。只依赖标准库。

职责：
  - SQLite 连接助手（WAL / busy_timeout / synchronous=NORMAL / foreign_keys）
  - 冻结 schema 初始化（权威 DDL 为包内 schema/schema.sql 资源，本文件内嵌同文兜底）
  - 身份解析（--as human / AB_AS=human > 祖先链法 > tty 回退法）
  - room CRUD（create/invite/close/show，invite 自动 system 摘要消息）
  - dm 确定性键（成员编号字典序排序 dm:a+b，INSERT OR IGNORE 防并发分裂）
  - post 带 fan-out（为当时每个成员建 message_reads=new）
  - poll（按 room 分组、置 delivered、标 delivered_at）/ ack / reply
  - delivered 30min 超时重投、closed room 三阶段、成员移除清理、权限校验
"""

import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from importlib.resources import files as _resource_files

# ── 路径与常量 ────────────────────────────────────────────────────────────
DEFAULT_DB_PATH = os.path.expanduser("~/.agent-bus/bus.db")
SCHEMA_RESOURCE = _resource_files(__package__) / "schema" / "schema.sql"

REDELIVER_SECONDS = 30 * 60  # delivered 重投阈值 30min
FREEZE_SECONDS = 24 * 60 * 60  # closed 冻结 24h
ARCHIVE_DAYS = 30  # 归档 30 天
ROOM_SOFT_CAP = 500  # 每 room 软上限 500 条
VALID_KINDS = ("claude", "codex", "hermes")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# 冻结 DDL 的内嵌兜底副本（与包内 schema/schema.sql 资源逐字一致，测试会断言两者相同）。
# 运行时优先读包资源；资源缺失（如单文件分发）时用此副本。
_FALLBACK_SCHEMA_DDL = """\
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS rooms(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',       -- open / closed
  created_by TEXT, created_ts TEXT, closed_ts TEXT,
  dm_key TEXT UNIQUE                        -- dm 确定性键；普通 room 为 NULL(多个 NULL 可共存)
);
CREATE TABLE IF NOT EXISTS room_members(
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  member  TEXT NOT NULL,                    -- codex#1 / human / claude:*(通配,--wild 才开)
  role    TEXT NOT NULL DEFAULT 'member',   -- creator / member
  joined_ts TEXT,
  PRIMARY KEY(room_id, member)
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  sender TEXT NOT NULL,                     -- 编号/human/system（不可认证）
  body TEXT NOT NULL, refs TEXT, reply_to INTEGER
);
CREATE TABLE IF NOT EXISTS message_reads(
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  member TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',       -- new / delivered / acked
  delivered_at TEXT,                        -- first delivery time (never overwritten)
  acked_at TEXT,                            -- ack time (frozen-room activity signal)
  PRIMARY KEY(message_id, member)
);
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
CREATE INDEX IF NOT EXISTS idx_reads_status ON message_reads(status);
CREATE INDEX IF NOT EXISTS idx_msg_room     ON messages(room_id);

-- ════════════════════════════════════════════════════════════════════
-- v3 新增（不违反冻结契约「禁止增删列」）：
--   表级变更计数器 global_revision + 任务层 tasks/task_relations + 触发器
-- ════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS global_revision(
  table_name TEXT PRIMARY KEY,               -- rooms / messages / tasks / instances 四行
  revision   INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
INSERT OR IGNORE INTO global_revision(table_name) VALUES
  ('rooms'), ('messages'), ('tasks'), ('instances');

CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  body  TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'todo'
    CHECK (status IN ('todo','in_progress','in_review','blocked','done','canceled')),
  priority TEXT NOT NULL DEFAULT 'medium'
    CHECK (priority IN ('urgent','high','medium','low')),
  labels TEXT NOT NULL DEFAULT '[]',         -- JSON 数组 tag 语义（hold/返工/for-claude）
  due_date TEXT,                             -- ISO 日期 YYYY-MM-DD，可空
  assignee TEXT,                             -- 实例编号；NULL=未认领
  claim_version INTEGER NOT NULL DEFAULT 1 CHECK (claim_version > 0),
  created_by TEXT, created_ts TEXT, closed_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_room_status ON tasks(room_id, status, priority);

CREATE TABLE IF NOT EXISTS task_relations(
  source_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  target_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  type TEXT NOT NULL CHECK (type IN ('parent','blocks','related')),
  PRIMARY KEY (source_id, target_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS task_relations_one_parent
  ON task_relations(source_id) WHERE type = 'parent';   -- 每任务至多一个父

-- v0.1 开源化新增（只加表纪律，不改既有列）：meta 键值表。
-- protocol_version 供客户端版本协商；历史库无此行时按未标注处理。
CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta(key, value) VALUES('protocol_version', '3');

-- 表级 revision 触发器（message_reads 不 bump：投递状态是 agent 私有语义；
--   instances 只 bump state/busy 变更，last_seen/tty/cwd/cmdline 心跳不 bump）
CREATE TRIGGER IF NOT EXISTS trg_rooms_rev_ins AFTER INSERT ON rooms BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='rooms'; END;
CREATE TRIGGER IF NOT EXISTS trg_rooms_rev_upd AFTER UPDATE ON rooms BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='rooms'; END;
CREATE TRIGGER IF NOT EXISTS trg_rooms_rev_del AFTER DELETE ON rooms BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='rooms'; END;
CREATE TRIGGER IF NOT EXISTS trg_members_rev_ins AFTER INSERT ON room_members BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='rooms'; END;
CREATE TRIGGER IF NOT EXISTS trg_members_rev_del AFTER DELETE ON room_members BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='rooms'; END;
CREATE TRIGGER IF NOT EXISTS trg_members_rev_upd AFTER UPDATE ON room_members BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='rooms'; END;
CREATE TRIGGER IF NOT EXISTS trg_msg_rev_ins AFTER INSERT ON messages BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='messages'; END;
CREATE TRIGGER IF NOT EXISTS trg_msg_rev_upd AFTER UPDATE ON messages BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='messages'; END;
CREATE TRIGGER IF NOT EXISTS trg_msg_rev_del AFTER DELETE ON messages BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='messages'; END;
CREATE TRIGGER IF NOT EXISTS trg_task_rev_ins AFTER INSERT ON tasks BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='tasks'; END;
CREATE TRIGGER IF NOT EXISTS trg_task_rev_upd AFTER UPDATE ON tasks BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='tasks'; END;
CREATE TRIGGER IF NOT EXISTS trg_task_rev_del AFTER DELETE ON tasks BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='tasks'; END;
CREATE TRIGGER IF NOT EXISTS trg_trel_rev_ins AFTER INSERT ON task_relations BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='tasks'; END;
CREATE TRIGGER IF NOT EXISTS trg_trel_rev_del AFTER DELETE ON task_relations BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='tasks'; END;
CREATE TRIGGER IF NOT EXISTS trg_inst_rev_ins AFTER INSERT ON instances BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='instances'; END;
CREATE TRIGGER IF NOT EXISTS trg_inst_rev_del AFTER DELETE ON instances BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='instances'; END;
CREATE TRIGGER IF NOT EXISTS trg_inst_rev_upd AFTER UPDATE OF state, busy ON instances BEGIN
  UPDATE global_revision SET revision=revision+1 WHERE table_name='instances'; END;
"""


class BusError(Exception):
    """业务错误：code 为 E_<CODE>（如 E_NOT_MEMBER），CLI 捕获后写 stderr 退出。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ── 时间与连接助手 ────────────────────────────────────────────────────────
def utcnow() -> str:
    """当前 UTC 时间（冻结格式 %Y-%m-%dT%H:%M:%SZ；同格式字典序即时间序）。"""
    return datetime.now(UTC).strftime(TIME_FORMAT)


def _ago(seconds: int) -> str:
    """N 秒前的 UTC 时间字符串（用于超时比较）。"""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime(TIME_FORMAT)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开总线连接（WAL / busy_timeout / synchronous=NORMAL / foreign_keys）。

    db_path 缺省时依次取 AB_DB 环境变量（测试用）→ ~/.agent-bus/bus.db。
    测试一律 connect("/tmp/xxx-test.db") 或设置 AB_DB，严禁写真实 DB_PATH。
    """
    if db_path is None:
        db_path = os.environ.get("AB_DB") or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        try:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise BusError("E_DB", f"cannot prepare bus directory {parent}: {exc}") from exc
    try:
        conn = sqlite3.connect(db_path, timeout=10)
    except sqlite3.Error as exc:
        raise BusError("E_DB", f"cannot open bus db {db_path}: {exc}") from exc
    # 拒绝 umask 默认（常见 0644）：库内有他进程 cmdline 等敏感采集数据
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass  # 只读介质等场景下尽力而为；doctor 会报告实际权限

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")  # CASCADE 依赖此前置
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """按冻结 DDL 幂等建表（生产建表职责归 buslib，可反复调用）。"""
    ddl = (
        SCHEMA_RESOURCE.read_text(encoding="utf-8")
        if SCHEMA_RESOURCE.is_file()
        else _FALLBACK_SCHEMA_DDL
    )
    conn.executescript(ddl)
    # 旧库迁移：CREATE IF NOT EXISTS 不会给已存在的表补列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)")}
    if "busy" not in cols:
        conn.execute("ALTER TABLE instances ADD COLUMN busy TEXT")
        conn.commit()
    read_cols = {r[1] for r in conn.execute("PRAGMA table_info(message_reads)")}
    if "acked_at" not in read_cols:
        conn.execute("ALTER TABLE message_reads ADD COLUMN acked_at TEXT")
        conn.commit()


# ── 身份解析（两级：祖先链法 + tty 回退法）──────────────────────────────
IDENTITY_SOURCE_LABEL = {
    "flag": "via --as human",
    "env": "via AB_AS=human",
    "ancestor": "via ancestor-chain (pid match)",
    "tty": "via tty fallback",
}


def _proc_ppid(pid: int) -> int:
    """读 /proc/<pid>/stat 取 ppid；异常一律返回 0（链终止）。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except OSError:
        return 0
    rp = data.rfind(")")
    if rp < 0:
        return 0
    fields = data[rp + 1 :].split()
    if len(fields) < 2:
        return 0
    try:
        return int(fields[1])
    except ValueError:
        return 0


def resolve_identity(
    conn,
    *,
    as_human: bool = False,
    pid: int | None = None,
    environ=None,
    ppid_of=None,
    ttyname_of=None,
):
    """身份解析，返回 (identity, source)。

    优先级：--as human 显式参数 > AB_AS=human 环境变量 > 祖先链法 > tty 回退法。
    tty 回退多个实例同 tty 时报 E_AMBIGUOUS（提示加 --pid）；全无匹配报 E_NO_IDENTITY。
    ppid_of / ttyname_of 供测试注入，缺省走真实 /proc 与终端。
    """
    env = os.environ if environ is None else environ
    if as_human:
        return "human", "flag"
    # AB_AS 显式身份：human 沿用旧语义；任意身份供自动化编排为无终端的一次性
    # worker 注入（worker 无实例祖先/无 tty，祖先链与 tty 回退均无法识别）。
    # 伪造风险是 sender 不可认证的同 UID 残余风险，见 docs/threat-model.md。
    ab_as = (env.get("AB_AS") or "").strip()
    if ab_as:
        return ab_as, "env"

    # ① 祖先链法：沿 ppid 链找 instances 表里的锚点 pid（agent 执行的命令必经该链）
    walker = ppid_of or _proc_ppid
    cur = os.getpid() if pid is None else pid
    seen = set()
    while cur > 0:
        if cur in seen:
            break
        seen.add(cur)
        row = conn.execute(
            "SELECT kind, agent_id FROM instances WHERE pid=? AND state='online'", (cur,)
        ).fetchone()
        if row and row["agent_id"]:
            return row["agent_id"], "ancestor"
        cur = walker(cur)

    # ② tty 回退法：人工手敲场景，取当前终端对应的 online 实例
    try:
        tty = ttyname_of() if ttyname_of is not None else os.ttyname(0)
    except OSError as exc:
        raise BusError(
            "E_NO_IDENTITY", "cannot resolve identity: no ancestor match and no controlling tty"
        ) from exc
    rows = conn.execute(
        "SELECT pid, kind, agent_id FROM instances "
        "WHERE tty=? AND state='online' AND agent_id IS NOT NULL",
        (tty,),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["agent_id"], "tty"
    if len(rows) > 1:
        raise BusError("E_AMBIGUOUS", f"multiple instances on {tty}; ambiguous — pass --pid")
    raise BusError("E_NO_IDENTITY", "cannot resolve identity: no ancestor or tty match")


# ── room / 成员 基础 ──────────────────────────────────────────────────────
def _get_room(conn, room_id: int):
    room = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room:
        raise BusError("E_NOT_FOUND", f"room#{room_id} not found")
    return room


def _room_members(conn, room_id: int) -> list:
    rows = conn.execute(
        "SELECT member, role, joined_ts FROM room_members WHERE room_id=? ORDER BY rowid",
        (room_id,),
    ).fetchall()
    return [{"member": r["member"], "role": r["role"], "joined_ts": r["joined_ts"]} for r in rows]


def _member_kind(identity: str) -> str:
    return identity.split("#", 1)[0] if "#" in identity else identity


def is_member(conn, room_id: int, identity: str) -> bool:
    """成员判定：human 是超集成员；agent 按精确编号或 kind 通配匹配。"""
    if identity == "human":
        return True
    kind = _member_kind(identity)
    row = conn.execute(
        "SELECT 1 FROM room_members WHERE room_id=? AND (member=? OR member=?) LIMIT 1",
        (room_id, identity, f"{kind}:*"),
    ).fetchone()
    return row is not None


def _instance_exists(conn, agent_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM instances WHERE agent_id=? LIMIT 1", (agent_id,)).fetchone()
        is not None
    )


_INSTANCE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*(#\d+)?$")


def register_identities(conn, ids: list[str]) -> list[dict]:
    """注册 headless 身份（Path B：无守护的脚本/容器场景）。

    幂等：已存在（任意状态）的 id 置回 online；新 id 以负合成 pid 插入
    （discover 的 /proc 扫描不会见到负 pid——有守护共存时该行会按冷却
    机制老化，脚本应在启动时重新 register）。`human` 保留名不可注册。
    """
    results = []
    for raw in ids:
        ident = raw.strip()
        if ident == "human" or not _INSTANCE_ID_RE.match(ident):
            raise BusError(
                "E_INVALID",
                f"invalid instance id {raw!r} (expected tool#N, lowercase)",
            )
        row = conn.execute("SELECT pid FROM instances WHERE agent_id=?", (ident,)).fetchone()
        if row:
            conn.execute(
                "UPDATE instances SET state='online', offline_at=NULL WHERE agent_id=?", (ident,)
            )
            results.append({"agent_id": ident, "status": "online"})
        else:
            pid = (
                conn.execute("SELECT MIN(pid) FROM instances WHERE pid < 0").fetchone()[0] or 0
            ) - 1
            conn.execute(
                "INSERT INTO instances(pid, kind, agent_id, state) VALUES(?,?,?,'online')",
                (pid, ident.split("#", 1)[0], ident),
            )
            results.append({"agent_id": ident, "status": "registered"})
    conn.commit()
    return results


def set_busy(conn, agent_id: str, reason: str | None = None) -> None:
    """自报忙碌状态：reason 非空=忙碌（存理由），空/None=空闲（编排者选人依据）。

    discover 只 UPDATE 指定列，busy 不会被巡检覆盖；实例复活（pid 复用重建行）
    时 busy 随旧行丢弃，视为空闲——新进程本就无旧任务在身。
    """
    if not _instance_exists(conn, agent_id):
        raise BusError("E_NOT_FOUND", f"instance {agent_id} not found")
    busy = (reason or "").strip() or None
    conn.execute("UPDATE instances SET busy=? WHERE agent_id=?", (busy, agent_id))
    conn.commit()


def _validate_member(conn, member: str, wild: bool = False) -> None:
    """校验成员名合法：human / 已注册编号 / kind 通配。"""
    if member == "human":
        return
    if member.endswith(":*"):
        if not wild:
            raise BusError("E_INVALID", f"wildcard member {member} requires explicit --wild")
        kind = member[:-2]
        if kind not in VALID_KINDS:
            raise BusError("E_INVALID", f"unknown instance kind {member}")
        return
    if not _instance_exists(conn, member):
        raise BusError("E_NOT_FOUND", f"instance {member} not found (run agentbus ls)")


def visible_rooms(conn, identity: str) -> list:
    """我可感知的 room 列表（human 全量；agent 按成员表裁决）。"""
    if identity == "human":
        return conn.execute("SELECT * FROM rooms ORDER BY id").fetchall()
    kind = _member_kind(identity)
    return conn.execute(
        "SELECT DISTINCT r.* FROM rooms r JOIN room_members m ON m.room_id=r.id "
        "WHERE m.member=? OR m.member=? ORDER BY r.id",
        (identity, f"{kind}:*"),
    ).fetchall()


def create_room(conn, topic: str, creator: str, members=None, wild: bool = False) -> int:
    """新建共享会话，返回 room id。创建者自动入会（role=creator）。"""
    topic = (topic or "").strip()
    if not topic:
        raise BusError("E_INVALID", "topic cannot be empty")
    ms = []
    for raw in members or []:
        m = (raw or "").strip()
        if m and m not in ms:
            ms.append(m)
    for m in ms:
        _validate_member(conn, m, wild=wild)
    if creator not in ms:
        ms.append(creator)
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO rooms(topic, state, created_by, created_ts) VALUES(?,?,?,?)",
        (topic, "open", creator, now),
    )
    rid = cur.lastrowid
    for m in ms:
        role = "creator" if m == creator else "member"
        conn.execute(
            "INSERT INTO room_members(room_id, member, role, joined_ts) VALUES(?,?,?,?)",
            (rid, m, role, now),
        )
    conn.commit()
    return rid


def _history_summary(conn, room_id: int, limit: int = 20) -> str:
    """入会前最近 N 条消息摘要（invite 用，给新成员交接上下文）。"""
    rows = conn.execute(
        "SELECT ts, sender, body FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ?",
        (room_id, limit),
    ).fetchall()
    if not rows:
        return "(no history yet)"
    lines = []
    for r in reversed(rows):
        body = (r["body"] or "").replace("\n", " ")
        if len(body) > 160:
            body = body[:160] + "…"
        lines.append(f"[{r['ts']}] {r['sender']}: {body}")
    return "\n".join(lines)


def invite_member(conn, room_id: int, member: str, actor: str) -> None:
    """邀请成员入会，并自动给新成员生成 system 摘要消息。幂等（已入会则不动）。"""
    room = _get_room(conn, room_id)
    if room["dm_key"]:
        raise BusError("E_INVALID", "dm rooms are fixed pairs; no third member allowed")
    if room["state"] != "open":
        raise BusError("E_ROOM_CLOSED", f"room#{room_id} is closed; cannot invite")
    if not is_member(conn, room_id, actor):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{room_id}; cannot invite")
    _validate_member(conn, member, wild=False)
    if conn.execute(
        "SELECT 1 FROM room_members WHERE room_id=? AND member=?", (room_id, member)
    ).fetchone():
        return  # 幂等：已入会则不重复发摘要
    now = utcnow()
    conn.execute(
        "INSERT INTO room_members(room_id, member, role, joined_ts) VALUES(?,?,?,?)",
        (room_id, member, "member", now),
    )
    members_txt = ", ".join(m["member"] for m in _room_members(conn, room_id))
    body = (
        f"[invite] You were invited to room#{room_id} ({room['topic']}).\n"
        f"Members: {members_txt}\n"
        f"Recent messages (up to 20):\n{_history_summary(conn, room_id)}"
    )
    _insert_message(conn, room_id, "system", body, fan_out_targets=[member])
    conn.commit()


def close_room(conn, room_id: int, actor: str, force: bool = False) -> None:
    """关闭会话（只读归档）。幂等：已 closed 直接成功。

    v3：room 有未终态任务时拒绝（E_TASK_OPEN）；force=True 自动 cancel 未终态任务
    并发 system 消息（协议任务层关房条目，见 docs/protocol-v3.md）。
    """
    room = _get_room(conn, room_id)
    if not is_member(conn, room_id, actor):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{room_id}; cannot close")
    if room["state"] == "closed":
        return
    if not force:
        assert_room_closable(conn, room_id)
    else:
        cancel_open_tasks(conn, room_id)
    conn.execute("UPDATE rooms SET state='closed', closed_ts=? WHERE id=?", (utcnow(), room_id))
    conn.commit()


def remove_member(conn, room_id: int, member: str, actor: str) -> None:
    """移除成员，并清其在 open room 的未 ack reads（视为放弃）。"""
    room = _get_room(conn, room_id)
    if room["state"] != "open":
        raise BusError("E_ROOM_CLOSED", f"room#{room_id} is closed; cannot remove members")
    if not is_member(conn, room_id, actor):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{room_id}; cannot remove")
    if not conn.execute(
        "SELECT 1 FROM room_members WHERE room_id=? AND member=?", (room_id, member)
    ).fetchone():
        raise BusError("E_NOT_FOUND", f"{member} is not a member of room#{room_id}")
    conn.execute("DELETE FROM room_members WHERE room_id=? AND member=?", (room_id, member))
    conn.execute(
        "DELETE FROM message_reads WHERE member=? AND status!='acked' "
        "AND message_id IN (SELECT id FROM messages WHERE room_id=?)",
        (member, room_id),
    )
    conn.commit()


def show_room(conn, room_id: int, viewer: str, history: bool = False) -> dict:
    """房间详情；--history 时附成员可见的全量历史（只读）。"""
    room = _get_room(conn, room_id)
    if not is_member(conn, room_id, viewer):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{room_id}; cannot view")
    stats = conn.execute(
        "SELECT COUNT(*) AS n, MAX(ts) AS last FROM messages WHERE room_id=?", (room_id,)
    ).fetchone()
    data = {
        "id": room["id"],
        "topic": room["topic"],
        "state": room["state"],
        "created_by": room["created_by"],
        "created_ts": room["created_ts"],
        "closed_ts": room["closed_ts"],
        "dm_key": room["dm_key"],
        "members": _room_members(conn, room_id),
        "message_count": stats["n"],
        "last_ts": stats["last"],
    }
    if history:
        rows = conn.execute(
            "SELECT id, ts, sender, body, refs, reply_to FROM messages WHERE room_id=? ORDER BY id",
            (room_id,),
        ).fetchall()
        data["messages"] = [dict(r) for r in rows]
    return data


def _unread_count(conn, room_id: int, identity: str) -> int:
    if identity == "human":
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages m WHERE m.room_id=? "
            "AND NOT EXISTS (SELECT 1 FROM message_reads r WHERE r.message_id=m.id "
            "AND r.member='human' AND r.status IN ('delivered','acked'))",
            (room_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM message_reads r "
            "JOIN messages m ON m.id=r.message_id "
            "WHERE m.room_id=? AND r.member=? AND r.status='new'",
            (room_id, identity),
        ).fetchone()
    return row["n"]


def _dm_stale(conn, members: list) -> bool:
    """dm 房含当前不在线的失效成员 → 标灰提示。"""
    for m in members:
        name = m["member"]
        if name == "human" or name.endswith(":*"):
            continue
        if not conn.execute(
            "SELECT 1 FROM instances WHERE agent_id=? AND state='online' LIMIT 1", (name,)
        ).fetchone():
            return True
    return False


def list_rooms(
    conn, identity: str, all_rooms: bool = False, limit: int | None = None, offset: int = 0
) -> list:
    """我所属的 room 列表；--all 仅 human 可用（看全部会话）。"""
    if all_rooms and identity != "human":
        raise BusError("E_NOT_MEMBER", "--all is human-only")
    out = []
    for room in visible_rooms(conn, identity):
        rid = room["id"]
        members = _room_members(conn, rid)
        stats = conn.execute(
            "SELECT COUNT(*) AS n, MAX(ts) AS last FROM messages WHERE room_id=?", (rid,)
        ).fetchone()
        item = {
            "id": rid,
            "topic": room["topic"],
            "state": room["state"],
            "dm_key": room["dm_key"],
            "created_ts": room["created_ts"],
            "closed_ts": room["closed_ts"],
            "members": members,
            "message_count": stats["n"],
            "last_ts": stats["last"],
            "unread": _unread_count(conn, rid, identity),
        }
        item["stale_dm"] = bool(room["dm_key"]) and _dm_stale(conn, members)
        out.append(item)
    if limit is not None:
        out = out[offset : offset + limit]  # 稳定序（按 room id）
    return out


# ── 消息核心：dm / post fan-out / poll / ack / reply ─────────────────────
def dm_key(a: str, b: str) -> str:
    """dm 确定性键：成员编号字典序排序生成 dm:a+b（谁发起都一样）。"""
    return "dm:" + "+".join(sorted((a, b)))


def ensure_dm_room(conn, a: str, b: str) -> int:
    """查找/创建双方二人 dm 房（INSERT OR IGNORE + UNIQUE 防双向并发分裂）。"""
    if a == b:
        raise BusError("E_INVALID", "cannot DM yourself")
    if b.endswith(":*"):
        raise BusError("E_INVALID", "wildcard members cannot be DM targets")
    if b != "human" and not _instance_exists(conn, b):
        raise BusError("E_NOT_FOUND", f"instance {b} not found")
    base = dm_key(a, b)
    # Generation keys: a closed dm room, or one whose participant is a
    # recycled instance number (started_at later than the room — a new
    # process inheriting the id), is left behind and the next generation
    # takes over. Matches the protocol: stale dm rooms stay readable, talk
    # continues in a fresh room.
    rid = None
    # 复用现存的最大代 open 房；不复用空出来的旧代键——否则归档旧代后
    # 会凭空再建一个同族 open 房，破坏"一对只有一个活动 dm 房"
    # LIKE 通配转义：身份串可含 _ / %（AB_AS 合法面），不转义会误配他房间
    like_pat = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "#%"
    existing = conn.execute(
        "SELECT id, dm_key, state, created_ts FROM rooms "
        "WHERE dm_key=? OR dm_key LIKE ? ESCAPE '\\' ORDER BY id",
        (base, like_pat),
    ).fetchall()

    def _gen(row) -> int:
        k = row["dm_key"]
        return 1 if k == base else int(k.rsplit("#", 1)[1])

    max_gen = max((_gen(r) for r in existing), default=1)
    if existing:
        latest = max(existing, key=_gen)
        if latest["state"] == "open" and not _dm_member_recycled(conn, a, b, latest["created_ts"]):
            return latest["id"]
    for gen in range(max_gen + 1, max_gen + 10000):
        key = base if gen == 1 else f"{base}#{gen}"
        row = conn.execute(
            "SELECT id, state, created_ts FROM rooms WHERE dm_key=?", (key,)
        ).fetchone()
        if row:
            # 并发方刚建/或本代可用：复用而非跳过（防双向并发分裂）
            if row["state"] == "open" and not _dm_member_recycled(conn, a, b, row["created_ts"]):
                return row["id"]
            continue
        conn.execute(
            "INSERT OR IGNORE INTO rooms(topic, state, created_by, created_ts, dm_key) "
            "VALUES(?,?,?,?,?)",
            (f"{a} ⇄ {b}", "open", a, utcnow(), key),
        )
        rid = conn.execute("SELECT id FROM rooms WHERE dm_key=?", (key,)).fetchone()["id"]
        break
    if rid is None:
        raise BusError("E_INVALID", "dm generation overflow")
    now = utcnow()
    for m in (a, b):
        conn.execute(
            "INSERT OR IGNORE INTO room_members(room_id, member, role, joined_ts) VALUES(?,?,?,?)",
            (rid, m, "member", now),
        )
    conn.commit()
    return rid


_DM_RECYCLE_TOLERANCE_SECONDS = 300  # 时钟偏斜容差：秒级偏移不得造成会话碎片化


def _dm_member_recycled(conn, a: str, b: str, room_created_ts: str | None) -> bool:
    """True when a dm participant is a *new process* that inherited the id:
    its instances.started_at is later than the room's creation by more than
    the tolerance window. Revived processes keep their original started_at,
    so they never trigger this."""
    if not room_created_ts:
        return False
    cutoff = (
        datetime.strptime(room_created_ts, TIME_FORMAT)
        + timedelta(seconds=_DM_RECYCLE_TOLERANCE_SECONDS)
    ).strftime(TIME_FORMAT)
    for member in (a, b):
        if member == "human":
            continue
        row = conn.execute(
            "SELECT started_at FROM instances WHERE agent_id=?", (member,)
        ).fetchone()
        if row and row["started_at"] and row["started_at"] > cutoff:
            return True
    return False


def _fan_out(conn, message_id: int, room_id: int, exclude_member=None) -> None:
    """为当时每个成员各建一行 new；通配成员展开为当时在线的同类实例。发送者除外。"""
    rows = conn.execute("SELECT member FROM room_members WHERE room_id=?", (room_id,)).fetchall()
    targets = []
    for (m,) in rows:
        if m == exclude_member:
            continue
        if m.endswith(":*"):
            kind = m[:-2]
            for (aid,) in conn.execute(
                "SELECT agent_id FROM instances WHERE kind=? AND state='online'", (kind,)
            ):
                if aid and aid != exclude_member:
                    targets.append(aid)
        else:
            targets.append(m)
    for t in dict.fromkeys(targets):
        conn.execute(
            "INSERT OR IGNORE INTO message_reads(message_id, member, status, delivered_at) "
            "VALUES(?,?,'new',NULL)",
            (message_id, t),
        )


def _insert_message(
    conn,
    room_id: int,
    sender: str,
    body: str,
    reply_to: int | None = None,
    refs: str | None = None,
    fan_out_targets: list[str] | None = None,
) -> int:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO messages(ts, room_id, sender, body, refs, reply_to) VALUES(?,?,?,?,?,?)",
        (now, room_id, sender, body, refs, reply_to),
    )
    mid = cur.lastrowid
    if fan_out_targets is None:
        _fan_out(conn, mid, room_id, exclude_member=sender)
    else:
        for t in dict.fromkeys(fan_out_targets):
            if t == sender:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO message_reads(message_id, member, status, delivered_at) "
                "VALUES(?,?,'new',NULL)",
                (mid, t),
            )
    return mid


def resolve_mentions(conn, room_id: int, body: str, identity: str | None = None) -> list[str]:
    """从 body 解析 @成员（群聊 @ 定向约定）：@token 必须命中 room 成员才算有效。

    member 格式：codex#2 / human / claude:*。返回去重后的成员名列表（不含 sender）。
    """
    members = {m["member"] for m in _room_members(conn, room_id)}
    # 通配成员允许 @ 具体编号（claude:* 房里 @claude#3 视为定向该实例）
    kind_wild = {m[:-2] for m in members if m.endswith(":*")}
    found = []
    for tok in re.findall(r"@([A-Za-z]+(?:#[0-9]+|:\*)?)", body or ""):
        if tok in members or (("#" in tok) and tok.split("#", 1)[0] in kind_wild):
            if identity is None or tok != identity:
                if tok not in found:
                    found.append(tok)
    return found


def post_message(
    conn,
    body: str,
    sender: str,
    *,
    room_id: int | None = None,
    to: str | None = None,
    reply_to: int | None = None,
    refs: str | None = None,
    mentions=None,
    cap: int = ROOM_SOFT_CAP,
) -> int:
    """会话内发言（--room）；或私聊（--to 自动 dm）。返回消息 id。

    mentions（群聊 @ 定向）：非空时消息仍入 room 历史（全员可查），但投递
    （message_reads）只建给被 @ 成员；为空时维持全员 fan-out。
    refs 被占用存储 mention 目标（"@codex#2 @claude#3"），供历史渲染。
    """
    body = (body or "").strip()
    if not body:
        raise BusError("E_INVALID", "message body cannot be empty")
    if mentions and to is not None:
        raise BusError(
            "E_INVALID", "mentions (--at) apply to room posts; a dm delivers only to the pair"
        )
    if room_id is None and to is not None:
        room_id = ensure_dm_room(conn, sender, to)
    if room_id is None:
        raise BusError("E_INVALID", "post needs --room or --to")
    room = _get_room(conn, room_id)
    if room["state"] != "open":
        raise BusError("E_ROOM_CLOSED", f"room#{room_id} is closed; new messages rejected")
    if not is_member(conn, room_id, sender):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{room_id}; cannot post")
    fan_out_targets = None
    if mentions is None:
        mentions = resolve_mentions(conn, room_id, body, identity=sender) or None
    if mentions:
        # human 是超集成员：agent 自建房（无 human 成员行）里 @human 也应有效
        mentions = ["human" if m == "human" else m for m in mentions]
        valid = {m["member"] for m in _room_members(conn, room_id)}
        kind_wild = {m[:-2] for m in valid if m.endswith(":*")}
        for m in mentions:
            if m == sender:
                continue
            if m == "human":
                continue  # superset member
            wild_ok = ("#" in m) and m.split("#", 1)[0] in kind_wild
            if not (m in valid or wild_ok):
                raise BusError("E_NOT_MEMBER", f"@{m} is not a member of room#{room_id}")
            if wild_ok and m not in valid and not _instance_exists(conn, m):
                # 通配放行只对真实存在的编号（不存在的编号会留下永无主的
                # read 行，钉死软上限并可能被回收后的新进程继承）
                raise BusError("E_NOT_FOUND", f"@{m} instance does not exist")
        # 通配 @ 目标展开为当时在线同类实例（与 _fan_out 同语义）；
        # sender 自身剔除；过滤后为空（如 @自己）视为群发——
        # 零投递的"隐形消息"是更糟的失败模式（协议：@human 自指=群发）。
        expanded: list[str] = []
        for m in mentions:
            if m.endswith(":*"):
                kind = m[:-2]
                expanded.extend(
                    aid
                    for (aid,) in conn.execute(
                        "SELECT agent_id FROM instances WHERE kind=? AND state='online'", (kind,)
                    )
                    if aid
                )
            else:
                expanded.append(m)
        fan_out_targets = [m for m in dict.fromkeys(expanded) if m != sender] or None
        refs = " ".join("@" + m for m in fan_out_targets) if fan_out_targets else None
    mid = _insert_message(
        conn, room_id, sender, body, reply_to=reply_to, refs=refs, fan_out_targets=fan_out_targets
    )
    _enforce_soft_cap(conn, room_id, cap)
    conn.commit()
    return mid


def _room_frozen(conn, room_id: int) -> bool:
    """closed 三阶段：close 后 24h 无任何 poll/ack 活动 → 冻结（poll 不再返回）。"""
    room = _get_room(conn, room_id)
    if room["state"] != "closed":
        return False
    if not room["closed_ts"]:
        return False
    cutoff = _ago(FREEZE_SECONDS)
    if room["closed_ts"] >= cutoff:
        return False
    row = conn.execute(
        "SELECT MAX(r.delivered_at) AS d, MAX(r.acked_at) AS a "
        "FROM message_reads r JOIN messages m ON m.id=r.message_id "
        "WHERE m.room_id=?",
        (room_id,),
    ).fetchone()
    # 活动 = poll（delivered_at）与 ack（acked_at）的较晚者——协议约定
    # "24h 无任何 poll/ack 活动"才冻结；ack 不覆盖投递时刻（取证语义）
    candidates = [x for x in (row["d"], row["a"]) if x] if row else []
    last = max(candidates) if candidates else None
    return last is None or last < cutoff


def _poll_keep(conn, identity: str, msg: dict, only: set) -> bool:
    """收端过滤（poll --only）。only 为空集→恒 True。

    保留条件（或）：① body/refs @我；② refs 含我名下任务的 task:<id>；
    ③ sender 是 human/system（human 指令与任务事件通道恒放行）。
    """
    if not only:
        return True
    if msg["sender"] in ("human", "system"):
        return True
    text = f"{msg['body'] or ''} {msg['refs'] or ''}"
    if re.search(rf"@{re.escape(identity)}\b", text):
        return True
    if "task" in only:
        task_ids = re.findall(r"task:(\d+)", msg["refs"] or "")
        for tid in task_ids:
            row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (int(tid),)).fetchone()
            if row and row["assignee"] == identity:
                return True
    return False


def poll(conn, identity: str, since: int | None = None, only: str | None = None) -> dict:
    """取走我所有可见 room 的新消息（JSON 结构，按 room 分组），置 delivered。

    closed 未冻结的 room 仍可收尾 poll/ack；冻结 room 跳过（历史仍可 show --history）。
    human 无预建 reads，轮询时按「未投递/未确认」即时补行（UPSERT 防重）。

    v3：since 为上次 poll_revision（messages.rev + rooms.rev 合成判定值），
    未变化时 O(1) 快路径返回空；响应带 revision 供下次 --since 闭环。
    """
    rev = poll_revision(conn)
    if since is not None and rev <= since and not _has_pending(conn, identity):
        return {"rooms": [], "revision": rev}
    only_set = {t for t in re.split(r"[+,\s]+", only or "") if t}
    unknown = only_set - {"mention", "task"}
    if unknown:
        raise BusError(
            "E_INVALID", f"unknown --only token(s) {sorted(unknown)} (valid: mention, task)"
        )
    out = []
    now = utcnow()
    for room in visible_rooms(conn, identity):
        rid = room["id"]
        if _room_frozen(conn, rid):
            continue
        if identity == "human":
            rows = conn.execute(
                "SELECT id, ts, sender, body, refs, reply_to FROM messages "
                "WHERE room_id=? AND NOT EXISTS ("
                "  SELECT 1 FROM message_reads r WHERE r.message_id=messages.id "
                "  AND r.member='human' AND r.status IN ('delivered','acked')) "
                "ORDER BY id",
                (rid,),
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO message_reads(message_id, member, status, delivered_at) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(message_id, member) DO UPDATE SET "
                    "status='delivered', delivered_at=excluded.delivered_at "
                    "WHERE message_reads.status != 'acked'",
                    (r["id"], "human", "delivered", now),
                )
        else:
            rows = conn.execute(
                "SELECT m.id, m.ts, m.sender, m.body, m.refs, m.reply_to "
                "FROM message_reads r JOIN messages m ON m.id=r.message_id "
                "WHERE r.member=? AND r.status='new' AND m.room_id=? "
                "ORDER BY m.id",
                (identity, rid),
            ).fetchall()
            # 收端裁剪：被滤除的消息保持 new（不置 delivered），全量 poll 仍可取
            if only_set:
                rows = [r for r in rows if _poll_keep(conn, identity, r, only_set)]
            for r in rows:
                conn.execute(
                    "UPDATE message_reads SET status='delivered', delivered_at=? "
                    "WHERE message_id=? AND member=? AND status='new'",
                    (now, r["id"], identity),
                )
        if rows:
            out.append(
                {
                    "room_id": rid,
                    "topic": room["topic"],
                    "state": room["state"],
                    "messages": [dict(r) for r in rows],
                }
            )
    conn.commit()
    return {"rooms": out, "revision": rev}


def _has_pending(conn, identity: str) -> bool:
    """Any new-state delivery for this identity? Guards the --since fast path:
    reads-table transitions (30min redelivery) do not bump global_revision,
    so "revision unchanged" alone does not imply "nothing to drain"."""
    if identity == "human":
        # human 是超集成员（visible_rooms 返回全部房）——不按成员表过滤；
        # 冻结判定与 _room_frozen 同口径（活动 = poll/ack 的较晚者），
        # 否则"close 超 24h 但近期有 ack"的房会让快路径吞消息
        row = conn.execute(
            "SELECT 1 FROM messages m "
            "JOIN rooms rm2 ON rm2.id=m.room_id "
            "WHERE NOT EXISTS (SELECT 1 FROM message_reads r "
            "  WHERE r.message_id=m.id AND r.member='human' "
            "  AND r.status IN ('delivered','acked')) "
            "AND (rm2.state != 'closed' OR rm2.closed_ts IS NULL "
            "  OR EXISTS (SELECT 1 FROM message_reads r2 "
            "    WHERE r2.message_id=m.id "
            "    AND COALESCE(r2.acked_at, r2.delivered_at, '') >= rm2.closed_ts) "
            "  ) LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM message_reads r "
            "JOIN messages m ON m.id=r.message_id "
            "JOIN rooms rm2 ON rm2.id=m.room_id "
            "WHERE r.member=? AND r.status='new' "
            "AND (rm2.state != 'closed' OR rm2.closed_ts IS NULL) LIMIT 1",
            (identity,),
        ).fetchone()
    return row is not None


def ack_message(conn, message_id: int, identity: str) -> None:
    """已读回执：我的 read 行置 acked（幂等；同时刷新活动时间）。

    delivered_at 保留首次投递时刻（COALESCE，仅未经 poll 直 ack 的 NULL 补填
    ack 时刻）——ack 不再覆盖，投递时间可取证（2026-08-17 #291 丢失事故教训）。
    """
    msg = conn.execute("SELECT id, room_id FROM messages WHERE id=?", (message_id,)).fetchone()
    if not msg:
        raise BusError("E_NOT_FOUND", f"message #{message_id} not found")
    if not is_member(conn, msg["room_id"], identity):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{msg['room_id']}; cannot ack")
    if not conn.execute(
        "SELECT 1 FROM message_reads WHERE message_id=? AND member=?", (message_id, identity)
    ).fetchone():
        raise BusError("E_NOT_FOUND", f"message #{message_id} not delivered to you; cannot ack")
    conn.execute(
        "UPDATE message_reads SET status='acked', acked_at=?, "
        "delivered_at=COALESCE(delivered_at, ?) "
        "WHERE message_id=? AND member=?",
        (utcnow(), utcnow(), message_id, identity),
    )
    conn.commit()


def reply_message(conn, message_id: int, body: str, sender: str) -> int:
    """同 room 回复（reply_to 指向原消息），返回新消息 id。"""
    msg = conn.execute("SELECT id, room_id FROM messages WHERE id=?", (message_id,)).fetchone()
    if not msg:
        raise BusError("E_NOT_FOUND", f"message #{message_id} not found")
    room = _get_room(conn, msg["room_id"])
    if room["state"] != "open":
        raise BusError("E_ROOM_CLOSED", f"room#{msg['room_id']} is closed; new messages rejected")
    if not is_member(conn, msg["room_id"], sender):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{msg['room_id']}; cannot reply")
    return post_message(conn, body, sender, room_id=msg["room_id"], reply_to=message_id)


# ── 超时重投 / 软上限 / 归档 ──────────────────────────────────────────────
def redeliver_stale(conn, threshold_seconds: int = REDELIVER_SECONDS) -> int:
    """delivered 超过 30min 未 ack 退回 new（跳过已冻结 room）。返回重投条数。"""
    cutoff = _ago(threshold_seconds)
    rows = conn.execute(
        "SELECT r.message_id, r.member, r.delivered_at, m.room_id "
        "FROM message_reads r JOIN messages m ON m.id=r.message_id "
        "WHERE r.status='delivered'"
    ).fetchall()
    frozen_cache = {}
    count = 0
    for r in rows:
        if r["delivered_at"] is not None and r["delivered_at"] >= cutoff:
            continue
        rid = r["room_id"]
        if rid not in frozen_cache:
            frozen_cache[rid] = _room_frozen(conn, rid)
        if frozen_cache[rid]:
            continue
        cur = conn.execute(
            "UPDATE message_reads SET status='new', delivered_at=NULL "
            "WHERE message_id=? AND member=? AND status='delivered'",
            (r["message_id"], r["member"]),
        )
        if cur.rowcount == 1:
            count += 1
    conn.commit()
    return count


def _enforce_soft_cap(conn, room_id: int, cap: int = ROOM_SOFT_CAP) -> None:
    """每 room 软上限：先删最老全 acked，再删最老全 delivered，绝不删 new。

    v3：被开放任务引用的消息（refs 整词匹配 task:<id>）不参与裁剪——
    任务评论随消息被裁会丢上下文，开放任务必须保全。
    """
    cnt = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE room_id=?", (room_id,)).fetchone()[
        "n"
    ]
    excess = cnt - cap
    if excess <= 0:
        return
    protected = protected_message_ids(conn, room_id)
    # 审计保护：某个成员对该消息还没有任何 read 行（如 human 的懒建行、
    # mention 的非目标成员）时绝不裁剪——"绝不删 new"对无行成员同样成立。
    unseen_guard = (
        "AND NOT EXISTS (SELECT 1 FROM room_members rm "
        "  WHERE rm.room_id=m.room_id AND rm.member != m.sender "
        "  AND rm.member NOT LIKE '%:*' "  # 通配成员由展开出的具体实例代表
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM message_reads r2 "
        "    WHERE r2.message_id=m.id AND r2.member=rm.member)) "
        # human 的 reads 懒建——human 是房间成员（被创建/邀请）而无 read 行
        # 时视为未读保护；纯 agent 自建房（human 非成员）不适用
        "AND NOT (EXISTS (SELECT 1 FROM room_members hm "
        "  WHERE hm.room_id=m.room_id AND hm.member='human') "
        "  AND NOT EXISTS (SELECT 1 FROM message_reads r3 "
        "    WHERE r3.message_id=m.id AND r3.member='human')) "
    )
    acked = [
        r["id"]
        for r in conn.execute(
            "SELECT m.id FROM messages m WHERE m.room_id=? "
            "AND EXISTS (SELECT 1 FROM message_reads r WHERE r.message_id=m.id) "
            "AND NOT EXISTS (SELECT 1 FROM message_reads r "
            "                WHERE r.message_id=m.id AND r.status!='acked') "
            + unseen_guard
            + "ORDER BY m.id",
            (room_id,),
        ).fetchall()
        if r["id"] not in protected
    ]
    delivered = [
        r["id"]
        for r in conn.execute(
            "SELECT m.id FROM messages m WHERE m.room_id=? "
            "AND EXISTS (SELECT 1 FROM message_reads r "
            "            WHERE r.message_id=m.id AND r.status='delivered') "
            "AND NOT EXISTS (SELECT 1 FROM message_reads r "
            "                WHERE r.message_id=m.id AND r.status='new') "
            + unseen_guard
            + "ORDER BY m.id",
            (room_id,),
        ).fetchall()
        if r["id"] not in protected
    ]
    to_delete = acked[:excess]
    if len(to_delete) < excess:
        to_delete += [x for x in delivered if x not in set(to_delete)][: excess - len(to_delete)]
    for mid in to_delete:
        conn.execute("DELETE FROM messages WHERE id=?", (mid,))


def archive_closed(conn, days: int = ARCHIVE_DAYS) -> int:
    """closed 超过 N 天物理归档删除（先删消息让 reads CASCADE，再删成员与房）。

    v3：任务层随房删除——显式先删房内 task_relations/tasks，
    tasks.room_id ON DELETE CASCADE 兜底。
    """
    cutoff = _ago(days * 86400)
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM rooms WHERE state='closed' AND closed_ts IS NOT NULL AND closed_ts < ?",
            (cutoff,),
        ).fetchall()
    ]
    for rid in ids:
        conn.execute(
            "DELETE FROM task_relations WHERE source_id IN "
            "(SELECT id FROM tasks WHERE room_id=?) OR target_id IN "
            "(SELECT id FROM tasks WHERE room_id=?)",
            (rid, rid),
        )
        conn.execute("DELETE FROM tasks WHERE room_id=?", (rid,))
        conn.execute("DELETE FROM messages WHERE room_id=?", (rid,))
        conn.execute("DELETE FROM room_members WHERE room_id=?", (rid,))
        conn.execute("DELETE FROM rooms WHERE id=?", (rid,))
    conn.commit()
    return len(ids)


# ════════════════════════════════════════════════════════════════════
# v3 任务层与表级 revision（协议任务层，规范见 docs/protocol-v3.md）
# ════════════════════════════════════════════════════════════════════
TASK_STATUSES = ("todo", "in_progress", "in_review", "blocked", "done", "canceled")
TASK_TERMINAL = ("done", "canceled")
TASK_PRIORITIES = ("urgent", "high", "medium", "low")
RELATION_TYPES = ("parent", "blocks", "related")
LABEL_MAX_LEN = 32
LABEL_MAX_COUNT = 16
_DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def revisions(conn) -> dict:
    """四个表级计数器 → {rooms, messages, tasks, instances}。"""
    return {r["table_name"]: r["revision"] for r in conn.execute("SELECT * FROM global_revision")}


def revision(conn, table_name: str) -> int:
    return conn.execute(
        "SELECT revision FROM global_revision WHERE table_name=?", (table_name,)
    ).fetchone()["revision"]


def poll_revision(conn) -> int:
    """poll 判定用合成值 = messages.rev + rooms.rev。

    和值单调递增：任一侧 bump 必增。max() 会在大计数器不动、小计数器增长时漏变
    （rooms=1 时 messages 0→1，max 仍为 1 导致 --since 快路径误跳过，原型实测后修正）。
    """
    return revision(conn, "messages") + revision(conn, "rooms")


# ── 任务读写 ──────────────────────────────────────────────────────────
def _get_task(conn, task_id: int):
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise BusError("E_NOT_FOUND", f"task #{task_id} not found")
    return row


def _assert_task_visible(conn, task, identity: str) -> None:
    """可见性铁律：任务只在 room 成员间可见；human 超集由 is_member 覆盖。"""
    if not is_member(conn, task["room_id"], identity):
        raise BusError(
            "E_NOT_MEMBER", f"not a member of room#{task['room_id']}; cannot access this task"
        )


def _task_sys_msg(
    conn, room_id: int, body: str, task_id: int | None = None, targets: list[str] | None = None
) -> None:
    """任务事件以 system 消息入房（refs 整词 token `task:<id>`）。

    targets=None 时 fan-out 全部成员（既有行为，所有现存调用点不变）；
    targets 为成员列表时只对目标建 message_reads（定向投递——消息仍入房史
    可查，但非目标 poll 不到）。
    """
    _insert_message(
        conn,
        room_id,
        "system",
        body,
        refs=f"task:{task_id}" if task_id is not None else None,
        fan_out_targets=targets,
    )


def _labels_list(raw) -> list:
    """labels 列（JSON 字符串）→ 数组；脏数据回退 []（服务端转数组，调用方拿到的恒为数组）。"""
    try:
        val = json.loads(raw or "[]")
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def _validate_labels(labels) -> str:
    if labels is None:
        return "[]"
    if not isinstance(labels, (list, tuple)) or len(labels) > LABEL_MAX_COUNT:
        raise BusError("E_INVALID", f"labels must be an array of at most {LABEL_MAX_COUNT} items")
    for lab in labels:
        if not isinstance(lab, str) or not lab.strip() or len(lab) > LABEL_MAX_LEN:
            raise BusError(
                "E_INVALID",
                f"label items must be non-empty strings of at most {LABEL_MAX_LEN} chars",
            )
    return json.dumps(list(labels), ensure_ascii=False)


def _validate_due_date(due_date) -> str | None:
    if due_date is None:
        return None
    if not isinstance(due_date, str) or not _DUE_RE.match(due_date):
        raise BusError("E_INVALID", "due_date must be YYYY-MM-DD")
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError as exc:
        raise BusError("E_INVALID", "due_date is not a valid date") from exc
    return due_date


def create_task(
    conn,
    room_id: int,
    title: str,
    creator: str,
    *,
    body: str = "",
    priority: str = "medium",
    labels=None,
    due_date: str | None = None,
) -> int:
    """建任务并自动发 system 消息（refs=task:<id>）；返回 task id。"""
    title = (title or "").strip()
    if not title:
        raise BusError("E_INVALID", "title cannot be empty")
    if priority not in TASK_PRIORITIES:
        raise BusError("E_INVALID", f"priority must be one of {'/'.join(TASK_PRIORITIES)}")
    room = _get_room(conn, room_id)
    if room["state"] != "open":
        raise BusError("E_ROOM_CLOSED", f"room#{room_id} is closed; cannot create tasks")
    if not is_member(conn, room_id, creator):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{room_id}; cannot create tasks")
    labels_json = _validate_labels(labels)
    due = _validate_due_date(due_date)
    cur = conn.execute(
        "INSERT INTO tasks(room_id, title, body, status, priority, labels, "
        "due_date, claim_version, created_by, created_ts) "
        "VALUES(?,?,?,'todo',?,?,?,1,?,?)",
        (room_id, title, body, priority, labels_json, due, creator, utcnow()),
    )
    tid = cur.lastrowid
    _task_sys_msg(conn, room_id, f"[task] created task #{tid}: {title}", tid)
    conn.commit()
    return tid


def get_task(conn, task_id: int, identity: str) -> dict:
    """任务全量：字段 + 关系边（含目标标题/状态）+ 评论（refs 整词引用的 room 消息）。"""
    task = dict(_get_task(conn, task_id))
    _assert_task_visible(conn, task, identity)
    task["labels"] = _labels_list(task.get("labels"))
    task["relations"] = [
        dict(r)
        for r in conn.execute(
            "SELECT type, target_id, t2.title AS target_title, t2.status AS target_status "
            "FROM task_relations tr JOIN tasks t2 ON t2.id=tr.target_id "
            "WHERE tr.source_id=? ORDER BY tr.rowid",
            (task_id,),
        )
    ]
    task["comments"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, ts, sender, body, refs FROM messages "
            "WHERE room_id=? AND (' '||COALESCE(refs,'')||' ') LIKE '% task:'||?||' %' "
            "ORDER BY id",
            (task["room_id"], task_id),
        )
    ]
    return task


def list_tasks(conn, room_id: int, identity: str, status: str | None = None) -> list[dict]:
    """room 内任务列表（status 过滤可选）；排序 = 状态机序, 优先级, id。"""
    if not is_member(conn, room_id, identity):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{room_id}; cannot list tasks")
    sql = "SELECT * FROM tasks WHERE room_id=?"
    args: list[int | str] = [room_id]
    if status:
        if status not in TASK_STATUSES:
            raise BusError("E_INVALID", f"status must be one of {'/'.join(TASK_STATUSES)}")
        sql += " AND status=?"
        args.append(status)
    sql += (
        " ORDER BY CASE status WHEN 'todo' THEN 0 WHEN 'in_progress' THEN 1 "
        "WHEN 'in_review' THEN 2 WHEN 'blocked' THEN 3 WHEN 'done' THEN 4 "
        "ELSE 5 END, "
        "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END, id"
    )
    rows = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["labels"] = _labels_list(d.get("labels"))
        rows.append(d)
    return rows


# ── 任务状态机（服务端只裁决版本/状态/归属；纪律在协议，见 docs/protocol-v3.md）──
def claim_task(conn, task_id: int, identity: str) -> dict:
    """原子认领：todo→in_progress + assignee + claim_version+1；冲突 E_CONFLICT（exit 5）。"""
    task = _get_task(conn, task_id)
    if not is_member(conn, task["room_id"], identity):
        raise BusError("E_NOT_MEMBER", f"not a member of room#{task['room_id']}; cannot claim")
    if task["status"] == "blocked":
        # blocked 是被依赖阻塞而非被人抢占，报因必须区分（P0.5-1），
        # 否则 worker 会误判"被抢"而重试/绕路
        raise BusError("E_INVALID", f"task #{task_id} is blocked; cannot claim")
    blockers = conn.execute(
        "SELECT COUNT(*) FROM task_relations tr JOIN tasks t2 ON t2.id=tr.source_id "
        "WHERE tr.target_id=? AND tr.type='blocks' AND t2.status != 'done'",
        (task_id,),
    ).fetchone()[0]
    if blockers:
        raise BusError(
            "E_TASK_OPEN",
            f"task #{task_id} is blocked by {blockers} unfinished task(s); settle them first",
        )
    cur = conn.execute(
        "UPDATE tasks SET status='in_progress', assignee=?, "
        "claim_version=claim_version+1 "
        "WHERE id=? AND status='todo' AND claim_version=?",
        (identity, task_id, task["claim_version"]),
    )
    if cur.rowcount != 1:
        raise BusError(
            "E_CONFLICT",
            f"task #{task_id} already claimed or version conflict (re-read, retry once)",
        )
    conn.commit()
    return dict(_get_task(conn, task_id))


def task_report(conn, task_id: int, identity: str, body: str) -> dict:
    """汇报完成：写评论消息 + in_progress→in_review（协议任务层：绝不直接 done）。"""
    task = _get_task(conn, task_id)
    _assert_task_visible(conn, task, identity)
    if task["assignee"] != identity:
        raise BusError(
            "E_NOT_MEMBER",
            f"task #{task_id} belongs to {task['assignee'] or 'nobody'}; cannot report",
        )
    if task["status"] != "in_progress":
        raise BusError("E_INVALID", f"task #{task_id} is {task['status']}; cannot report")
    _task_sys_msg(conn, task["room_id"], f"[task] #{task_id} reported: {body}", task_id)
    conn.execute(
        "UPDATE tasks SET status='in_review', claim_version=claim_version+1 WHERE id=?", (task_id,)
    )
    conn.commit()
    return dict(_get_task(conn, task_id))


def task_review(conn, task_id: int, identity: str, body: str) -> dict:
    """仅打回：in_review→in_progress（无 ok 分支——唯一进入 done 的路径是 task_done，
    防「review 即批准」旁路）。"""
    task = _get_task(conn, task_id)
    _assert_task_visible(conn, task, identity)
    if task["status"] != "in_review":
        raise BusError("E_INVALID", f"task #{task_id} is {task['status']}; cannot review")
    if identity != "human" and identity == task["assignee"]:
        raise BusError(
            "E_NOT_MEMBER",
            f"task #{task_id} belongs to you; review must come from another member or the human",
        )
    _task_sys_msg(conn, task["room_id"], f"[task] #{task_id} rework: {body}", task_id)
    conn.execute(
        "UPDATE tasks SET status='in_progress', claim_version=claim_version+1 WHERE id=?",
        (task_id,),
    )
    conn.commit()
    return dict(_get_task(conn, task_id))


def task_block(conn, task_id: int, identity: str, reason: str) -> dict:
    """阻塞：→blocked + 评论（reason）。仅任务归属者可执行。"""
    task = _get_task(conn, task_id)
    _assert_task_visible(conn, task, identity)
    if task["assignee"] != identity:
        raise BusError(
            "E_NOT_MEMBER",
            f"task #{task_id} belongs to {task['assignee'] or 'nobody'}; cannot block",
        )
    if task["status"] in TASK_TERMINAL:
        raise BusError("E_INVALID", f"task #{task_id} is terminal; cannot block")
    _task_sys_msg(conn, task["room_id"], f"[task] #{task_id} blocked: {reason}", task_id)
    conn.execute(
        "UPDATE tasks SET status='blocked', claim_version=claim_version+1 WHERE id=?", (task_id,)
    )
    conn.commit()
    return dict(_get_task(conn, task_id))


def _assert_human(identity: str) -> None:
    if identity != "human":
        raise BusError("E_NOT_MEMBER", "human-only operation")


def _assert_no_open_children(conn, task_id: int) -> None:
    """父任务 done 前置：parent 边上的子任务必须全部终态。"""
    open_children = conn.execute(
        "SELECT COUNT(*) FROM task_relations tr JOIN tasks t2 ON t2.id=tr.source_id "
        "WHERE tr.target_id=? AND tr.type='parent' AND t2.status NOT IN ('done','canceled')",
        (task_id,),
    ).fetchone()[0]
    if open_children:
        raise BusError("E_TASK_OPEN", f"task #{task_id} has non-terminal children; cannot done")


def task_done(conn, task_id: int, identity: str, expected_version: int | None = None) -> dict:
    """done：仅 human（用户确认后）；前置 in_review + 子任务全终态。

    expected_version：乐观裁决——UPDATE ... AND claim_version=? 原子，
    不匹配 → E_CONFLICT（重读后仅重试一次是协议纪律）。
    """
    _assert_human(identity)
    task = _get_task(conn, task_id)
    if task["status"] != "in_review":
        raise BusError(
            "E_INVALID",
            f"task #{task_id} is {task['status']}; cannot done (the only path into done)",
        )
    _assert_no_open_children(conn, task_id)
    if expected_version is not None and expected_version != task["claim_version"]:
        raise BusError(
            "E_CONFLICT",
            f"task #{task_id} version conflict (expected v{expected_version}, actual v{task['claim_version']})",
        )
    cur = conn.execute(
        "UPDATE tasks SET status='done', claim_version=claim_version+1, "
        "closed_ts=? WHERE id=? AND claim_version=?",
        (utcnow(), task_id, task["claim_version"]),
    )
    if cur.rowcount != 1:
        raise BusError("E_CONFLICT", f"task #{task_id} version conflict")
    conn.commit()
    return dict(_get_task(conn, task_id))


def task_cancel(conn, task_id: int, identity: str, expected_version: int | None = None) -> dict:
    """cancel：仅 human（监督兜底动作）；expected_version 乐观裁决同 task_done。"""
    _assert_human(identity)
    task = _get_task(conn, task_id)
    if task["status"] in TASK_TERMINAL:
        raise BusError("E_INVALID", f"task #{task_id} is terminal; cannot cancel")
    if expected_version is not None and expected_version != task["claim_version"]:
        raise BusError(
            "E_CONFLICT",
            f"task #{task_id} version conflict (expected v{expected_version}, actual v{task['claim_version']})",
        )
    cur = conn.execute(
        "UPDATE tasks SET status='canceled', claim_version=claim_version+1, "
        "closed_ts=? WHERE id=? AND claim_version=?",
        (utcnow(), task_id, task["claim_version"]),
    )
    if cur.rowcount != 1:
        raise BusError("E_CONFLICT", f"task #{task_id} version conflict")
    conn.commit()
    return dict(_get_task(conn, task_id))


def task_reopen(conn, task_id: int, identity: str) -> dict:
    """reopen：仅 human（监督兜底动作）；blocked/canceled→todo 清 assignee。"""
    _assert_human(identity)
    task = _get_task(conn, task_id)
    if task["status"] not in ("blocked", "canceled"):
        raise BusError("E_INVALID", f"task #{task_id} is {task['status']}; cannot reopen")
    conn.execute(
        "UPDATE tasks SET status='todo', assignee=NULL, claim_version=claim_version+1 WHERE id=?",
        (task_id,),
    )
    conn.commit()
    return dict(_get_task(conn, task_id))


def update_task(conn, task_id: int, changes: dict, identity: str) -> dict:
    """编辑文本类字段（title/body/priority/labels/due_date）；状态变更必须走动作端点。

    供上层监督工具 PATCH 用；claim_version 自增保持乐观语义。
    """
    allowed = {"title", "body", "priority", "labels", "due_date"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise BusError("E_INVALID", f"unknown fields {unknown} (allowed: {sorted(allowed)})")
    task = _get_task(conn, task_id)
    _assert_task_visible(conn, task, identity)
    assignments: list[str] = []
    values: list[object] = []
    if "title" in changes:
        if not isinstance(changes["title"], str):
            raise BusError("E_INVALID", "title must be a string")
        title = (changes["title"] or "").strip()
        if not title:
            raise BusError("E_INVALID", "title cannot be empty")
        assignments.append("title=?")
        values.append(title)
    if "body" in changes:
        assignments.append("body=?")
        values.append(changes["body"] or "")
    if "priority" in changes:
        if changes["priority"] not in TASK_PRIORITIES:
            raise BusError("E_INVALID", f"priority must be one of {'/'.join(TASK_PRIORITIES)}")
        assignments.append("priority=?")
        values.append(changes["priority"])
    if "labels" in changes:
        assignments.append("labels=?")
        values.append(_validate_labels(changes["labels"]))
    if "due_date" in changes:
        assignments.append("due_date=?")
        values.append(_validate_due_date(changes["due_date"]))
    if not assignments:
        out = dict(task)
        out["labels"] = _labels_list(out.get("labels"))
        return out
    values.append(task_id)
    conn.execute(
        f"UPDATE tasks SET {', '.join(assignments)}, claim_version=claim_version+1 WHERE id=?",
        values,
    )
    conn.commit()
    out = dict(_get_task(conn, task_id))
    out["labels"] = _labels_list(out.get("labels"))
    return out


# ── 执行档案（协议执行模式节：supervised=先确认后动 / trusted=自动执行）──
# 纪律层裁决：bus 不强制 agent 行为，meta 键是人可审计的事实源；
# set 仅 human（CLI/MCP 路径级门——同 UID 进程可直改 meta，见威胁模型）。
EXECUTION_PROFILES = ("supervised", "trusted")
_PROFILE_META_KEY = "execution_profile"


def get_execution_profile(conn) -> dict:
    """读取执行档案；未设置时返回 supervised（default 标记由调用方呈现）。"""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (_PROFILE_META_KEY,)).fetchone()
    if row:
        try:
            data = json.loads(row["value"])
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and data.get("value") in EXECUTION_PROFILES:
            return {
                "value": data["value"],
                "set_by": data.get("set_by"),
                "set_ts": data.get("set_ts"),
                "default": False,
            }
    return {"value": "supervised", "set_by": None, "set_ts": None, "default": True}


def set_execution_profile(conn, value: str, actor: str) -> dict:
    """设置执行档案（仅 human）；写入三要素（value/set_by/set_ts）供审计。"""
    _assert_human(actor)
    if value not in EXECUTION_PROFILES:
        raise BusError("E_INVALID", f"profile must be one of {'/'.join(EXECUTION_PROFILES)}")
    payload = json.dumps({"value": value, "set_by": actor, "set_ts": utcnow()}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_PROFILE_META_KEY, payload),
    )
    conn.commit()
    out = json.loads(payload)
    out["default"] = False
    return out


# ── 关系边（编排者建边，执行者只读）──────────────────────────────────
def task_relation_add(conn, task_id: int, rel_type: str, target_id: int, identity: str) -> None:
    """同 room 建关系边；限任务创建者或 human（协议：编排者/建任务者建边，
    执行者只读——机制层不做角色表，以创建者字段为授权事实源）。"""
    task = _get_task(conn, task_id)
    if identity != "human" and identity != task["created_by"]:
        raise BusError(
            "E_NOT_MEMBER", f"task #{task_id} relations are managed by its creator or human"
        )
    if rel_type not in RELATION_TYPES:
        raise BusError("E_INVALID", f"type must be one of {'/'.join(RELATION_TYPES)}")
    if task_id == target_id:
        raise BusError("E_INVALID", "cannot relate a task to itself")
    task = _get_task(conn, task_id)
    _assert_task_visible(conn, task, identity)
    target = _get_task(conn, target_id)
    _assert_task_visible(conn, target, identity)
    if task["room_id"] != target["room_id"]:
        raise BusError("E_INVALID", "relations cannot cross rooms (visibility rule)")
    if rel_type in ("parent", "blocks"):
        # parent 链与 blocks 链都禁止成环（A blocks B + B blocks A 会让双方
        # 永远不可认领）
        frontier = [target_id]
        seen: set[int] = set()
        while frontier:
            cur = frontier.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur == task_id:
                raise BusError("E_INVALID", f"{rel_type} relation would form a cycle")
            # 一条 source 可有多条同型出边（blocks 不受单父约束）——遍历全部
            frontier.extend(
                r["target_id"]
                for r in conn.execute(
                    "SELECT target_id FROM task_relations WHERE source_id=? AND type=?",
                    (cur, rel_type),
                )
            )
    if rel_type == "related":
        task_id, target_id = sorted((task_id, target_id))
    try:
        conn.execute(
            "INSERT INTO task_relations(source_id, target_id, type) VALUES(?,?,?)",
            (task_id, target_id, rel_type),
        )
    except sqlite3.IntegrityError as exc:
        raise BusError(
            "E_INVALID", "relation exists or violates the single-parent constraint"
        ) from exc
    conn.commit()


def task_relation_remove(conn, task_id: int, rel_type: str, target_id: int, identity: str) -> None:
    """移除关系边：与 add 对称的校验——类型合法、双方任务可见且同 room、
    边确实存在（否则 E_NOT_FOUND，防误报成功）。"""
    if rel_type not in RELATION_TYPES:
        raise BusError("E_INVALID", f"type must be one of {'/'.join(RELATION_TYPES)}")
    task = _get_task(conn, task_id)
    _assert_task_visible(conn, task, identity)
    target = _get_task(conn, target_id)
    _assert_task_visible(conn, target, identity)
    cur = conn.execute(
        "DELETE FROM task_relations WHERE source_id=? AND type=? AND target_id=?",
        (task_id, rel_type, target_id),
    )
    if cur.rowcount == 0:
        raise BusError(
            "E_NOT_FOUND", f"relation {rel_type} task#{task_id}->task#{target_id} not found"
        )
    conn.commit()


# ── 生命周期联动（关房/归档与任务层的联动）────────────────────────────
def assert_room_closable(conn, room_id: int) -> None:
    """room 有未终态任务 → E_TASK_OPEN（协议任务层关房前置）。"""
    open_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE room_id=? AND status NOT IN ('done','canceled')",
        (room_id,),
    ).fetchone()[0]
    if open_tasks:
        raise BusError(
            "E_TASK_OPEN",
            f"room#{room_id} has {open_tasks} non-terminal tasks; settle them or use --force",
        )


def cancel_open_tasks(conn, room_id: int) -> None:
    """--force 关房：cancel 全部未终态任务并发 system 消息。"""
    rows = conn.execute(
        "SELECT id, title FROM tasks WHERE room_id=? AND status NOT IN ('done','canceled')",
        (room_id,),
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE tasks SET status='canceled', claim_version=claim_version+1, "
            "closed_ts=? WHERE id=?",
            (utcnow(), r["id"]),
        )
        _task_sys_msg(
            conn, room_id, f"[task] #{r['id']} canceled due to room close: {r['title']}", r["id"]
        )


def protected_message_ids(conn, room_id: int) -> set:
    """软上限裁剪前：被开放任务引用的消息 id 集合。

    refs 为空白分隔 token 列表，任务引用 token = `task:<id>`；
    空格边界整词匹配（`' '||refs||' ' LIKE '% task:'||id||' %'`），
    避免 task:1 误匹配 task:12。
    """
    rows = conn.execute(
        "SELECT m.id FROM messages m JOIN tasks t ON t.room_id=m.room_id "
        "WHERE m.room_id=? AND t.status NOT IN ('done','canceled') "
        "AND (' '||COALESCE(m.refs,'')||' ') LIKE '% task:'||t.id||' %'",
        (room_id,),
    ).fetchall()
    return {r["id"] for r in rows}
