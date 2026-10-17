"""doctor — agentbus installation check (read-only health report).

Every check is observational: nothing here mutates state. The CLI wiring
turns `hard_fail` into exit code 1; warnings and skips never fail.
"""

import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from . import PROTOCOL_VERSION
from .buslib import (
    DEFAULT_DB_PATH,
    BusError,
    get_execution_profile,
    resolve_identity,
)

CORE_TABLES = {
    "rooms",
    "room_members",
    "messages",
    "message_reads",
    "instances",
    "global_revision",
    "tasks",
    "task_relations",
    "meta",
}

# Daemon-only cmdline predicate: module form + script basenames.
# Deliberately NOT the full _is_ab_component set — that one also matches
# /ab and /agentbus, which would make doctor match itself (its own argv[0]
# ends with /agentbus) and always report the daemon as running.
DAEMON_MODULE_FORM = "agentbus.discover"
DAEMON_SCRIPT_BASENAMES = ("/agentbus-discover", "/ab-discover")

OK, WARN, SKIP, FAIL = "ok", "warn", "skip", "fail"

_RECOGNIZED_KINDS = (
    "codex (node wrapper form)",
    "claude",
    "hermes (--yolo|--tui|chat)",
    "qoder",
    "opencode",
)


@dataclass
class Check:
    name: str
    status: str  # ok / warn / skip / fail
    detail: str


_INTERPRETERS = ("python", "python3", "sh", "bash", "env", "nohup", "time")


def is_daemon_cmdline(parts: list[str]) -> bool:
    """True iff the cmdline looks like the discovery daemon (not the CLI).

    Only launch positions are inspected (argv[0], plus argv[1:4] when the
    launcher is an interpreter/wrapper): ``less <path>/agentbus-discover``
    (reading the script) is not a running daemon, while
    ``python -m agentbus.discover`` and ``sh -c agentbus-discover`` are.
    """
    if not parts:
        return False
    first = parts[0].rsplit("/", 1)[-1]
    candidates = parts[1:6] if first in _INTERPRETERS or first.startswith("python") else parts[:1]
    for part in candidates:
        if part == DAEMON_MODULE_FORM:
            return True
        if part.rsplit("/", 1)[-1] in ("agentbus-discover", "ab-discover"):
            return True
    return False


def find_daemon_pids(proc_root: str = "/proc", exclude_pids=()) -> list[int]:
    """Scan proc_root for daemon-shaped cmdlines, excluding given pids."""
    found: list[int] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return found
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid in exclude_pids or pid == os.getpid():
            continue
        try:
            with open(os.path.join(proc_root, name, "cmdline"), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        parts = raw.decode("utf-8", "replace").rstrip("\0").split("\0")
        if parts and is_daemon_cmdline(parts):
            found.append(pid)
    return found


def _db_path() -> str:
    return os.environ.get("AB_DB") or DEFAULT_DB_PATH


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """Read-only connection: doctor must never write (no WAL pragma, no
    migrations) — a missing db stays a SKIP item, a broken one a FAIL.
    The path is percent-encoded: a raw '?' or '#' in AB_DB would otherwise
    truncate the URI and open (or create!) the wrong file."""
    from urllib.parse import quote

    # mode=ro is the normal path (concurrency-safe). On read-only media a WAL
    # database cannot open read-only (it would need to write -shm), so fall
    # back to immutable=1 — an explicit degraded snapshot read, never writing
    # to the database file itself.
    uri = f"file:{quote(db_path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.OperationalError:
        conn = sqlite3.connect(uri + "&immutable=1", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _identity_remedy(exc: BusError) -> str:
    if exc.code == "E_AMBIGUOUS":
        return "pass --pid <pid> to disambiguate"
    if exc.code == "E_NO_IDENTITY":
        return (
            "run commands from inside a registered AI instance shell, start "
            "the daemon (agentbus-discover) and retry after ~3s, or use "
            "--as human / AB_AS=<id>"
        )
    return exc.message


def run_checks(
    conn: sqlite3.Connection | None,
    proc_root: str = "/proc",
    identity_resolver: Callable | None = None,
    as_human: bool = False,
    pid: int | None = None,
) -> tuple:
    """Run all checks; returns (checks, hard_fail). conn may be None when
    the DB does not exist yet — db-dependent checks are then skipped with
    an informational note."""
    checks: list[Check] = []
    db_path = _db_path()
    exists = os.path.exists(db_path)

    # 1. DB reachable
    if not exists:
        checks.append(Check("db", SKIP, f"{db_path} does not exist yet (created on first command)"))
    else:
        try:
            conn = conn or _open_readonly(db_path)
            conn.execute("SELECT 1").fetchone()
            checks.append(Check("db", OK, f"{db_path}"))
        except sqlite3.Error as exc:
            checks.append(Check("db", FAIL, f"{db_path}: {exc}"))
            return checks, True

        # 2. schema completeness
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            missing = CORE_TABLES - tables
            if missing:
                checks.append(
                    Check(
                        "schema",
                        WARN,
                        f"missing tables {sorted(missing)} (run any agentbus command to migrate)",
                    )
                )
            else:
                checks.append(Check("schema", OK, f"{len(CORE_TABLES)} core tables"))
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='protocol_version'").fetchone()
                db_ver = row["value"] if row else "(unset)"
                if db_ver != PROTOCOL_VERSION:
                    checks.append(
                        Check(
                            "protocol",
                            WARN,
                            f"db was created at protocol {db_ver}, code speaks "
                            f"{PROTOCOL_VERSION} — see docs/protocol-changelog.md",
                        )
                    )
                else:
                    checks.append(Check("protocol", OK, f"v{PROTOCOL_VERSION}"))
            except sqlite3.Error as exc:
                # meta 缺失已由 schema 项 warn——此处不再重复 FAIL 也不早退
                checks.append(Check("protocol", WARN, f"unreadable meta: {exc}"))
        except sqlite3.Error as exc:
            checks.append(Check("schema", FAIL, str(exc)))
            return checks, True

        # directory permission (makedirs(0700) only applies at creation)
        parent = os.path.dirname(os.path.abspath(db_path)) or "."
        default_dir = os.path.dirname(os.path.abspath(DEFAULT_DB_PATH))
        mode = os.stat(parent).st_mode & 0o777
        if mode == 0o700:
            checks.append(Check("db-perms", OK, f"{parent} is 0700"))
        elif os.path.abspath(parent) == default_dir:
            checks.append(
                Check(
                    "db-perms",
                    WARN,
                    f"{parent} is {oct(mode)}, expected 0700 — run: chmod 700 {parent}",
                )
            )
        else:
            # 自定义 AB_DB 路径：目录可能与他人共享，不直接建议 chmod
            checks.append(
                Check(
                    "db-perms",
                    WARN,
                    f"{parent} is {oct(mode)}; the default bus dir is 0700 — "
                    "restrict it only if this path is bus-dedicated",
                )
            )

        # 3. daemon liveness
        if os.path.isdir(proc_root):
            pids = find_daemon_pids(proc_root, exclude_pids=(os.getpid(),))
            if pids:
                checks.append(
                    Check("daemon", OK, f"discover running (pid {', '.join(map(str, pids))})")
                )
            else:
                checks.append(
                    Check(
                        "daemon",
                        WARN,
                        "not detected — start it: agentbus-discover "
                        "(or python -m agentbus.discover); instances stay "
                        "unregistered until it runs",
                    )
                )
        else:
            checks.append(
                Check(
                    "daemon",
                    SKIP,
                    "no /proc on this platform — cannot verify; confirm the "
                    "daemon is running yourself",
                )
            )

        # 4. instances
        try:
            rows = conn.execute(
                "SELECT agent_id FROM instances WHERE state='online' "
                "AND agent_id IS NOT NULL ORDER BY kind, agent_id"
            ).fetchall()
            ids = [r["agent_id"] for r in rows]
            if ids:
                checks.append(Check("instances", OK, ", ".join(ids)))
            else:
                checks.append(
                    Check(
                        "instances",
                        WARN,
                        "no online instances registered — recognized kinds: "
                        + "; ".join(_RECOGNIZED_KINDS)
                        + ". Other tools: set AB_AS=<id> explicitly",
                    )
                )
        except sqlite3.Error as exc:
            checks.append(Check("instances", WARN, f"db error: {exc}"))

        # 5. identity resolution
        try:
            resolver = identity_resolver or (
                lambda: resolve_identity(conn, as_human=as_human, pid=pid)
            )
            ident, source = resolver()
            checks.append(Check("identity", OK, f"{ident} ({source})"))
        except BusError as exc:
            checks.append(Check("identity", WARN, f"{exc.code}: {_identity_remedy(exc)}"))
        except sqlite3.Error as exc:
            checks.append(Check("identity", WARN, f"db error: {exc}"))

        # 6. execution profile
        try:
            prof = get_execution_profile(conn)
        except sqlite3.Error as exc:
            # meta 表缺失=守护先建库的半成品库，schema 项已 warn——这里不再 FAIL
            checks.append(Check("profile", WARN, f"db error: {exc}"))
            prof = None
        if prof is None:
            pass
        elif prof["default"]:
            checks.append(
                Check(
                    "profile",
                    OK,
                    "default: supervised; opt in to trusted with: "
                    "agentbus --as human profile set trusted "
                    "(bus-global, affects every agent)",
                )
            )
        else:
            checks.append(
                Check(
                    "profile",
                    OK,
                    f"{prof['value']} (set by {prof['set_by']} at {prof['set_ts']}; "
                    "bus-global, affects every agent)",
                )
            )

        # 7. notifier prerequisites
        has_tmux = shutil.which("tmux") is not None
        has_tty = os.isatty(0) if hasattr(os, "isatty") else False
        checks.append(
            Check(
                "notifier",
                OK,
                f"tmux {'available' if has_tmux else 'not found'}; "
                f"controlling tty {'present' if has_tty else 'absent'} "
                "(BEL channel needs a tty)",
            )
        )

    hard_fail = any(c.status == FAIL for c in checks)
    return checks, hard_fail
