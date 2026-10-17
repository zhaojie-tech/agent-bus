"""agentbus — 本机多 AI 实例互通总线（纯标准库，零运行时依赖）。

版本二元组：包版本（__version__，语义化版本）与协议版本（PROTOCOL_VERSION）。
协议版本与包版本解耦：协议 v3 可横跨多个包版本；协议变更必须走
docs/protocol-changelog.md 并递增协议版本号（冻结前的定稿前修订除外，
见该文件）。
"""

__version__ = "0.1.0"
PROTOCOL_VERSION = "3"
