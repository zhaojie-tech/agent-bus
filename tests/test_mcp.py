"""test_mcp — MCP stdio 服务器测试。

进程内（StringIO 管道）覆盖帧层与全部工具；另有一条子进程端到端。
stdout 纯净性：每行输出必须可解析为单个 JSON-RPC 消息（一行杂 print 即毁流）。
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agentbus.buslib import connect, init_schema
from agentbus.mcp import MAX_LINE_BYTES, TOOL_NAMES, TOOLS, Server


class Pipe:
    """进程内 serve() 驱动：逐条写入请求，回收全部响应。"""

    def __init__(self, tmp, initialize=True):
        self.db = os.path.join(os.fspath(tmp), "mcp.db")
        conn = connect(self.db)
        init_schema(conn)
        self.server = Server(conn, stdout=io.StringIO())
        self.responses = []
        if initialize:
            self.request(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {},
                    },
                }
            )
            self.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def request(self, msg):
        self.server.handle(msg)
        return self.drain()

    def notify(self, msg):
        self.server.handle(msg)

    def raw_line(self, line):
        self.server.serve(iter([line]))
        return self.drain()

    def drain(self):
        out = self.server.stdout.getvalue()
        self.server.stdout = io.StringIO()
        msgs = [json.loads(line) for line in out.splitlines() if line]
        self.responses.extend(msgs)
        return msgs

    def call(self, name, arguments=None, msg_id=99):
        msgs = self.request(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        result = msgs[-1]["result"]
        if result.get("isError"):
            raise ToolCallError(result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def call_raw(self, name, arguments=None, msg_id=99):
        return self.request(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )[-1]


class ToolCallError(AssertionError):
    pass


class InitializeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pipe = Pipe(self.tmp.name, initialize=False)

    def test_supported_version_echoed(self):
        msgs = self.pipe.request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        self.assertEqual(msgs[-1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(msgs[-1]["result"]["serverInfo"]["name"], "agent-bus")

    def test_unsupported_version_gets_latest(self):
        msgs = self.pipe.request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            }
        )
        self.assertEqual(msgs[-1]["result"]["protocolVersion"], "2025-06-18")

    def test_business_call_before_initialize_rejected(self):
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(msgs[-1]["error"]["code"], -32600)

    def test_initialized_notification_alone_does_not_open_gate(self):
        # 只有 initialize 开门；client 的 ack 通知单独不能解锁业务方法
        self.pipe.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual(msgs[-1]["error"]["code"], -32600)

    def test_initialize_repeat_is_harmless(self):
        msgs = self.pipe.request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        self.assertIn("result", msgs[-1])


class ToolsCatalogTest(unittest.TestCase):
    def test_19_tools_exact_set(self):
        expected = {
            "whoami",
            "list_instances",
            "set_busy",
            "list_rooms",
            "create_room",
            "show_room",
            "close_room",
            "post_message",
            "poll_messages",
            "ack",
            "reply",
            "create_task",
            "list_tasks",
            "get_task",
            "claim_task",
            "report_task",
            "task_block",
            "review_task",
            "execution_profile",
        }
        self.assertEqual(TOOL_NAMES, expected)
        self.assertEqual(len(TOOLS), 19)
        for tool in TOOLS:
            self.assertTrue(tool["description"])
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_poll_description_carries_three_contracts(self):
        poll = next(t for t in TOOLS if t["name"] == "poll_messages")
        self.assertIn("section 12", poll["description"])
        self.assertIn("section 5", poll["description"])
        self.assertIn("section 9", poll["description"])
        # 防客户端截断丢契约：描述设长度上限
        self.assertLessEqual(len(poll["description"]), 500)

    def test_excluded_face_is_intentional(self):
        # human-only / 管理面不进 MCP：排除是收窄默认面而非防线（设计声明）
        for absent in ("task_done", "profile_set", "invite", "watch"):
            self.assertNotIn(absent, TOOL_NAMES)


class FrameHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = patch.dict(os.environ, {"AB_AS": "codex#1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.pipe = Pipe(self.tmp.name)

    def test_oversized_line_rejected_then_stream_survives(self):
        huge = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "ping",
                "params": {"pad": "x" * (MAX_LINE_BYTES + 10)},
            }
        )
        msgs = self.pipe.raw_line(huge + "\n")
        self.assertEqual(msgs[-1]["error"]["code"], -32700)
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(msgs[-1]["result"], {})

    def test_unparseable_frame_is_parse_error(self):
        msgs = self.pipe.raw_line("not json\n")
        self.assertEqual(msgs[-1]["error"]["code"], -32700)

    def test_non_object_frame_is_invalid_request(self):
        msgs = self.pipe.raw_line("[1, 2, 3]\n")
        self.assertEqual(msgs[-1]["error"]["code"], -32600)

    def test_deep_nesting_is_rejected_not_crash(self):
        # 契约（版本无关）：深嵌套帧不得杀进程、流必须存活。具体响应取决于
        # 解释器：老版本 json 抛 RecursionError → -32600 "nested"；3.12+ 的
        # C 解析器可直接解析该深度 → 按合法 ping 应答 {}。两者都满足
        # "不崩 + 存活"，只钉这两个属性，不钉具体码。
        payload = (
            '{"jsonrpc": "2.0", "id": 3, "method": "ping", "params": '
            + "[" * 60000
            + "]" * 60000
            + "}"
        )
        msgs = self.pipe.raw_line(payload + "\n")
        self.assertTrue(msgs, "no response at all would hang the client")
        last = msgs[-1]
        self.assertTrue("error" in last or "result" in last)
        if "error" in last:  # RecursionError path (older interpreters)
            self.assertEqual(last["error"]["code"], -32600)
            self.assertIn("nested", last["error"]["message"])
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        self.assertEqual(msgs[-1]["result"], {})

    def test_stdout_purity_every_line_is_single_json_rpc(self):
        self.pipe.request({"jsonrpc": "2.0", "id": 5, "method": "ping"})
        self.pipe.call("whoami")
        out = self.pipe.server.stdout.getvalue()
        for line in out.splitlines():
            msg = json.loads(line)  # 任一杂音行在此爆
            self.assertIsInstance(msg, dict)

    def test_invalid_utf8_bytes_do_not_kill_the_stream(self):
        raw = b'{"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {"x": "a\xffb"}}' + b"\n"
        self.pipe.server.serve(iter([raw]))
        msgs = self.pipe.drain()
        self.assertEqual(msgs[-1]["error"]["code"], -32700)
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": 8, "method": "ping"})
        self.assertEqual(msgs[-1]["result"], {})

    def test_non_object_params_and_arguments_do_not_kill_the_stream(self):
        msgs = self.pipe.request(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": [1, 2]}
        )
        self.assertEqual(msgs[-1]["error"]["code"], -32602)
        for arguments in ([1], "x", 5):
            msgs = self.pipe.request(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": "set_busy", "arguments": arguments},
                }
            )
            self.assertEqual(msgs[-1]["error"]["code"], -32602)
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": 11, "method": "ping"})
        self.assertEqual(msgs[-1]["result"], {})

    def test_id_null_is_a_request_not_a_notification(self):
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": None, "method": "no/such"})
        self.assertEqual(msgs[-1]["error"]["code"], -32601)

    def test_unknown_method_is_32601(self):
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
        self.assertEqual(msgs[-1]["error"]["code"], -32601)


class ToolsRoundTripTest(unittest.TestCase):
    """工具 happy-path 与 BusError→isError（AB_AS 显式身份驱动）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = patch.dict(os.environ, {"AB_AS": "codex#1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.pipe = Pipe(self.tmp.name)
        conn = self.pipe.server.conn
        conn.execute(
            "INSERT INTO instances(pid, kind, agent_id, state) "
            "VALUES(1301, 'codex', 'codex#1', 'online')"
        )
        conn.execute(
            "INSERT INTO instances(pid, kind, agent_id, state) "
            "VALUES(1302, 'claude', 'claude#1', 'online')"
        )
        conn.commit()

    def test_whoami(self):
        self.assertEqual(self.pipe.call("whoami"), {"identity": "codex#1", "source": "env"})

    def test_room_and_message_roundtrip(self):
        rid = self.pipe.call("create_room", {"topic": "demo", "members": "codex#1,claude#1"})[
            "room_id"
        ]
        mid = self.pipe.call("post_message", {"room_id": rid, "body": "hello"})["message_id"]
        rooms = self.pipe.call("list_rooms")["rooms"]
        self.assertEqual(len(rooms), 1)
        # 切换身份消费
        self._env.stop()
        self._env2 = patch.dict(os.environ, {"AB_AS": "claude#1"})
        self._env2.start()
        self.addCleanup(self._env2.stop)
        polled = self.pipe.call("poll_messages")
        bodies = [m["body"] for r in polled["rooms"] for m in r["messages"]]
        self.assertIn("hello", bodies)
        self.pipe.call("ack", {"message_id": mid})

    def test_task_flow(self):
        rid = self.pipe.call("create_room", {"topic": "t"})["room_id"]
        tid = self.pipe.call("create_task", {"room_id": rid, "title": "t1"})["task_id"]
        self.assertEqual(self.pipe.call("claim_task", {"task_id": tid})["status"], "in_progress")
        self.assertEqual(
            self.pipe.call("report_task", {"task_id": tid, "body": "done"})["status"], "in_review"
        )
        self.assertEqual(
            self.pipe.call(
                "task_block",
                {
                    "task_id": tid,
                    "reason": "stuck",
                },
            )["status"],
            "blocked",
        )

    def test_profile_read_only(self):
        prof = self.pipe.call("execution_profile")
        self.assertEqual(prof["value"], "supervised")
        self.assertTrue(prof["default"])

    def test_bus_error_becomes_is_error_with_code_text(self):
        result = self.pipe.call_raw("create_room", {"topic": ""})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("E_INVALID", result["result"]["content"][0]["text"])

    def test_unknown_tool_is_error_result(self):
        result = self.pipe.call_raw("definitely_not_a_tool")
        self.assertTrue(result["result"]["isError"])
        self.assertIn("unknown tool", result["result"]["content"][0]["text"])

    def test_bad_arguments_become_is_error_not_crash(self):
        result = self.pipe.call_raw("create_task", {"room_id": 1})
        self.assertTrue(result["result"]["isError"])

    def test_notification_form_tool_call_is_not_executed_or_answered(self):
        rid = self.pipe.call("create_room", {"topic": "notif"})["room_id"]
        before = len(self.pipe.responses)
        self.pipe.notify(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "post_message", "arguments": {"room_id": rid, "body": "sneaky"}},
            }
        )
        self.assertEqual(len(self.pipe.responses), before)  # silent (JSON-RPC)
        hist = self.pipe.call("show_room", {"room_id": rid, "history": True})
        self.assertEqual(hist["messages"], [])  # and not executed

    def test_handler_crash_becomes_is_error_and_stream_survives(self):
        from agentbus import mcp as mcp_mod

        with patch.dict(mcp_mod.HANDLERS, {"whoami": lambda self, args: 1 / 0}):
            result = self.pipe.call_raw("whoami")
        self.assertTrue(result["result"]["isError"])
        self.assertIn("E_INTERNAL", result["result"]["content"][0]["text"])
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": 12, "method": "ping"})
        self.assertEqual(msgs[-1]["result"], {})

    def test_set_busy_empty_reason_clears_and_mirrors_db(self):
        out = self.pipe.call("set_busy", {"reason": "   "})
        self.assertIsNone(out["busy"])
        row = self.pipe.server.conn.execute(
            "SELECT busy FROM instances WHERE agent_id='codex#1'"
        ).fetchone()
        self.assertIsNone(row["busy"])  # db state mirrored, not "busy'


class IdentityRemedyTest(unittest.TestCase):
    """E_NO_IDENTITY 的工具结果必须是 MCP 语境补救（含 discover / AB_AS，
    不含 CLI 的 --pid 提示）——设计硬承诺。"""

    def test_no_identity_remedy_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AB_DB": os.path.join(tmp, "r.db")}):
                os.environ.pop("AB_AS", None)
                pipe = Pipe(tmp)
                result = pipe.call_raw("whoami")
                self.assertTrue(result["result"]["isError"])
                text = result["result"]["content"][0]["text"]
                self.assertIn("E_NO_IDENTITY", text)
                self.assertIn("agentbus-discover", text)
                self.assertIn("AB_AS", text)
                self.assertNotIn("--pid", text)


class MemoryBoundedReadTest(unittest.TestCase):
    """1 MiB 读限必须真的防内存：喂 8 MiB 无换行输入，缓冲峰值常量级。"""

    def test_oversized_unterminated_input_is_bounded(self):
        import tracemalloc

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AB_DB": os.path.join(tmp, "m.db")}):
                pipe = Pipe(tmp)
                payload = b"y" * (8 * 1024 * 1024)  # 8 MiB, no newline
                tracemalloc.start()
                pipe.server.serve(io.BytesIO(payload))
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                msgs = pipe.drain()
                self.assertLess(peak, 4 * 1024 * 1024, f"peak {peak} suggests unbounded buffering")
                self.assertEqual(msgs[-1]["error"]["code"], -32700)


class FrameHardeningRegressionTest(unittest.TestCase):
    """回归：init 参数类型 / 参数错误分类 / 帧边界一致性。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = patch.dict(os.environ, {"AB_AS": "codex#1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.pipe = Pipe(self.tmp.name)

    def test_init_non_string_protocol_version_does_not_crash(self):
        msgs = self.pipe.request(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "initialize",
                "params": {"protocolVersion": ["2025-06-18"]},
            }
        )
        self.assertIn("result", msgs[-1])  # 回退到 latest，不崩
        self.assertEqual(msgs[-1]["result"]["protocolVersion"], "2025-06-18")
        msgs = self.pipe.request({"jsonrpc": "2.0", "id": 22, "method": "ping"})
        self.assertEqual(msgs[-1]["result"], {})

    def test_argument_errors_classified_as_e_invalid(self):
        result = self.pipe.call_raw("claim_task", {})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("E_INVALID", result["result"]["content"][0]["text"])
        result = self.pipe.call_raw("poll_messages", {"since": "42"})
        self.assertIn("E_INVALID", result["result"]["content"][0]["text"])

    def test_frame_boundary_accepts_exactly_limit(self):
        from agentbus.mcp import MAX_LINE_BYTES

        # 恰为 limit 的合法帧（不含换行）必须被接受；limit+1 拒绝
        base = json.dumps({"jsonrpc": "2.0", "id": 23, "method": "ping", "params": {"p": ""}})
        filler = "x" * (MAX_LINE_BYTES - len(base.encode()))
        body = base.replace('""', f'"{filler}"')
        assert len(body.encode()) == MAX_LINE_BYTES
        msgs = self.pipe.raw_line(body + "\n")
        self.assertEqual(msgs[-1]["result"], {})
        oversize = body + "x"
        msgs = self.pipe.raw_line(oversize + "\n")
        self.assertEqual(msgs[-1]["error"]["code"], -32700)

    def test_crlf_frame_within_limit_accepted(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 24, "method": "ping"})
        msgs = self.pipe.raw_line(body + "\r\n")
        self.assertEqual(msgs[-1]["result"], {})

    def test_streaming_and_fallback_agree_on_crlf_boundary(self):
        from agentbus.mcp import MAX_LINE_BYTES, _iter_frames

        base = json.dumps({"jsonrpc": "2.0", "id": 25, "method": "ping", "params": {"p": ""}})
        filler = "x" * (MAX_LINE_BYTES - len(base.encode()))
        body = base.replace('""', f'"{filler}"')
        assert len(body.encode()) == MAX_LINE_BYTES
        wire = (body + "\r\n").encode()
        streaming = list(_iter_frames(io.BytesIO(wire), MAX_LINE_BYTES))
        fallback = list(_iter_frames(iter([wire]), MAX_LINE_BYTES))
        self.assertEqual(
            [o for _, o in streaming],
            [o for _, o in fallback],
            "CRLF boundary must behave identically on both read paths",
        )
        self.assertFalse(streaming[0][1])  # exactly limit + CRLF accepted
        # 裸 CR 结束（非帧终止符）：两路同样一致（按 payload 裁决）
        bare = (body + "\r").encode()
        s2 = list(_iter_frames(io.BytesIO(bare), MAX_LINE_BYTES))
        f2 = list(_iter_frames(iter([bare]), MAX_LINE_BYTES))
        self.assertEqual([o for _, o in s2], [o for _, o in f2])
        self.assertFalse(s2[0][1])


class SubprocessEndToEndTest(unittest.TestCase):
    def test_real_stdio_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AB_DB=os.path.join(tmp, "e2e.db"), AB_AS="human")
            proc = subprocess.Popen(
                [sys.executable, "-m", "agentbus.mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            def send(obj):
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()

            def recv():
                line = proc.stdout.readline()
                return json.loads(line)

            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }
            )
            self.assertEqual(recv()["result"]["serverInfo"]["name"], "agent-bus")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = recv()["result"]["tools"]
            self.assertEqual(len(tools), 19)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "whoami", "arguments": {}},
                }
            )
            payload = json.loads(recv()["result"]["content"][0]["text"])
            self.assertEqual(payload["identity"], "human")
            proc.stdin.close()
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
