#!/usr/bin/env python3
"""test_notifier — 通知器单元测试（全部 mock/临时文件，不碰真实终端与 bus.db）。"""

import io
import pathlib
import tempfile
import unittest
from types import SimpleNamespace

from agentbus import notifier


def build_proc_stat(root: pathlib.Path, pid: int, comm: str, ppid: int) -> None:
    """伪造单个 /proc/<pid>/stat，格式：pid (comm) S ppid ..."""
    proc_dir = root / str(pid)
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0 0 0 0\n")


class AncestorChainTest(unittest.TestCase):
    def test_ancestor_comm_list_walks_ppid_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # 链：500(node 实例) -> 400(bash) -> 300(tmux: server) -> 1
            build_proc_stat(root, 500, "node", 400)
            build_proc_stat(root, 400, "bash", 300)
            build_proc_stat(root, 300, "tmux: server", 1)
            comms = notifier.ancestor_comm_list(500, str(root))
        self.assertEqual(comms, ["node", "bash", "tmux: server"])

    def test_ancestor_comm_list_stops_on_missing_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            build_proc_stat(root, 700, "claude", 699)  # 父进程不存在
            comms = notifier.ancestor_comm_list(700, str(root))
        self.assertEqual(comms, ["claude"])

    def test_ancestor_comm_list_handles_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            build_proc_stat(root, 10, "a", 20)
            build_proc_stat(root, 20, "b", 10)  # 环：10<->20，应终止不无限循环
            comms = notifier.ancestor_comm_list(10, str(root))
        self.assertEqual(comms, ["a", "b"])


class IsTmuxInstanceTest(unittest.TestCase):
    def _procs(self, chain):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        for pid, comm, ppid in chain:
            build_proc_stat(pathlib.Path(root.name), pid, comm, ppid)
        return root.name

    def test_tmux_server_ancestor_is_true(self):
        root = self._procs([(100, "codex", 99), (99, "bash", 98), (98, "tmux: server", 1)])
        self.assertTrue(notifier.is_tmux_instance(100, root))

    def test_sshd_ancestor_is_false(self):
        root = self._procs([(100, "claude", 99), (99, "bash", 98), (98, "sshd", 1)])
        self.assertFalse(notifier.is_tmux_instance(100, root))

    def test_dead_pid_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(notifier.is_tmux_instance(99999, tmp))


class TmuxDisplayTest(unittest.TestCase):
    def test_success_calls_tmux_display_message(self):
        calls = []

        def fake_runner(cmd, timeout):
            calls.append((cmd, timeout))
            return SimpleNamespace(returncode=0)

        ok = notifier._tmux_display("/dev/pts/4", "有新消息", fake_runner)
        self.assertTrue(ok)
        self.assertEqual(
            calls,
            [(["tmux", "display-message", "-c", "/dev/pts/4", "有新消息"], 5)],
        )

    def test_nonzero_returncode_is_false(self):
        def fake_runner(cmd, timeout):
            return SimpleNamespace(returncode=1)

        self.assertFalse(notifier._tmux_display("/dev/pts/4", "x", fake_runner))

    def test_oserror_is_false(self):
        def fake_runner(cmd, timeout):
            raise OSError("tmux not found")

        self.assertFalse(notifier._tmux_display("/dev/pts/4", "x", fake_runner))

    def test_empty_tty_short_circuits(self):
        self.assertFalse(notifier._tmux_display("", "x", lambda cmd, timeout: None))


class BellWriteTest(unittest.TestCase):
    def test_writes_single_bel_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "fake-pts"

            def fake_opener(path, mode, buffering):
                self.assertEqual(mode, "wb")
                return open(target, "wb", buffering=buffering)

            self.assertTrue(notifier._write_bell("/dev/pts/9", fake_opener))
            self.assertEqual(target.read_bytes(), notifier.BEL)

    def test_oserror_is_false(self):
        def fake_opener(path, mode, buffering):
            raise OSError("permission denied")

        self.assertFalse(notifier._write_bell("/dev/pts/9", fake_opener))

    def test_empty_tty_short_circuits(self):
        self.assertFalse(notifier._write_bell("", lambda *a, **k: io.BytesIO()))


class FakeWriter:
    """记录写入字节的伪 tty（with 语句不关闭底层缓冲，供断言读回）。"""

    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data
        return len(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NotifyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def _fake_tmux_chain(self, pid):
        build_proc_stat(self.root, pid, "node", pid + 1)
        build_proc_stat(self.root, pid + 1, "bash", pid + 2)
        build_proc_stat(self.root, pid + 2, "tmux: server", 1)

    def _fake_ssh_chain(self, pid):
        build_proc_stat(self.root, pid, "claude", pid + 1)
        build_proc_stat(self.root, pid + 1, "bash", pid + 2)
        build_proc_stat(self.root, pid + 2, "sshd", 1)

    def test_tmux_instance_uses_display_message(self):
        self._fake_tmux_chain(300)
        calls = []

        def fake_runner(cmd, timeout):
            calls.append(cmd)
            return SimpleNamespace(returncode=0)

        path = notifier.notify(
            {"pid": 300, "tty": "/dev/pts/3"}, "摘要", str(self.root), fake_runner
        )
        self.assertEqual(path, "tmux")
        self.assertEqual(calls, [["tmux", "display-message", "-c", "/dev/pts/3", "摘要"]])

    def test_ssh_instance_writes_bell(self):
        self._fake_ssh_chain(400)
        writer = FakeWriter()

        def fake_opener(path, mode, buffering):
            return writer

        path = notifier.notify(
            {"pid": 400, "tty": "/dev/pts/5"}, "摘要", str(self.root), opener=fake_opener
        )
        self.assertEqual(path, "bell")
        self.assertEqual(writer.data, notifier.BEL)

    def test_tmux_failure_falls_back_to_bell(self):
        self._fake_tmux_chain(500)
        writer = FakeWriter()

        def fake_runner(cmd, timeout):
            raise OSError("tmux not found")

        path = notifier.notify(
            {"pid": 500, "tty": "/dev/pts/6"},
            "摘要",
            str(self.root),
            runner=fake_runner,
            opener=lambda path, mode, buffering: writer,
        )
        self.assertEqual(path, "bell")
        self.assertEqual(writer.data, notifier.BEL)

    def test_none_when_no_way_to_notify(self):
        self._fake_ssh_chain(600)
        path = notifier.notify(
            {"pid": 600, "tty": None},
            "摘要",
            str(self.root),
            runner=lambda cmd, timeout: SimpleNamespace(returncode=1),
            opener=lambda path, mode, buffering: (_ for _ in ()).throw(OSError("denied")),
        )
        self.assertIsNone(path)


if __name__ == "__main__":
    unittest.main()
