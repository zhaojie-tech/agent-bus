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
