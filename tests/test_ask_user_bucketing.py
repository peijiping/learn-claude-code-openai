#!/usr/bin/env python3
"""`ask_user` 分桶与执行器契约守护测试 —— 2026-09-21。

`ask_user` 会**阻塞执行线程**等用户点击，因此它在四个执行桶里的位置不是风格
问题而是正确性问题。这里把三条硬约束固化成断言：

1. **独占**：即使模型同轮传了 `run_in_background=true` / `parallel=true`，
   `ask_user` 也必须落进提问桶 —— 落进并行池会在 worker 里等用户动作，
   落进后台桶会在守护线程里等一个永远无人应答的回复（前端看不到卡），
   两者都把线程挂死。
2. **排最后**：提问阶段必须排在串行阶段之后，保证"本轮其它工具先跑完，
   用户作答后模型才在下一迭代继续"（问完再干）。
3. **永不后台**：`_execute_tool_call` 的后台判定必须对 `ask_user` 短路 ——
   这是防御非 agent_loop 调用方的第二道闸。

另测 `_make_executor` 的私有参数透传（`_tool_call_id` / `_stop_event`）：
前者供前端把只读小结锚回发起它的 assistant 消息，后者让阻塞等待能被"停止"收束。
这两个键**不在模型可见的 schema 里**，普通工具也绝不能收到它们。

为什么用假 `tool_call` 对象而不是起整个 `agent_loop`：分桶是纯函数，喂
`SimpleNamespace` 即可；`_make_executor` / `_execute_tool_call` 只用几个字段，
`Agent.__new__` 手搭即可（不起 LLM、不碰用户目录）。

入口：`.venv/bin/python -m unittest discover -s tests`
"""

import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import agent_full_v2  # noqa: E402
from agent_full_v2 import Agent, _partition_tool_calls, _tool_args_of  # noqa: E402


def _tc(name, args=None, tcid=None, arguments=None, cached=None):
    """假 tool_call：只具备被读取的属性。

    `arguments` 可显式覆盖（测解析失败）；`cached` 用于模拟 agent_loop 预先写好的
    `_args_cache`。
    """
    raw = arguments if arguments is not None else json.dumps(args or {}, ensure_ascii=False)
    tc = SimpleNamespace(
        id=tcid or f"tc_{name}",
        function=SimpleNamespace(name=name, arguments=raw),
    )
    if cached is not None:
        tc._args_cache = cached
    return tc


class TestToolArgsOf(unittest.TestCase):
    def test_parses_json_string(self):
        self.assertEqual(_tool_args_of(_tc("bash", {"command": "ls"})), {"command": "ls"})

    def test_prefers_args_cache(self):
        tc = _tc("bash", {"command": "ls"}, cached={"command": "cached"})
        self.assertEqual(_tool_args_of(tc), {"command": "cached"})

    def test_broken_json_returns_empty(self):
        self.assertEqual(_tool_args_of(_tc("bash", arguments="{ 不是 json")), {})

    def test_missing_function_is_safe(self):
        self.assertEqual(_tool_args_of(SimpleNamespace(id="x")), {})


class TestBucketSemantics(unittest.TestCase):
    """分桶纯函数语义 —— 普通工具与改造前逐条一致，ask_user 独占。"""

    def test_normal_tools_keep_previous_semantics(self):
        plain = _tc("bash", {"command": "ls"})
        par = _tc("run_read", {"path": "a", "parallel": True})
        bg = _tc("sub_agent", {"prompt": "p", "run_in_background": True})
        background, parallel, serial, ask = _partition_tool_calls([plain, par, bg])
        self.assertEqual(background, [bg])
        self.assertEqual(parallel, [par])
        self.assertEqual(serial, [plain])
        self.assertEqual(ask, [])

    def test_background_wins_over_parallel_for_normal_tools(self):
        tc = _tc("bash", {"command": "ls", "run_in_background": True, "parallel": True})
        background, parallel, serial, _ = _partition_tool_calls([tc])
        self.assertEqual((background, parallel, serial), ([tc], [], []))

    def test_ask_user_ignores_background_and_parallel_flags(self):
        tc = _tc("ask_user", {
            "questions": [{"id": "q"}],
            "run_in_background": True,
            "parallel": True,
        })
        background, parallel, serial, ask = _partition_tool_calls([tc])
        self.assertEqual((background, parallel, serial), ([], [], []))
        self.assertEqual(ask, [tc])

    def test_ask_user_ignores_flags_in_args_cache_too(self):
        """agent_loop 走的是 _args_cache 分支，那条路上也必须被识别。"""
        tc = _tc("ask_user", cached={"questions": [], "run_in_background": True})
        _, _, _, ask = _partition_tool_calls([tc])
        self.assertEqual(ask, [tc])

    def test_ask_user_coexists_with_other_buckets(self):
        ask = _tc("ask_user", {"questions": []}, tcid="ask1")
        bg = _tc("bash", {"command": "ls", "run_in_background": True}, tcid="bg1")
        ser = _tc("run_read", {"path": "a"}, tcid="ser1")
        background, parallel, serial, asks = _partition_tool_calls([ask, bg, ser])
        self.assertEqual([t.id for t in background], ["bg1"])
        self.assertEqual([t.id for t in serial], ["ser1"])
        self.assertEqual([t.id for t in asks], ["ask1"])

    def test_declaration_order_preserved_within_bucket(self):
        first = _tc("ask_user", {"questions": []}, tcid="a1")
        second = _tc("ask_user", {"questions": []}, tcid="a2")
        _, _, _, asks = _partition_tool_calls([first, second])
        self.assertEqual([t.id for t in asks], ["a1", "a2"])

    def test_empty_input(self):
        self.assertEqual(_partition_tool_calls([]), ([], [], [], []))

    def test_malformed_call_is_not_dropped(self):
        """解析失败/形状残缺的调用不能被丢弃 —— 丢了就没人给它回填 tool_result。"""
        broken = _tc("bash", arguments="{ 不是 json")
        background, parallel, serial, _ = _partition_tool_calls([broken])
        self.assertEqual(serial, [broken])


class TestStageOrdering(unittest.TestCase):
    """提问阶段必须在串行阶段之后（结构断言 —— 执行顺序无法用纯函数表达）。"""

    def test_stage4_after_stage3(self):
        src = Path(agent_full_v2.__file__).read_text(encoding="utf-8")
        i3 = src.index("# 阶段 3")
        i4 = src.index("# 阶段 4")
        self.assertLess(i3, i4, "提问阶段必须排在串行阶段之后（否则用户作答前本轮工具还没跑）")

    def test_replay_loop_iterates_declaration_order(self):
        """回放顺序必须仍是"按声明顺序取结果"，否则 tool 消息会与 tool_calls 错位。"""
        src = Path(agent_full_v2.__file__).read_text(encoding="utf-8")
        self.assertIn("for tc in response_tool_calls:", src)


class _Recorder:
    """假的 ToolRegistry：记录 execute 调用。"""

    def __init__(self, resolvable=True):
        self.calls: list[tuple] = []
        self.resolvable = resolvable

    def resolve_handler(self, name):
        return (lambda **kw: "ok") if self.resolvable else None

    def execute(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return "ok"


class _FakeBG:
    def __init__(self, run_bg=True):
        self.run_bg = run_bg
        self.dispatched: list[tuple] = []

    def should_run_background(self, tool_name, tool_args):
        return self.run_bg

    def start_background_task(self, *args, **kwargs):
        self.dispatched.append((args, kwargs))
        return "bg_0001"


def _agent(tools, bg=None):
    ag = Agent.__new__(Agent)
    ag.tools = tools
    ag.background_manager = bg or _FakeBG(run_bg=False)
    ag.session_prefix = "[test]"
    ag.session_id = "FAKESESSION"
    ag._stop_evt = threading.Event()
    ag._print = lambda *a, **kw: None
    return ag


class TestMakeExecutor(unittest.TestCase):
    """私有参数只给 ask_user，普通工具零变化（这是"不影响存量"的证明）。"""

    def test_ask_user_gets_private_kwargs(self):
        rec = _Recorder()
        ag = _agent(rec)
        questions = [{"id": "q", "header": "h", "question": "q?", "options": [{"label": "a"}]}]
        executor = ag._make_executor("ask_user", {"questions": questions},
                                     tool_call_id="toolu_9", stop_event="EVT")
        self.assertEqual(executor(), "ok")
        name, kwargs = rec.calls[0]
        self.assertEqual(name, "ask_user")
        self.assertEqual(kwargs["questions"], questions)
        self.assertEqual(kwargs["_tool_call_id"], "toolu_9")
        self.assertEqual(kwargs["_stop_event"], "EVT")

    def test_ask_user_defaults_questions_to_empty_list(self):
        rec = _Recorder()
        executor = _agent(rec)._make_executor("ask_user", {}, tool_call_id="t", stop_event=None)
        executor()
        self.assertEqual(rec.calls[0][1]["questions"], [])

    def test_normal_tool_receives_no_private_kwargs(self):
        rec = _Recorder()
        executor = _agent(rec)._make_executor("bash", {"command": "ls"},
                                             tool_call_id="toolu_1", stop_event="EVT")
        executor()
        self.assertEqual(rec.calls[0], ("bash", {"command": "ls"}))

    def test_unknown_tool_returns_error_string(self):
        executor = _agent(_Recorder(resolvable=False))._make_executor("nope", {})
        self.assertIn("Unknown tool", executor())


class TestBackgroundGuard(unittest.TestCase):
    """`_execute_tool_call` 的后台判定必须对 ask_user 短路（第二道闸）。"""

    def test_ask_user_never_goes_background(self):
        rec, bg = _Recorder(), _FakeBG(run_bg=True)
        ag = _agent(rec, bg)
        out = ag._execute_tool_call(_tc("ask_user", {"questions": []}, tcid="toolu_1"))
        self.assertEqual(bg.dispatched, [], "ask_user 被误判为后台任务 → 用户看不到卡片且线程挂死")
        self.assertEqual(rec.calls[0][0], "ask_user")
        self.assertEqual(out["role"], "tool")
        self.assertEqual(out["tool_call_id"], "toolu_1")

    def test_ask_user_result_is_always_a_string(self):
        rec, bg = _Recorder(), _FakeBG(run_bg=True)
        ag = _agent(rec, bg)
        out = ag._execute_tool_call(_tc("ask_user", {"questions": []}))
        self.assertIsInstance(out["content"], str)

    def test_normal_tool_still_goes_background(self):
        """护栏不能误伤普通工具的后台路径。"""
        rec, bg = _Recorder(), _FakeBG(run_bg=True)
        ag = _agent(rec, bg)
        out = ag._execute_tool_call(_tc("bash", {"command": "ls"}, tcid="toolu_2"))
        self.assertEqual(len(bg.dispatched), 1)
        self.assertIn("Background task", out["content"])
        self.assertEqual(out["tool_call_id"], "toolu_2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
