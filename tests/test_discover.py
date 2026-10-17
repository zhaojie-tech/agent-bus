#!/usr/bin/env python3
"""test_discover — 发现守护单元测试。

全部使用伪造 /proc 结构与临时 DB；不读真实 /proc，严禁写真实 ~/.agent-bus/bus.db。
"""

import contextlib
import datetime
import io
import os
import pathlib
import re
import tempfile
import unittest

from agentbus import discover as ab

# fixture 路径根：识别逻辑只看路径后缀（/bin/codex）与 basename，
# 不依赖真实安装位置；禁止硬编码真实用户家目录（脱敏扫描门）。
FX = "/srv/agent-bus-fixtures"
CWD = f"{FX}/users/demo"
CODEX_BIN_WRAP = f"{FX}/tools/npm-global/bin/codex"
CODEX_BIN_NATIVE = (
    f"{FX}/tools/npm-global/lib/node_modules/@openai/codex/node_modules/"
    "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
)
HERMES_PY = f"{FX}/tools/hermes/venv/bin/python"
HERMES_MAIN = f"{FX}/tools/hermes/hermes"

NOW0 = "2026-08-13T12:00:00Z"


def shift(iso: str, minutes: float = 0.0) -> str:
    return ab.format_ts(ab.parse_ts(iso) + datetime.timedelta(minutes=minutes))


# 冻结 DDL 逐字副本（除 instances 外，测试 fixture 用）
BUS_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS rooms(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',
  created_by TEXT, created_ts TEXT, closed_ts TEXT,
  dm_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS room_members(
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  member  TEXT NOT NULL,
  role    TEXT NOT NULL DEFAULT 'member',
  joined_ts TEXT,
  PRIMARY KEY(room_id, member)
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  sender TEXT NOT NULL,
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
"""


def make_fake_proc(root: pathlib.Path, entries) -> pathlib.Path:
    """按 entries 列表伪造 /proc 树。entry: pid/ppid/comm/cmdline/tty/cwd。"""
    for e in entries:
        d = root / str(e["pid"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "comm").write_text(e["comm"], encoding="utf-8")
        (d / "cmdline").write_bytes("\0".join(e["cmdline"]).encode("utf-8") + b"\0")
        # stat 格式：pid (comm) S ppid ...（ppid 是 ) 之后第 2 个字段）
        (d / "stat").write_text(f"{e['pid']} ({e['comm']}) S {e['ppid']} 0 0 0 0 0 0 0 0 0 0\n")
        if e.get("cwd"):
            os.symlink(e["cwd"], d / "cwd")
        if e.get("tty"):
            (d / "fd").mkdir(exist_ok=True)
            os.symlink(e["tty"], d / "fd" / "0")
    return root


def make_proc(pid, ppid, comm, cmdline, tty=None, cwd=None) -> ab.Proc:
    return ab.Proc(pid=pid, ppid=ppid, comm=comm, cmdline=cmdline, tty=tty, cwd=cwd)


def fake_notify(calls):
    """返回记录调用参数的 notify mock（agent_id -> 摘要列表）。"""

    def notify(instance, summary):
        calls.append((instance["agent_id"], summary))

    return notify


class IdentifyRulesTest(unittest.TestCase):
    """identify()：识别规则、双进程合并、一次性/subagent/自身排除。"""

    def test_codex_pair_merged_into_one(self):
        procs = {
            100: make_proc(100, 1, "node", ["node", CODEX_BIN_WRAP], tty="/dev/pts/1", cwd=CWD),
            101: make_proc(101, 100, "codex", [CODEX_BIN_NATIVE]),
        }
        found = ab.identify(procs)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].kind, found[0].pid), ("codex", 100))
        self.assertEqual(found[0].tty, "/dev/pts/1")
        self.assertEqual(found[0].cwd, CWD)

    def test_codex_anchor_tty_falls_back_to_native_child(self):
        procs = {
            110: make_proc(110, 1, "node", ["node", CODEX_BIN_WRAP]),
            111: make_proc(111, 110, "codex", [CODEX_BIN_NATIVE], tty="/dev/pts/2"),
        }
        found = ab.identify(procs)
        self.assertEqual(found[0].tty, "/dev/pts/2")

    def test_codex_exec_onetime_excluded(self):
        procs = {
            200: make_proc(200, 1, "node", ["node", CODEX_BIN_WRAP, "exec", "打个招呼"]),
            201: make_proc(201, 200, "codex", [CODEX_BIN_NATIVE]),
        }
        self.assertEqual(ab.identify(procs), [])

    def test_codex_review_subcommand_excluded(self):
        procs = {
            210: make_proc(210, 1, "node", ["node", CODEX_BIN_WRAP, "review"]),
            211: make_proc(211, 210, "codex", [CODEX_BIN_NATIVE]),
        }
        self.assertEqual(ab.identify(procs), [])

    def test_codex_subagent_child_ignored(self):
        # 原生 codex 的子进程（ppid 不是 node 锚点）不应被单独编号
        procs = {
            120: make_proc(120, 1, "node", ["node", CODEX_BIN_WRAP]),
            121: make_proc(121, 120, "codex", [CODEX_BIN_NATIVE]),
            122: make_proc(122, 121, "codex", [CODEX_BIN_NATIVE, "--subagent"]),
        }
        found = ab.identify(procs)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pid, 120)

    def test_claude_detected(self):
        procs = {
            300: make_proc(
                300, 1, "claude", ["claude", "--dangerously-skip-permissions"], tty="/dev/pts/3"
            ),
        }
        found = ab.identify(procs)
        self.assertEqual([(i.kind, i.pid) for i in found], [("claude", 300)])

    def test_claude_print_mode_excluded(self):
        procs = {
            310: make_proc(310, 1, "claude", ["claude", "-p", "hi"]),
            311: make_proc(311, 1, "claude", ["claude", "--print", "hi"]),
        }
        self.assertEqual(ab.identify(procs), [])

    def test_claude_masked_tool_child_excluded(self):
        # 实测回归：Claude Code 把 shell 命令子进程 comm 伪装成 claude.exe
        # （真实案例：comm=claude.exe，cmdline=ugrep ...），不得误判为实例
        procs = {
            315: make_proc(315, 1, "claude.exe", ["ugrep", "-G", "--hidden", "-E", "(^Ran|^OK)"]),
            316: make_proc(316, 1, "claude.exe", ["bash", "-c", "echo hi"]),
        }
        self.assertEqual(ab.identify(procs), [])

    def test_claude_subagent_child_excluded(self):
        procs = {
            320: make_proc(320, 1, "claude", ["claude"]),
            321: make_proc(321, 320, "claude", ["claude", "--subagent"]),
        }
        found = ab.identify(procs)
        self.assertEqual([i.pid for i in found], [320])

    def test_hermes_yolo_detected(self):
        procs = {
            400: make_proc(400, 1, "hermes", [HERMES_PY, HERMES_MAIN, "--yolo"], tty="/dev/pts/4"),
        }
        found = ab.identify(procs)
        self.assertEqual([(i.kind, i.pid) for i in found], [("hermes", 400)])

    def test_hermes_tui_and_chat_detected(self):
        procs = {
            410: make_proc(410, 1, "python", [HERMES_PY, HERMES_MAIN, "--tui"]),
            411: make_proc(411, 1, "python", [HERMES_PY, HERMES_MAIN, "chat"]),
        }
        found = ab.identify(procs)
        self.assertEqual(len(found), 2)

    def test_hermes_gateway_subcommand_excluded(self):
        procs = {
            420: make_proc(420, 1, "python", [HERMES_PY, HERMES_MAIN, "gateway"]),
            421: make_proc(421, 1, "python", [HERMES_PY, HERMES_MAIN, "cron"]),
        }
        self.assertEqual(ab.identify(procs), [])

    def test_hermes_without_interactive_flag_excluded(self):
        procs = {
            430: make_proc(430, 1, "python", [HERMES_PY, HERMES_MAIN]),
        }
        self.assertEqual(ab.identify(procs), [])

    def test_hermes_subagent_child_excluded(self):
        procs = {
            440: make_proc(440, 1, "hermes", [HERMES_PY, HERMES_MAIN, "--yolo"]),
            441: make_proc(441, 440, "hermes", [HERMES_PY, HERMES_MAIN, "--yolo", "--sub"]),
        }
        found = ab.identify(procs)
        self.assertEqual([i.pid for i in found], [440])

    def test_opencode_tui_detected(self):
        # 2026-08-25 实测：comm=opencode，cmdline[0] basename=opencode
        procs = {
            450: make_proc(450, 1, "opencode", ["opencode", "--yolo"], tty="/dev/pts/5"),
        }
        found = ab.identify(procs)
        self.assertEqual([(i.kind, i.pid) for i in found], [("opencode", 450)])

    def test_opencode_session_resume_detected(self):
        procs = {
            451: make_proc(451, 1, "opencode", ["opencode", "-s", "ses_demo01"]),
        }
        found = ab.identify(procs)
        self.assertEqual([(i.kind, i.pid) for i in found], [("opencode", 451)])

    def test_opencode_run_onetime_excluded(self):
        procs = {
            452: make_proc(452, 1, "opencode", ["opencode", "run", "OK"]),
            453: make_proc(453, 1, "opencode", ["opencode", "serve"]),
        }
        self.assertEqual(ab.identify(procs), [])

    def test_opencode_masked_comm_child_excluded(self):
        # cmdline[0] 校验：comm 伪装成 opencode 的 shell 子进程不误判
        procs = {
            454: make_proc(454, 1, "opencode", ["bash", "-c", "echo hi"]),
        }
        self.assertEqual(ab.identify(procs), [])

    def test_opencode_subagent_child_excluded(self):
        procs = {
            455: make_proc(455, 1, "opencode", ["opencode", "--yolo"]),
            456: make_proc(456, 455, "opencode", ["opencode", "--yolo", "--sub"]),
        }
        found = ab.identify(procs)
        self.assertEqual([i.pid for i in found], [455])

    def test_ab_self_and_components_excluded(self):
        procs = {
            500: make_proc(500, 1, "python", ["/usr/bin/python3", "-m", "agentbus.discover"]),
            501: make_proc(501, 1, "python", ["/usr/bin/python3", "-m", "agentbus.cli", "whoami"]),
            502: make_proc(502, 1, "python", ["/usr/local/bin/agentbus", "whoami"]),
            503: make_proc(503, 1, "python", ["/usr/local/bin/ab", "room", "ls"]),
        }
        self.assertEqual(ab.identify(procs), [])

    def test_mixed_kinds_all_counted(self):
        procs = {
            600: make_proc(600, 1, "node", ["node", CODEX_BIN_WRAP]),
            601: make_proc(601, 600, "codex", [CODEX_BIN_NATIVE]),
            610: make_proc(610, 1, "claude", ["claude"]),
            620: make_proc(620, 1, "hermes", [HERMES_PY, HERMES_MAIN, "--yolo"]),
        }
        found = ab.identify(procs)
        self.assertEqual(sorted(i.kind for i in found), ["claude", "codex", "hermes"])
        self.assertEqual(len(found), 3)


class ProcScanTest(unittest.TestCase):
    """scan_procs()：伪造 /proc 树解析。"""

    def test_scan_procs_reads_all_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fake_proc(
                pathlib.Path(tmp),
                [
                    {
                        "pid": 10,
                        "ppid": 1,
                        "comm": "node",
                        "cmdline": ["node", CODEX_BIN_WRAP],
                        "tty": "/dev/pts/1",
                        "cwd": CWD,
                    },
                    {
                        "pid": 11,
                        "ppid": 10,
                        "comm": "codex",
                        "cmdline": [CODEX_BIN_NATIVE],
                    },
                ],
            )
            # 加一个非数字目录，确认被跳过
            (root / "version").mkdir()
            procs = ab.scan_procs(str(root))
        self.assertEqual(set(procs), {10, 11})
        self.assertEqual(procs[10].comm, "node")
        self.assertEqual(procs[10].cmdline, ["node", CODEX_BIN_WRAP])
        self.assertEqual(procs[10].ppid, 1)
        self.assertEqual(procs[10].tty, "/dev/pts/1")
        self.assertEqual(procs[10].cwd, CWD)
        self.assertEqual(procs[11].ppid, 10)

    def test_scan_procs_missing_root_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            procs = ab.scan_procs(str(pathlib.Path(tmp) / "nonexistent"))
        self.assertEqual(procs, {})


class UpdateInstancesTest(unittest.TestCase):
    """update_instances()：编号分配、冷却期、复活、pid 复用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "test.db"
        self.conn = ab.connect(str(self.db))
        self.addCleanup(self.conn.close)

    def _scan(self, round_dir, entries, now):
        """用给定进程集合与受控时间跑一轮 scan_once。"""
        root = pathlib.Path(self.tmp.name) / f"round-{round_dir}"
        make_fake_proc(root, entries)
        return ab.scan_once(
            str(root),
            str(self.db),
            now=now,
            notify_func=fake_notify([]),
        )

    def test_first_discovery_allocates_sequence(self):
        result = self._scan(
            "a",
            [
                {"pid": 100, "ppid": 1, "comm": "node", "cmdline": ["node", CODEX_BIN_WRAP]},
                {"pid": 101, "ppid": 100, "comm": "codex", "cmdline": [CODEX_BIN_NATIVE]},
                {"pid": 200, "ppid": 1, "comm": "node", "cmdline": ["node", CODEX_BIN_WRAP]},
                {"pid": 201, "ppid": 200, "comm": "codex", "cmdline": [CODEX_BIN_NATIVE]},
            ],
            NOW0,
        )
        ids = sorted(r["agent_id"] for r in result["online"])
        self.assertEqual(ids, ["codex#1", "codex#2"])
        # 双进程对只编一个号：两对 → 恰 2 行
        self.assertEqual(len(result["online"]), 2)

    def test_offline_within_cooldown_does_not_reuse_number(self):
        # 第一轮：claude#1（pid 100）
        self._scan(
            "a",
            [
                {"pid": 100, "ppid": 1, "comm": "claude", "cmdline": ["claude"]},
            ],
            NOW0,
        )
        row = self.conn.execute("SELECT agent_id FROM instances WHERE pid=100").fetchone()
        self.assertEqual(row["agent_id"], "claude#1")

        # 第二轮（+1min）：pid 100 消失 → offline
        result2 = self._scan("b", [], shift(NOW0, 1))
        self.assertEqual(result2["cooling"][0]["agent_id"], "claude#1")

        # 第三轮（+2min，冷却期内）：新 pid 200 → 不能拿 claude#1
        result3 = self._scan(
            "c",
            [{"pid": 200, "ppid": 1, "comm": "claude", "cmdline": ["claude"]}],
            shift(NOW0, 2),
        )
        ids = [r["agent_id"] for r in result3["online"]]
        self.assertEqual(ids, ["claude#2"])
        # 冷却中的旧编号仍在
        self.assertEqual(result3["cooling"][0]["agent_id"], "claude#1")

    def test_cooldown_expiry_releases_number(self):
        self._scan(
            "a",
            [
                {"pid": 100, "ppid": 1, "comm": "claude", "cmdline": ["claude"]},
            ],
            NOW0,
        )
        self._scan("b", [], shift(NOW0, 1))
        # 61 分钟后：旧行清理，新进程复用最小空号 claude#1
        result = self._scan(
            "c",
            [{"pid": 300, "ppid": 1, "comm": "claude", "cmdline": ["claude"]}],
            shift(NOW0, 61),
        )
        self.assertEqual([r["agent_id"] for r in result["online"]], ["claude#1"])
        self.assertEqual(result["cooling"], [])
        # 释放事件应被记录
        self.assertTrue(any("released" in e for e in result["events"]))

    def test_same_pid_same_kind_resurrect_keeps_number(self):
        self._scan(
            "a",
            [
                {
                    "pid": 400,
                    "ppid": 1,
                    "comm": "hermes",
                    "cmdline": [HERMES_PY, HERMES_MAIN, "--yolo"],
                },
            ],
            NOW0,
        )
        self._scan("b", [], shift(NOW0, 5))
        result = self._scan(
            "c",
            [
                {
                    "pid": 400,
                    "ppid": 1,
                    "comm": "hermes",
                    "cmdline": [HERMES_PY, HERMES_MAIN, "--yolo"],
                }
            ],
            shift(NOW0, 10),
        )
        self.assertEqual([r["agent_id"] for r in result["online"]], ["hermes#1"])
        self.assertTrue(any("revived" in e for e in result["events"]))

    def test_same_pid_other_kind_reallocates(self):
        self._scan(
            "a",
            [
                {"pid": 500, "ppid": 1, "comm": "node", "cmdline": ["node", CODEX_BIN_WRAP]},
                {"pid": 501, "ppid": 500, "comm": "codex", "cmdline": [CODEX_BIN_NATIVE]},
            ],
            NOW0,
        )
        result = self._scan(
            "b",
            [
                {
                    "pid": 500,
                    "ppid": 1,
                    "comm": "hermes",
                    "cmdline": [HERMES_PY, HERMES_MAIN, "--yolo"],
                }
            ],
            shift(NOW0, 1),
        )
        self.assertEqual([r["agent_id"] for r in result["online"]], ["hermes#1"])


class ArchiveAndFreezeTest(unittest.TestCase):
    """回归：归档接入守护；requeue/notify 跳冻结近似房。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "test.db"
        self.conn = ab.connect(str(self.db))
        self.addCleanup(self.conn.close)
        self.conn.executescript(BUS_TABLES_DDL)
        self.conn.executescript("CREATE TABLE IF NOT EXISTS rooms_extra(x);") if False else None
        self.conn.commit()

    def _seed_room(self, state, closed_minutes_ago):
        closed_ts = shift(NOW0, -closed_minutes_ago) if state == "closed" else None
        self.conn.execute(
            "INSERT INTO rooms(id, topic, state, closed_ts) VALUES(1, 'r', ?, ?)",
            (state, closed_ts),
        )
        self.conn.execute(
            "INSERT INTO messages(id, ts, room_id, sender, body) VALUES(1, ?, 1, 'claude#1', 'x')",
            (NOW0,),
        )
        self.conn.commit()

    def test_archive_stale_rooms_removes_old_closed(self):
        self._seed_room("closed", 31 * 24 * 60)
        n = ab.archive_stale_rooms(self.conn, NOW0)
        self.assertEqual(n, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_archive_keeps_recent_closed(self):
        self._seed_room("closed", 60)
        self.assertEqual(ab.archive_stale_rooms(self.conn, NOW0), 0)

    def test_requeue_skips_frozen_rooms(self):
        self._seed_room("closed", 25 * 60)  # closed 25h 前 → 冻结近似
        self.conn.execute(
            "INSERT INTO message_reads(message_id, member, status, delivered_at) "
            "VALUES(1, 'codex#1', 'delivered', ?)",
            (shift(NOW0, -31),),
        )
        self.conn.commit()
        self.assertEqual(ab.requeue_stale_delivered(self.conn, NOW0), 0)
        row = self.conn.execute("SELECT status FROM message_reads WHERE message_id=1").fetchone()
        self.assertEqual(row["status"], "delivered")  # 未被推进不可取状态

    def test_requeue_still_works_in_open_rooms(self):
        self._seed_room("open", 0)
        self.conn.execute(
            "INSERT INTO message_reads(message_id, member, status, delivered_at) "
            "VALUES(1, 'codex#1', 'delivered', ?)",
            (shift(NOW0, -31),),
        )
        self.conn.commit()
        self.assertEqual(ab.requeue_stale_delivered(self.conn, NOW0), 1)

    def test_notify_skips_frozen_rooms(self):
        self._seed_room("closed", 25 * 60)
        self.conn.execute(
            "INSERT INTO instances(pid, kind, agent_id, tty, cwd, cmdline,"
            " started_at, last_seen, state, offline_at)"
            " VALUES(910, 'codex', 'codex#1', '/dev/pts/3', '/x', 'node codex',"
            " ?, ?, 'online', NULL)",
            (NOW0, NOW0),
        )
        self.conn.execute(
            "INSERT INTO message_reads(message_id, member, status, delivered_at) "
            "VALUES(1, 'codex#1', 'new', NULL)"
        )
        self.conn.commit()
        calls = []
        n = ab.collect_notifications(self.conn, NOW0, fake_notify(calls), {}, 60.0)
        self.assertEqual(n, 0)
        self.assertEqual(calls, [])

    def test_fixture_message_reads_ddl_matches_authoritative(self):
        # 本文件的 fixture DDL 声称为冻结 DDL 逐字副本——message_reads 段
        # 必须与包内 schema.sql 的同段一致（此前缺 acked_at 已漂移）
        import importlib.resources

        schema = (importlib.resources.files("agentbus") / "schema" / "schema.sql").read_text(
            encoding="utf-8"
        )
        start = schema.index("CREATE TABLE IF NOT EXISTS message_reads(")
        end = schema.index(");", start) + 2
        self.assertIn(
            BUS_TABLES_DDL.strip()
            .split("CREATE TABLE IF NOT EXISTS message_reads(")[1]
            .split(");")[0]
            .strip(),
            schema[start:end],
        )

    def test_component_predicate_no_substring_false_positive(self):
        # agent 的 cmdline 提到总线模块名不得把自己排除掉
        self.assertFalse(ab._is_ab_component(["claude", "explain src/agentbus.mcp design"]))
        self.assertFalse(
            ab._is_ab_component(["claude", "--resume", "why is agentbus.discover slow?"])
        )
        # 总线自身形态仍排除
        self.assertTrue(ab._is_ab_component(["/usr/bin/python3", "-m", "agentbus.mcp"]))
        self.assertTrue(ab._is_ab_component(["/usr/local/bin/agentbus-mcp"]))
        self.assertTrue(ab._is_ab_component(["agentbus-mcp"]))


class MaintenanceTest(unittest.TestCase):
    """重投与通知：delivered 30min 超时、节流、表缺失容错。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "test.db"
        self.conn = ab.connect(str(self.db))
        self.addCleanup(self.conn.close)

    def _seed_bus_tables(self):
        self.conn.executescript(BUS_TABLES_DDL)
        self.conn.commit()

    def _seed_message(self, member, status, delivered_at):
        self.conn.execute("INSERT INTO rooms(id, topic) VALUES(1, '测试房')")
        self.conn.execute(
            "INSERT INTO messages(id, ts, room_id, sender, body)"
            " VALUES(1, ?, 1, 'claude#1', '你好')",
            (NOW0,),
        )
        self.conn.execute(
            "INSERT INTO message_reads(message_id, member, status, delivered_at)"
            " VALUES(1, ?, ?, ?)",
            (member, status, delivered_at),
        )
        self.conn.commit()

    def test_requeue_stale_delivered(self):
        self._seed_bus_tables()
        self._seed_message("codex#1", "delivered", shift(NOW0, -31))
        self.assertEqual(ab.requeue_stale_delivered(self.conn, NOW0), 1)
        row = self.conn.execute(
            "SELECT status, delivered_at FROM message_reads WHERE member='codex#1'"
        ).fetchone()
        self.assertEqual((row["status"], row["delivered_at"]), ("new", None))

    def test_requeue_leaves_fresh_delivered_alone(self):
        self._seed_bus_tables()
        self._seed_message("codex#1", "delivered", shift(NOW0, -29))
        self.assertEqual(ab.requeue_stale_delivered(self.conn, NOW0), 0)
        row = self.conn.execute(
            "SELECT status FROM message_reads WHERE member='codex#1'"
        ).fetchone()
        self.assertEqual(row["status"], "delivered")

    def test_requeue_skips_when_table_missing(self):
        # 只有 instances 表（buslib 尚未建 message_reads）时不抛异常
        self.assertEqual(ab.requeue_stale_delivered(self.conn, NOW0), 0)

    def test_notify_new_message_with_throttle(self):
        self._seed_bus_tables()
        self._seed_message("codex#1", "new", None)
        self.conn.execute(
            "INSERT INTO instances(pid, kind, agent_id, tty, cwd, cmdline,"
            " started_at, last_seen, state, offline_at)"
            f" VALUES(900, 'codex', 'codex#1', '/dev/pts/3', '{CWD}',"
            " 'node codex', ?, ?, 'online', NULL)",
            (NOW0, NOW0),
        )
        self.conn.commit()

        calls = []
        last = {}
        n1 = ab.collect_notifications(self.conn, NOW0, fake_notify(calls), last, 60.0)
        self.assertEqual(n1, 1)
        self.assertEqual(calls[0][0], "codex#1")
        self.assertIn("1 new message(s)", calls[0][1])
        self.assertIn("room#1", calls[0][1])
        self.assertIn("测试房", calls[0][1])

        # 节流：60s 内重复扫描不再通知
        n2 = ab.collect_notifications(
            self.conn,
            NOW0,
            fake_notify(calls),
            last,
            60.0,
        )
        self.assertEqual(n2, 0)

        # 超过节流窗口后再次通知
        n3 = ab.collect_notifications(self.conn, shift(NOW0, 61), fake_notify(calls), last, 60.0)
        self.assertEqual(n3, 1)
        self.assertEqual(len(calls), 2)

    def test_notify_skips_when_tables_missing(self):
        last = {}
        n = ab.collect_notifications(self.conn, NOW0, fake_notify([]), last, 60.0)
        self.assertEqual(n, 0)

    def test_notify_only_online_instances(self):
        self._seed_bus_tables()
        self._seed_message("codex#1", "new", None)
        # codex#1 离线（冷却期）→ 不通知
        self.conn.execute(
            "INSERT INTO instances(pid, kind, agent_id, tty, cwd, cmdline,"
            " started_at, last_seen, state, offline_at)"
            f" VALUES(901, 'codex', 'codex#1', '/dev/pts/3', '{CWD}',"
            " 'node codex', ?, ?, 'offline', ?)",
            (NOW0, NOW0, NOW0),
        )
        self.conn.commit()
        calls = []
        n = ab.collect_notifications(self.conn, NOW0, fake_notify(calls), {}, 60.0)
        self.assertEqual(n, 0)
        self.assertEqual(calls, [])


class EndToEndTest(unittest.TestCase):
    """scan_once / main --once 端到端：编号无重复、输出可读。"""

    def test_scan_once_end_to_end_no_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            db = tmp_path / "bus.db"
            root = make_fake_proc(
                tmp_path / "proc",
                [
                    {
                        "pid": 100,
                        "ppid": 1,
                        "comm": "node",
                        "cmdline": ["node", CODEX_BIN_WRAP],
                        "tty": "/dev/pts/1",
                    },
                    {"pid": 101, "ppid": 100, "comm": "codex", "cmdline": [CODEX_BIN_NATIVE]},
                    {
                        "pid": 200,
                        "ppid": 1,
                        "comm": "node",
                        "cmdline": ["node", CODEX_BIN_WRAP],
                        "tty": "/dev/pts/2",
                    },
                    {"pid": 201, "ppid": 200, "comm": "codex", "cmdline": [CODEX_BIN_NATIVE]},
                    {
                        "pid": 300,
                        "ppid": 1,
                        "comm": "claude",
                        "cmdline": ["claude"],
                        "tty": "/dev/pts/3",
                    },
                    {
                        "pid": 400,
                        "ppid": 1,
                        "comm": "hermes",
                        "cmdline": [HERMES_PY, HERMES_MAIN, "--yolo"],
                        "tty": "/dev/pts/4",
                    },
                ],
            )
            result = ab.scan_once(str(root), str(db), now=NOW0, notify_func=fake_notify([]))
            ids = [r["agent_id"] for r in result["online"]]
            self.assertEqual(len(ids), len(set(ids)), f"编号重复：{ids}")
            self.assertEqual(set(ids), {"codex#1", "codex#2", "claude#1", "hermes#1"})
            self.assertEqual(len(result["online"]), 4)

    def test_main_once_output_lists_instances_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            db = tmp_path / "bus.db"
            root = make_fake_proc(
                tmp_path / "proc",
                [
                    {
                        "pid": 100,
                        "ppid": 1,
                        "comm": "node",
                        "cmdline": ["node", CODEX_BIN_WRAP],
                        "tty": "/dev/pts/1",
                    },
                    {"pid": 101, "ppid": 100, "comm": "codex", "cmdline": [CODEX_BIN_NATIVE]},
                    {
                        "pid": 300,
                        "ppid": 1,
                        "comm": "claude",
                        "cmdline": ["claude"],
                        "tty": "/dev/pts/3",
                    },
                ],
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ab.main(["--once", "--db", str(db), "--proc", str(root)])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("total 2 instances (online 2, cooling 0)", text)
            ids = re.findall(r"^\[online\] (\S+)", text, flags=re.M)
            self.assertEqual(len(ids), len(set(ids)), f"编号重复：{ids}")
            self.assertIn("codex#1", ids)
            self.assertIn("claude#1", ids)


if __name__ == "__main__":
    unittest.main()
