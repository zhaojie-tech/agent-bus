---
name: Bug report
about: agent-bus 行为与协议/文档不符，或非预期报错
title: "[bug] "
labels: bug
assignees: ""
---

<!-- 提交前请先跑一遍：python -m pytest tests/ -q 与 scripts/check_zero_deps.py -->
<!-- 排除自身环境：agentbus version 输出贴上来（版本二元组） -->

**描述**
一句话说明实际行为与预期行为。

**复现**
```bash
# 最小复现命令序列（用临时库，勿贴真实 bus.db 内容）
export AB_DB=/tmp/repro.db
agentbus ...
```

**环境**
- OS / 内核：
- Python：`python --version`
- agent-bus：`agentbus version`
- 安装方式：pipx / pip / 其他

**附加**
stderr 全文（含 `E_*` 前缀）、`agentbus ls` 输出（打码 tty/cwd 可）。
