# ADR-0003: 零运行时依赖纪律

- 状态：Accepted
- 日期：v0.1 定稿
- 关联：scripts/check_zero_deps.py、CONTRIBUTING「三条硬纪律」

## 背景

agent-bus 的目标场景是「在本机给多个 AI 实例装一个地基件」：安装摩擦
必须趋近于零，审计面必须足够小，让运维者敢于在敏感机器上 `pipx install`。

## 裁决

- 运行时依赖恒为空（pyproject `dependencies = []`），仅 Python 标准库
  + SQLite（标准库 sqlite3 模块）。
- 存储：SQLite WAL 单文件，不引入数据库服务。
- 传输：v0.1 无网络栈；v0.3 relay 如需 HTTP，用标准库 http 实现，
  不为传输层引入依赖。
- 开发期依赖（pytest/pytest-cov/ruff/mypy）全部隔离在
  `[project.optional-dependencies].dev`，绝不进入构建产物。

## 断言机制

`scripts/check_zero_deps.py`：

1. 构建 wheel；
2. 装入干净 venv；
3. AST 扫描已安装包源码的顶层 import，断言
   ⊆ `sys.stdlib_module_names ∪ {agentbus}`；
4. `pip list` 断言无第三方包。

任何提交引入第三方运行时 import 都会在该门变红。

## 后果

- 收益：`pipx install` 即用即走；供应链攻击面为零依赖；行为完全可审计
  （一个 ~1300 行核心库 + 守护）；离线机器可装。
- 代价：无插件生态可用（web 框架/迁移工具都要自写或 v0.2 评估）；
  功能扩展只能靠标准库，某些实现更啰嗦。该代价是明码标价且自愿支付的。
