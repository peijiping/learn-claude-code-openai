#!/usr/bin/env python3
"""工具执行 / 后台续轮的异常韧性守护测试（2026-09-18 事故）。

运行方式::

    .venv/bin/python -m unittest discover -s tests -v

## 事故复盘（session_oqlA8pXPQT）

3 个后台子智能体读完 6 篇 PDF 后，`_watch_background` 自动续轮。模型执行
`sed -n '1,40p' raw_T1_A.md | head -c 4200` —— `head -c` 按**字节**切割，
把中文切成半个字，尾部留下不完整 UTF-8 序列。当时 `run_bash` 用
`text=True`（严格解码）→ `UnicodeDecodeError` 沿
`run_bash → ToolRegistry.execute → _execute_tool_call → agent_loop` 一路穿透：

1. 该批 6 个 bash 的 tool_result **一条都没写回**，会话文件留下
   "有 tool_calls 无 tool_result" 的孤儿 assistant；
2. 异常冒泡到 `SessionRuntime._watch_background` 的 except，当时只 log 不重试；
3. 本批后台结果已在 agent_loop 起点被 `collect_background_results()` 消费成
   `notified` → 回循环顶时 `has_completed_pending()` 恒为 False；
4. 守望直接收尾 `status -> done`。

用户看到的现象：会话"结束"了，最终总结没出来，任务面板永久停在
"任务进度 1/3 待继续"，T2/T3 再也不推进。

## 被守护的约定

1. `run_bash` 对"被字节截断的输出"必须容错（显式 utf-8 + errors="replace"），
   不得用严格解码炸掉整轮；
2. `run_bash` 自身抛出的任何异常必须降级为 `Error: ...` 字符串
   （工具层契约：永远返回字符串）；
3. `_execute_tool_call` 同步路径的工具异常必须转成 tool_result，**不得穿透
   agent_loop**（否则整轮死 + 历史留孤儿 tool_call）；
4. `_watch_background` 的续轮异常必须回滚本批后台结果并重试，最多
   `MAX_BG_FOLLOWUP_RETRIES` 次后才收尾 —— 不得静默 done。
"""
import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import paths  # noqa: E402
from background_manager import BackgroundManager  # noqa: E402
from session_runtime import MAX_BG_FOLLOWUP_RETRIES, SessionRuntime  # noqa: E402
from tools import ToolRegistry  # noqa: E402


def _bare_tools() -> ToolRegistry:
    """跳过 __init__ 的依赖注入，只要 run_bash 能用。

    2026-09-18：run_bash 不再只依赖 staticmethod —— 多工作空间改造后它读
    `self.bash_cwd`（命令的缺省工作目录）与 `self.workdir`（文件工具沙箱根），
    故这里手工补上这两个路径字段（与 `Agent` 构造时的口径一致）。
    """
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.workdir = paths.WORKDIR
    reg.bash_cwd = None  # None = 进程 cwd（default 空间的历史行为）
    return reg


class RunBashDecodingTests(unittest.TestCase):
    """约定 1 / 2：bash 输出解码必须容错，异常必须降级。"""

    @staticmethod
    def _truncated_output_command() -> tuple[str, tempfile.TemporaryDirectory]:
        """造一条"把中文按字节切成半个字"的命令 —— 事故现场的同构复现。"""
        tmp = tempfile.TemporaryDirectory()
        p = Path(tmp.name) / "cn.md"
        p.write_text("中文测试内容", encoding="utf-8")
        # "中文测试内容" 每字 3 字节；head -c 5 = "中"(3B) + "文"的前 2B → 不完整序列
        return f"head -c 5 {p}", tmp

    def test_truncated_utf8_output_does_not_raise(self):
        """根因回归：字节截断的中文输出不得让 run_bash 抛异常。"""
        cmd, tmp = self._truncated_output_command()
        with tmp:
            out = _bare_tools().run_bash(cmd)
        self.assertIsInstance(out, str)
        self.assertNotIn("UnicodeDecodeError", out)
        # 合法前缀必须保留下来（替换字符只吃掉那个半截字节）
        self.assertIn("中", out)

    def test_strict_decode_would_have_raised(self):
        """反证：同一命令在旧实现（text=True 严格解码）下确实会抛 —— 坐实根因。"""
        cmd, tmp = self._truncated_output_command()
        with tmp:
            with self.assertRaises(UnicodeDecodeError):
                subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=30)

    def test_unexpected_exception_degrades_to_error_string(self):
        """约定 2：即便再冒出别的意外异常，也只能变成一条 Error 结果。"""
        reg = _bare_tools()
        original = subprocess.run

        def boom(*a, **kw):
            raise RuntimeError("模拟子进程层意外崩坏")

        subprocess.run = boom
        try:
            out = reg.run_bash("echo hi")
        finally:
            subprocess.run = original
        self.assertTrue(out.startswith("Error:"), out)
        self.assertIn("RuntimeError", out)


class ToolCallResilienceTests(unittest.TestCase):
    """约定 3：单条工具失败不得杀死整轮对话。"""

    def test_handler_exception_becomes_tool_result(self):
        from agent_full_v2 import Agent

        agent = Agent.__new__(Agent)          # 跳过繁重 __init__，只填用到的字段
        agent.session_prefix = "session_"
        agent.session_id = "resilience_test"
        agent.background_manager = BackgroundManager()
        agent._print = lambda *a, **kw: None

        class _ExplodingTools:
            """execute 必炸 —— 模拟 run_bash 之外仍可能出现的意外异常。"""

            def resolve_handler(self, name):
                return lambda **kw: None      # 非 None：让 _make_executor 走 execute 分支

            def execute(self, name, **kw):
                raise UnicodeDecodeError(
                    "utf-8", b"\xe7", 4199, 4200, "unexpected end of data")

        agent.tools = _ExplodingTools()

        tool_call = SimpleNamespace(
            id="call_deadbeef",
            function=SimpleNamespace(name="bash", arguments='{"command": "echo hi"}'),
        )
        result = agent._execute_tool_call(tool_call)   # 关键：不得抛异常

        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "call_deadbeef")
        self.assertTrue(result["content"].startswith("Error:"), result["content"])
        self.assertIn("UnicodeDecodeError", result["content"])


class BackgroundRequeueTests(unittest.TestCase):
    """约定 4 的底层能力：按 id 精确回滚，不误伤历史已消费结果。"""

    def test_snapshot_and_restore_roundtrip(self):
        bm = BackgroundManager()
        bm.background_tasks = {
            "bg_0001": {"tool_call_id": "", "command": "a", "status": "completed"},
            "bg_0002": {"tool_call_id": "", "command": "b", "status": "notified"},
            "bg_0003": {"tool_call_id": "", "command": "c", "status": "running"},
        }
        self.assertEqual(bm.snapshot_completed_ids(), ["bg_0001"])

        # 模拟续轮把 bg_0001 消费掉后失败
        bm.collect_background_results()
        self.assertFalse(bm.has_completed_pending())

        self.assertEqual(bm.restore_completed(["bg_0001"]), 1)
        self.assertTrue(bm.has_completed_pending())
        # 早已 consumed 的 bg_0002 不受影响（未在快照里）
        self.assertEqual(bm.background_tasks["bg_0002"]["status"], "notified")
        # 运行中的任务状态不被触碰
        self.assertEqual(bm.background_tasks["bg_0003"]["status"], "running")

    def test_restore_ignores_unknown_and_running(self):
        bm = BackgroundManager()
        bm.background_tasks = {
            "bg_0001": {"tool_call_id": "", "command": "a", "status": "running"},
        }
        self.assertEqual(bm.restore_completed(["bg_0001", "bg_ghost"]), 0)


class BgWatchRetryTests(unittest.TestCase):
    """约定 4：续轮异常必须回滚重试，不得静默 done。"""

    def _runtime_with_completed_bg(self):
        events: list[tuple[str, dict]] = []

        async def reply_sessions():
            return None

        rt = SessionRuntime("test_sid", lambda k, p: events.append((k, p)),
                            reply_sessions, lambda _sid: {})
        bm = BackgroundManager()
        bm.background_tasks = {
            "bg_0001": {"tool_call_id": "", "command": "sub_agent", "status": "completed"},
        }
        bm.background_results = {"bg_0001": "PDF 提取结果"}
        rt.agent = SimpleNamespace(background_manager=bm)
        return rt, events

    def test_failed_followup_is_rolled_back_and_retried(self):
        rt, events = self._runtime_with_completed_bg()
        calls: list[int] = []

        def exploding_worker():
            calls.append(1)
            # 模拟 agent_loop 起点已消费结果，随后执行期炸掉
            rt.agent.background_manager.collect_background_results()
            raise UnicodeDecodeError(
                "utf-8", b"\xe7", 4199, 4200, "unexpected end of data")

        rt._run_followup_worker = exploding_worker
        asyncio.run(rt._watch_background())

        # 首次 + 回滚后重试一次 = 2；不能再多（上限保护）
        self.assertEqual(len(calls), 1 + MAX_BG_FOLLOWUP_RETRIES)
        statuses = [p["status"] for k, p in events if k == "session_status"]
        self.assertEqual(statuses[-1], "done")
        self.assertIn("running", statuses)

    def test_successful_followup_runs_once(self):
        rt, events = self._runtime_with_completed_bg()
        calls: list[int] = []

        def ok_worker():
            calls.append(1)
            rt.agent.background_manager.collect_background_results()

        rt._run_followup_worker = ok_worker
        asyncio.run(rt._watch_background())

        self.assertEqual(len(calls), 1)
        statuses = [p["status"] for k, p in events if k == "session_status"]
        self.assertEqual(statuses[-1], "done")

    def test_restore_is_noop_when_nothing_consumed(self):
        """续轮在消费结果之前就炸了：没有可回滚项，直接收尾，不死循环。"""
        rt, events = self._runtime_with_completed_bg()
        calls: list[int] = []

        def early_boom():
            calls.append(1)
            raise RuntimeError("LLM 客户端构建失败")

        rt._run_followup_worker = early_boom
        asyncio.run(rt._watch_background())

        self.assertEqual(len(calls), 1)
        statuses = [p["status"] for k, p in events if k == "session_status"]
        self.assertEqual(statuses[-1], "done")


if __name__ == "__main__":
    unittest.main()
