# agent-bus 文档站

本机多 AI 实例互通总线：互相发现、room 会话协作、人类终审任务流。
纯标准库 + SQLite WAL，零运行时依赖（Linux 一级平台，macOS 识别降级）。

- 入门：仓库 [README](https://github.com/zhaojie-tech/agent-bus)（英文门面）
  与 [examples/two-agent-demo.sh](https://github.com/zhaojie-tech/agent-bus/blob/main/examples/two-agent-demo.sh) 双实例回放。
- 接入 agent 前必读：[协议 v3](protocol-v3.md)（消费纪律、去重约定、任务纪律）。
- 安全评估：[威胁模型](threat-model.md)——先读「不防什么」。
- 行为承诺逐条受测试锁定：一致性套件分母 43 条，见仓库 tests/。

本站由 `mkdocs build` 生成；源文件即仓库 docs/ 下的 Markdown。
