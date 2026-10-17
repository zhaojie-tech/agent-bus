# PROGRESS — 开源化交付记录

> 本仓由内部生产系统按文件级处置表脱敏重建而来（重建过程与批次划分见
> git 历史）。本文件记录已交付范围与验收证据；内部仓保留为部署下游，
> 公开仓为上游。

## 交付范围（v0.1）

### 重建与脱敏
- 处置表逐行执行：进/改 11 组文件、弃 12 组（web 驾驶舱、内部任务执行层、
  委托批量 done 子系统及其命令面）；四类内部信息（设计文档章节引用、
  评审编号、角色暗号、内部流程用语）清零。
- `scripts/check_forbidden_words.py`：脱敏扫描门，作用域覆盖 src/tests/
  packaging/scripts/.github/examples + docs + 根文档与配置；扫描器本身
  经合成泄漏样本反向验证（防假绿）。

### 工程基线
- 测试：11 个测试文件全绿（含协议一致性套件 43 条目、双向覆盖对拍、
  缺陷回归守卫）；`python -m pytest tests/ -q`。
- 覆盖率门：核心库行覆盖 ≥80%（当前 ~90%）。
- 门禁：ruff（check + format）、mypy（渐进，先公共 API 面）、脱敏扫描、
  零依赖断言（构建 wheel → 干净环境 → AST 全量扫 import ⊆ 标准库 ∪ 本包）。
- 打包冒烟：`pipx install .` 后四个入口（agentbus / ab / agentbus-discover /
  agentbus-mcp）可用；`examples/two-agent-demo.sh` 双实例协作全链路跑通。

### 功能面
- room 作用域消息（fan-out、per-member reads、30min 重投、closed 三阶段）；
- v3 任务流（claim 乐观锁 / report / review / human-only done）；
- 执行档案：supervised（缺省，先确认后动）/ trusted（显式 opt-in）；
- `agentbus doctor` 安装体检；`agentbus-discover` 守护 + systemd user unit 模板；
- `agentbus-mcp`：MCP stdio 服务器（19 工具），供 Claude Code / Codex 等接入；
- 机器面（CLI help / 错误消息 / system 消息模板 / 守护日志）英文，
  文档中文为主（协议英译列后续版本）。

### 发布物料
- README 英文门面、CHANGELOG、SECURITY、CONTRIBUTING、issue/PR 模板；
- docs：protocol-v3 / threat-model / architecture / comparison / cli（自动生成）/
  protocol-changelog / agent-context-template / ADR×3；
- mkdocs 文档站配置（`mkdocs build --strict` 通过）；
- `examples/two-agent-demo.sh`（同时也是 asciinema 录制脚本，
  `docs/media/two-agent-demo.cast`）。


## 发布前置项（release prerequisites）

1. 确定 GitHub 账号 / 组织名后，回填 pyproject `[project.urls]`、
   README 徽章与 mkdocs `repo_url`（当前为占位）。
2. PyPI 侧为 `agent-bus` 创建 trusted publishing 的 pending publisher
   （首版上传前完成）。
3. 发布提交者身份定名（当前为仓库默认占位）。
4. 首次发布前重跑全部门禁与打包冒烟。

## 后续版本（roadmap 摘要）

- 下一版本：web 驾驶舱回归（剥离内部面）、协议文档英译、签名层重新设计；
- 再后：可插拔传输层（跨主机 relay，签名随消息过网）、留存策略配置化、
  PostgreSQL 存储后端。
