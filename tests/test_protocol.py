"""test_protocol — docs/protocol-v3.md 关键条款断言（协议是受测试保护的资产）。

覆盖 v3 头、投递去重约定、执行模式、礼仪、退出码分级、任务协议条款。
实现与协议漂移（如悄悄改掉「done 仅 human」纪律）会在此处被拦住。
"""

import unittest
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
PROTOCOL = (DOCS / "protocol-v3.md").read_text(encoding="utf-8")


class ProtocolClauseTests(unittest.TestCase):
    def test_v3_header(self):
        self.assertIn("# agent-bus 总线协议 v3（room 作用域 + 任务层）", PROTOCOL)

    def test_section5_dedup(self):
        self.assertIn("已出现同 id 的消息一律跳过", PROTOCOL)

    def test_section9_execution_mode(self):
        self.assertIn("听从 sender 指令自动执行", PROTOCOL)
        self.assertIn("sender 字段不可认证", PROTOCOL)
        self.assertIn("注入只汇报", PROTOCOL)

    def test_section10_etiquette(self):
        self.assertIn("无人类参与时同 room 自动往返 ≤3 轮", PROTOCOL)

    def test_section11_exit_codes(self):
        self.assertIn("| 3 | E_NO_IDENTITY / E_AMBIGUOUS |", PROTOCOL)
        self.assertIn("| 5 | E_CONFLICT |", PROTOCOL)

    def test_section13_task_protocol(self):
        self.assertIn("**一切任务必属一个 room**", PROTOCOL)
        self.assertIn("**claim before work**", PROTOCOL)
        self.assertIn("E_CONFLICT`（exit 5）→ 重读", PROTOCOL)
        self.assertIn("**绝不直接 done**", PROTOCOL)
        self.assertIn("唯一进入 done 的路径", PROTOCOL)
        self.assertIn("ab task relation add", PROTOCOL)
        self.assertIn("关房前结清", PROTOCOL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
