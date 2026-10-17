#!/usr/bin/env python3
"""Zero-runtime-dependency assertion (ADR-0003).

步骤：
1. `pip wheel` 构建本仓 wheel（隔离构建环境）；
2. 装入干净环境（优先干净 venv；解释器不带 ensurepip 的发行版回退为
   `pip install --no-index --target` 干净目录——no-index 下装得成功，
   本身即证明零依赖、无需外网）；
3. AST 扫描已安装源码的**顶层 import**，断言
   模块根 ⊆ sys.stdlib_module_names ∪ {agentbus}；
4. 断言安装结果中除本包与元数据目录外无任何第三方包
   （venv 路线用 `pip list`，target 路线直接枚举目录）。

用法：python scripts/check_zero_deps.py   （退出码 0=绿，1=红）
构建目标只含 src/（tests/examples 不进 wheel），pytest 等 dev 依赖天然隔离。
"""

import ast
import json
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = "agentbus"
TOOLCHAIN = {"pip", "setuptools", "wheel"}


def run(cmd, **kw):
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        print(f"FAIL: command failed: {' '.join(map(str, cmd))}\n{proc.stderr}")
        sys.exit(1)
    return proc.stdout


def top_level_imports(py_file: Path) -> set:
    """全量 AST 扫描（v2 口径：ast.walk 覆盖函数局部 import——任何位置引
    第三方都拦；此前只看顶层，函数内偷引可穿透）。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zero-deps-") as tmp:
        tmp = Path(tmp)
        print("[1/4] building wheel ...")
        run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(tmp / "dist"), str(REPO)])
        wheel = next((tmp / "dist").glob("*.whl"))

        print("[2/4] installing into clean env ...")
        venv_dir = tmp / "venv"
        mode = None
        try:
            venv.create(str(venv_dir), with_pip=True)
            vp = venv_dir / "bin" / "python"
            run([vp, "-m", "pip", "install", "--no-index", str(wheel)])
            pip_cmd = [vp, "-m", "pip"]
            mode = "venv"
        except subprocess.CalledProcessError:
            # 某些发行版（如 uv 管理的解释器）不带 ensurepip，venv 派生
            # 解释器不可用：回退干净 target 目录 + 外部 pip no-index 安装
            target = tmp / "target"
            run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--target",
                    str(target),
                    str(wheel),
                ]
            )
            mode = "target"

        print("[3/4] AST scan of installed sources ...")
        if mode == "venv":
            site = next((venv_dir / "lib").glob("python*/site-packages"))
        else:
            site = tmp / "target"
        installed = site / PKG
        allowed = set(sys.stdlib_module_names) | {PKG}
        bad = []
        roots_seen = set()
        for py in sorted(installed.rglob("*.py")):
            roots = top_level_imports(py)
            roots_seen |= roots
            bad.extend(sorted(roots - allowed))
        if bad:
            print(f"FAIL: non-stdlib top-level imports: {sorted(set(bad))}")
            return 1
        print(f"      import roots = {sorted(roots_seen)}")

        print("[4/4] installed-content assertion ...")
        if mode == "venv":
            listing = json.loads(run(pip_cmd + ["list", "--format=json"]))
            # pip 报告的是 distribution 名（agent-bus），与本包 import 名
            # （agentbus）不同形——归一化后比较（否则本包被当成第三方）
            names = {p["name"].lower() for p in listing}

            def norm(s: str) -> str:
                return s.replace("-", "").replace("_", "")

            third_party = {n for n in names if norm(n) not in {norm(PKG)} and n not in TOOLCHAIN}
        else:
            names = {p.name.lower() for p in site.iterdir()}
            # target 目录里合法的只有：本包目录、*.dist-info 元数据、
            # pip 为 console script 生成的 bin/（内容即本包入口脚本）
            third_party = {
                n
                for n in names
                if n != PKG
                and n != "bin"
                and not n.replace("-", "").replace("_", "").startswith(PKG)
            }
        if third_party:
            print(f"FAIL: third-party packages installed: {sorted(third_party)}")
            return 1
        print(f"      installed = {sorted(names)} (mode={mode})")
        print("ZERO-DEPS: GREEN — runtime imports ⊆ stdlib ∪ {agentbus}; no third-party packages")
        return 0


if __name__ == "__main__":
    sys.exit(main())
