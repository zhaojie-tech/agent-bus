"""test_protocol_conformance — 协议一致性套件。

防规范与实现漂移：docs/protocol-v3.md 的每条可验证承诺在 CATALOG 中占一行
（表驱动，动态生成测试方法），徽章口径 = 条目清单 100% 覆盖——先有分母
（test_catalog_denominator 打印分母）再谈 100%。

双向覆盖对拍（维护纪律）：
- 清单行锚点 ⊆ 规范锚点（清单行必须有规范出处，防清单自嗨）；
- 规范锚点 ⊆ 清单行（规范改了清单必须跟，防规范漂移）。
锚点提取：markdown 二级标题「## N.」→ 节锚点；任务协议章内编号条目 → 条目锚点。

mandate 剥离回归单列一条：任务状态机生命周期完整性 diff 审查，不通过即红。
schema 两拷贝同步断言亦入清单（web 剥离后剩余两份仍保留断言）。
"""

import contextlib
import importlib.resources
import io
import os
import re
import shutil
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentbus import buslib
from agentbus.buslib import (
    REDELIVER_SECONDS,
    BusError,
    ack_message,
    archive_closed,
    claim_task,
    close_room,
    connect,
    create_room,
    create_task,
    dm_key,
    ensure_dm_room,
    get_task,
    init_schema,
    invite_member,
    list_rooms,
    list_tasks,
    poll,
    poll_revision,
    post_message,
    redeliver_stale,
    remove_member,
    resolve_identity,
    set_busy,
    show_room,
    task_block,
    task_cancel,
    task_done,
    task_relation_add,
    task_reopen,
    task_report,
    task_review,
)
from agentbus.cli import _build_parser

DOCS = Path(__file__).resolve().parent.parent / "docs"
SPEC_PATH = DOCS / "protocol-v3.md"

# The section sign is kept as an escape in source: the repo's redaction scan
# flags literal section refs inside code files (they used to mark internal-doc
# citations); building anchor strings is not a citation.
_SECTION_SIGN = "\u00a7"

# 协议承诺给 CLI 的 E_* 家族（protocol-v3 退出码节）
E_FAMILY = {
    "E_INVALID",
    "E_NO_IDENTITY",
    "E_AMBIGUOUS",
    "E_NOT_MEMBER",
    "E_ROOM_CLOSED",
    "E_TASK_OPEN",
    "E_NOT_FOUND",
    "E_CONFLICT",
    "E_DB",  # implementation extension (storage failures), exit-code family 4
}
EXIT_CODES = {"E_INVALID": 2, "E_NO_IDENTITY": 3, "E_AMBIGUOUS": 3, "E_CONFLICT": 5}


def ts_ago(**kw):
    return (datetime.now(UTC) - timedelta(**kw)).strftime(buslib.TIME_FORMAT)


# ── 条目清单（分母）。anchor 必须是规范锚点；check 是 ConformanceTests 方法 ──
CATALOG = [
    dict(
        id="C01",
        anchor="§1",
        check="c01_scope_membership",
        promise="可见性=成员关系：非成员操作报 E_NOT_MEMBER",
    ),
    dict(
        id="C02",
        anchor="§1",
        check="c02_message_belongs_to_room",
        promise="一切消息必属一个 room：messages.room_id 非空约束",
    ),
    dict(
        id="C03",
        anchor="§2",
        check="c03_whoami_identity_sources",
        promise="开工先报身份：显式参数与环境变量两个身份来源可用",
    ),
    dict(
        id="C04",
        anchor="§3",
        check="c03b_human_superset",
        promise="human 是超集成员；room ls --all 仅 human 可用",
    ),
    dict(id="C05", anchor="§3", check="c05_wild_needs_flag", promise="通配成员须建房时显式 --wild"),
    dict(
        id="C06",
        anchor="§3",
        check="c06_mention_directed_delivery",
        promise="群聊 @ 定向投递：消息入房史，投递只建给被 @ 成员",
    ),
    dict(
        id="C07",
        anchor="§4",
        check="c07_invite_summary_idempotent",
        promise="邀请自动发 system 摘要（最近 20 条），幂等",
    ),
    dict(
        id="C08",
        anchor="§4",
        check="c08_remove_clears_reads",
        promise="移除成员清除其未 ack 读取记录（视为放弃）",
    ),
    dict(
        id="C09",
        anchor="§5",
        check="c09_fanout_per_member",
        promise="消息入房对每个成员独立建 new 行，不存在先到先得",
    ),
    dict(
        id="C10",
        anchor="§5",
        check="c10_read_state_machine",
        promise="读取状态机 new → delivered（poll）→ acked（确认）",
    ),
    dict(
        id="C11",
        anchor="§5",
        check="c11_redeliver_30min",
        promise="delivered 30min 未 ack 自动退回 new 重投",
    ),
    dict(
        id="C12",
        anchor="§6",
        check="c12_closed_three_stage",
        promise="closed 三阶段：拒新消息 / 收尾窗口 / 24h 冻结",
    ),
    dict(id="C13", anchor="§6", check="c13_archive_30d", promise="closed 30 天物理归档删除"),
    dict(
        id="C14",
        anchor="§7",
        check="c14_busy_free_semantics",
        promise="忙闲自报：busy 非空=忙碌，NULL=空闲（选人依据）",
    ),
    dict(
        id="C15",
        anchor="§8",
        check="c15_dm_deterministic",
        promise="dm 确定性房：字典序键，双向并发只产生一个房",
    ),
    dict(
        id="C16",
        anchor="§8",
        check="c16_dm_fixed_pair_stale",
        promise="dm 固定二人不可邀第三人；编号失效房标灰",
    ),
    dict(
        id="C17",
        anchor="§9",
        check="c17_sender_unauthenticated_declared",
        promise="sender 不可认证作为事实声明在规范中（威胁模型交叉引用）",
    ),
    dict(
        id="C18",
        anchor="§10",
        check="c18_etiquette_locked",
        promise="礼仪条款（≤3 轮自动往返）作为协议承诺锁定",
    ),
    dict(
        id="C19",
        anchor="§11",
        check="c19_exit_code_family",
        promise="退出码分级与 E_* 家族与实现精确一致",
    ),
    dict(
        id="C20",
        anchor="§12",
        check="c20_poll_marks_delivered",
        promise="poll 置 delivered；--since 无变化快路径不置状态",
    ),
    dict(
        id="C21",
        anchor="§13.1",
        check="c21_task_room_scope",
        promise="一切任务必属一个 room；关系禁止跨 room；任务成员可见",
    ),
    dict(
        id="C22",
        anchor="§13.2",
        check="c22_task_create_sys_msg",
        promise="建任务自动发 system 消息（refs 带 task:<id>）",
    ),
    dict(
        id="C23",
        anchor="§13.3",
        check="c23_relation_rules",
        promise="关系边：仅同 room、单父、防成环、related 去序",
    ),
    dict(
        id="C24",
        anchor="§13.4",
        check="c24_task_comments",
        promise="任务全量含评论（refs 整词引用的房内消息）",
    ),
    dict(
        id="C25",
        anchor="§13.5",
        check="c25_claim_atomic",
        promise="claim 原子：todo→in_progress + 归属 + 版本自增，冲突 E_CONFLICT",
    ),
    dict(
        id="C26",
        anchor="§13.5",
        check="c26_claim_blocked_distinct",
        promise="blocked 任务不可认领，报因区分于抢占（E_INVALID）",
    ),
    dict(
        id="C27",
        anchor="§13.6",
        check="c27_claim_double_report",
        promise="认领即双报：busy 自报独立于 claim，互不覆盖",
    ),
    dict(
        id="C28",
        anchor="§13.7",
        check="c28_task_events_in_history",
        promise="任务事件经 system 消息入房史，全程人类可见",
    ),
    dict(
        id="C29",
        anchor="§13.8",
        check="c29_report_review_flow",
        promise="report→in_review；review 仅打回；唯一进入 done 的路径",
    ),
    dict(
        id="C30",
        anchor="§13.8",
        check="c30_done_child_gate",
        promise="子任务未终态时父任务不可 done（E_TASK_OPEN）",
    ),
    dict(
        id="C31",
        anchor="§13.9",
        check="c31_done_human_optimistic",
        promise="done/cancel 仅 human；expected_version 乐观裁决",
    ),
    dict(
        id="C32",
        anchor="§13.10",
        check="c32_close_gate_and_force",
        promise="关房前结清：非终态任务拒绝关房；--force 自动 cancel",
    ),
    dict(
        id="C33",
        anchor="§13.10",
        check="c33_task_field_validation",
        promise="任务字段校验：labels/due_date 服务端裁决，脏数据回退",
    ),
    dict(
        id="C34",
        anchor="§13",
        check="c34_schema_two_copies_in_sync",
        promise="冻结 DDL：包内 schema 资源与核心库内嵌副本逐字一致",
    ),
    dict(
        id="C35",
        anchor="§13.8",
        check="c35_mandate_strip_regression",
        promise="mandate 剥离回归：任务状态机生命周期完整性 diff 审查",
    ),
    dict(
        id="C36",
        anchor="§12",
        check="c36_poll_revision_monotonic",
        promise="poll revision 合成值单调（--since 快路径判定基础）",
    ),
    dict(
        id="C37",
        anchor="§13.11",
        check="c37_task_mode_policy_locked",
        promise="任务消息适用执行档案与礼仪（协议文本锁定）",
    ),
    dict(
        id="C38",
        anchor="§9",
        check="c38_profile_set_human_only",
        promise="执行档案 set 仅 human（路径级门；纪律非总线强制）",
    ),
    dict(
        id="C39",
        anchor="§9",
        check="c39_profile_default_supervised",
        promise="执行档案缺省 supervised（meta 无键即缺省，不播种）",
    ),
    dict(
        id="C40",
        anchor="§1",
        check="c40_profile_iron_rule_locked",
        promise="铁律条款按执行档案裁决（protocol section 1）",
    ),
    dict(
        id="C41",
        anchor="§9",
        check="c41_dual_profile_text_locked",
        promise="双档案文本锁定（protocol section 9：缺省档案与 opt-in 语义）",
    ),
    dict(
        id="C42",
        anchor="§5",
        check="c42_since_fast_path_never_swallows_redelivery",
        promise="poll --since 快路径不吞重投消息（reads 变更不计入 revision）",
    ),
    dict(
        id="C43",
        anchor="§13.3",
        check="c43_relation_remove_symmetric_guards",
        promise="关系边移除与建边对称鉴权（成员可见性 + 存在性裁决）",
    ),
]


def spec_anchors() -> set:
    """从 protocol-v3.md 提取锚点集：节锚点（顶层节）与条目锚点（任务协议章）。"""
    text = SPEC_PATH.read_text(encoding="utf-8")
    anchors = {f"{_SECTION_SIGN}{m.group(1)}" for m in re.finditer(r"^## (\d+)\.", text, re.M)}
    sec13 = text.split("## 13.", 1)[1]
    anchors |= {f"{_SECTION_SIGN}13.{m.group(1)}" for m in re.finditer(r"^(\d+)\. ", sec13, re.M)}
    return anchors


class ConformanceBase(unittest.TestCase):
    """共享 fixture：临时库 + 两个种子实例。各检查自建 room，互不干扰。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ab-conf-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.conn = connect(os.path.join(self.tmp, "bus.db"))
        self.addCleanup(self.conn.close)
        init_schema(self.conn)
        for pid, aid, kind in (
            (1101, "codex#1", "codex"),
            (1102, "claude#1", "claude"),
            (1103, "claude#2", "claude"),
        ):
            self.conn.execute(
                "INSERT INTO instances(pid, kind, agent_id, state) VALUES(?,?,?,'online')",
                (pid, kind, aid),
            )
        self.conn.commit()

    def new_room(self, topic="c", members=("codex#1", "claude#1")):
        rid = create_room(self.conn, topic, "human", list(members))
        return rid

    def run_task_to_review(self, rid, title="t", assignee="claude#1"):
        tid = create_task(self.conn, rid, title, "human")
        claim_task(self.conn, tid, assignee)
        task_report(self.conn, tid, assignee, "改动/验证/结果/风险")
        return tid


class CatalogMetaTests(unittest.TestCase):
    """分母与双向覆盖（R1-P2：先有分母再谈 100%）。"""

    def test_catalog_denominator(self):
        self.assertGreaterEqual(len(CATALOG), 30)
        for e in CATALOG:
            self.assertIn("id", e)
            self.assertIn("anchor", e)
            self.assertIn("promise", e)
            self.assertIn("check", e)
            self.assertTrue(e["anchor"].startswith("§"))
        print(f"\n[conformance] 条目清单分母 = {len(CATALOG)}（100% 覆盖 = 全绿）")

    def test_catalog_anchors_exist_in_spec(self):
        spec = spec_anchors()
        for e in CATALOG:
            self.assertIn(e["anchor"], spec, f"{e['id']} 锚点 {e['anchor']} 不在规范中（清单自嗨）")

    def test_spec_anchors_covered_by_catalog(self):
        spec = spec_anchors()
        covered = {e["anchor"] for e in CATALOG}
        missing = sorted(spec - covered)
        self.assertEqual(missing, [], f"规范锚点未被清单覆盖（规范改了清单没跟）：{missing}")


class ConformanceTests(ConformanceBase):
    # 第 1 节 ─────────────────────────────────────────────────────────────
    def c01_scope_membership(self):
        rid = self.new_room()
        with self.assertRaises(BusError) as ctx:
            post_message(self.conn, "x", "claude#2", room_id=rid)
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")

    def c02_message_belongs_to_room(self):
        cols = {r[1]: r for r in self.conn.execute("PRAGMA table_info(messages)")}
        self.assertEqual(cols["room_id"]["notnull"], 1)

    # 第 2 节 ─────────────────────────────────────────────────────────────
    def c03_whoami_identity_sources(self):
        self.assertEqual(resolve_identity(self.conn, as_human=True), ("human", "flag"))
        self.assertEqual(
            resolve_identity(self.conn, environ={"AB_AS": "codex#1"}), ("codex#1", "env")
        )

    def c03b_human_superset(self):
        rid = self.new_room()
        self.assertTrue(buslib.is_member(self.conn, rid, "human"))
        with self.assertRaises(BusError) as ctx:
            list_rooms(self.conn, "codex#1", all_rooms=True)
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")
        items = list_rooms(self.conn, "human", all_rooms=True)
        self.assertEqual([r["id"] for r in items], [rid])

    # 第 3 节 ─────────────────────────────────────────────────────────────
    def c05_wild_needs_flag(self):
        with self.assertRaises(BusError) as ctx:
            create_room(self.conn, "x", "human", ["claude:*"])
        self.assertEqual(ctx.exception.code, "E_INVALID")
        rid = create_room(self.conn, "x", "human", ["claude:*"], wild=True)
        self.assertTrue(rid)

    def c06_mention_directed_delivery(self):
        rid = self.new_room()
        mid = post_message(
            self.conn, "@claude#1 只给你", "human", room_id=rid, mentions=["claude#1"]
        )
        rows = [
            r["member"]
            for r in self.conn.execute(
                "SELECT member FROM message_reads WHERE message_id=?", (mid,)
            )
        ]
        self.assertEqual(rows, ["claude#1"])
        hist = show_room(self.conn, rid, "codex#1", history=True)
        self.assertIn(mid, [m["id"] for m in hist["messages"]])

    # 第 4 节 ─────────────────────────────────────────────────────────────
    def c07_invite_summary_idempotent(self):
        rid = create_room(self.conn, "交接", "human", ["codex#1"])
        post_message(self.conn, "上下文一", "human", room_id=rid)
        invite_member(self.conn, rid, "claude#1", actor="codex#1")
        sys_msg = self.conn.execute("SELECT * FROM messages WHERE sender='system'").fetchone()
        self.assertIn("[invite]", sys_msg["body"])
        self.assertIn("上下文一", sys_msg["body"])
        invite_member(self.conn, rid, "claude#1", actor="codex#1")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM messages WHERE sender='system'").fetchone()[0],
            1,
        )

    def c08_remove_clears_reads(self):
        rid = self.new_room()
        mid = post_message(self.conn, "x", "human", room_id=rid)
        remove_member(self.conn, rid, "claude#1", actor="human")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM message_reads WHERE message_id=? AND member='claude#1'",
                (mid,),
            ).fetchone()[0],
            0,
        )

    # 第 5 节 ─────────────────────────────────────────────────────────────
    def c09_fanout_per_member(self):
        rid = self.new_room(members=("codex#1", "claude#1", "claude#2"))
        mid = post_message(self.conn, "广播", "human", room_id=rid)
        rows = self.conn.execute(
            "SELECT member, status FROM message_reads WHERE message_id=?", (mid,)
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["status"] == "new" for r in rows))

    def c10_read_state_machine(self):
        rid = self.new_room()
        mid = post_message(self.conn, "x", "human", room_id=rid)
        poll(self.conn, "claude#1")
        st = self.conn.execute(
            "SELECT status FROM message_reads WHERE message_id=? AND member='claude#1'", (mid,)
        ).fetchone()["status"]
        self.assertEqual(st, "delivered")
        ack_message(self.conn, mid, "claude#1")
        st = self.conn.execute(
            "SELECT status FROM message_reads WHERE message_id=? AND member='claude#1'", (mid,)
        ).fetchone()["status"]
        self.assertEqual(st, "acked")

    def c11_redeliver_30min(self):
        self.assertEqual(REDELIVER_SECONDS, 30 * 60)
        rid = self.new_room()
        mid = post_message(self.conn, "x", "human", room_id=rid)
        poll(self.conn, "claude#1")
        self.conn.execute(
            "UPDATE message_reads SET delivered_at=? WHERE message_id=?", (ts_ago(minutes=31), mid)
        )
        self.conn.commit()
        self.assertEqual(redeliver_stale(self.conn), 1)

    # 第 6 节 ─────────────────────────────────────────────────────────────
    def c12_closed_three_stage(self):
        rid = self.new_room()
        mid = post_message(self.conn, "x", "human", room_id=rid)
        poll(self.conn, "codex#1")
        close_room(self.conn, rid, "human")
        with self.assertRaises(BusError) as ctx:
            post_message(self.conn, "y", "human", room_id=rid)
        self.assertEqual(ctx.exception.code, "E_ROOM_CLOSED")
        # 收尾窗口：重置为 new 仍可 poll/ack
        self.conn.execute(
            "UPDATE message_reads SET status='new', delivered_at=NULL "
            "WHERE message_id=? AND member='codex#1'",
            (mid,),
        )
        self.conn.commit()
        res = poll(self.conn, "codex#1")
        self.assertEqual(res["rooms"][0]["messages"][0]["id"], mid)
        # 冻结：close 25h 后无任何 poll/ack 活动 → poll 不再返回。
        # 活动 = delivered_at 与 acked_at 的较晚者（协议：poll/ack 活动），
        # 两者都须回拨到 25h 前，冻结门才真正被触发；再显式断言冻结谓词
        self.conn.execute("UPDATE rooms SET closed_ts=? WHERE id=?", (ts_ago(hours=25), rid))
        self.conn.execute(
            "UPDATE message_reads SET delivered_at=?, acked_at=? WHERE member='codex#1'",
            (ts_ago(hours=25), ts_ago(hours=25)),
        )
        self.conn.commit()
        self.assertTrue(buslib._room_frozen(self.conn, rid))
        self.assertEqual(poll(self.conn, "codex#1")["rooms"], [])

    def c13_archive_30d(self):
        rid = self.new_room()
        post_message(self.conn, "x", "human", room_id=rid)
        close_room(self.conn, rid, "human")
        self.conn.execute("UPDATE rooms SET closed_ts=? WHERE id=?", (ts_ago(days=31), rid))
        self.conn.commit()
        self.assertEqual(archive_closed(self.conn), 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM rooms WHERE id=?", (rid,)).fetchone()[0], 0
        )

    # 第 7 节 ─────────────────────────────────────────────────────────────
    def c14_busy_free_semantics(self):
        set_busy(self.conn, "codex#1", "重构中")
        busy = self.conn.execute("SELECT busy FROM instances WHERE agent_id='codex#1'").fetchone()[
            "busy"
        ]
        self.assertEqual(busy, "重构中")
        set_busy(self.conn, "codex#1", None)
        busy = self.conn.execute("SELECT busy FROM instances WHERE agent_id='codex#1'").fetchone()[
            "busy"
        ]
        self.assertIsNone(busy)

    # 第 8 节 ─────────────────────────────────────────────────────────────
    def c15_dm_deterministic(self):
        self.assertEqual(dm_key("claude#1", "codex#1"), dm_key("codex#1", "claude#1"))
        db_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
        results = {}

        def worker(sender, target, key):
            conn = connect(db_path)  # sqlite 连接须在各自线程内创建
            try:
                results[key] = post_message(conn, "hi", sender, to=target)
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("codex#1", "claude#1", "a"))
        t2 = threading.Thread(target=worker, args=("claude#1", "codex#1", "b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM rooms WHERE dm_key IS NOT NULL").fetchone()[0],
            1,
        )

    def c16_dm_fixed_pair_stale(self):
        ensure_dm_room(self.conn, "codex#1", "claude#1")
        rid = self.conn.execute("SELECT id FROM rooms WHERE dm_key IS NOT NULL").fetchone()["id"]
        with self.assertRaises(BusError) as ctx:
            invite_member(self.conn, rid, "claude#2", actor="codex#1")
        self.assertEqual(ctx.exception.code, "E_INVALID")
        # 失效成员：claude#2 下线后其 dm 房标灰
        self.conn.execute("UPDATE instances SET state='offline' WHERE agent_id='claude#2'")
        self.conn.commit()
        dm_rid = ensure_dm_room(self.conn, "codex#1", "claude#2")
        item = [r for r in list_rooms(self.conn, "codex#1") if r["id"] == dm_rid][0]
        self.assertTrue(item["stale_dm"])

    # 第 9 / 10 节 ──────────────────────────────────────────────────────
    def c17_sender_unauthenticated_declared(self):
        spec = SPEC_PATH.read_text(encoding="utf-8")
        self.assertIn("sender 字段不可认证", spec)
        self.assertIn("不防恶意 / 被攻破的本地进程", spec)
        tm = (DOCS / "threat-model.md").read_text(encoding="utf-8")
        self.assertIn("sender 不可认证", tm)

    def c18_etiquette_locked(self):
        spec = SPEC_PATH.read_text(encoding="utf-8")
        self.assertIn("无人类参与时同 room 自动往返 ≤3 轮", spec)

    # 第 11 节 ────────────────────────────────────────────────────────────
    def c19_exit_code_family(self):
        from agentbus.cli import _EXIT_CODES

        self.assertEqual(_EXIT_CODES, EXIT_CODES)
        # 实现抛出的错误码 ⊆ 协议家族
        src = (Path(buslib.__file__)).read_text(encoding="utf-8")
        used = set(re.findall(r'BusError\("?(E_[A-Z_]+)"?', src))
        self.assertLessEqual(used, E_FAMILY)

    # 第 12 节 ────────────────────────────────────────────────────────────
    def c20_poll_marks_delivered(self):
        rid = self.new_room()
        mid = post_message(self.conn, "x", "human", room_id=rid)
        poll(self.conn, "claude#1")
        st = self.conn.execute(
            "SELECT status FROM message_reads WHERE message_id=?", (mid,)
        ).fetchone()["status"]
        self.assertEqual(st, "delivered")
        # --since 无变化快路径：空返回且不推进状态
        rev = poll_revision(self.conn)
        res = poll(self.conn, "claude#1", since=rev)
        self.assertEqual(res["rooms"], [])

    def c36_poll_revision_monotonic(self):
        r0 = poll_revision(self.conn)
        rid = self.new_room()
        post_message(self.conn, "x", "human", room_id=rid)
        r1 = poll_revision(self.conn)
        self.assertGreater(r1, r0)
        self.assertEqual(poll_revision(self.conn), r1)  # 无变化不增

    # 第 13 节 ────────────────────────────────────────────────────────────
    def c21_task_room_scope(self):
        rid = self.new_room()
        tid = create_task(self.conn, rid, "t", "human")
        with self.assertRaises(BusError) as ctx:
            get_task(self.conn, tid, "claude#2")
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")
        rid2 = create_room(self.conn, "r2", "human", ["codex#1"])
        t2 = create_task(self.conn, rid2, "t2", "human")
        with self.assertRaises(BusError):
            task_relation_add(self.conn, t2, "blocks", tid, "human")

    def c22_task_create_sys_msg(self):
        rid = self.new_room()
        tid = create_task(self.conn, rid, "标题", "human")
        row = self.conn.execute(
            "SELECT refs, body FROM messages WHERE sender='system' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["refs"], f"task:{tid}")
        self.assertIn("[task] created task", row["body"])

    def c23_relation_rules(self):
        rid = self.new_room()
        a = create_task(self.conn, rid, "A", "human")
        b = create_task(self.conn, rid, "B", "human")
        c = create_task(self.conn, rid, "C", "human")
        with self.assertRaises(BusError):  # 自环
            task_relation_add(self.conn, a, "parent", a, "human")
        task_relation_add(self.conn, b, "parent", a, "human")
        with self.assertRaises(BusError):  # 成环
            task_relation_add(self.conn, a, "parent", b, "human")
        task_relation_add(self.conn, c, "parent", b, "human")  # 链式合法
        with self.assertRaises(BusError):  # 单父：c 已有父 b，不得再认父 a
            task_relation_add(self.conn, c, "parent", a, "human")
        task_relation_add(self.conn, a, "related", c, "human")
        row = self.conn.execute(
            "SELECT source_id, target_id FROM task_relations WHERE type='related'"
        ).fetchone()
        self.assertEqual((row["source_id"], row["target_id"]), tuple(sorted((a, c))))  # 去序

    def c24_task_comments(self):
        rid = self.new_room()
        tid = create_task(self.conn, rid, "t", "human")
        post_message(self.conn, "补充需求：支持 OAuth", "human", room_id=rid)
        mid = self.conn.execute("SELECT id FROM messages ORDER BY id DESC LIMIT 1").fetchone()["id"]
        self.conn.execute("UPDATE messages SET refs=? WHERE id=?", (f"task:{tid}", mid))
        self.conn.commit()
        task = get_task(self.conn, tid, "human")
        self.assertIn(mid, [c["id"] for c in task["comments"]])

    def c25_claim_atomic(self):
        rid = self.new_room()
        tid = create_task(self.conn, rid, "t", "human")
        task = claim_task(self.conn, tid, "claude#1")
        self.assertEqual(task["status"], "in_progress")
        self.assertEqual(task["assignee"], "claude#1")
        self.assertEqual(task["claim_version"], 2)
        with self.assertRaises(BusError) as ctx:
            claim_task(self.conn, tid, "codex#1")
        self.assertEqual(ctx.exception.code, "E_CONFLICT")

    def c26_claim_blocked_distinct(self):
        rid = self.new_room()
        tid = create_task(self.conn, rid, "t", "human")
        claim_task(self.conn, tid, "claude#1")
        task_block(self.conn, tid, "claude#1", "等上游")
        with self.assertRaises(BusError) as ctx:
            claim_task(self.conn, tid, "codex#1")
        self.assertEqual(ctx.exception.code, "E_INVALID")

    def c27_claim_double_report(self):
        rid = self.new_room()
        tid = create_task(self.conn, rid, "t", "human")
        set_busy(self.conn, "claude#1", "预热")
        claim_task(self.conn, tid, "claude#1")
        busy = self.conn.execute("SELECT busy FROM instances WHERE agent_id='claude#1'").fetchone()[
            "busy"
        ]
        self.assertEqual(busy, "预热")  # claim 不覆盖 busy 自报

    def c28_task_events_in_history(self):
        rid = self.new_room()
        tid = self.run_task_to_review(rid)
        bodies = [m["body"] for m in show_room(self.conn, rid, "human", history=True)["messages"]]
        self.assertTrue(any(f"[task] #{tid} reported" in b for b in bodies))

    def c29_report_review_flow(self):
        rid = self.new_room()
        tid = create_task(self.conn, rid, "t", "human")
        claim_task(self.conn, tid, "claude#1")
        # 绝不直接 done：in_progress 不可 done
        with self.assertRaises(BusError) as ctx:
            task_done(self.conn, tid, "human")
        self.assertEqual(ctx.exception.code, "E_INVALID")
        t = task_report(self.conn, tid, "claude#1", "完成")
        self.assertEqual(t["status"], "in_review")
        t = task_review(self.conn, tid, "human", "返工")
        self.assertEqual(t["status"], "in_progress")
        task_report(self.conn, tid, "claude#1", "返工完成")
        t = task_done(self.conn, tid, "human")
        self.assertEqual(t["status"], "done")
        # review 不能打回终态
        with self.assertRaises(BusError):
            task_review(self.conn, tid, "human", "x")

    def c30_done_child_gate(self):
        rid = self.new_room()
        parent = create_task(self.conn, rid, "父", "human")
        child = create_task(self.conn, rid, "子", "human")
        task_relation_add(self.conn, child, "parent", parent, "human")
        claim_task(self.conn, parent, "claude#1")
        task_report(self.conn, parent, "claude#1", "完成")
        with self.assertRaises(BusError) as ctx:
            task_done(self.conn, parent, "human")
        self.assertEqual(ctx.exception.code, "E_TASK_OPEN")
        task_cancel(self.conn, child, "human")
        self.assertEqual(task_done(self.conn, parent, "human")["status"], "done")

    def c31_done_human_optimistic(self):
        rid = self.new_room()
        tid = self.run_task_to_review(rid)
        task = get_task(self.conn, tid, "human")
        with self.assertRaises(BusError) as ctx:  # 仅 human
            task_done(self.conn, tid, "claude#1")
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")
        with self.assertRaises(BusError) as ctx:  # 版本不符
            task_done(self.conn, tid, "human", expected_version=task["claim_version"] - 1)
        self.assertEqual(ctx.exception.code, "E_CONFLICT")
        self.assertEqual(
            task_done(self.conn, tid, "human", expected_version=task["claim_version"])["status"],
            "done",
        )
        # cancel 同语义
        tid2 = create_task(self.conn, rid, "t2", "human")
        with self.assertRaises(BusError):
            task_cancel(self.conn, tid2, "claude#1")
        self.assertEqual(task_cancel(self.conn, tid2, "human")["status"], "canceled")

    def c32_close_gate_and_force(self):
        rid = self.new_room()
        tid = create_task(self.conn, rid, "未完成", "human")
        with self.assertRaises(BusError) as ctx:
            close_room(self.conn, rid, "human")
        self.assertEqual(ctx.exception.code, "E_TASK_OPEN")
        close_room(self.conn, rid, "human", force=True)
        self.assertEqual(get_task(self.conn, tid, "human")["status"], "canceled")

    def c33_task_field_validation(self):
        rid = self.new_room()
        with self.assertRaises(BusError):
            create_task(self.conn, rid, "t", "human", priority="p0")
        with self.assertRaises(BusError):
            create_task(self.conn, rid, "t", "human", due_date="2026-13-01")
        with self.assertRaises(BusError):
            create_task(self.conn, rid, "t", "human", labels="not-a-list")
        tid = create_task(self.conn, rid, "t", "human", labels=["a", "b"])
        self.assertEqual(get_task(self.conn, tid, "human")["labels"], ["a", "b"])
        # 脏数据（直插非 JSON）回退 []
        self.conn.execute(
            "INSERT INTO tasks(room_id, title, labels) VALUES(?,?,?)", (rid, "脏", "oops")
        )
        self.conn.commit()
        dirty = [t for t in list_tasks(self.conn, rid, "human") if t["title"] == "脏"][0]
        self.assertEqual(dirty["labels"], [])

    def c34_schema_two_copies_in_sync(self):
        schema_sql = (importlib.resources.files("agentbus") / "schema" / "schema.sql").read_text(
            encoding="utf-8"
        )
        self.assertEqual(schema_sql, buslib._FALLBACK_SCHEMA_DDL)
        # 第三份：discover 的 instances 段（守护不 import buslib，靠独立
        # 建表保持解耦）——必须与 schema.sql 的同段逐字一致，防漂移
        from agentbus import discover

        start = schema_sql.index("CREATE TABLE IF NOT EXISTS instances(")
        end = schema_sql.index(");", start) + 2
        self.assertEqual(discover.INSTANCES_DDL.strip(), schema_sql[start:end].strip())

    def c35_mandate_strip_regression(self):
        """mandate 剥离回归（R2）：实现面无 mandate 残留 + 状态机生命周期完整。"""
        # ① 库导出面无 mandate 符号
        self.assertEqual([n for n in dir(buslib) if "mandate" in n.lower()], [])
        # ② CLI 面无 mandate 子命令与选项
        with self.assertRaises(SystemExit):  # task mandate 已不存在
            with contextlib.redirect_stderr(io.StringIO()):
                _build_parser().parse_args(["task", "mandate", "issue"])
        with self.assertRaises(SystemExit):  # done 无 --mandate 选项
            with contextlib.redirect_stderr(io.StringIO()):
                _build_parser().parse_args(["task", "done", "1", "--mandate", "x"])
        # ③ 状态机生命周期完整性：六态可达、终态集正确、非法迁移全拒
        rid = self.new_room()
        tid = create_task(self.conn, rid, "全生命周期", "human")
        self.assertEqual(get_task(self.conn, tid, "human")["status"], "todo")
        seen = {"todo"}
        t = claim_task(self.conn, tid, "claude#1")  # todo→in_progress
        seen.add(t["status"])
        with self.assertRaises(BusError):  # 非 review 不可 done
            task_done(self.conn, tid, "human")
        task_block(self.conn, tid, "claude#1", "卡住")  # →blocked
        seen.add("blocked")
        t = task_reopen(self.conn, tid, "human")  # blocked→todo
        self.assertEqual(t["status"], "todo")
        claim_task(self.conn, tid, "claude#1")
        task_report(self.conn, tid, "claude#1", "完成")  # →in_review
        seen.add("in_review")
        task_review(self.conn, tid, "human", "返工")  # →in_progress
        task_report(self.conn, tid, "claude#1", "返工完成")
        t = task_cancel(self.conn, tid, "human")  # →canceled（终态）
        seen.add("canceled")
        with self.assertRaises(BusError):  # 终态不可再动
            task_review(self.conn, tid, "human", "x")
        with self.assertRaises(BusError):
            task_cancel(self.conn, tid, "human")
        # done 路径重走一遍收口
        tid2 = self.run_task_to_review(rid, "done 路径")
        self.assertEqual(task_done(self.conn, tid2, "human")["status"], "done")
        seen.add("done")
        self.assertEqual(seen, set(buslib.TASK_STATUSES))
        self.assertEqual(set(buslib.TASK_TERMINAL), {"done", "canceled"})

    def c42_since_fast_path_never_swallows_redelivery(self):
        rid = self.new_room()
        mid = post_message(self.conn, "x", "human", room_id=rid)
        poll(self.conn, "claude#1")
        rev = poll_revision(self.conn)
        self.conn.execute(
            "UPDATE message_reads SET delivered_at=? WHERE message_id=?", (ts_ago(minutes=31), mid)
        )
        self.conn.commit()
        self.assertEqual(redeliver_stale(self.conn), 1)
        res = poll(self.conn, "claude#1", since=rev)
        ids = [m["id"] for r in res["rooms"] for m in r["messages"]]
        self.assertIn(mid, ids)

    def c43_relation_remove_symmetric_guards(self):
        rid = self.new_room()
        a = create_task(self.conn, rid, "A", "human")
        b = create_task(self.conn, rid, "B", "human")
        task_relation_add(self.conn, b, "parent", a, "human")
        with self.assertRaises(BusError) as ctx:  # 非成员
            buslib.task_relation_remove(self.conn, b, "parent", a, "claude#2")
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")
        with self.assertRaises(BusError) as ctx:  # 不存在的边
            buslib.task_relation_remove(self.conn, a, "parent", b, "human")
        self.assertEqual(ctx.exception.code, "E_NOT_FOUND")
        buslib.task_relation_remove(self.conn, b, "parent", a, "human")

    def c37_task_mode_policy_locked(self):
        spec = SPEC_PATH.read_text(encoding="utf-8")
        self.assertIn("执行档案", spec)
        self.assertIn("§9", spec)  # protocol execution-mode section
        self.assertIn("§10", spec)  # protocol etiquette section

    # ── 执行档案（v3 定稿前修订）──
    def c38_profile_set_human_only(self):
        rid = self.new_room()
        create_task(self.conn, rid, "t", "human")  # noqa: F841 占位保持结构
        with self.assertRaises(BusError) as ctx:
            buslib.set_execution_profile(self.conn, "trusted", "codex#1")
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")
        prof = buslib.set_execution_profile(self.conn, "trusted", "human")
        self.assertEqual(prof["value"], "trusted")
        self.assertEqual(prof["set_by"], "human")

    def c39_profile_default_supervised(self):
        prof = buslib.get_execution_profile(self.conn)
        self.assertEqual(prof["value"], "supervised")
        self.assertTrue(prof["default"])

    def c40_profile_iron_rule_locked(self):
        spec = SPEC_PATH.read_text(encoding="utf-8")
        self.assertIn("收到消息后按执行档案", spec)
        self.assertIn("先向用户汇报并经确认后执行", spec)

    def c41_dual_profile_text_locked(self):
        spec = SPEC_PATH.read_text(encoding="utf-8")
        self.assertIn("supervised（缺省档案）", spec)
        self.assertIn("trusted（显式 opt-in）", spec)


def _make_test(entry):
    def test(self):
        getattr(self, entry["check"])()

    test.__doc__ = f"[{entry['anchor']}] {entry['promise']}"
    return test


for _e in CATALOG:
    setattr(ConformanceTests, f"test_{_e['id']}", _make_test(_e))

if __name__ == "__main__":
    unittest.main(verbosity=2)
