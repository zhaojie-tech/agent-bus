# 协议变更日志

协议版本与包版本解耦：协议 v3 可横跨多个包版本。本文件的任何条目变更
（措辞澄清除外）必须递增协议版本号——**例外：随 agent-bus v0.1 首次公开发布
冻结之前的「定稿前修订」不递增**（下方 v3 条目内记录），并同步
`tests/test_protocol.py` 与协议一致性套件。

## v3（当前）

- 随 agent-bus v0.1 首次公开发布冻结：room 作用域 + 任务层
  （文档：docs/protocol-v3.md）。
- **定稿前修订（不递增版本号）**：执行模式档案化——§1/§4/§7/§9/§10/§13.11
  联动改写为 `execution_profile` 双档案（supervised 缺省=先确认后动；
  trusted 显式 opt-in=自动执行），注入只汇报与 sender 不可认证声明保留；
  新增 meta 键 `execution_profile`（JSON 三要素，不播种，缺省 supervised）；
  新增命令 `ab profile`（get / set，set 仅 human——路径级门）、
  `ab register`（headless 身份注册，幂等）、`ab doctor`、
  `ab room ls --limit/--offset` 分页；机器面消息模板英化
  （`[invite]`/`[task]` token 前缀不变，正文动词英化，随 v0.1 批落地）。`meta.protocol_version` 语义：记录建库时版本，升级不回写；展示
  一律读代码常量。
- v0.1 划定：**mandate（委托批量 done）未含于协议**——内部生产系统曾有
  未记载于协议的实现，随 arm 层一并剥离；v0.2 以协议章节形式回归
  （见 README roadmap）。v0.1 中任务 done 的唯一入口是 human
  `ab task done`（§13 第 8/9 条）。
- HTTP 契约（`/api/v1` 族）未含于 v0.1，随 web 组件 v0.2 另行交付；
  届时与 CLI 共用同一 `E_*` 家族。
- 新增 `meta` 键值表与 `protocol_version` 种子（只加表纪律，
  不改既有列；见 src/agentbus/schema/schema.sql）。

## 变更流程

1. 修改 docs/protocol-v3.md，写明影响面（CLI 行为 / 数据层 / 两者）。
2. 本文件新增条目并递增版本（major：破坏性契约变更；minor：只加表、
   只增命令；patch：澄清）。
3. 同步 tests/test_protocol.py 断言与协议一致性条目清单。
