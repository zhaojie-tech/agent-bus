#!/usr/bin/env python3
"""test_cli — agentbus CLI 端到端测试（subprocess -m + AB_DB 临时库，不碰真实 bus.db）。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from agentbus.buslib import connect, init_schema

CLI = [sys.executable, "-m", "agentbus.cli"]


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ab-cli-test-")
        self.db = os.path.join(self.tmp, "cli.db")
        conn = connect(self.db)
        init_schema(conn)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, as_human=False):
        env = dict(os.environ)
        env["AB_DB"] = self.db
        if as_human:
            env["AB_AS"] = "human"
        return env

    def run_ab(self, *args, as_human=False):
        return subprocess.run(
            CLI + [str(a) for a in args],
            env=self.env(as_human=as_human),
            capture_output=True,
            text=True,
        )

    def seed(self, pid, agent_id, kind, state="online", tty=None):
        conn = connect(self.db)
        conn.execute(
            "INSERT INTO instances(pid, kind, agent_id, tty, state) VALUES(?,?,?,?,?)",
            (pid, kind, agent_id, tty, state),
        )
        conn.commit()
        conn.close()

    def query(self, sql, params=()):
        conn = connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class WhoamiTests(CliTestCase):
    def test_whoami_as_human_flag_and_env(self):
        p = self.run_ab("--as", "human", "whoami")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "human (via --as human)")
        p = self.run_ab("whoami", as_human=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "human (via AB_AS=human)")

    def test_whoami_ancestor_via_pid(self):
        self.seed(424242, "codex#1", "codex")
        p = self.run_ab("--pid", "424242", "whoami")
        self.assertEqual(p.returncode, 0, p.stderr)
        # ancestor 键补齐后（处置表顺手修），祖先链来源同样带来源标签
        self.assertEqual(p.stdout.strip(), "codex#1 (via ancestor-chain (pid match))")

    def test_whoami_no_identity(self):
        # 子进程 stdin 为管道（无 tty），且无实例 → E_NO_IDENTITY
        p = self.run_ab("whoami")
        self.assertEqual(p.returncode, 3)  # 退出码分级：身份解析失败=3
        self.assertIn("E_NO_IDENTITY", p.stderr)


class EndToEndTests(CliTestCase):
    def test_create_post_poll_ack_flow(self):
        self.seed(424242, "codex#1", "codex")
        p = self.run_ab(
            "--as", "human", "room", "create", "--topic", "端到端", "--members", "codex#1"
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = int(p.stdout.strip())

        p = self.run_ab("--as", "human", "post", "--room", str(rid), "--body", "你好 codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        mid = int(p.stdout.strip())

        p = self.run_ab("--pid", "424242", "poll")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["rooms"][0]["room_id"], rid)
        self.assertEqual(data["rooms"][0]["messages"][0]["id"], mid)
        self.assertEqual(data["rooms"][0]["messages"][0]["body"], "你好 codex")

        p = self.run_ab("--pid", "424242", "ack", str(mid))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("acknowledged", p.stdout)
        row = self.query(
            "SELECT status FROM message_reads WHERE message_id=? AND member='codex#1'", (mid,)
        )[0]
        self.assertEqual(row[0], "acked")

        p = self.run_ab("--pid", "424242", "poll")
        self.assertEqual(json.loads(p.stdout)["rooms"], [])

    def test_non_member_gets_e_not_member(self):
        self.seed(424242, "codex#1", "codex")
        self.seed(424243, "codex#2", "codex")
        p = self.run_ab("--as", "human", "room", "create", "--topic", "房", "--members", "codex#1")
        rid = int(p.stdout.strip())
        p = self.run_ab("--pid", "424243", "post", "--room", str(rid), "--body", "越界发言")
        self.assertEqual(p.returncode, 4)  # 退出码分级：一般业务错误=4
        self.assertIn("E_NOT_MEMBER", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_room_ls_all_human_only(self):
        self.seed(424242, "codex#1", "codex")
        self.run_ab("--as", "human", "room", "create", "--topic", "房", "--members", "codex#1")
        p = self.run_ab("--pid", "424242", "room", "ls", "--all")
        self.assertEqual(p.returncode, 4)
        self.assertIn("E_NOT_MEMBER", p.stderr)
        p = self.run_ab("--as", "human", "room", "ls", "--all")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("room#1", p.stdout)

    def test_ls_lists_online_instances(self):
        self.seed(424242, "codex#1", "codex", tty="/dev/pts/4")
        p = self.run_ab("ls")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("codex#1", p.stdout)
        self.assertIn("/dev/pts/4", p.stdout)


class DmCliTests(CliTestCase):
    def test_concurrent_two_directions_single_dm_room(self):
        # Acceptance：两个方向同时 ab post --to 只产生 1 个 dm room
        self.seed(111, "codex#1", "codex")
        self.seed(222, "codex#2", "codex")
        env = self.env()
        p1 = subprocess.Popen(
            CLI + ["--pid", "111", "post", "--to", "codex#2", "--body", "hi from 1"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        p2 = subprocess.Popen(
            CLI + ["--pid", "222", "post", "--to", "codex#1", "--body", "hi from 2"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out1, err1 = p1.communicate()
        out2, err2 = p2.communicate()
        self.assertEqual(p1.returncode, 0, err1)
        self.assertEqual(p2.returncode, 0, err2)
        self.assertEqual(self.query("SELECT COUNT(*) FROM rooms WHERE dm_key IS NOT NULL")[0][0], 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages")[0][0], 2)


class RoomRemoveCliTests(CliTestCase):
    def test_remove_member_end_to_end(self):
        self.seed(424242, "codex#1", "codex")
        self.seed(424243, "codex#2", "codex")
        p = self.run_ab(
            "--as", "human", "room", "create", "--topic", "移除", "--members", "codex#1,codex#2"
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = int(p.stdout.strip())
        p = self.run_ab("--as", "human", "post", "--room", str(rid), "--body", "移除前消息")
        self.assertEqual(p.returncode, 0, p.stderr)

        # 以成员 codex#1 身份移除 codex#2
        p = self.run_ab("--pid", "424242", "room", "remove", str(rid), "codex#2")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("removed", p.stdout)
        self.assertIn("codex#2", p.stdout)

        # 移除后再发消息，被移除者 poll 不再见该 room 任何消息
        p = self.run_ab("--as", "human", "post", "--room", str(rid), "--body", "移除后消息")
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_ab("--pid", "424243", "poll")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["rooms"], [])

        # 成员关系与 reads 已按语义清理
        self.assertEqual(
            self.query(
                "SELECT COUNT(*) FROM room_members WHERE room_id=? AND member='codex#2'", (rid,)
            )[0][0],
            0,
        )
        self.assertEqual(
            self.query("SELECT COUNT(*) FROM message_reads WHERE member='codex#2'")[0][0], 0
        )
        # 留在房内的 codex#1 仍正常收到两条消息
        p = self.run_ab("--pid", "424242", "poll")
        self.assertEqual(p.returncode, 0, p.stderr)
        msgs = json.loads(p.stdout)["rooms"][0]["messages"]
        self.assertEqual([m["body"] for m in msgs], ["移除前消息", "移除后消息"])

    def test_remove_by_non_member_e_not_member(self):
        self.seed(424242, "codex#1", "codex")
        self.seed(424243, "codex#2", "codex")
        p = self.run_ab("--as", "human", "room", "create", "--topic", "房", "--members", "codex#1")
        rid = int(p.stdout.strip())
        p = self.run_ab("--pid", "424243", "room", "remove", str(rid), "codex#1")
        self.assertEqual(p.returncode, 4)
        self.assertIn("E_NOT_MEMBER", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_remove_unknown_member_e_not_found(self):
        self.seed(424242, "codex#1", "codex")
        self.seed(424244, "claude#3", "claude")
        p = self.run_ab("--as", "human", "room", "create", "--topic", "房", "--members", "codex#1")
        rid = int(p.stdout.strip())
        p = self.run_ab("--pid", "424242", "room", "remove", str(rid), "claude#3")
        self.assertEqual(p.returncode, 4)
        self.assertIn("E_NOT_FOUND", p.stderr)

    def test_remove_from_closed_room_e_room_closed(self):
        self.seed(424242, "codex#1", "codex")
        self.seed(424243, "codex#2", "codex")
        p = self.run_ab(
            "--as", "human", "room", "create", "--topic", "房", "--members", "codex#1,codex#2"
        )
        rid = int(p.stdout.strip())
        p = self.run_ab("--as", "human", "room", "close", str(rid))
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_ab("--pid", "424242", "room", "remove", str(rid), "codex#2")
        self.assertEqual(p.returncode, 4)
        self.assertIn("E_ROOM_CLOSED", p.stderr)


if __name__ == "__main__":
    unittest.main()
