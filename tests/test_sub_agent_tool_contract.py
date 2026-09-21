"""sub_agent 工具定义 / 子智能体提示词的「约定守卫」测试。

运行方式（无需额外依赖，pytest 未安装也能跑；装了 pytest 同样可收集）::

    cd /Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main
    .venv/bin/python -m unittest discover -s tests -v

## 被守护的约定（2026-09-14 事故复盘）

事故现象：用户发「看看我的pdf文件内容有哪些，用子智能体看」，主智能体回了
一句「我派一个子智能体后台去读」然后**本轮直接结束**——子智能体从未启动，
用户侧表现为「直接中断、不往下执行、看不到最终结果」。

事后还原（`~/.aigent/projects/default/.chathistory/session_4.jsonl` + 后端日志）：
该轮只发生了 2 次 LLM 调用（token 数 6319 + 6840 + 276 ≈ 日志里的 13435 可精确对上），
第二次响应带着正常正文与思考、`tool_calls=null`，`finish_reason=stop`
→ `agent_loop` 判定"模型想停"，收尾。即：**模型放弃了工具调用**。

而 `sub_agent` 的工具定义里同时存在三处"逼模型违规"的构造：

1. `required=["prompt","parallel"]` —— 但 description 又写「已传
   run_in_background=true 时不要再传 parallel」，而系统提示词强制"批量任务必须
   后台"。于是**批量读多文件这个最高频场景，每一次都必须违规**。
   实测模型产出的参数是 `['prompt','description','allowed_tools','run_in_background']`
   —— 确实缺 `parallel`，实打实的"必填但被禁止"。
2. 示例里的工具名写成了 `read_file` / `read_pdf`，真实名是
   `run_read` / `run_read_pdf`。若模型照抄进 `allowed_tools`，
   `spawn_subagent` 过滤后子智能体**拿不到 run_read_pdf**，读 PDF 必然失败。
3. 子智能体自己的 `DEFAULT_SYSTEM_PROMPT` 也写着「必须使用 read_pdf 工具」，
   与真实工具名不一致（会直接 "Unknown tool"）。

本文件的断言就是为了让这三类"提示词与真实契约不一致"的问题**下次改不动**。
新增/修改 `_sub_agent_tool_def()` 或 `SubAgent.DEFAULT_SYSTEM_PROMPT` 时，
请同步在这里加断言。
"""
import re
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from background_manager import BackgroundManager  # noqa: E402
from session_runtime import SessionRuntime, SessionRuntimeRegistry  # noqa: E402
from subagent import SubAgent  # noqa: E402
from tools import ToolRegistry  # noqa: E402


def _bare_registry() -> ToolRegistry:
    """不触发任何磁盘构造的 ToolRegistry（只用它的工具定义，不碰用户目录）。"""
    reg = ToolRegistry.__new__(ToolRegistry)
    reg._base_tools_cache = None
    return reg


def _real_tool_names() -> set:
    return {t["function"]["name"] for t in _bare_registry().base_tools}


# 不是工具名、但会被上面的正则误伤的同形词（工具参数名）
_NOT_TOOL_NAMES = {"run_in_background"}
# 明确"点名否定"的语境标记：允许提示词用「没有 read_pdf 这些名字」这类方式纠错
_NEGATION_MARKERS = ("没有", "不存在", "不要使用", "不要用", "禁止使用", "禁止用", "没有这些名字")


def _unknown_tool_names(text: str) -> set:
    """提取文本里出现、"看起来像工具名"但真实不存在的名字。

    排除两类误伤：
    - 参数名同形词（`run_in_background`）；
    - 出现在否定语境（"没有 read_pdf"）里的名字 —— 这是**有意**的纠错提示，
      恰恰是要鼓励保留的内容。
    """
    real = _real_tool_names()
    unknown = set()
    for line in text.splitlines():
        for name in re.findall(r"\b(?:run|read|write)_[a-z_]+\b", line):
            if name in real or name in _NOT_TOOL_NAMES:
                continue
            if any(mark in line for mark in _NEGATION_MARKERS):
                continue  # 否定语境，属于纠错提示
            unknown.add(name)
    return unknown


class TestSubAgentToolSchema(unittest.TestCase):
    """sub_agent 定义必须与真实契约自洽 —— 这是"模型会不会发起调用"的直接变量。"""

    def setUp(self):
        reg = ToolRegistry.__new__(ToolRegistry)
        fn = reg._sub_agent_tool_def()["function"]
        self.fn = fn
        self.params = fn["parameters"]
        self.desc = fn["description"]

    def test_required_only_prompt(self):
        """parallel 只能是可选：写进 required 就会与"后台任务不要传 parallel"互斥。"""
        self.assertEqual(self.params["required"], ["prompt"])
        self.assertIn("parallel", self.params["properties"])
        # 可选字段应给默认值，模型不传时语义明确
        self.assertEqual(self.params["properties"]["parallel"].get("default"), False)

    def test_no_contradiction_between_required_and_description(self):
        """定义里不得再出现"必填但禁止填"的自相矛盾表述。"""
        self.assertNotIn("不要再传 parallel", self.desc)
        self.assertNotIn("与 parallel 互斥", self.desc)
        # 允许/不传的语义要写清楚
        self.assertIn("无意义", self.desc)

    def test_examples_use_real_tool_names(self):
        """示例里出现的 run_* / read_* / write_* 名字必须都是真实工具名。"""
        unknown = _unknown_tool_names(self.desc)
        self.assertEqual(unknown, set(),
                         f"sub_agent 描述里出现不存在的工具名: {sorted(unknown)}；"
                         f"真实名为 {sorted(_real_tool_names())}")
        # 只读示例必须是真实存在的工具名。run_read 自 2026-09-21 起是**读文件
        # 的唯一入口**（PDF / 图片 / Office 都由它分派），示例里不该再出现
        # 已退役的 run_read_pdf / view_image。
        for name in ("bash", "run_read"):
            self.assertIn(name, self.desc)
        self.assertNotIn("run_read_pdf", self.desc)
        self.assertNotIn("view_image", self.desc)

    def test_warns_against_promise_without_tool_call(self):
        """必须显式禁止"只回正文不调用工具"——这正是事故的直接触发动作。"""
        self.assertIn("立即发起", self.desc)
        self.assertIn("永远不会执行", self.desc)


class TestSubAgentSystemPrompt(unittest.TestCase):
    """子智能体自己的系统提示词必须只提真实工具名。"""

    def test_default_prompt_uses_real_tool_names(self):
        unknown = _unknown_tool_names(SubAgent.DEFAULT_SYSTEM_PROMPT)
        self.assertEqual(unknown, set(),
                         f"子智能体系统提示词里出现不存在的工具名: {sorted(unknown)}")
        # 读文件的唯一入口是 run_read（PDF / 图片 / Office 都由它分派）
        self.assertIn("run_read", SubAgent.DEFAULT_SYSTEM_PROMPT)
        # 必须明确纠正"read_pdf 这种名字不存在"，否则子智能体会照着幻觉的名字调用
        self.assertIn("没有", SubAgent.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("read_pdf", SubAgent.DEFAULT_SYSTEM_PROMPT)


class TestRuntimeActiveState(unittest.TestCase):
    """is_active 必须覆盖"后台子智能体执行中"这个窗口（busy=False 但仍在写文件）。"""

    def _runtime(self, busy: bool, bg_running: bool) -> SessionRuntime:
        rt = SessionRuntime(1, lambda *a, **k: None, lambda: None, lambda n: {})
        rt.busy = busy
        bg = BackgroundManager()
        rt.agent = type("A", (), {"background_manager": bg})()
        if bg_running:
            gate = threading.Event()
            bg.start_background_task("bash", {"command": "sleep"}, "call_x",
                                     lambda: gate.wait(5) and "done")
            deadline = time.monotonic() + 1.0
            while not bg.has_running() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.gate = gate
        return rt

    def _registry(self, rt):
        reg = SessionRuntimeRegistry(lambda *a, **k: None, lambda: None, lambda n: {})
        reg._sessions[1] = rt
        return reg

    def test_idle_is_not_active(self):
        reg = self._registry(self._runtime(busy=False, bg_running=False))
        self.assertFalse(reg.is_busy(1))
        self.assertFalse(reg.is_active(1))

    def test_turn_running_is_active(self):
        reg = self._registry(self._runtime(busy=True, bg_running=False))
        self.assertTrue(reg.is_active(1))

    def test_background_window_is_active_though_not_busy(self):
        """核心回归：后台子智能体在跑时 is_busy=False，但 is_active 必须为 True。"""
        rt = self._runtime(busy=False, bg_running=True)
        reg = self._registry(rt)
        self.assertFalse(reg.is_busy(1), "turn 已结束，busy 应为 False")
        self.assertTrue(reg.is_active(1), "后台任务在跑时必须视为有活动")
        self.gate.set()

    def test_unknown_session_is_not_active(self):
        reg = self._registry(self._runtime(busy=False, bg_running=False))
        self.assertFalse(reg.is_active(42))


if __name__ == "__main__":
    unittest.main()
