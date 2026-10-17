#!/usr/bin/env python3
"""Generate docs/cli.md from the argparse definitions (single source of truth:
agentbus.cli._build_parser()).

Usage:
    python scripts/gen_cli_docs.py            # write docs/cli.md
    python scripts/gen_cli_docs.py --check    # exit 1 if docs/cli.md is stale

Language policy: CLI help is English since v0.1 (machine-facing surfaces);
this page quotes it verbatim. Docs are Chinese-first by design.
"""

import argparse
import difflib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentbus import PROTOCOL_VERSION, __version__  # noqa: E402
from agentbus.cli import _build_parser  # noqa: E402


def fmt_action(a: argparse.Action) -> str:
    if a.option_strings:
        opts = " / ".join(a.option_strings)
        if a.metavar:
            opts += f" {a.metavar}"
        default = ""
        if a.default not in (None, False, argparse.SUPPRESS, ""):
            default = f"（默认 {a.default}）"
        help_txt = (a.help or "").rstrip("。")
        return f"| `{opts}` | {help_txt}{default} |"
    return f"| `{a.metavar or a.dest}` | {a.help or ''}（位置参数） |"


def walk(parser: argparse.ArgumentParser, path: str, out: list) -> None:
    if path:
        out.append(f"\n## {path}\n")
        if parser.description:
            out.append(f"{parser.description}\n")
    rows = []
    for a in parser._actions:
        if a.dest == "help" or a.option_strings == ["-h", "--help"]:
            continue
        if a.__class__.__name__ == "_SubParsersAction":
            continue
        if not path and a.dest in ("as_human", "pid"):
            continue  # documented once in the global section above
        rows.append(fmt_action(a))
    if rows:
        out.append("| 参数 | 说明 |")
        out.append("|---|---|")
        out.extend(rows)
    sub = next((a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"), None)
    if sub is not None:
        for name, subp in sub.choices.items():
            walk(subp, f"{path} {name}".strip(), out)


def render() -> str:
    out = [
        "# CLI 参考（自动生成）",
        "",
        "<!-- 由 scripts/gen_cli_docs.py 从 agentbus.cli._build_parser() 生成，勿手改；",
        "     CI 以 --check 校验同步。CLI help 已是英文（机器面），本页按原文引用。 -->",
        "",
        "主命令 `agentbus`，`ab` 为别名（与 apache2-utils 的 Apache Bench 同名，",
        "PATH 劫持用 `which -a ab` 排查）。退出码分级：0 成功；2 `E_INVALID`；",
        "3 `E_NO_IDENTITY`/`E_AMBIGUOUS`；4 一般业务错误；5 `E_CONFLICT`。",
        "",
        "注意：命令按执行身份裁决——AI 实例在自己的 shell 内直接运行（祖先链",
        "自动解析）；人类在自己的终端操作用 `agentbus --as human ...`。",
        "",
        "## 全局身份参数（所有子命令可用）",
        "",
        "| 参数 | 说明 |",
        "|---|---|",
        "| `--as human` | act as the human superuser identity (highest priority) |",
        "| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |",
        "",
        "## version",
        "",
        "`agentbus version` 输出版本二元组：包版本 + 协议版本（如",
        f"`agentbus {__version__} (protocol v{PROTOCOL_VERSION})`）。所有子命令自动携带身份，",
        "识别失败报 `E_NO_IDENTITY`。",
    ]
    walk(_build_parser(), "", out)
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    args = argparse.ArgumentParser(description="generate docs/cli.md")
    args.add_argument(
        "--check", action="store_true", help="exit 1 when docs/cli.md differs from generated output"
    )
    opts = args.parse_args(argv)
    target = REPO / "docs" / "cli.md"
    rendered = render()
    if opts.check:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current != rendered:
            diff = list(
                difflib.unified_diff(
                    current.splitlines(),
                    rendered.splitlines(),
                    "docs/cli.md",
                    "generated",
                    lineterm="",
                )
            )
            print("\n".join(diff[:80]))
            print("docs/cli.md is stale — run: python scripts/gen_cli_docs.py")
            return 1
        print("docs/cli.md is in sync with argparse")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"written {target} ({len(rendered.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
