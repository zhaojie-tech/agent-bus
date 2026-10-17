"""test_profile_doctor — 执行档案与 doctor 体检测试。

profile：缺省 supervised / set 仅 human（路径级门，非总线强制——同 UID 可直改
meta，见威胁模型）/ JSON 三要素持久化。
doctor：各检查项（注入 proc_root/db），守护谓词「守护子集 + 排除自身 pid」。
"""

import os
import pathlib
import tempfile
import unittest

from agentbus import buslib
from agentbus.buslib import (
    BusError,
    connect,
    get_execution_profile,
    init_schema,
    set_execution_profile,
)
from agentbus.doctor import (
    FAIL,
    OK,
    SKIP,
    WARN,
    find_daemon_pids,
    is_daemon_cmdline,
    run_checks,
)


def _fresh_conn(tmp, name="bus.db"):
    conn = connect(os.path.join(tmp, name))
    init_schema(conn)
    return conn


def _seed_instance(conn, pid, agent_id, kind="codex"):
    conn.execute(
        "INSERT INTO instances(pid, kind, agent_id, state) VALUES(?,?,?,'online')",
        (pid, kind, agent_id),
    )
    conn.commit()


class ExecutionProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = _fresh_conn(self.tmp.name)
        _seed_instance(self.conn, 1201, "codex#1")

    def test_default_is_supervised(self):
        prof = get_execution_profile(self.conn)
        self.assertEqual(prof["value"], "supervised")
        self.assertTrue(prof["default"])
        self.assertIsNone(prof["set_by"])

    def test_set_requires_human(self):
        with self.assertRaises(BusError) as ctx:
            set_execution_profile(self.conn, "trusted", "codex#1")
        self.assertEqual(ctx.exception.code, "E_NOT_MEMBER")
        # 路径级门：同 UID 进程仍可直改 meta（威胁模型既有声明，不测旁路细节）

    def test_set_rejects_unknown_value(self):
        with self.assertRaises(BusError) as ctx:
            set_execution_profile(self.conn, "yolo", "human")
        self.assertEqual(ctx.exception.code, "E_INVALID")

    def test_set_persists_three_elements(self):
        prof = set_execution_profile(self.conn, "trusted", "human")
        self.assertEqual(prof["value"], "trusted")
        self.assertEqual(prof["set_by"], "human")
        self.assertTrue(prof["set_ts"])
        again = get_execution_profile(self.conn)
        self.assertEqual(again["value"], "trusted")
        self.assertFalse(again["default"])
        self.assertEqual(again["set_ts"], prof["set_ts"])

    def test_dirty_meta_falls_back_to_default(self):
        self.conn.execute("INSERT INTO meta(key, value) VALUES('execution_profile', 'oops')")
        self.conn.commit()
        prof = get_execution_profile(self.conn)
        self.assertEqual(prof["value"], "supervised")
        self.assertTrue(prof["default"])


class DaemonPredicateTest(unittest.TestCase):
    """守护子集谓词：认守护双形态，不认 CLI/MCP 形态（防 doctor 自匹配）。"""

    def test_matches_daemon_forms(self):
        self.assertTrue(is_daemon_cmdline(["/usr/bin/python3", "-m", "agentbus.discover"]))
        self.assertTrue(
            is_daemon_cmdline(["/usr/bin/python3", "/users/x/.local/bin/agentbus-discover"])
        )
        self.assertTrue(is_daemon_cmdline(["/users/x/.local/bin/ab-discover"]))

    def test_launch_position_predicate_fp_fn(self):
        # FN 修复：相对/解释器形态的守护必须命中
        self.assertTrue(is_daemon_cmdline(["/usr/bin/python3", "agentbus-discover", "--once"]))
        self.assertTrue(is_daemon_cmdline(["/bin/sh", "-c", "agentbus-discover"]))
        self.assertTrue(is_daemon_cmdline(["agentbus-discover"]))
        # FP 修复：只是"读到"该路径的进程（less/vim）不得误判为守护
        self.assertFalse(is_daemon_cmdline(["less", "/users/x/bin/agentbus-discover"]))
        self.assertFalse(is_daemon_cmdline(["vim", "/users/x/bin/ab-discover"]))

    def test_does_not_match_cli_or_mcp(self):
        self.assertFalse(
            is_daemon_cmdline(["/users/x/.local/bin/agentbus", "doctor"])
        )  # doctor 自身
        self.assertFalse(is_daemon_cmdline(["/usr/bin/python3", "-m", "agentbus.cli", "whoami"]))
        self.assertFalse(is_daemon_cmdline(["/users/x/.local/bin/agentbus-mcp"]))
        self.assertFalse(is_daemon_cmdline(["/usr/bin/python3", "-m", "agentbus.mcp"]))

    def test_scan_excludes_given_and_own_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for pid, parts in (
                (7001, ["/usr/bin/python3", "-m", "agentbus.discover"]),
                (7002, ["/users/x/.local/bin/agentbus", "doctor"]),
            ):
                d = root / str(pid)
                d.mkdir()
                (d / "cmdline").write_bytes("\0".join(parts).encode() + b"\0")
            pids = find_daemon_pids(tmp, exclude_pids=(7002,))
            self.assertEqual(pids, [7001])


class DoctorChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _proc_root_with_daemon(self):
        root = pathlib.Path(self.tmp.name) / "proc"
        root.mkdir()
        d = root / "8001"
        d.mkdir()
        (d / "cmdline").write_bytes(
            "\0".join(["/usr/bin/python3", "-m", "agentbus.discover"]).encode() + b"\0"
        )
        return str(root)

    def test_missing_db_is_informational_not_fatal(self):
        os.environ["AB_DB"] = os.path.join(self.tmp.name, "absent.db")
        try:
            checks, hard_fail = run_checks(None, proc_root=self._proc_root_with_daemon())
        finally:
            os.environ.pop("AB_DB", None)
        self.assertFalse(hard_fail)
        db_check = next(c for c in checks if c.name == "db")
        self.assertEqual(db_check.status, SKIP)

    def test_full_run_on_seeded_db(self):
        conn = _fresh_conn(self.tmp.name)
        _seed_instance(conn, 1201, "codex#1")
        checks, hard_fail = run_checks(
            conn,
            proc_root=self._proc_root_with_daemon(),
            identity_resolver=lambda: ("codex#1", "ancestor"),
        )
        self.assertFalse(hard_fail)
        by_name = {c.name: c for c in checks}
        self.assertEqual(by_name["db"].status, OK)
        self.assertEqual(by_name["schema"].status, OK)
        self.assertEqual(by_name["daemon"].status, OK)
        self.assertEqual(by_name["profile"].status, OK)
        self.assertIn("default: supervised", by_name["profile"].detail)
        self.assertEqual(by_name["identity"].status, OK)

    def test_identity_failure_gets_remedy(self):
        conn = _fresh_conn(self.tmp.name)
        checks, hard_fail = run_checks(
            conn,
            proc_root="/nonexistent-proc",
            identity_resolver=lambda: (_ for _ in ()).throw(
                BusError("E_NO_IDENTITY", "no identity")
            ),
        )
        self.assertFalse(hard_fail)
        identity = next(c for c in checks if c.name == "identity")
        self.assertEqual(identity.status, WARN)
        self.assertIn("AB_AS", identity.detail)
        self.assertIn("agentbus-discover", identity.detail)

    def test_profile_remedy_uses_human_flag(self):
        conn = _fresh_conn(self.tmp.name)
        checks, _ = run_checks(conn, proc_root="/nonexistent-proc")
        profile = next(c for c in checks if c.name == "profile")
        self.assertIn("agentbus --as human profile set trusted", profile.detail)

    def test_ambiguous_identity_remedy_mentions_pid(self):
        conn = _fresh_conn(self.tmp.name)
        checks, _ = run_checks(
            conn,
            proc_root="/nonexistent-proc",
            identity_resolver=lambda: (_ for _ in ()).throw(BusError("E_AMBIGUOUS", "ambiguous")),
        )
        identity = next(c for c in checks if c.name == "identity")
        self.assertIn("--pid", identity.detail)

    def test_unwritable_db_is_hard_fail(self):
        os.environ["AB_DB"] = os.path.join(self.tmp.name, "bus.db")
        try:
            pathlib.Path(os.environ["AB_DB"]).write_bytes(b"")

            class Boom:
                """execute 即抛 sqlite3.Error 的坏连接（模拟库不可用）。"""

                def execute(self, *args, **kwargs):
                    raise buslib.sqlite3.OperationalError("database is locked")

            checks, hard_fail = run_checks(Boom(), proc_root="/nonexistent-proc")
        finally:
            os.environ.pop("AB_DB", None)
        self.assertTrue(hard_fail)
        self.assertEqual(checks[0].name, "db")
        self.assertEqual(checks[0].status, FAIL)


if __name__ == "__main__":
    unittest.main()
