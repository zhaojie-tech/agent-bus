"""test_tasks — v3 任务层与表级 revision 契约测试。

claim 原子性、report→review(--back)→done 流程、done 仅 human、父任务子任务门禁、
关系规则、关房/归档联动、软上限 refs 整词保护、revision 触发与 poll --since 快路径。
规范条款见 docs/protocol-v3.md 任务协议章节。
"""

import os
import shutil
import tempfile
import unittest

from agentbus import buslib
from agentbus.buslib import (
    BusError,
    archive_closed,
    claim_task,
    close_room,
    connect,
    create_room,
    create_task,
    init_schema,
    poll,
    poll_revision,
    post_message,
    revision,
    task_block,
    task_cancel,
    task_done,
    task_relation_add,
    task_reopen,
    task_report,
    task_review,
)


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ab-task-test-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, "bus.db")
        self.conn = connect(self.db)
        init_schema(self.conn)
        # 种子实例（create_room 校验成员须在 instances 表）
        for pid, aid in ((1001, "codex#1"), (1002, "claude#9")):
            self.conn.execute(
                "INSERT INTO instances(pid, kind, agent_id, state) VALUES(?,?,?,'online')",
                (pid, aid.split("#")[0], aid),
            )
        self.conn.commit()
        self.rid = create_room(self.conn, "t", "human", members=["codex#1", "claude#9"])

    def test_revisions_bump(self):
        revision(self.conn, "rooms")
        post_message(self.conn, "hi", "human", room_id=self.rid)
        self.assertGreater(revision(self.conn, "messages"), 0)
        create_task(self.conn, self.rid, "T1", "human")
        self.assertEqual(revision(self.conn, "tasks"), 1)

    def test_poll_since_fast_path(self):
        current = poll_revision(self.conn)
        res = poll(self.conn, "human", since=current)
        self.assertEqual(res["rooms"], [])
        post_message(self.conn, "新消息", "human", room_id=self.rid)
        res = poll(self.conn, "human", since=current)
        self.assertTrue(res["rooms"], "since 未推进时应返回新消息")
        self.assertEqual(res["revision"], poll_revision(self.conn))

    def test_claim_atomic_and_conflict(self):
        tid = create_task(self.conn, self.rid, "认领测试", "human")
        task = claim_task(self.conn, tid, "codex#1")
        self.assertEqual(task["status"], "in_progress")
        self.assertEqual(task["assignee"], "codex#1")
        self.assertEqual(task["claim_version"], 2)
        with self.assertRaises(BusError) as ctx:
            claim_task(self.conn, tid, "claude#9")
        self.assertEqual(ctx.exception.code, "E_CONFLICT")
        # 非成员认领 → E_NOT_MEMBER（安全裁决先于状态）
        rid2 = create_room(self.conn, "t2", "human", members=["claude#9"])
        tid2 = create_task(self.conn, rid2, "T2", "human")
        with self.assertRaises(BusError) as ctx:
            claim_task(self.conn, tid2, "codex#1")
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")

    def test_claim_blocked_reports_invalid_not_conflict(self):
        # P0.5-1：blocked 是依赖阻塞不是被抢占，报因必须 E_INVALID 而非 E_CONFLICT
        tid = create_task(self.conn, self.rid, "阻塞任务", "human")
        claim_task(self.conn, tid, "codex#1")
        task_block(self.conn, tid, "codex#1", "等上游")
        with self.assertRaises(BusError) as ctx:
            claim_task(self.conn, tid, "claude#9")
        self.assertEqual(ctx.exception.code, "E_INVALID")
        self.assertIn("blocked", ctx.exception.message)

    def test_task_sys_msg_targets_directs_delivery(self):
        # P0.5-2：targets 定向投递——消息入房史，但只有目标成员有 new reads
        tid = create_task(self.conn, self.rid, "定向事件", "human")
        buslib._task_sys_msg(
            self.conn, self.rid, "[task] 定向测试", task_id=tid, targets=["claude#9"]
        )
        self.conn.commit()
        got = poll(self.conn, "claude#9")
        bodies = [m["body"] for r in got["rooms"] for m in r["messages"]]
        self.assertIn("[task] 定向测试", bodies)
        # 非目标成员 poll 不到
        got2 = poll(self.conn, "codex#1")
        bodies2 = [m["body"] for r in got2["rooms"] for m in r["messages"]]
        self.assertNotIn("[task] 定向测试", bodies2)
        # 消息本体仍在房史（show --history 语义），只是不投递
        row = self.conn.execute("SELECT refs FROM messages WHERE body='[task] 定向测试'").fetchone()
        self.assertEqual(row["refs"], f"task:{tid}")

    def test_poll_only_filter(self):
        # P1 组件 B：L3 收端裁剪——旁任务事件被滤除，@我与自己任务保留
        mine = create_task(self.conn, self.rid, "我的任务", "human")
        other = create_task(self.conn, self.rid, "旁人任务", "human")
        claim_task(self.conn, mine, "claude#9")
        claim_task(self.conn, other, "codex#1")
        task_report(self.conn, other, "codex#1", "旁人的汇报")  # system 通道→放行
        post_message(self.conn, "闲聊噪音", "codex#1", room_id=self.rid)
        post_message(self.conn, "@claude#9 注意返工口径", "human", room_id=self.rid)
        got = poll(self.conn, "claude#9", only="mention+task")
        bodies = [m["body"] for r in got["rooms"] for m in r["messages"]]
        self.assertNotIn("闲聊噪音", bodies)
        self.assertIn(f"[task] #{other} reported: 旁人的汇报", bodies)  # system 放行
        self.assertIn("@claude#9 注意返工口径", bodies)  # @我
        # 被滤除消息保持 new：全量 poll 仍可取
        full = poll(self.conn, "claude#9")
        bodies_full = [m["body"] for r in full["rooms"] for m in r["messages"]]
        self.assertIn("闲聊噪音", bodies_full)

    def test_report_review_back_done_flow(self):
        tid = create_task(self.conn, self.rid, "流程", "human")
        claim_task(self.conn, tid, "codex#1")
        t = task_report(self.conn, tid, "codex#1", "改动/验证/结果/风险")
        self.assertEqual(t["status"], "in_review")
        t = task_review(self.conn, tid, "human", "返工要求")
        self.assertEqual(t["status"], "in_progress")
        task_report(self.conn, tid, "codex#1", "返工完成")
        # done 仅 human
        with self.assertRaises(BusError):
            task_done(self.conn, tid, "codex#1")
        t = task_done(self.conn, tid, "human")
        self.assertEqual(t["status"], "done")

    def test_done_blocked_by_open_child(self):
        parent = create_task(self.conn, self.rid, "父", "human")
        child = create_task(self.conn, self.rid, "子", "human")
        task_relation_add(self.conn, child, "parent", parent, "human")
        claim_task(self.conn, parent, "codex#1")
        task_report(self.conn, parent, "codex#1", "父完成")
        with self.assertRaises(BusError) as ctx:
            task_done(self.conn, parent, "human")
        self.assertEqual(ctx.exception.code, "E_TASK_OPEN")
        task_cancel(self.conn, child, "human")
        t = task_done(self.conn, parent, "human")
        self.assertEqual(t["status"], "done")

    def test_relation_rules(self):
        a = create_task(self.conn, self.rid, "A", "human")
        b = create_task(self.conn, self.rid, "B", "human")
        c = create_task(self.conn, self.rid, "C", "human")
        with self.assertRaises(BusError):
            task_relation_add(self.conn, a, "parent", a, "human")  # 自环
        task_relation_add(self.conn, b, "parent", a, "human")
        with self.assertRaises(BusError):
            task_relation_add(self.conn, a, "parent", b, "human")  # 成环
        task_relation_add(self.conn, c, "parent", b, "human")
        with self.assertRaises(BusError):
            task_relation_add(self.conn, c, "parent", a, "human")  # 单父（DB 部分唯一索引）
        task_relation_add(self.conn, c, "blocks", a, "human")  # 不同对，合法
        with self.assertRaises(BusError):
            task_relation_add(self.conn, c, "blocks", a, "human")  # 同对重复边（PK 拦截）
        # 跨 room
        rid2 = create_room(self.conn, "t2", "human")
        d = create_task(self.conn, rid2, "D", "human")
        with self.assertRaises(BusError):
            task_relation_add(self.conn, d, "blocks", a, "human")

    def test_close_room_open_task_gate_and_force(self):
        tid = create_task(self.conn, self.rid, "未完成", "human")
        with self.assertRaises(BusError) as ctx:
            close_room(self.conn, self.rid, "human")
        self.assertEqual(ctx.exception.code, "E_TASK_OPEN")
        # --force：自动 cancel 未终态任务并关闭
        close_room(self.conn, self.rid, "human", force=True)
        row = self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["status"], "canceled")
        room = self.conn.execute("SELECT state FROM rooms WHERE id=?", (self.rid,)).fetchone()
        self.assertEqual(room["state"], "closed")

    def test_archive_cascade(self):
        create_task(self.conn, self.rid, "任务A", "human")
        create_task(self.conn, self.rid, "任务B", "human")
        rows = [r["id"] for r in self.conn.execute("SELECT id FROM tasks")]
        task_relation_add(self.conn, rows[1], "parent", rows[0], "human")
        close_room(self.conn, self.rid, "human", force=True)
        # 推到 30 天前触发归档
        self.conn.execute(
            "UPDATE rooms SET closed_ts=? WHERE id=?", (buslib._ago(31 * 86400), self.rid)
        )
        self.conn.commit()
        n = archive_closed(self.conn)
        self.assertEqual(n, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_relations").fetchone()[0], 0)

    def test_protected_message_ids_word_boundary(self):
        t1 = create_task(self.conn, self.rid, "开放任务", "human")
        post_message(self.conn, "x", "human", room_id=self.rid)
        post_message(self.conn, "y", "human", room_id=self.rid)
        ids = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM messages WHERE room_id=? ORDER BY id", (self.rid,)
            )
        ]
        self.conn.execute("UPDATE messages SET refs=? WHERE id=?", (f"task:{t1}", ids[0]))
        self.conn.execute(
            "UPDATE messages SET refs=? WHERE id=?", ("task:99", ids[1])
        )  # 99 不是真实任务 id
        self.conn.commit()
        prot = buslib.protected_message_ids(self.conn, self.rid)
        self.assertIn(ids[0], prot, "task:1 引用应受保护")
        self.assertNotIn(ids[1], prot, "task:99 不得被 task:1 匹配（整词边界）")
        task_cancel(self.conn, t1, "human")
        self.assertNotIn(ids[0], buslib.protected_message_ids(self.conn, self.rid))

    def test_block_cancel_reopen(self):
        tid = create_task(self.conn, self.rid, "阻塞流转", "human")
        claim_task(self.conn, tid, "codex#1")
        with self.assertRaises(BusError):
            task_block(self.conn, tid, "claude#9", "非归属者")
        t = task_block(self.conn, tid, "codex#1", "环境坏了")
        self.assertEqual(t["status"], "blocked")
        with self.assertRaises(BusError):
            task_reopen(self.conn, tid, "codex#1")  # 仅 human
        t = task_reopen(self.conn, tid, "human")
        self.assertEqual(t["status"], "todo")
        self.assertIsNone(t["assignee"])

    def test_task_comments_via_refs(self):
        tid = create_task(self.conn, self.rid, "评论任务", "human")
        post_message(self.conn, "进展A", "human", room_id=self.rid)
        mids = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM messages WHERE room_id=? ORDER BY id DESC LIMIT 1", (self.rid,)
            )
        ]
        self.conn.execute("UPDATE messages SET refs=? WHERE id=?", (f"task:{tid}", mids[0]))
        self.conn.commit()
        task = buslib.get_task(self.conn, tid, "human")
        ref_ids = [c["id"] for c in task["comments"]]
        self.assertIn(mids[0], ref_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TaskVisibilityTests(unittest.TestCase):
    """可见性铁律：非成员读/建边/review 一律 E_NOT_MEMBER。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ab-task-vis-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, "bus.db")
        self.conn = connect(self.db)
        init_schema(self.conn)
        for pid, aid in ((1001, "codex#1"), (1002, "claude#9")):
            self.conn.execute(
                "INSERT INTO instances(pid, kind, agent_id, state) VALUES(?,?,?,'online')",
                (pid, aid.split("#")[0], aid),
            )
        self.conn.commit()
        # room#1 成员：human + codex#1（claude#9 不在场）
        self.rid = create_room(self.conn, "v", "human", members=["codex#1"])
        self.tid = create_task(self.conn, self.rid, "私密任务", "human")

    def test_non_member_get_task(self):
        with self.assertRaises(BusError) as ctx:
            buslib.get_task(self.conn, self.tid, "claude#9")
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")

    def test_non_member_list_tasks(self):
        with self.assertRaises(BusError) as ctx:
            buslib.list_tasks(self.conn, self.rid, "claude#9")
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")

    def test_non_member_update_task(self):
        with self.assertRaises(BusError):
            buslib.update_task(self.conn, self.tid, {"title": "x"}, "claude#9")

    def test_member_visible(self):
        task = buslib.get_task(self.conn, self.tid, "codex#1")
        self.assertEqual(task["title"], "私密任务")
        items = buslib.list_tasks(self.conn, self.rid, "human")
        self.assertEqual(len(items), 1)

    def test_non_member_relation_and_review(self):
        # claim → report → in_review 后，非成员 review 应被拒
        claim_task(self.conn, self.tid, "codex#1")
        task_report(self.conn, self.tid, "codex#1", "完成")
        with self.assertRaises(BusError):
            task_review(self.conn, self.tid, "claude#9", "打回")
        # 关系建边：claude#9 需 room#2 内另一个任务来测——直接测对 room#1 任务建边
        tid2 = create_task(self.conn, self.rid, "第二个", "human")
        with self.assertRaises(BusError):
            task_relation_add(self.conn, tid2, "blocks", self.tid, "claude#9")


class TaskVersionAdjudicationTests(unittest.TestCase):
    """done/cancel 带 expected_version 的乐观裁决。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ab-task-ver-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, "bus.db")
        self.conn = connect(self.db)
        init_schema(self.conn)
        self.conn.execute(
            "INSERT INTO instances(pid, kind, agent_id, state) VALUES(1001,'codex','codex#1','online')"
        )
        self.conn.commit()
        self.rid = create_room(self.conn, "v", "human", members=["codex#1"])
        self.tid = create_task(self.conn, self.rid, "版本裁决", "human")
        claim_task(self.conn, self.tid, "codex#1")  # v2
        task_report(self.conn, self.tid, "codex#1", "r")  # v3, in_review

    def test_done_with_correct_version(self):
        t = task_done(self.conn, self.tid, "human", expected_version=3)
        self.assertEqual(t["status"], "done")

    def test_done_with_stale_version_conflicts(self):
        with self.assertRaises(BusError) as ctx:
            task_done(self.conn, self.tid, "human", expected_version=2)
        self.assertEqual(ctx.exception.code, "E_CONFLICT")

    def test_cancel_with_stale_version_conflicts(self):
        with self.assertRaises(BusError) as ctx:
            task_cancel(self.conn, self.tid, "human", expected_version=1)
        self.assertEqual(ctx.exception.code, "E_CONFLICT")
        # 正确版本可 cancel
        t = task_cancel(self.conn, self.tid, "human", expected_version=3)
        self.assertEqual(t["status"], "canceled")


class TaskLabelsShapeTests(unittest.TestCase):
    """labels 响应为字符串数组（服务端转数组，调用方拿到恒为数组）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ab-task-label-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db = os.path.join(self.tmp, "bus.db")
        self.conn = connect(self.db)
        init_schema(self.conn)
        self.conn.execute(
            "INSERT INTO instances(pid, kind, agent_id, state) VALUES(1001,'codex','codex#1','online')"
        )
        self.conn.commit()
        self.rid = create_room(self.conn, "v", "human", members=["codex#1"])

    def test_create_with_labels_returns_array(self):
        tid = create_task(self.conn, self.rid, "标签任务", "human", labels=["hold", "返工"])
        task = buslib.get_task(self.conn, tid, "human")
        self.assertEqual(task["labels"], ["hold", "返工"])
        items = buslib.list_tasks(self.conn, self.rid, "human")
        self.assertEqual(items[0]["labels"], ["hold", "返工"])

    def test_dirty_labels_fallback(self):
        self.conn.execute(
            "INSERT INTO tasks(room_id, title, labels) VALUES(?,?,?)",
            (self.rid, "脏数据", "not-json"),
        )
        self.conn.commit()
        items = buslib.list_tasks(self.conn, self.rid, "human")
        dirty = [t for t in items if t["title"] == "脏数据"][0]
        self.assertEqual(dirty["labels"], [])
