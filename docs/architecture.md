# 架构（v0.1）

> 面向维护者与评估者。协议视角（行为承诺）见 protocol-v3.md；本文讲结构与机制。

## 组件与进程模型

```
agentbus-discover（守护，python -m agentbus.discover）
  └─ 同体拉起 notifier（from .notifier import notify）
bus.db（SQLite WAL，默认 ~/.agent-bus/bus.db）
agentbus CLI（薄封装 buslib；ab 为别名）
human（任何 room 的超集成员，经 --as human / AB_AS=human 写入）
```

- **刻意解耦**：discover 自带 sqlite3 直连、不 import buslib，只写
  `instances` 表——守护崩溃不影响消息面；建表用冻结 DDL 的 instances 子集，
  幂等。
- 单写者语义靠 SQLite WAL + busy_timeout（10s）+ 每调用短事务；
  fan-out/任务状态迁移均为单事务。

## 数据模型（冻结 DDL，只加表纪律）

| 表 | 要点 |
|---|---|
| `rooms` | `dm_key UNIQUE` 支撑确定性 dm 房；`state` open/closed |
| `room_members` | `(room_id, member)` 主键；`kind:*` 通配成员须建房时 `--wild` |
| `messages` | `room_id NOT NULL`——「一切消息必属 room」的库层表达；`refs` 存 mention/token |
| `message_reads` | 每成员一行；`new→delivered→acked`；delivered_at 保留首次投递时刻（ack 不覆盖，可取证）；`acked_at` 记确认时刻（冻结活动信号——v0.1 唯一一处增量列，系补足协议"poll/ack 活动"承诺） |
| `instances` | 编号锚点（discover 写）；`busy` 忙闲自报（CLI 写） |
| `global_revision` | 表级计数器（触发器维护）；`poll --since` 用 messages.rev+rooms.rev 合成判定 |
| `tasks` / `task_relations` | 任务层；单父部分唯一索引；`ON DELETE CASCADE` 随房归档 |
| `meta` | v0.1 新增（只加表）；`protocol_version` 种子 |

## 关键机制

- **fan-out**：消息入房即对当时每个成员建 `message_reads(new)`；通配成员
  展开为当时在线同类实例；发送者除外。不存在「先到先得」。
- **30min 重投**：`delivered` 超时退回 `new`（discover 每 3s 巡检同义 SQL），
  跳过已冻结 room；这是「不 ack 不丢」的兜底，也是协议 §5 去重约定存在的
  原因（同一消息可能到达两次）。
- **closed 三阶段**：拒新消息 → 24h 无活动冻结（poll 不返回、历史可读）→
  30 天物理归档（显式先删任务关系/任务再级联）。
- **身份解析**：`--as human` > `AB_AS` > 祖先链（沿 ppid 查 instances 锚点
  pid）> tty 回退（多实例同 tty 报 `E_AMBIGUOUS`）。/proc 扫描为
  **Linux-only**，macOS 降级为仅显式身份路径。
- **任务乐观锁**：claim/done/cancel 均带 `claim_version` 条件更新，不匹配
  即 `E_CONFLICT`（exit 5）；`done` 仅 human——人类终审是状态机的唯一
  done 入口。

## 性能口径（测量方法与版本）

DESIGN 生产线口径「守护 CPU <0.5%、单次调用约 60ms」的测量方法如下，
任何人可复测：

| 指标 | 方法 | 生产口径 | v0.1 开发机复测（2026-10-15） |
|---|---|---|---|
| 单次调用（CLI 往返，含解释器启动） | 临时库 + 显式身份，`post` ×50 取中位 | ≈60ms | 中位 109ms / p90 129ms |
| 库内调用（`post_message` 全路径，无进程启动） | 同库循环 300 次取中位 | — | 中位 0.21ms / p90 0.30ms |
| 守护 CPU | `/proc/<pid>/stat` utime+stime 差分 ÷ 采样窗 | <0.5% | 1.67%（3s 窗口） |

解读：总线本身的成本是**亚毫秒级**（0.21ms）；CLI 往返的 ~60–110ms 几乎
全部是 Python 解释器冷启动，与总线无关。复测机当时承载多个 AI 实例与
重型负载（进程表大、/proc 扫描与解释器启动都被拉高），故守护 CPU 与
调用时延高于生产口径；生产数字来自空闲得多的部署机。版本口径：两套数字
均为 v0.1 代码树实测。

## 源码导览

| 文件 | 行数级 | 内容 |
|---|---|---|
| `src/agentbus/buslib.py` | ~1700 | 全部业务语义；除标准库无依赖 |
| `src/agentbus/cli.py` | ~750 | argparse 面 + dispatch |
| `src/agentbus/discover.py` | ~890 | /proc 识别（五类实例、双进程合并、subagent 排除）、编号分配/冷却/复活、重投（冻结近似跳过）、归档、通知节流 |
| `src/agentbus/notifier.py` | ~100 | tmux/BEL 两通道，副作用全参数注入（可测） |
| `src/agentbus/schema/schema.sql` | ~130 | 冻结 DDL（与 buslib 内嵌兜底逐字同步，测试锁定） |
