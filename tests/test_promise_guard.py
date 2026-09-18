"""「承诺未兑现」守卫的守卫测试（`agent_full_v2._looks_like_unfulfilled_promise`）。

运行方式::

    cd /Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main
    .venv/bin/python -m unittest discover -s tests -v

## 被守护的约定（2026-09-14 事故）

模型偶尔会产出「正文承诺要干活、却一个 `tool_call` 都没发」的**正常 `stop`** 响应
（实测 session_4：回了"我派一个子智能体后台去读"然后本轮直接结束，用户侧表现为
"直接中断、不往下执行、看不到最终结果"）。引擎在停止边界加了一道**窄口径**拦截：
短正文 + 明确行动承诺 + 零工具调用 + 非过去语态 → 回注一条
`<system-reminder>` 提醒，让模型在本轮把话说圆。

本文件守住三件事：

1. **口径够窄**：真实语料里 6 条"零工具调用的 assistant 消息"只能命中 1 条
   （就是事故那条），过去式汇报 / 长最终报告 / 普通问答 / 反问**一律不许命中**；
2. **提醒必须被包裹**：`PROMISE_GUARD_REMINDER` 必须以 `<system-reminder>` 开头，
   否则会以"用户气泡"的形式漏到前端聊天界面（同 `task_notification` 的教训）；
3. **循环真的会续跑**：命中后 `agent_loop` 注入提醒并 continue，且每轮有次数上限。

改动 `_PROMISE_RE` / `_PAST_FOLLOW` / `PROMISE_GUARD_MAX*` 时请同步加/改这里的断言。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import agent_full_v2  # noqa: E402
from agent_full_v2 import (  # noqa: E402
    Agent,
    PROMISE_GUARD_MAX,
    PROMISE_GUARD_REMINDER,
    _looks_like_unfulfilled_promise,
)
from background_manager import BackgroundManager  # noqa: E402
from error_recovery import ErrorRecovery  # noqa: E402
from hooks import HookSystem  # noqa: E402
from session_manage import SessionManager  # noqa: E402
from streaming_client import PrintSink, StreamedMessage, StreamedToolCall  # noqa: E402
from subagent_store import SubagentStore  # noqa: E402

SYSTEM_REMINDER_PREFIX = "<system-reminder>"

# 事故原文（session_4.jsonl 末条 assistant），必须命中
INCIDENT_TEXT = (
    "工作空间里 `DRG_Docs/` 下有 6 个 PDF，属于批量读取，"
    "我派一个子智能体后台去读，避免污染主上下文。"
)


class PromiseDetectorTests(unittest.TestCase):
    """判定口径：宁可不拦，也不许打断正常收尾。"""

    def test_incident_text_is_caught(self):
        """回归事故原文 —— 这是本守卫存在的唯一理由。"""
        self.assertTrue(_looks_like_unfulfilled_promise(INCIDENT_TEXT))

    def test_short_promises_are_caught(self):
        for text in (
            "我这就去读取这 6 个 PDF。",
            "我先去查看一下 DRG_Docs 目录。",
            "接下来我会派一个子智能体处理这批文件。",
            "Let me dispatch a sub-agent to read all 6 PDFs.",
            "I'll read all six PDFs now.",
        ):
            with self.subTest(text=text):
                self.assertTrue(_looks_like_unfulfilled_promise(text), text)

    def test_past_tense_reports_are_not_caught(self):
        """过去/完成/名词化语态是在汇报，不是在承诺。"""
        for text in (
            "我派出的那个子智能体已完成，6 篇 PDF 的摘要如下：…",
            "我派的子智能体已完成。",
            "我已经读取了 6 篇 PDF 的首页文本，结论如下…",
            "我读取了全部 6 篇文件，下面逐篇说明。",
        ):
            with self.subTest(text=text):
                self.assertFalse(_looks_like_unfulfilled_promise(text), text)

    def test_long_final_report_is_not_caught(self):
        """真正的最终答复通常很长；长度上限是防止误拦的主要阀门。"""
        long_report = (
            "已完成。先说明一点：子智能体在本环境里只返回占位摘要，拿不到真实内容，"
            "所以我改用 run_read_pdf 直接读取，6 篇结论如下：\n" + "详细内容" * 300
        )
        self.assertFalse(_looks_like_unfulfilled_promise(long_report))

    def test_plain_answers_are_not_caught(self):
        for text in ("1+1=2。", "好的。", "", "   ", "你希望我用子智能体读，还是我直接读？"):
            with self.subTest(text=text):
                self.assertFalse(_looks_like_unfulfilled_promise(text), text)


class PromiseReminderContractTests(unittest.TestCase):
    def test_reminder_is_wrapped_as_system_reminder(self):
        """必须以 <system-reminder> 开头：ws_bridge._history_to_ui 靠它从前端过滤。"""
        self.assertTrue(PROMISE_GUARD_REMINDER.startswith(SYSTEM_REMINDER_PREFIX))
        self.assertIn("promise_guard", PROMISE_GUARD_REMINDER)
        # 提醒里必须同时给"立刻调用工具"与"给最终答复"两条出路，不能只让模型干活
        self.assertIn("发起", PROMISE_GUARD_REMINDER)
        self.assertIn("最终答复", PROMISE_GUARD_REMINDER)

    def test_budget_is_bounded_and_configurable(self):
        self.assertGreaterEqual(PROMISE_GUARD_MAX, 0)
        self.assertLessEqual(PROMISE_GUARD_MAX, 3, "上限过高会与原回复反复纠缠")


# ── 离线 Agent：驱动真实 agent_loop（照 scripts/repro_bg_subagent_events.py）──

class _ScriptedLLM:
    """按脚本逐次返回 (message, finish_reason, usage) 的 streamed_create 替身。"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = 0

    def __call__(self, llm, sinks=None, should_stop=None, **kwargs):
        self.calls += 1
        idx = min(self.calls, len(self.scripts)) - 1
        spec = self.scripts[idx]
        tools = None
        if spec.get("tool"):
            tools = [StreamedToolCall("call_x", spec["tool"], "{}")]
        msg = StreamedMessage(spec.get("content"), spec.get("reasoning"), tools)
        return msg, spec.get("finish", "stop"), {"prompt_tokens": 1, "completion_tokens": 1}


class _StubTools:
    def build_agent_tools(self, team_mode=False):
        return []

    def get_todo_manager(self):
        return SimpleNamespace(has_open_items=lambda: False)


class _StubSubAgentRunner:
    """子智能体替身：只把 (摘要, transcript) 原样返回，避免真调 LLM。"""

    def spawn_subagent(self, prompt, system_prompt=None, allowed_tools=None,
                       workdir=None, tool_call_id=""):
        return "桩摘要", {
            "subagent_id": "sub_stub", "tool_call_id": tool_call_id,
            "name": "子智能体", "status": "done", "prompt": prompt, "thinking": "",
            "text": "桩摘要", "toolCalls": [], "error": "",
            "started_at": "", "duration_ms": 1,
        }


def _make_offline_agent(tmp: Path) -> Agent:
    """离线 Agent（绕开会真连 LLM/MCP 的 __init__），只填 agent_loop 需要的字段。"""
    agent = Agent.__new__(Agent)
    agent.session_prefix = "session_"
    agent.session_id = "1"
    agent.silent = True
    agent.total_tokens = 0
    # token 记账字段：对齐 Agent.__init__（2026-09 新增的用量统计段）。
    # 本桩直接调 agent.agent_loop()（绕过 __init__ 与 run_turn），必须手工补齐，
    # 否则 _accumulate_usage 抛 AttributeError → [_unrecoverable] 提前收尾，
    # 表现为"脚本只被调用 1 次"（2026-09-16 修复：随引擎新增记账字段而桩过期）。
    agent._turn_usage = dict(agent_full_v2._ZERO_USAGE)
    agent.usage_totals = {**agent_full_v2._ZERO_USAGE, "turns": 0}
    agent._in_turn = True          # 真实链路里由 run_turn 置位
    agent._turn_model_id = None    # 由 SessionRuntime 在 run_turn 前设置
    agent._turn_switches = []
    agent.max_agent_iterations = 10
    agent._stop_evt = __import__("threading").Event()
    agent.model = "offline-stub"
    agent.llm_client = None
    agent._request_overrides = {"reasoning_effort": None, "max_context": None}
    agent.team_mode = False
    agent.background_manager = BackgroundManager()
    agent.stream_sink = PrintSink(silent=True)
    agent.tools = _StubTools()

    sm = SessionManager(tmp, "sys", subagent_store=SubagentStore(tmp))
    agent.session_manager = sm
    agent.session_file = tmp / "session_1.jsonl"
    agent.session_file.write_text("", encoding="utf-8")
    agent.history_messages = [{"role": "system", "content": "sys"},
                             {"role": "user", "content": "看看我的pdf文件内容有哪些，用子智能体看"}]

    agent._sync_memory_index = lambda: None
    agent._sync_environment = lambda: None
    agent._sync_project_rules = lambda: None
    agent._print = lambda *a, **k: None
    agent._advanced_llm_kwargs = lambda: {}
    agent.goal_controller = SimpleNamespace(
        begin_query=lambda: None, active=None,
        evaluate_after_turn=lambda *a, **k: SimpleNamespace(action="allow", reason=""))

    hooks = HookSystem(silent=True)
    hooks.register_default_hooks()
    agent.hook_system = hooks
    agent.recovery = ErrorRecovery(primary_model="offline-stub", fallback_model="")

    import threading
    agent._subagent_transcripts = {}
    agent._subagent_persisted = set()
    agent._subagent_lock = threading.Lock()
    agent.subagent_runner = _StubSubAgentRunner()
    return agent


class PromiseGuardLoopTests(unittest.TestCase):
    """真实 agent_loop 的停止边界行为。"""

    def _run(self, scripts):
        ctx = tempfile.TemporaryDirectory()
        self.addCleanup(ctx.cleanup)
        agent = _make_offline_agent(Path(ctx.name))
        orig = agent_full_v2.streamed_create
        scripted = _ScriptedLLM(scripts)
        agent_full_v2.streamed_create = scripted
        self.addCleanup(lambda: setattr(agent_full_v2, "streamed_create", orig))
        agent.agent_loop()
        return agent, scripted

    def test_promise_then_answer_continues_once(self):
        """承诺无调用 → 注入提醒并续跑；第二次给出正常答复后收尾。"""
        agent, scripted = self._run([
            {"content": INCIDENT_TEXT, "finish": "stop"},
            {"content": "6 篇 PDF 的主题分别是…（正常最终答复）", "finish": "stop"},
        ])

        self.assertEqual(scripted.calls, 2, "必须在守卫处续跑一次")
        injected = [m for m in agent.history_messages
                    if m.get("content") == PROMISE_GUARD_REMINDER]
        self.assertEqual(len(injected), 1, "提醒必须且只注入一次")
        # 提醒必须落盘（与 task_notification 同一约定），否则切会话后模型上下文缺一块
        persisted = [m for m in agent.session_manager.load_session_history(agent.session_file)
                     if isinstance(m.get("content"), str)
                     and m["content"].startswith(SYSTEM_REMINDER_PREFIX)]
        self.assertTrue(persisted, "提醒必须写入会话文件")
        self.assertEqual(agent.history_messages[-1]["content"],
                         "6 篇 PDF 的主题分别是…（正常最终答复）")

    def test_normal_answer_ends_turn_immediately(self):
        """正常收尾不许被拦：只调一次 LLM。"""
        agent, scripted = self._run([
            {"content": "6 篇 PDF 的结论如下：…（一份很正常的最终答复）", "finish": "stop"},
        ])
        self.assertEqual(scripted.calls, 1)
        self.assertFalse([m for m in agent.history_messages
                          if m.get("content") == PROMISE_GUARD_REMINDER])

    def test_tool_call_is_never_intercepted(self):
        """真的发起了工具调用 → 不关守卫的事。"""
        agent, scripted = self._run([
            {"content": "我派一个子智能体去读。", "tool": "sub_agent", "finish": "tool_calls"},
            {"content": "已派发。", "finish": "stop"},
        ])
        self.assertEqual(scripted.calls, 2, "因工具调用而继续，非守卫触发")
        self.assertFalse([m for m in agent.history_messages
                          if m.get("content") == PROMISE_GUARD_REMINDER])

    def test_max_hits_bounded(self):
        """连续承诺也不会无限纠缠：拦截次数受 PROMISE_GUARD_MAX 限制。"""
        scripts = [{"content": INCIDENT_TEXT, "finish": "stop"}] * (PROMISE_GUARD_MAX + 3)
        agent, scripted = self._run(scripts)
        self.assertEqual(scripted.calls, PROMISE_GUARD_MAX + 1,
                         f"最多拦 {PROMISE_GUARD_MAX} 次，之后必须放行收尾")


if __name__ == "__main__":
    unittest.main()
