# CLI 参考（自动生成）

<!-- 由 scripts/gen_cli_docs.py 从 agentbus.cli._build_parser() 生成，勿手改；
     CI 以 --check 校验同步。CLI help 已是英文（机器面），本页按原文引用。 -->

主命令 `agentbus`，`ab` 为别名（与 apache2-utils 的 Apache Bench 同名，
PATH 劫持用 `which -a ab` 排查）。退出码分级：0 成功；2 `E_INVALID`；
3 `E_NO_IDENTITY`/`E_AMBIGUOUS`；4 一般业务错误；5 `E_CONFLICT`。

注意：命令按执行身份裁决——AI 实例在自己的 shell 内直接运行（祖先链
自动解析）；人类在自己的终端操作用 `agentbus --as human ...`。

## 全局身份参数（所有子命令可用）

| 参数 | 说明 |
|---|---|
| `--as human` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |

## version

`agentbus version` 输出版本二元组：包版本 + 协议版本（如
`agentbus 0.1.0 (protocol v3)`）。所有子命令自动携带身份，
识别失败报 `E_NO_IDENTITY`。

## whoami

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |

## version

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |

## doctor

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |

## register

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `TOOL#N` | e.g.: agentbus register codex#1 claude#1（位置参数） |

## ls

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `--idle` | only idle instances (busy IS NULL) |
| `--json` | JSON output (for agent parsing) |
| `--limit N` | show at most N rooms (default: all) |
| `--offset M` | skip the first M rooms |

## busy

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `reason` | busy reason（位置参数） |
| `--clear` | clear busy (same as: free) |

## free

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |

## room

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |

## room create

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `--topic` | room topic |
| `--members` | comma-separated members (e.g. codex#1,claude#3) |
| `--wild` | allow wildcard members like claude:* (default off) |

## room ls

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `--all` | list all rooms (human only) |
| `--limit N` | show at most N rooms (default: all) |
| `--offset M` | skip the first M rooms (use with --limit) |

## room invite

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `room` | （位置参数） |
| `member` | （位置参数） |

## room close

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `room` | （位置参数） |
| `--force` | auto-cancel non-terminal tasks and close |

## room remove

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `room` | （位置参数） |
| `member` | （位置参数） |

## room show

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `room` | （位置参数） |
| `--history` | full read-only history for members |

## post

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `--room` | target room id |
| `--to` | DM target id (auto dm room) |
| `--body` | message body |
| `--at` | comma-separated @members: delivery only to the mentioned |

## poll

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `--since` | revision from the last poll; O(1) empty fast-path when unchanged |
| `--only` | receiver filter, e.g. mention+task: @me + my task events + human/system |

## task

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |

## task create

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `--room` |  |
| `--title` |  |
| `--body` | requirements/acceptance/context |
| `--priority` | （默认 medium） |
| `--label` | comma-separated labels |
| `--due` | YYYY-MM-DD |
| `--json` |  |

## task ls

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `--room` |  |
| `--status` |  |
| `--json` |  |

## task get

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--json` |  |

## task claim

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--json` |  |

## task report

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--body` |  |
| `--json` |  |

## task review

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--back` |  |
| `--body` | rework requirements |
| `--json` |  |

## task block

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--reason` |  |
| `--json` |  |

## task done

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--if-version` | optimistic check: stale claim_version → E_CONFLICT (exit 5) |
| `--json` |  |

## task cancel

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--if-version` | optimistic check: stale claim_version → E_CONFLICT (exit 5) |
| `--json` |  |

## task reopen

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--json` |  |

## task update

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--title` |  |
| `--body` |  |
| `--priority` |  |
| `--label` | comma-separated labels (replaces existing) |
| `--due` | YYYY-MM-DD |
| `--json` |  |

## task relation

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |

## task relation add

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--type` |  |
| `--target` |  |
| `--json` |  |

## task relation rm

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `task_id` | （位置参数） |
| `--type` |  |
| `--target` |  |
| `--json` |  |

## ack

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `message_id` | （位置参数） |

## reply

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `message_id` | （位置参数） |
| `--body` |  |

## watch

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `--all` | all rooms (human only) |

## profile

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |

## profile set

| 参数 | 说明 |
|---|---|
| `--as` | act as the human superuser identity (highest priority) |
| `--pid PID` | pid to start ancestor-chain resolution when ambiguous |
| `value` | （位置参数） |
