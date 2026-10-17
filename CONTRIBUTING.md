# Contributing

感谢关注 agent-bus。提交前请先读 [docs/protocol-v3.md](docs/protocol-v3.md)——
协议是受测试保护的资产，行为变更必须同步协议与一致性套件。

## 开发环境

```bash
# Python >= 3.11
pip install -e ".[dev]"
python -m pytest                 # 全量测试
python -m pytest tests/test_buslib.py -q   # 快档单套件
```

本项目测试为标准库 unittest 风格（pytest 兼容收集），全部使用临时数据库，
不会触碰真实 `~/.agent-bus/bus.db`。

## 三条硬纪律

1. **零运行时依赖**：`dependencies` 必须保持为空。任何依赖只能进
   `[project.optional-dependencies].dev`。`scripts/check_zero_deps.py`
   会在构建产物上断言（构建 wheel → 干净 venv → AST 扫顶层 import
   ⊆ 标准库 ∪ 本包）。
2. **协议变更走变更日志**：`docs/protocol-v3.md` 的任何行为承诺变更必须
   递增协议版本并记入 `docs/protocol-changelog.md`，同时同步
   `tests/` 协议一致性套件（每条协议承诺一行清单，双向覆盖）。
   minor 版本只加表不改列。
3. **脱敏门**：`scripts/check_forbidden_words.py` 对 `src/`、`tests/`、
   `packaging/`、`scripts/`、`.github/`、`examples/`、`docs/` 与根文档
   （README/CHANGELOG/CONTRIBUTING/SECURITY/mkdocs.yml/pyproject.toml）
   扫描泄漏模式，命中即红。内置模式为结构性通用项（硬路径、内网段）；
   针对你自己环境的标识（主机名、用户名、内部代号等）加在
   `scripts/forbidden_patterns.local`（gitignored，每行 `name=regex`）或
   `FORBIDDEN_PATTERNS_EXTRA` 环境变量——规则文件不提交，模式内容不外泄。仓库由内部生产系统脱敏而来，此门是长期兜底，
   不要在代码注释里新增「设计文档 §」类引用——写自述式不变量。

## 提交规范

- 每批一件事，message 用祈使句概述动机与行为变化（如
  `fix(buslib): delivered 超时重投跳过冻结 room`）。
- 行为变更必须带测试；修复须先有能复现的失败用例。
- 涉及架构取舍的改动请先提 issue，必要时补 ADR（docs/adr/）。

## 测试口径

- 快档（默认 CI）：消息/room/任务/协议一致性/CLI 单元。
- 慢档：`tests/test_discover.py`（/proc 语义）由 CI 的 Linux 腿单独跑；
  该文件无平台 skip 机制，macOS 腿以 `--ignore` 排除（其余套件全部注入
  fixture，跨平台可跑）。
- 核心库 buslib 行覆盖率门槛 ≥ 80%。

## 提交协议

1. Fork / 建分支 → 2. 改动 + 测试 → 3. `python -m pytest` 全绿 +
   两个 scripts 门脚本绿 → 4. PR 描述写清动机、行为变化、协议影响。
