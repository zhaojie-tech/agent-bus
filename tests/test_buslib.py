#!/usr/bin/env python3
"""test_buslib — agent-bus 核心库单测（一律临时 DB，严禁写真实 bus.db）。"""

import importlib.resources
import os
import shutil
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta

from agentbus import buslib
from agentbus.buslib import (
    BusError,
    ack_message,
    archive_closed,
    close_room,
    connect,
    create_room,
    dm_key,
    ensure_dm_room,
    init_schema,
    invite_member,
    list_rooms,
    poll,
    post_message,
    redeliver_stale,
    remove_member,
    reply_message,
    resolve_identity,
    resolve_mentions,
    set_busy,
    show_room,
)


def ts_ago(minutes=0, hours=0, days=0):
    return (datetime.now(UTC) - timedelta(minutes=minutes, hours=hours, days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class BuslibTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ab-test-")
        self.db = os.path.join(self.tmp, "test.db")
        self.conn = connect(self.db)
        init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, pid, agent_id, kind, state="online", tty=None):
        self.conn.execute(
            "INSERT INTO instances(pid, kind, agent_id, tty, state) VALUES(?,?,?,?,?)",
            (pid, kind, agent_id, tty, state),
        )
        self.conn.commit()

    def assertBusError(self, code, fn, *args, **kwargs):
        with self.assertRaises(BusError) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def counts(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class SchemaTests(BuslibTestCase):
    def test_tables_and_indexes(self):
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertEqual(
            tables,
            {
                "rooms",
                "room_members",
                "messages",
                "message_reads",
                "instances",
                "global_revision",
                "tasks",
                "task_relations",
                "meta",
            },
        )
        indexes = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertEqual(
            indexes,
            {
                "idx_reads_status",
                "idx_msg_room",
                "idx_tasks_room_status",
                "task_relations_one_parent",
            },
        )

    def test_schema_file_matches_embedded(self):
        # 冻结契约自洽：包内 schema/schema.sql 资源与 buslib 内嵌副本逐字一致
        schema_sql = (importlib.resources.files("agentbus") / "schema" / "schema.sql").read_text(
            encoding="utf-8"
        )
        self.assertEqual(schema_sql, buslib._FALLBACK_SCHEMA_DDL)


class IdentityTests(BuslibTestCase):
    def test_flag_and_env_priority(self):
        self.assertEqual(resolve_identity(self.conn, as_human=True), ("human", "flag"))
        self.assertEqual(resolve_identity(self.conn, environ={"AB_AS": "human"}), ("human", "env"))
        # --as 显式参数优先于环境变量
        self.assertEqual(
            resolve_identity(self.conn, as_human=True, environ={"AB_AS": "human"}),
            ("human", "flag"),
        )

    def test_ancestor_chain(self):
        self.seed(500, "claude#3", "claude")
        chain = {100: 50, 50: 500, 500: 0}
        self.assertEqual(
            resolve_identity(self.conn, pid=100, environ={}, ppid_of=chain.get),
            ("claude#3", "ancestor"),
        )

    def test_tty_fallback(self):
        self.seed(1, "codex#1", "codex", tty="/dev/pts/4")
        ident = resolve_identity(
            self.conn, pid=999, environ={}, ppid_of=lambda p: 0, ttyname_of=lambda: "/dev/pts/4"
        )
        self.assertEqual(ident, ("codex#1", "tty"))

    def test_tty_ambiguous_and_missing(self):
        self.seed(1, "codex#1", "codex", tty="/dev/pts/4")
        self.seed(2, "codex#2", "codex", tty="/dev/pts/4")
        self.assertBusError(
            "E_AMBIGUOUS",
            resolve_identity,
            self.conn,
            pid=999,
            environ={},
            ppid_of=lambda p: 0,
            ttyname_of=lambda: "/dev/pts/4",
        )
        self.assertBusError(
            "E_NO_IDENTITY",
            resolve_identity,
            self.conn,
            pid=999,
            environ={},
            ppid_of=lambda p: 0,
            ttyname_of=lambda: "/dev/pts/9",
        )

    def test_no_tty_at_all(self):
        def broken():
            raise OSError("not a tty")

        self.assertBusError(
            "E_NO_IDENTITY",
            resolve_identity,
            self.conn,
            pid=999,
            environ={},
            ppid_of=lambda p: 0,
            ttyname_of=broken,
        )


class RoomTests(BuslibTestCase):
    def test_create_and_list(self):
        self.seed(101, "codex#1", "codex")
        self.seed(102, "codex#2", "codex")
        rid = create_room(self.conn, "登录重构", "codex#1", ["codex#1", "codex#2"])
        room = show_room(self.conn, rid, "human")
        self.assertEqual(room["topic"], "登录重构")
        self.assertEqual(room["state"], "open")
        roles = {m["member"]: m["role"] for m in room["members"]}
        self.assertEqual(roles, {"codex#1": "creator", "codex#2": "member"})
        # agent 只见自己的 room；--all 仅 human
        self.assertEqual([r["id"] for r in list_rooms(self.conn, "codex#1")], [rid])
        self.assertEqual(len(list_rooms(self.conn, "human")), 1)
        self.assertBusError("E_NOT_MEMBER", list_rooms, self.conn, "codex#1", True)

    def test_show_non_member_denied(self):
        self.seed(101, "codex#1", "codex")
        self.seed(102, "codex#2", "codex")
        rid = create_room(self.conn, "房", "codex#1", ["codex#1"])
        self.assertBusError("E_NOT_MEMBER", show_room, self.conn, rid, "codex#2")

    def test_invite_creates_system_summary_and_idempotent(self):
        self.seed(101, "codex#1", "codex")
        self.seed(102, "codex#2", "codex")
        rid = create_room(self.conn, "交接房", "codex#1", ["codex#1"])
        post_message(self.conn, "上下文一：接口见契约", "human", room_id=rid)
        post_message(self.conn, "上下文二：我做后端", "human", room_id=rid)
        invite_member(self.conn, rid, "codex#2", actor="codex#1")
        self.assertEqual(self.counts("room_members"), 2)
        sys_msg = self.conn.execute("SELECT * FROM messages WHERE sender='system'").fetchone()
        self.assertIsNotNone(sys_msg)
        self.assertIn("[invite]", sys_msg["body"])
        self.assertIn("上下文一", sys_msg["body"])
        self.assertIn("上下文二", sys_msg["body"])
        # 摘要消息只 fan-out 给新成员，不影响老成员
        reads = self.conn.execute(
            "SELECT member, status FROM message_reads WHERE message_id=?", (sys_msg["id"],)
        ).fetchall()
        self.assertEqual([(r["member"], r["status"]) for r in reads], [("codex#2", "new")])
        # 幂等：重复邀请不重复发摘要
        invite_member(self.conn, rid, "codex#2", actor="codex#1")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM messages WHERE sender='system'").fetchone()[0],
            1,
        )
        self.assertEqual(self.counts("room_members"), 2)

    def test_invite_errors(self):
        self.seed(101, "codex#1", "codex")
        self.seed(102, "codex#2", "codex")
        rid = create_room(self.conn, "房", "codex#1", ["codex#1"])
        self.assertBusError(
            "E_NOT_MEMBER", invite_member, self.conn, rid, "codex#2", actor="codex#2"
        )
        self.assertBusError("E_INVALID", invite_member, self.conn, rid, "claude:*", actor="codex#1")
        self.assertBusError(
            "E_NOT_FOUND", invite_member, self.conn, rid, "codex#9", actor="codex#1"
        )
        close_room(self.conn, rid, "codex#1")
        self.assertBusError(
            "E_ROOM_CLOSED", invite_member, self.conn, rid, "codex#2", actor="codex#1"
        )

    def test_remove_member_clears_unacked_reads(self):
        self.seed(101, "codex#1", "codex")
        self.seed(102, "codex#2", "codex")
        rid = create_room(self.conn, "房", "codex#1", ["codex#1", "codex#2"])
        m1 = post_message(self.conn, "一", "human", room_id=rid)
        post_message(self.conn, "二", "human", room_id=rid)
        poll(self.conn, "codex#2")
        ack_message(self.conn, m1, "codex#2")
        remove_member(self.conn, rid, "codex#2", actor="codex#1")
        self.assertEqual(self.counts("room_members"), 1)
        # 已 ack 的保留，未 ack（new/delivered）的一并删除
        rows = self.conn.execute(
            "SELECT message_id, status FROM message_reads WHERE member='codex#2'"
        ).fetchall()
        self.assertEqual([(r["message_id"], r["status"]) for r in rows], [(m1, "acked")])
        # codex#1 的 reads 不受影响
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM message_reads WHERE member='codex#1'"
            ).fetchone()[0],
            2,
        )


class DmTests(BuslibTestCase):
    def test_dm_key_ordering(self):
        self.assertEqual(dm_key("claude#3", "codex#1"), "dm:claude#3+codex#1")
        self.assertEqual(dm_key("codex#1", "claude#3"), "dm:claude#3+codex#1")

    def test_dm_concurrent_two_directions_single_room(self):
        # Acceptance：两个方向同时 post --to 只产生 1 个 dm room
        self.seed(101, "codex#1", "codex")
        self.seed(102, "codex#2", "codex")
        results = {}

        def worker(sender, target, key):
            # sqlite 连接与线程绑定，连接必须在各自线程内创建
            conn = connect(self.db)
            try:
                results[key] = post_message(conn, f"hi from {sender}", sender, to=target)
            except Exception as exc:  # noqa: BLE001
                results[key] = exc
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("codex#1", "codex#2", "t1"))
        t2 = threading.Thread(target=worker, args=("codex#2", "codex#1", "t2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertIsInstance(results["t1"], int, results)
        self.assertIsInstance(results["t2"], int, results)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM rooms WHERE dm_key IS NOT NULL").fetchone()[0],
            1,
        )
        self.assertEqual(self.counts("messages"), 2)
        self.assertEqual(self.counts("room_members"), 2)
        self.assertEqual(self.counts("message_reads"), 2)

    def test_dm_target_validation(self):
        self.seed(101, "codex#1", "codex")
        self.assertBusError("E_NOT_FOUND", ensure_dm_room, self.conn, "codex#1", "codex#9")
        self.assertBusError("E_INVALID", ensure_dm_room, self.conn, "codex#1", "claude:*")
        self.assertBusError("E_INVALID", ensure_dm_room, self.conn, "codex#1", "codex#1")

    def test_dm_stale_flag(self):
        self.seed(101, "codex#1", "codex")
        self.seed(102, "codex#2", "codex", state="offline")
        ensure_dm_room(self.conn, "codex#1", "codex#2")
        item = list_rooms(self.conn, "codex#1")[0]
        self.assertTrue(item["stale_dm"])
        self.conn.execute("UPDATE instances SET state='online' WHERE agent_id='codex#2'")
        self.conn.commit()
        self.assertFalse(list_rooms(self.conn, "codex#1")[0]["stale_dm"])


class MessageTests(BuslibTestCase):
    def seed_trio(self):
        for pid, aid, kind in [
            (101, "codex#1", "codex"),
            (102, "codex#2", "codex"),
            (103, "claude#3", "claude"),
        ]:
            self.seed(pid, aid, kind)

    def test_fan_out_three_members_three_new_rows(self):
        # Acceptance：3 成员 room 发 1 条 → message_reads 生成 3 行 new
        self.seed_trio()
        rid = create_room(self.conn, "三人房", "codex#1", ["codex#1", "codex#2", "claude#3"])
        mid = post_message(self.conn, "广播", "human", room_id=rid)
        rows = self.conn.execute(
            "SELECT member, status FROM message_reads WHERE message_id=?", (mid,)
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["status"] == "new" for r in rows))

    def test_fan_out_excludes_sender(self):
        self.seed_trio()
        rid = create_room(self.conn, "房", "codex#1", ["codex#1", "codex#2"])
        mid = post_message(self.conn, "由我发出", "codex#1", room_id=rid)
        rows = self.conn.execute(
            "SELECT member FROM message_reads WHERE message_id=?", (mid,)
        ).fetchall()
        self.assertEqual([r["member"] for r in rows], ["codex#2"])

    def test_wildcard_expands_to_online_instances(self):
        self.seed(101, "claude#1", "claude")
        self.seed(102, "claude#2", "claude")
        self.seed(103, "claude#3", "claude", state="offline")
        self.seed(104, "codex#2", "codex")
        rid = create_room(self.conn, "通配房", "human", ["claude:*", "codex#2"], wild=True)
        mid = post_message(self.conn, "广播", "human", room_id=rid)
        rows = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        ]
        self.assertEqual(sorted(rows), ["claude#1", "claude#2", "codex#2"])
        # 通配成员发送时自身同样被排除
        mid2 = post_message(self.conn, "claude 发言", "claude#1", room_id=rid)
        rows2 = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid2,)
            )
        ]
        # 创建者 human 自动入会，因此 human 也是成员行
        self.assertEqual(sorted(rows2), ["claude#2", "codex#2", "human"])
        # 无 --wild 时通配成员被拒绝
        self.assertBusError("E_INVALID", create_room, self.conn, "房", "human", ["claude:*"], False)

    def test_post_poll_ack_flow(self):
        self.seed_trio()
        rid = create_room(self.conn, "房", "codex#1", ["codex#1", "codex#2"])
        mid = post_message(self.conn, "你好", "human", room_id=rid)
        res = poll(self.conn, "codex#1")
        self.assertEqual(len(res["rooms"]), 1)
        self.assertEqual(res["rooms"][0]["room_id"], rid)
        self.assertEqual([m["id"] for m in res["rooms"][0]["messages"]], [mid])
        read = self.conn.execute(
            "SELECT status, delivered_at FROM message_reads "
            "WHERE message_id=? AND member='codex#1'",
            (mid,),
        ).fetchone()
        self.assertEqual(read["status"], "delivered")
        self.assertIsNotNone(read["delivered_at"])
        # 已取走不再返回
        self.assertEqual(poll(self.conn, "codex#1")["rooms"], [])
        ack_message(self.conn, mid, "codex#1")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM message_reads WHERE message_id=? AND member='codex#1'", (mid,)
            ).fetchone()["status"],
            "acked",
        )

    def test_human_poll_sees_all_once(self):
        self.seed(101, "codex#1", "codex")
        rid = create_room(self.conn, "房", "codex#1", ["codex#1"])
        post_message(self.conn, "来自 codex", "codex#1", room_id=rid)
        post_message(self.conn, "人类自己说", "human", room_id=rid)
        res = poll(self.conn, "human")
        self.assertEqual(len(res["rooms"][0]["messages"]), 2)
        self.assertEqual(poll(self.conn, "human")["rooms"], [])


class MentionTests(BuslibTestCase):
    """群聊 @ 定向：消息入历史全员可见，投递只建给被 @ 成员。"""

    def seed_trio(self):
        for pid, aid, kind in [
            (101, "codex#1", "codex"),
            (102, "codex#2", "codex"),
            (103, "claude#3", "claude"),
        ]:
            self.seed(pid, aid, kind)

    def test_mention_delivers_only_to_mentioned(self):
        self.seed_trio()
        rid = create_room(self.conn, "群", "human", ["codex#1", "codex#2", "claude#3"])
        mid = post_message(
            self.conn, "@codex#2 这条只给你", "human", room_id=rid, mentions=["codex#2"]
        )
        rows = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        ]
        self.assertEqual(rows, ["codex#2"])
        # 被 @ 者 poll 收到；其他人 poll 不到
        self.assertEqual(len(poll(self.conn, "codex#2")["rooms"][0]["messages"]), 1)
        self.assertEqual(poll(self.conn, "codex#1")["rooms"], [])
        self.assertEqual(poll(self.conn, "claude#3")["rooms"], [])
        # 其他人仍可从历史看到该消息（群聊语义：可看历史、不推送）
        hist = show_room(self.conn, rid, "codex#1", history=True)
        self.assertIn(mid, [m["id"] for m in hist["messages"]])

    def test_mention_refs_stores_targets(self):
        self.seed_trio()
        rid = create_room(self.conn, "群", "human", ["codex#1", "codex#2"])
        mid = post_message(
            self.conn,
            "@codex#1 @codex#2 你们俩",
            "human",
            room_id=rid,
            mentions=["codex#1", "codex#2"],
        )
        refs = self.conn.execute("SELECT refs FROM messages WHERE id=?", (mid,)).fetchone()["refs"]
        self.assertEqual(refs, "@codex#1 @codex#2")

    def test_mention_non_member_rejected(self):
        self.seed_trio()
        rid = create_room(self.conn, "群", "human", ["codex#1"])
        self.assertBusError(
            "E_NOT_MEMBER",
            post_message,
            self.conn,
            "@codex#2 不是成员",
            "human",
            room_id=rid,
            mentions=["codex#2"],
        )

    def test_mention_in_wildcard_room(self):
        self.seed(101, "claude#1", "claude")
        self.seed(102, "claude#2", "claude")
        self.seed(103, "codex#1", "codex")
        rid = create_room(self.conn, "通配群", "human", ["claude:*", "codex#1"], wild=True)
        # claude:* 房里 @claude#2 视为定向该实例
        mid = post_message(
            self.conn, "@claude#2 单独喊你", "human", room_id=rid, mentions=["claude#2"]
        )
        rows = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        ]
        self.assertEqual(rows, ["claude#2"])

    def test_resolve_mentions_from_body(self):
        self.seed_trio()
        rid = create_room(self.conn, "群", "human", ["codex#1", "codex#2", "claude#3"])
        body = "@codex#1 看 @codex#2 的结论；@nobody#9 不算；@human 发起者"
        self.assertEqual(resolve_mentions(self.conn, rid, body), ["codex#1", "codex#2", "human"])
        # sender 自身被排除
        self.assertEqual(resolve_mentions(self.conn, rid, "@codex#1 hi", identity="codex#1"), [])

    def test_no_mentions_keeps_full_fanout(self):
        self.seed_trio()
        rid = create_room(self.conn, "群", "human", ["codex#1", "codex#2"])
        # body 里 @token 不命中成员（软约定）：mentions 未显式传 → 维持全员 fan-out
        mid = post_message(self.conn, "@unknown 群发一条", "human", room_id=rid)
        rows = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        ]
        self.assertEqual(sorted(rows), ["codex#1", "codex#2"])


class BusyTests(BuslibTestCase):
    """忙闲自报：编排者选人依据（instances.busy，NULL=空闲）。"""

    def test_set_and_clear_busy(self):
        self.seed(101, "codex#1", "codex")
        set_busy(self.conn, "codex#1", "登录重构")
        self.assertEqual(
            self.conn.execute("SELECT busy FROM instances WHERE agent_id='codex#1'").fetchone()[
                "busy"
            ],
            "登录重构",
        )
        set_busy(self.conn, "codex#1", None)  # ab free
        self.assertIsNone(
            self.conn.execute("SELECT busy FROM instances WHERE agent_id='codex#1'").fetchone()[
                "busy"
            ]
        )
        set_busy(self.conn, "codex#1", "   ")  # 空白理由视为空闲
        self.assertIsNone(
            self.conn.execute("SELECT busy FROM instances WHERE agent_id='codex#1'").fetchone()[
                "busy"
            ]
        )

    def test_busy_unknown_instance_rejected(self):
        self.assertBusError("E_NOT_FOUND", set_busy, self.conn, "codex#9", "x")

    def test_migration_adds_busy_column_to_old_db(self):
        # 旧库（无 busy 列）：init_schema 幂等补列
        old = os.path.join(self.tmp, "old.db")
        c2 = connect(old)
        c2.executescript("""
            CREATE TABLE instances(
              pid INTEGER PRIMARY KEY, kind TEXT NOT NULL, agent_id TEXT UNIQUE,
              tty TEXT, cwd TEXT, cmdline TEXT, started_at TEXT, last_seen TEXT,
              state TEXT NOT NULL DEFAULT 'online', offline_at TEXT);
        """)
        c2.execute("INSERT INTO instances(pid, kind, agent_id) VALUES(1,'codex','codex#1')")
        c2.commit()
        init_schema(c2)
        cols = {r[1] for r in c2.execute("PRAGMA table_info(instances)")}
        self.assertIn("busy", cols)
        set_busy(c2, "codex#1", "迁移后可正常自报")
        self.assertEqual(
            c2.execute("SELECT busy FROM instances WHERE agent_id='codex#1'").fetchone()["busy"],
            "迁移后可正常自报",
        )
        c2.close()

    def test_migration_adds_acked_at_column_to_old_db(self):
        # 旧库（无 acked_at 列）：init_schema 幂等补列，且冻结活动可利用它
        old = os.path.join(self.tmp, "old-reads.db")
        c2 = connect(old)
        c2.executescript("""
            CREATE TABLE instances(
              pid INTEGER PRIMARY KEY, kind TEXT NOT NULL, agent_id TEXT UNIQUE,
              tty TEXT, cwd TEXT, cmdline TEXT, started_at TEXT, last_seen TEXT,
              state TEXT NOT NULL DEFAULT 'online', offline_at TEXT);
            CREATE TABLE message_reads(
              message_id INTEGER NOT NULL, member TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'new', delivered_at TEXT,
              PRIMARY KEY(message_id, member));
        """)
        c2.commit()
        init_schema(c2)
        cols = {r[1] for r in c2.execute("PRAGMA table_info(message_reads)")}
        self.assertIn("acked_at", cols)
        init_schema(c2)  # 幂等
        cols = {r[1] for r in c2.execute("PRAGMA table_info(message_reads)")}
        self.assertIn("acked_at", cols)
        c2.close()

    def test_idle_query_semantics(self):
        self.seed(101, "codex#1", "codex")
        self.seed(102, "codex#2", "codex")
        set_busy(self.conn, "codex#1", "压测排查")
        idle = [
            r["agent_id"]
            for r in self.conn.execute(
                "SELECT agent_id FROM instances "
                "WHERE state='online' AND agent_id IS NOT NULL AND busy IS NULL"
            )
        ]
        self.assertEqual(idle, ["codex#2"])


class MessagePermissionTests(BuslibTestCase):
    def seed_trio(self):
        for pid, aid, kind in [
            (101, "codex#1", "codex"),
            (102, "codex#2", "codex"),
            (103, "claude#3", "claude"),
        ]:
            self.seed(pid, aid, kind)

    def test_permission_non_member(self):
        self.seed_trio()
        rid = create_room(self.conn, "房", "codex#1", ["codex#1"])
        mid = post_message(self.conn, "一条", "human", room_id=rid)
        self.assertBusError("E_NOT_MEMBER", post_message, self.conn, "x", "codex#2", room_id=rid)
        self.assertBusError("E_NOT_MEMBER", ack_message, self.conn, mid, "codex#2")
        self.assertBusError("E_NOT_MEMBER", reply_message, self.conn, mid, "x", "codex#2")
        self.assertBusError("E_NOT_MEMBER", close_room, self.conn, rid, "codex#2")

    def test_reply_same_room(self):
        self.seed_trio()
        rid = create_room(self.conn, "房", "codex#1", ["codex#1", "codex#2"])
        m1 = post_message(self.conn, "问题", "codex#1", room_id=rid)
        m2 = reply_message(self.conn, m1, "回答", "codex#2")
        row = self.conn.execute(
            "SELECT room_id, reply_to FROM messages WHERE id=?", (m2,)
        ).fetchone()
        self.assertEqual(row["room_id"], rid)
        self.assertEqual(row["reply_to"], m1)
        # 回复的 fan-out 也排除发送者
        reads = [
            r["member"]
            for r in self.conn.execute("SELECT member FROM message_reads WHERE message_id=?", (m2,))
        ]
        self.assertEqual(reads, ["codex#1"])


class RedeliveryTests(BuslibTestCase):
    def test_redeliver_stale(self):
        self.seed(101, "codex#1", "codex")
        rid = create_room(self.conn, "房", "codex#1", ["codex#1"])
        mid = post_message(self.conn, "一条", "human", room_id=rid)
        poll(self.conn, "codex#1")
        self.conn.execute(
            "UPDATE message_reads SET delivered_at=? WHERE message_id=? AND member='codex#1'",
            (ts_ago(minutes=31), mid),
        )
        self.conn.commit()
        self.assertEqual(redeliver_stale(self.conn), 1)
        read = self.conn.execute(
            "SELECT status, delivered_at FROM message_reads "
            "WHERE message_id=? AND member='codex#1'",
            (mid,),
        ).fetchone()
        self.assertEqual(read["status"], "new")
        self.assertIsNone(read["delivered_at"])
        # 未到 30min 不重投
        poll(self.conn, "codex#1")
        self.conn.execute(
            "UPDATE message_reads SET delivered_at=? WHERE message_id=? AND member='codex#1'",
            (ts_ago(minutes=29), mid),
        )
        self.conn.commit()
        self.assertEqual(redeliver_stale(self.conn), 0)


class ClosedRoomTests(BuslibTestCase):
    def test_closed_room_three_stage_semantics(self):
        self.seed(101, "codex#1", "codex")
        rid = create_room(self.conn, "房", "codex#1", ["codex#1"])
        m1 = post_message(self.conn, "第一条", "human", room_id=rid)
        m2 = post_message(self.conn, "第二条", "human", room_id=rid)
        poll(self.conn, "codex#1")
        close_room(self.conn, rid, "codex#1")
        # ① close 后拒绝新消息/新邀请（幂等关闭不报错）
        self.assertBusError("E_ROOM_CLOSED", post_message, self.conn, "x", "human", room_id=rid)
        close_room(self.conn, rid, "codex#1")
        # ② 收尾窗口：未取走的消息仍可 poll / ack
        self.conn.execute(
            "UPDATE message_reads SET status='new', delivered_at=NULL "
            "WHERE message_id=? AND member='codex#1'",
            (m2,),
        )
        self.conn.commit()
        res = poll(self.conn, "codex#1")
        self.assertEqual([m["id"] for m in res["rooms"][0]["messages"]], [m2])
        ack_message(self.conn, m2, "codex#1")
        # ③ close 后 24h 无活动 → 冻结：poll 跳过，历史仍可读，重投跳过
        # 活动信号 = poll(delivered_at) 与 ack(acked_at) 的较晚者——两者
        # 都须回拨，构造"最后一次活动在 25 小时前"
        self.conn.execute("UPDATE rooms SET closed_ts=? WHERE id=?", (ts_ago(hours=25), rid))
        self.conn.execute(
            "UPDATE message_reads SET delivered_at=?, acked_at=? WHERE member='codex#1'",
            (ts_ago(hours=25), ts_ago(hours=25)),
        )
        self.conn.commit()
        self.assertEqual(poll(self.conn, "codex#1")["rooms"], [])
        data = show_room(self.conn, rid, "codex#1", history=True)
        self.assertEqual([m["id"] for m in data["messages"]], [m1, m2])
        self.assertEqual(redeliver_stale(self.conn), 0)

    def test_archive_closed_after_30_days(self):
        self.seed(101, "codex#1", "codex")
        rid = create_room(self.conn, "房", "codex#1", ["codex#1"])
        post_message(self.conn, "一条", "human", room_id=rid)
        close_room(self.conn, rid, "codex#1")
        self.conn.execute("UPDATE rooms SET closed_ts=? WHERE id=?", (ts_ago(days=31), rid))
        self.conn.commit()
        self.assertEqual(archive_closed(self.conn), 1)
        self.assertEqual(self.counts("rooms"), 0)
        self.assertEqual(self.counts("messages"), 0)
        self.assertEqual(self.counts("message_reads"), 0)
        self.assertEqual(self.counts("room_members"), 0)


class SoftCapTests(BuslibTestCase):
    def test_soft_cap_order(self):
        # 先删最老全 acked，再删最老全 delivered，绝不删 new
        self.seed(101, "codex#1", "codex")
        rid = create_room(self.conn, "房", "codex#1", ["codex#1"])
        for i in range(1, 6):
            post_message(self.conn, f"消息{i}", "human", room_id=rid, cap=5)
        poll(self.conn, "codex#1")  # 1..5 全部 delivered
        post_message(self.conn, "消息6", "human", room_id=rid, cap=5)  # 删最老 delivered(1)
        post_message(self.conn, "消息7", "human", room_id=rid, cap=5)  # 删 2
        post_message(self.conn, "消息8", "human", room_id=rid, cap=5)  # 删 3
        ids = {
            r["id"] for r in self.conn.execute("SELECT id FROM messages WHERE room_id=?", (rid,))
        }
        self.assertEqual(ids, {4, 5, 6, 7, 8})
        ack_message(self.conn, 4, "codex#1")
        ack_message(self.conn, 5, "codex#1")
        post_message(self.conn, "消息9", "human", room_id=rid, cap=5)  # 删最老全 acked(4)
        ids = {
            r["id"] for r in self.conn.execute("SELECT id FROM messages WHERE room_id=?", (rid,))
        }
        self.assertEqual(ids, {5, 6, 7, 8, 9})
        self.assertEqual(self.counts("messages"), 5)


if __name__ == "__main__":
    unittest.main()
