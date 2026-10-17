# ADR-0001: v0.1 剥离 web 驾驶舱 / arm 执行层 / mandate 子系统

- 状态：Accepted
- 日期：v0.1 定稿
- 关联：ADR-0002（签名层）、README roadmap

## 背景

agent-bus 由一个已在生产运行的多 AI 协作系统脱敏而来。生产版包含三个
超出「通信与协作纪律地基」定位的部件：

1. **web 驾驶舱**（HTTP API + React UI）：在代码上与内部任务执行层
   （armlib）硬耦合——HTTP 层 import armlib、暴露整组 arm 路由、
   UI 有 arm 视图、web 测试含 arm 用例。「web 进 / armlib 不进」的原
   切割不可执行。
2. **armlib / arm 编排器 CLI / arm MCP 工具面**：内部任务编排层，
   与具体基础设施绑定。
3. **mandate（委托状）子系统**：内嵌在核心库中的委托批量 done 机制，
   从未记载于协议文档——协议与实现已经漂移。

## 裁决

v0.1 公开版剥离以上三者：

- web 全目录不进；test_web 整文件随之弃（其模块 import 时即加载 web 层，
  web 剥离后无法收集运行）。
- armlib、arm CLI、arm MCP 面不进；test_arm 不进。
- mandate 子系统从核心库整体删除（状态机函数、CLI `task mandate` 子命令组、
  `task done --mandate` 选项）；配套回归审查（任务状态机生命周期完整性
  diff）纳入协议一致性套件（M2）。

保留的核心：room/dm/消息 fan-out 与 per-member reads、delivered 超时重投、
closed room 三阶段、v3 任务层（create/claim/report/review/done/block/
cancel/reopen/relation + human 终审门禁）。

## 后果

- v0.1 中任务 done 的唯一入口是 human `ab task done`——这是有意的
  人类终审语义，不是功能缺失；CHANGELOG 记录该裁剪。
- v0.2 回归路径：web 驾驶舱以「剥离 arm 面」的形态回归（arm 路由组、
  UI arm 视图、arm 用例、service 文件剥离；`--no-auth` 启动样例删除或
  标注仅本地开发；SSE query token 风险写入威胁模型）；mandate 以协议
  章节形式回归（先入协议再进实现，防止再次漂移）；arm 执行层以通用
  「任务执行器 SPI」评估回归。
- 测试口径：（裁决立时口径）8 套件 213 例 → 6 套件约 130 例（裁 test_arm 44、
  test_web 35、test_tasks mandate 用例 2；后续迭代扩至 200+ 例）。
