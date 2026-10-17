"""notifier — agent-bus 通知器。

新消息到达某交互实例时，提醒坐在该终端前的人类：
- tmux 实例：`tmux display-message -c <tty>` 在客户端显示文字摘要；
- 普通 ssh pts 实例：直接向 /dev/pts/N 写 BEL 字节(\\x07)（同 UID 有权限）。

判定 tmux 的依据是 /proc/<pid>/stat 祖先链上出现 tmux 服务端进程
（tmux 内实例祖先为 tmux: server，ssh 直连为 sshd）。
纯 Python 标准库；所有外部副作用（subprocess/文件写）经参数注入，便于测试。
"""

import os
import subprocess
from collections.abc import Callable
from typing import Any

BEL = b"\x07"


def ancestor_comm_list(pid: int, proc_root: str = "/proc") -> list[str]:
    """沿 /proc/<pid>/stat 的 ppid 链向上取各进程 comm（含自身），失败即止。"""
    result: list[str] = []
    seen = set()
    cur = pid
    while cur > 1 and cur not in seen:
        seen.add(cur)
        try:
            with open(
                os.path.join(proc_root, str(cur), "stat"),
                encoding="utf-8",
                errors="replace",
            ) as fh:
                data = fh.read()
        except OSError:
            break
        lparen, rparen = data.find("("), data.rfind(")")
        if lparen < 0 or rparen < 0 or rparen + 1 >= len(data):
            break
        comm = data[lparen + 1 : rparen]
        fields = data[rparen + 1 :].split()
        if len(fields) < 2:
            break
        result.append(comm)
        try:
            cur = int(fields[1])  # fields[0]=state，fields[1]=ppid
        except (ValueError, IndexError):
            break
    return result


def is_tmux_instance(pid: int, proc_root: str = "/proc") -> bool:
    """祖先链含 tmux 服务端进程（comm=tmux 或 tmux:*）则视为 tmux 内实例。"""
    return any(c == "tmux" or c.startswith("tmux:") for c in ancestor_comm_list(pid, proc_root))


def _tmux_display(
    tty: str,
    summary: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    """调用 tmux display-message 定向到指定客户端；失败返回 False。"""
    if not tty:
        return False
    try:
        # tmux treats the message as a format string: escape '#' so message
        # content (room topics are attacker-controllable text) cannot trigger
        # '#(...)' command substitution or leak '#{...}' expansions
        safe = summary.replace("#", "##")
        proc = runner(["tmux", "display-message", "-c", tty, safe], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(proc.returncode == 0)


def _write_bell(tty: str, opener: Callable[..., Any] = open) -> bool:
    """向 tty 设备写入 BEL 字节；任何 OSError 一律静默返回 False。"""
    if not tty:
        return False
    try:
        with opener(tty, "wb", buffering=0) as fh:
            fh.write(BEL)
    except OSError:
        return False
    return True


def notify(
    instance: dict[str, Any],
    summary: str,
    proc_root: str = "/proc",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    opener: Callable[..., Any] = open,
) -> str | None:
    """通知单个在线实例。instance 至少含 pid/tty 字段。

    返回实际使用的途径：'tmux' / 'bell'；两种方式均不可用时返回 None。
    """
    pid = instance.get("pid")
    tty = instance.get("tty") or ""
    if pid and is_tmux_instance(int(pid), proc_root) and _tmux_display(tty, summary, runner):
        return "tmux"
    if tty.startswith("/dev/") and _write_bell(tty, opener):
        return "bell"
    return None
