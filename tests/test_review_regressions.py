"""test_review_regressions — 外部评审发现的回归用例。

每条都对应一次评审发现的缺陷：修复前失败、修复后通过。集中存放，
防止同类问题在后续改动中悄悄复归。
"""

import os
import shutil
import tempfile
import unittest

from agentbus import buslib
from agentbus.buslib import (
    BusError,
    ack_message,
    claim_task,
    close_room,
    connect,
    create_room,
    create_task,
    ensure_dm_room,
    init_schema,
    poll,
    poll_revision,
    post_message,
    task_relation_add,
    task_relation_remove,
    task_report,
    task_review,
)


def _ts(minutes_ago=0):
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime(buslib.TIME_FORMAT)


class ReviewBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ab-reg-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.conn = connect(os.path.join(self.tmp, "bus.db"))
        init_schema(self.conn)
        for pid, aid, kind in (
            (2101, "codex#1", "codex"),
            (2102, "claude#1", "claude"),
            (2103, "claude#2", "claude"),
            (2104, "codex#9", "codex"),
        ):
            self.conn.execute(
                "INSERT INTO instances(pid, kind, agent_id, state) VALUES(?,?,?,'online')",
                (pid, kind, aid),
            )
        self.conn.commit()
        self.rid = create_room(self.conn, "r", "human", ["codex#1", "claude#1", "claude#2"])


class PollSinceRedeliveryTest(ReviewBase):
    """P0：重投只改 message_reads（无 revision 触发器），--since 快路径
    不得把重投消息静默吞掉。"""

    def test_since_fast_path_drains_redelivered(self):
        mid = post_message(self.conn, "do X", "human", room_id=self.rid)
        poll(self.conn, "claude#1")
        rev = poll_revision(self.conn)
        # 让 delivered_at 落到 31 分钟前并重投（生产路径）
        self.conn.execute(
            "UPDATE message_reads SET delivered_at=? WHERE message_id=? AND member='claude#1'",
            (_ts(31), mid),
        )
        self.conn.commit()
        self.assertEqual(buslib.redeliver_stale(self.conn), 1)
        # revision 未变 —— 快路径仍必须返回这条消息
        self.assertEqual(poll_revision(self.conn), rev)
        res = poll(self.conn, "claude#1", since=rev)
        ids = [m["id"] for r in res["rooms"] for m in r["messages"]]
        self.assertIn(mid, ids)

    def test_since_fast_path_still_fast_when_nothing_pending(self):
        post_message(self.conn, "x", "human", room_id=self.rid)
        poll(self.conn, "claude#1")
        ack_message(self.conn, 1, "claude#1")
        rev = poll_revision(self.conn)
        res = poll(self.conn, "claude#1", since=rev)
        self.assertEqual(res["rooms"], [])

    def test_human_since_fast_path_considers_lazy_reads(self):
        rid = create_room(self.conn, "h", "human", ["codex#1"])
        post_message(self.conn, "hi", "codex#1", room_id=rid)
        # human 从未 poll：无 read 行 —— 快路径必须返回，不得跳过
        rev = poll_revision(self.conn)
        res = poll(self.conn, "human", since=rev)
        self.assertTrue(res["rooms"])


class MentionDeliveryTest(ReviewBase):
    """P1：@自己 不得造成零投递；@通配 必须展开；body-@ 兜底定向。"""

    def test_self_mention_falls_back_to_broadcast(self):
        mid = post_message(
            self.conn, "broadcast-intent", "human", room_id=self.rid, mentions=["human"]
        )
        rows = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        ]
        self.assertEqual(sorted(rows), ["claude#1", "claude#2", "codex#1"])

    def test_wildcard_mention_expands(self):
        rid = create_room(self.conn, "w", "human", ["claude:*"], wild=True)
        mid = post_message(
            self.conn, "hey all claudes", "human", room_id=rid, mentions=["claude:*"]
        )
        rows = sorted(
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        )
        self.assertEqual(rows, ["claude#1", "claude#2"])
        # 通配房内 @ 具体编号在 body 中也应定向
        mid2 = post_message(self.conn, "@claude#2 单独看这个", "human", room_id=rid)
        rows2 = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid2,)
            )
        ]
        self.assertEqual(rows2, ["claude#2"])

    def test_body_mention_directs_delivery(self):
        mid = post_message(
            self.conn, "@codex#1 这条只给你（协议 body-@ 承诺）", "human", room_id=self.rid
        )
        rows = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        ]
        self.assertEqual(rows, ["codex#1"])
        # 消息仍在房史（群聊语义：可看历史、不推送）
        hist = buslib.show_room(self.conn, self.rid, "claude#2", history=True)
        self.assertIn(mid, [m["id"] for m in hist["messages"]])

    def test_unmatched_body_at_keeps_broadcast(self):
        mid = post_message(self.conn, "@nobody hi", "human", room_id=self.rid)
        rows = sorted(
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        )
        self.assertEqual(rows, ["claude#1", "claude#2", "codex#1"])


class DmGenerationTest(ReviewBase):
    """P1：closed dm 不得成永久死路；编号回收复用后新实例不得继承旧房。"""

    def test_closed_dm_continues_in_next_generation(self):
        rid = ensure_dm_room(self.conn, "codex#1", "claude#1")
        close_room(self.conn, rid, "codex#1")
        rid2 = post_message(self.conn, "again", "codex#1", to="claude#1")
        self.assertIsInstance(rid2, int)
        row = self.conn.execute("SELECT COUNT(*) FROM rooms WHERE dm_key LIKE 'dm:%'").fetchone()[0]
        self.assertEqual(row, 2)  # 旧房保留，新房另立
        res = poll(self.conn, "claude#1")
        bodies = [m["body"] for r in res["rooms"] for m in r["messages"]]
        self.assertIn("again", bodies)

    def test_recycled_instance_number_gets_fresh_dm(self):
        rid = ensure_dm_room(self.conn, "codex#1", "human")
        # 编号回收：codex#1 变成一个新进程（明显晚于建房时间，超出时钟容差）
        self.conn.execute("UPDATE instances SET started_at=? WHERE agent_id='codex#1'", (_ts(-30),))
        self.conn.commit()
        rid2 = ensure_dm_room(self.conn, "codex#1", "human")
        self.assertNotEqual(rid, rid2)

    def test_clock_skew_does_not_fragment_dm(self):
        rid = ensure_dm_room(self.conn, "codex#1", "human")
        # 分钟级时钟偏斜（started_at 略领先）不得每次新建房
        self.conn.execute("UPDATE instances SET started_at=? WHERE agent_id='codex#1'", (_ts(-2),))
        self.conn.commit()
        for _ in range(3):
            self.assertEqual(ensure_dm_room(self.conn, "codex#1", "human"), rid)
        count = self.conn.execute("SELECT COUNT(*) FROM rooms WHERE dm_key LIKE 'dm:%'").fetchone()[
            0
        ]
        self.assertEqual(count, 1)

    def test_revived_instance_keeps_room(self):
        rid = ensure_dm_room(self.conn, "codex#1", "human")
        self.conn.execute(
            "UPDATE instances SET started_at=?, state='online' WHERE agent_id='codex#1'",
            (_ts(120),),
        )  # 更早（复活不更新）
        self.conn.commit()
        self.assertEqual(ensure_dm_room(self.conn, "codex#1", "human"), rid)


class RelationGuardsTest(ReviewBase):
    """P0：relation rm 必须与 add 对称鉴权；P2：blocks 链禁环。"""

    def _pair(self):
        a = create_task(self.conn, self.rid, "A", "human")
        b = create_task(self.conn, self.rid, "B", "human")
        return a, b

    def test_remove_requires_membership(self):
        a, b = self._pair()
        task_relation_add(self.conn, b, "parent", a, "human")
        with self.assertRaises(BusError) as ctx:
            # codex#9 是活实例但非本房成员（此前 rm 对任何人放行）
            task_relation_remove(self.conn, b, "parent", a, "codex#9")
        # claude#2 不在 room —— 或 E_NOT_MEMBER（非成员）；不变量：边还在
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_relations").fetchone()[0], 1)

    def test_remove_missing_edge_is_not_found(self):
        a, b = self._pair()
        with self.assertRaises(BusError) as ctx:
            task_relation_remove(self.conn, b, "parent", a, "human")
        self.assertEqual(ctx.exception.code, "E_NOT_FOUND")

    def test_remove_bad_type_rejected(self):
        a, b = self._pair()
        with self.assertRaises(BusError) as ctx:
            task_relation_remove(self.conn, b, "sibling", a, "human")
        self.assertEqual(ctx.exception.code, "E_INVALID")

    def test_remove_happy_path_unlocks_done_gate(self):
        parent = create_task(self.conn, self.rid, "父", "human")
        child = create_task(self.conn, self.rid, "子", "human")
        task_relation_add(self.conn, child, "parent", parent, "human")
        claim_task(self.conn, parent, "codex#1")
        task_report(self.conn, parent, "codex#1", "完成")
        task_relation_remove(self.conn, child, "parent", parent, "human")
        self.assertEqual(buslib.task_done(self.conn, parent, "human")["status"], "done")

    def test_blocks_cycle_rejected(self):
        a, b = self._pair()
        task_relation_add(self.conn, a, "blocks", b, "human")
        with self.assertRaises(BusError) as ctx:
            task_relation_add(self.conn, b, "blocks", a, "human")
        self.assertEqual(ctx.exception.code, "E_INVALID")


class ReviewSelfTest(ReviewBase):
    """P2：assignee 不得自我打回自己的任务。"""

    def test_assignee_cannot_review_own_task(self):
        tid = create_task(self.conn, self.rid, "t", "human")
        claim_task(self.conn, tid, "codex#1")
        task_report(self.conn, tid, "codex#1", "done")
        with self.assertRaises(BusError) as ctx:
            task_review(self.conn, tid, "codex#1", "self-rework")
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")
        # 其他成员与 human 可以打回
        self.assertEqual(task_review(self.conn, tid, "claude#1", "rework")["status"], "in_progress")


class SoftCapUnseenMemberTest(ReviewBase):
    """P1：软上限不得删除"尚有成员无 read 行"的消息（审计承诺）。"""

    def test_unseen_member_protects_message(self):
        rid = create_room(self.conn, "cap", "human", ["codex#1", "claude#1"])
        # codex#1 定向发 6 条给 claude#1（human 无 read 行）
        for i in range(6):
            post_message(self.conn, f"m{i}", "codex#1", room_id=rid, mentions=["claude#1"], cap=3)
            poll(self.conn, "claude#1")
        for i in range(1, 7):
            ack_message(self.conn, i, "claude#1")
        # 全部被 ack，但 human 从未收到 → 一条都不许删
        count = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE room_id=?", (rid,)
        ).fetchone()[0]
        self.assertEqual(count, 6)
        # 对照：无 unseen 成员时（human 自己 poll 过后）可裁剪
        poll(self.conn, "human")
        post_message(self.conn, "trigger", "codex#1", room_id=rid, mentions=["claude#1"], cap=3)
        poll(self.conn, "claude#1")
        count2 = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE room_id=?", (rid,)
        ).fetchone()[0]
        self.assertLess(count2, 7)  # 部分已可裁剪


class UpdateValidationTest(ReviewBase):
    """P2：title 非字符串类型校验收敛为 E_INVALID。"""

    def test_non_string_title_rejected(self):
        tid = create_task(self.conn, self.rid, "t", "human")
        with self.assertRaises(BusError) as ctx:
            buslib.update_task(self.conn, tid, {"title": 123}, "human")
        self.assertEqual(ctx.exception.code, "E_INVALID")

    def test_empty_change_returns_parsed_labels(self):
        tid = create_task(self.conn, self.rid, "t", "human", labels=["a"])
        out = buslib.update_task(self.conn, tid, {}, "human")
        self.assertEqual(out["labels"], ["a"])  # 解析后数组，非原始 JSON 串


class AbAsStrippingTest(ReviewBase):
    """P2：AB_AS 空白串不得成为伪身份。"""

    def test_blank_ab_as_ignored(self):
        # 空白 AB_AS 回落祖先链/tty；本构造无 tty → E_NO_IDENTITY
        with self.assertRaises(BusError) as ctx:
            buslib.resolve_identity(
                self.conn,
                environ={"AB_AS": "   "},
                ppid_of=lambda p: 0,
                ttyname_of=lambda: (_ for _ in ()).throw(OSError("no tty")),
            )
        self.assertEqual(ctx.exception.code, "E_NO_IDENTITY")


class HardeningRegressionsTest(ReviewBase):
    """回归：通配房软上限 / human 超集重投 / blocks 分支环。"""

    def test_wildcard_room_soft_cap_still_trims(self):
        rid = create_room(self.conn, "wcap", "claude#1", ["claude#1", "claude:*"], wild=True)
        for i in range(8):
            mid = post_message(self.conn, f"m{i}", "claude#1", room_id=rid, cap=3)
            poll(self.conn, "claude#2")
            ack_message(self.conn, mid, "claude#2")
        # 通配成员（字面量 claude:* 无 read 行）不得让软上限永久失效
        count = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE room_id=?", (rid,)
        ).fetchone()[0]
        self.assertLessEqual(count, 3, f"wildcard room grew unbounded: {count}")

    def test_human_superset_since_drains_agent_only_rooms(self):
        # 房无 human 成员行（agent 自建）——human 超集仍须在 --since 下收到
        rid = create_room(self.conn, "agent-only", "codex#1", ["codex#1", "claude#2"])
        mid = post_message(self.conn, "x", "codex#1", room_id=rid)
        poll(self.conn, "claude#2")
        self.conn.execute(
            "UPDATE message_reads SET delivered_at=? WHERE message_id=? AND member='claude#2'",
            (_ts(31), mid),
        )
        self.conn.commit()
        self.assertEqual(buslib.redeliver_stale(self.conn), 1)
        rev = poll_revision(self.conn)
        res = poll(self.conn, "human", since=rev)
        self.assertTrue(res["rooms"], "human superset missed an agent-only room")

    def test_blocks_branch_cycle_rejected(self):
        t1 = create_task(self.conn, self.rid, "T1", "human")
        t2 = create_task(self.conn, self.rid, "T2", "human")
        t3 = create_task(self.conn, self.rid, "T3", "human")
        task_relation_add(self.conn, t1, "blocks", t2, "human")
        task_relation_add(self.conn, t1, "blocks", t3, "human")
        with self.assertRaises(BusError) as ctx:  # 分支上仍须检出环
            task_relation_add(self.conn, t3, "blocks", t1, "human")
        self.assertEqual(ctx.exception.code, "E_INVALID")


class DoctorUriEscapeTest(unittest.TestCase):
    """回归：含 ?/# 的 AB_DB 不得被 URI 截断（读错文件/写出文件）。"""

    def test_open_readonly_handles_special_chars(self):
        from agentbus import doctor as doctor_mod

        with tempfile.TemporaryDirectory() as tmp:
            weird = os.path.join(tmp, "q?hash#tag.db")
            conn = connect(weird)
            init_schema(conn)
            conn.close()
            ro = doctor_mod._open_readonly(weird)
            try:
                tables = {
                    r[0] for r in ro.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            finally:
                ro.close()
            self.assertIn("meta", tables)
            # 截断路径不得被创建
            self.assertFalse(os.path.exists(os.path.join(tmp, "q")))


class ComplianceRegressionsTest(ReviewBase):
    """回归：ack 活动信号 / 冻结房快路径 / dm 归档代际 / @ 语义。"""

    def test_ack_counts_as_frozen_room_activity(self):
        rid = create_room(self.conn, "fr", "codex#1", ["codex#1", "claude#2"])
        mid = post_message(self.conn, "x", "human", room_id=rid)
        poll(self.conn, "claude#2")
        close_room(self.conn, rid, "codex#1")  # 立即关房
        # 25h 前 poll、1h 前 ack（真实序列）→ 不得冻结（协议：poll/ack 活动）
        self.conn.execute("UPDATE rooms SET closed_ts=? WHERE id=?", (_ts(40 * 60), rid))
        self.conn.execute(
            "UPDATE message_reads SET delivered_at=? WHERE message_id=?", (_ts(25 * 60), mid)
        )
        self.conn.commit()
        ack_message(self.conn, mid, "claude#2")  # acked_at = now
        self.assertFalse(buslib._room_frozen(self.conn, rid))

    def test_frozen_room_does_not_block_since_fast_path(self):
        rid = create_room(self.conn, "fz", "codex#1", ["codex#1", "claude#2"])
        post_message(self.conn, "stale", "codex#1", room_id=rid)
        close_room(self.conn, rid, "codex#1")
        self.conn.execute("UPDATE rooms SET closed_ts=? WHERE id=?", (_ts(48 * 60), rid))
        self.conn.commit()
        # claude#2 的 read 行仍 new，但房已冻结（poll 不会返回它）——
        # 快路径不得因此永久失效
        self.assertFalse(buslib._has_pending(self.conn, "claude#2"))
        rev = poll_revision(self.conn)
        res = poll(self.conn, "claude#2", since=rev)
        self.assertEqual(res["rooms"], [])

    def test_dm_generation_not_reused_after_archive(self):
        rid1 = ensure_dm_room(self.conn, "codex#1", "human")
        close_room(self.conn, rid1, "codex#1")
        rid2 = ensure_dm_room(self.conn, "codex#1", "human")
        self.assertNotEqual(rid1, rid2)
        # 归档 gen-1 后，不得复用旧键（否则同族出现两个 open 房）
        self.conn.execute("DELETE FROM room_members WHERE room_id=?", (rid1,))
        self.conn.execute("DELETE FROM rooms WHERE id=?", (rid1,))
        self.conn.commit()
        rid3 = ensure_dm_room(self.conn, "codex#1", "human")
        self.assertEqual(rid3, rid2)
        opens = self.conn.execute(
            "SELECT COUNT(*) FROM rooms WHERE (dm_key LIKE 'dm:%') AND state='open'"
        ).fetchone()[0]
        self.assertEqual(opens, 1)

    def test_human_mention_allowed_in_agent_room(self):
        rid = create_room(self.conn, "no-human-row", "codex#1", ["claude#2"])
        mid = post_message(self.conn, "@human 请裁决", "codex#1", room_id=rid, mentions=["human"])
        rows = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        ]
        self.assertEqual(rows, ["human"])

    def test_wildcard_mention_of_missing_instance_rejected(self):
        rid = create_room(self.conn, "w", "claude#1", ["claude:*"], wild=True)
        with self.assertRaises(BusError) as ctx:
            post_message(
                self.conn, "hi", "claude#1", room_id=rid, mentions=["claude#42"]
            )  # 不存在的编号
        self.assertEqual(ctx.exception.code, "E_NOT_FOUND")


class ConvergenceRegressionsTest(ReviewBase):
    """回归：并发 ack 不降级 / 存储目录不可用走 E_DB。"""

    def test_concurrent_poll_does_not_downgrade_acked(self):
        import threading

        rid = create_room(self.conn, "cc", "human", ["codex#1"])
        mids = [
            post_message(self.conn, str(i), "human", room_id=rid, cap=100000) for i in range(200)
        ]
        db_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
        errors = []

        def acker():
            c = None
            try:
                c = connect(db_path)
                for mid in mids:
                    try:
                        ack_message(c, mid, "codex#1")
                    except BusError:
                        pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                if c is not None:
                    c.close()

        def poller():
            c = None
            try:
                c = connect(db_path)
                poll(c, "codex#1")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                if c is not None:
                    c.close()

        threads = [threading.Thread(target=acker) for _ in range(3)]
        threads.append(threading.Thread(target=poller))
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        downgraded = self.conn.execute(
            "SELECT COUNT(*) FROM message_reads WHERE status='delivered' AND acked_at IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(downgraded, 0, "concurrent poll must not downgrade acked rows")

    def test_unwritable_db_dir_raises_e_db(self):
        bad = os.path.join(self.tmp, "ro", "sub", "bus.db")
        os.makedirs(os.path.dirname(os.path.dirname(bad)))
        os.chmod(os.path.join(self.tmp, "ro"), 0o500)
        self.addCleanup(os.chmod, os.path.join(self.tmp, "ro"), 0o700)
        if os.geteuid() == 0:  # root 无视权限位，跳过
            self.skipTest("running as root; permission bits not enforced")
        with self.assertRaises(BusError) as ctx:
            connect(bad)
        self.assertEqual(ctx.exception.code, "E_DB")


class UsabilityRegressionsTest(ReviewBase):
    """易用性实测轮回归：register / 分页 / --only 校验 / --at+--to 互斥。"""

    def test_register_lifecycle(self):
        # 新 id → registered；幂等重注册 → online；human/坏格式拒绝
        self.assertEqual(
            buslib.register_identities(self.conn, ["mybot#1"]),
            [{"agent_id": "mybot#1", "status": "registered"}],
        )
        prof = buslib.register_identities(self.conn, ["mybot#1"])
        self.assertEqual(prof[0]["status"], "online")
        with self.assertRaises(BusError) as ctx:
            buslib.register_identities(self.conn, ["human"])
        self.assertEqual(ctx.exception.code, "E_INVALID")
        with self.assertRaises(BusError):
            buslib.register_identities(self.conn, ["Not Upper"])
        # 注册后的 id 可作房间成员（Path B 核心诉求）
        rid = create_room(self.conn, "b", "human", ["mybot#1"])
        self.assertTrue(buslib.is_member(self.conn, rid, "mybot#1"))

    def test_room_list_pagination(self):
        new_ids = [create_room(self.conn, f"room{i:02d}", "human", ["codex#1"]) for i in range(5)]
        all_ids = [self.rid] + new_ids  # setUp 的房也在列表里
        page1 = buslib.list_rooms(self.conn, "human", limit=2, offset=0)
        page2 = buslib.list_rooms(self.conn, "human", limit=2, offset=2)
        self.assertEqual([r["id"] for r in page1], all_ids[:2])
        self.assertEqual([r["id"] for r in page2], all_ids[2:4])
        self.assertEqual(len(buslib.list_rooms(self.conn, "human")), 6)
        self.assertEqual(len(buslib.list_rooms(self.conn, "human", limit=2)), 2)

    def test_poll_only_unknown_token_rejected(self):
        create_room(self.conn, "r2", "human", ["codex#1"])
        with self.assertRaises(BusError) as ctx:
            poll(self.conn, "codex#1", only="mention+taskk")
        self.assertEqual(ctx.exception.code, "E_INVALID")
        res = poll(self.conn, "codex#1", only="mention+task")
        self.assertIn("rooms", res)

    def test_at_with_to_rejected(self):
        with self.assertRaises(BusError) as ctx:
            post_message(self.conn, "x", "human", to="codex#1", mentions=["codex#1"])
        self.assertEqual(ctx.exception.code, "E_INVALID")


if __name__ == "__main__":
    unittest.main()
