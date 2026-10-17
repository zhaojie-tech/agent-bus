#!/usr/bin/env python3
"""脱敏禁词扫描门（命中即红；规则与本文件同源维护）。

作用域（自 M1 首版扩围后收紧）：src/ tests/ packaging/ scripts/ .github/
examples/ 全部文件 + docs/（**只豁免 § 规范锚点，其余模式照扫**）+
README/CHANGELOG/CONTRIBUTING/SECURITY/mkdocs.yml/pyproject.toml。

模式集（两层）：
- **仓库内置（结构性通用项）**：硬路径、内网段——任何环境都属泄漏；
- **本地扩展（不入库）**：内部标识与流程用语（内部主机名、用户名、
  评审/决策用语、角色暗号、过程编号等）——这些字符串本身即指向内部
  环境，故规则存于 gitignored 的 scripts/forbidden_patterns.local
  （每行 `name=regex`）或环境变量 FORBIDDEN_PATTERNS_EXTRA，不随包发布。

用法：python scripts/check_forbidden_words.py   （退出码 0=绿，1=红）
"""

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 全文模式（结构性通用项，随仓库发布——不含任何内部标识本身）
PLAIN_PATTERNS = [
    ("hard-path", r"/home/\w+"),
    ("lan-segment", r"192\.168\."),
]

# § 章节引用：仅 .py 检测；行内出现以下标记视为公开协议锚点（合法）
SECTION_MARK = r"§\d"
SECTION_OK_MARKERS = ("protocol", "anchor=")
SELF_EXEMPT = "scripts/check_forbidden_words.py"

# 文档示例里的通用占位路径（非内部信息，白名单）
ALLOWED_PLACEHOLDERS = (r"/home/you", r"/home/<user>")

SCAN_DIRS = ("src", "tests", "packaging", "scripts", ".github", "examples")
SCAN_FILES = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "mkdocs.yml",
    "pyproject.toml",
)

LOCAL_PATTERNS_FILE = REPO / "scripts" / "forbidden_patterns.local"
EXTRA_PATTERNS_ENV = "FORBIDDEN_PATTERNS_EXTRA"


_EXEMPT_FILES = ("scripts/forbidden_patterns.local",)


def _is_exempt(path: Path) -> bool:
    rel = str(path.relative_to(REPO))
    return rel in _EXEMPT_FILES


def _load_extra_patterns() -> list:
    """本地扩展模式（内部标识/流程用语）：来自 gitignored 本地文件或环境
    变量，每行 `name=regex`。文件不入库——规则本身不暴露内部标识。"""
    out = []
    sources = []
    if LOCAL_PATTERNS_FILE.is_file():
        sources.append(LOCAL_PATTERNS_FILE.read_text(encoding="utf-8"))
    extra = os.environ.get(EXTRA_PATTERNS_ENV)
    if extra:
        sources.append(extra)
    for blob in sources:
        for line in blob.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, pat = line.partition("=")
            out.append((name.strip(), pat.strip()))
    return out


def plain_patterns() -> list:
    return PLAIN_PATTERNS + _load_extra_patterns()


def collect_files():
    files = []
    for d in SCAN_DIRS:
        files.extend(
            sorted(
                p
                for p in (REPO / d).rglob("*")
                if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts
            )
        )
    # docs/：只扫非 § 模式（scan() 内按目录名豁免 §）
    files.extend(
        sorted(
            p
            for p in (REPO / "docs").rglob("*")
            if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts
        )
    )
    files.extend(REPO / f for f in SCAN_FILES if (REPO / f).is_file())
    return files


def _section_scan(path: Path) -> bool:
    """§ 模式只扫 .py 代码；模式定义文件自豁免。"""
    rel = str(path.relative_to(REPO))
    return path.suffix == ".py" and rel != SELF_EXEMPT


def scan(path: Path) -> list:
    hits = []
    if _is_exempt(path) or str(path.relative_to(REPO)) == SELF_EXEMPT:
        return hits  # 本文件即模式定义，自豁免（见 SELF_EXEMPT 注释）
    text = path.read_text(encoding="utf-8", errors="replace")
    for name, pat in plain_patterns():
        for m in re.finditer(pat, text):
            frag = m.group(0)
            if any(re.fullmatch(ph, frag) for ph in ALLOWED_PLACEHOLDERS):
                continue
            ln = text.count("\n", 0, m.start()) + 1
            hits.append((path, ln, name, frag))
    if _section_scan(path) and re.search(SECTION_MARK, text):
        for i, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            if any(mark in low for mark in SECTION_OK_MARKERS):
                continue  # 引用公开协议/规范锚点元数据，合法
            for m in re.finditer(SECTION_MARK, line):
                hits.append((path, i, "section-ref", m.group(0)))
    return hits


def scan_git_history() -> list:
    """Scan commit messages for the same forbidden patterns (work-tree-only
    scanning leaves history — which `git clone` exposes — unchecked)."""
    import subprocess

    hits = []
    try:
        log = subprocess.run(
            ["git", "log", "--format=%H%x00%B%x01"],
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return hits
    for entry in log.split("\x01"):
        entry = entry.strip()
        if not entry or "\x00" not in entry:
            continue
        sha, message = entry.split("\x00", 1)
        for name, pat in PLAIN_PATTERNS:
            for m in re.finditer(pat, message):
                frag = m.group(0)
                if any(re.fullmatch(ph, frag) for ph in ALLOWED_PLACEHOLDERS):
                    continue
                hits.append((Path(f"git:{sha[:10]}"), 0, name, frag))
        for line in message.splitlines():
            low = line.lower()
            if "protocol" in low or "anchor=" in low:
                continue
            for m in re.finditer(SECTION_MARK, line):
                hits.append((Path(f"git:{sha[:10]}"), 0, "section-ref", m.group(0)))
    return hits


def main(argv=None) -> int:
    import argparse as _argparse

    parser = _argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-history", action="store_true", help="also scan commit messages")
    opts = parser.parse_args(argv)
    all_hits = []
    n_files = 0
    for f in collect_files():
        n_files += 1
        all_hits.extend(scan(f))
    if opts.git_history:
        all_hits.extend(scan_git_history())
    if all_hits:
        print(f"FORBIDDEN-WORDS: RED — {len(all_hits)} hit(s) in {n_files} files\n")
        for path, ln, name, frag in all_hits:
            try:
                rel = path.relative_to(REPO)
            except ValueError:
                rel = path  # git:<sha> pseudo-paths
            print(f"  {rel}:{ln}  [{name}]  {frag!r}")
        return 1
    print(
        f"FORBIDDEN-WORDS: GREEN — 0 hits across {n_files} files "
        f"({', '.join(SCAN_DIRS)} + docs + {', '.join(SCAN_FILES)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
