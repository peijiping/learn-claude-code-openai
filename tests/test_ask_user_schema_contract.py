#!/usr/bin/env python3
"""`ask_user` 工具契约守护测试 —— 2026-09-21。

`ask_user` 的定义与处理器是"模型会不会用、用了会不会挂死"的直接变量，
因此这里把四条硬约束固化成断言：

1. **只进主智能体工具集**（`tools`），**绝不进 `base_tools`**。
   子智能体只有单向事件上行、没有下行应答通道，且常跑在后台 daemon 线程里，
   拿到 `ask_user` 就会调用到一个永远无人应答的接口 → 线程挂死。
2. **schema 里不得出现 `parallel` / `run_in_background`**。
   引擎侧硬编码"ask_user 永远串行且独占"，schema 若给这两个字段就复刻了
   sub_agent 那次"required 必填、description 又禁止"的自相矛盾事故。
3. **`allow_custom` 必须是显式字段**（不靠 `label == "其他"` 的隐式约定）。
4. **description 必须写明禁止索取敏感信息** —— 这是唯一能直接把模型输出
   引向用户的通道。

另测处理器契约：无 broker 时返回可读 `Error:` 文本（不抛异常、不挂死）、
有 broker 时把 `questions` / `_tool_call_id` / `_stop_event` 正确透传。

入口：`.venv/bin/python -m unittest discover -s tests`
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from tools import ToolRegistry  # noqa: E402


def _bare_registry() -> ToolRegistry:
    """不触发任何磁盘构造的 ToolRegistry（只用它的工具定义，不碰用户目录）。"""
    reg = ToolRegistry.__new__(ToolRegistry)
    reg._base_tools_cache = None
    reg._tools_cache = None
    reg._main_agent_tools_cache = None
    reg._default_agent_tools_cache = None
    reg._handlers_cache = None
    # `tools` 属性会调用 _mcp_discovery_tool_defs()，它读 self._mcp_manager
    # （None = 未注入，返回空列表）。__new__ 桩不含 __init__ 字段，必须补。
    reg._mcp_manager = None
    # 刻意**不设** _interaction_broker：正好验证 getter 的 getattr 兜底。
    return reg


def _names(defs: list) -> set:
    return {d["function"]["name"] for d in defs}


class TestAskUserSchema(unittest.TestCase):
    """schema 自身的形状与措辞。"""

    def setUp(self):
        self.fn = _bare_registry()._ask_user_tool_def()["function"]
        self.params = self.fn["parameters"]
        self.desc = self.fn["description"]

    def test_required_is_questions_only(self):
        self.assertEqual(self.params["required"], ["questions"])

    def test_questions_bounds(self):
        q = self.params["properties"]["questions"]
        self.assertEqual(q["type"], "array")
        self.assertEqual(q["minItems"], 1)
        self.assertEqual(q["maxItems"], 4)

    def test_question_item_required_fields(self):
        item = self.params["properties"]["questions"]["items"]
        for field in ("id", "header", "question", "options"):
            self.assertIn(field, item["required"], f"缺少必填字段 {field}")
            self.assertIn(field, item["properties"])

    def test_options_bounds(self):
        opts = self.params["properties"]["questions"]["items"]["properties"]["options"]
        self.assertEqual(opts["minItems"], 2)
        self.assertEqual(opts["maxItems"], 4)
        self.assertEqual(opts["items"]["required"], ["label"])

    def test_allow_custom_is_explicit(self):
        """显式建模 —— 禁止靠 label 文案判断（那是 Claude Code 宿主的约定，不是 schema）。"""
        props = self.params["properties"]["questions"]["items"]["properties"]
        self.assertIn("allow_custom", props)
        self.assertEqual(props["allow_custom"]["type"], "boolean")
        self.assertIs(props["allow_custom"]["default"], True)
        self.assertIn("custom_label", props)

    def test_multi_select_defaults_false(self):
        props = self.params["properties"]["questions"]["items"]["properties"]
        self.assertIn("multi_select", props)
        self.assertIs(props["multi_select"]["default"], False)

    def test_description_forbids_sensitive_info(self):
        self.assertIn("敏感", self.desc)
        self.assertIn("禁止", self.desc)
        self.assertIn("凭据", self.desc)

    def test_description_states_blocking_and_serial(self):
        self.assertIn("阻塞", self.desc)
        self.assertIn("串行", self.desc)

    def test_description_has_negative_guidance(self):
        """必须明确"何时不该用"，否则模型会退化成每轮都问一遍。"""
        self.assertIn("何时不该用", self.desc)

    def test_schema_has_no_parallel_or_background_flags(self):
        """引擎侧硬编码 ask_user 独占串行；**schema 字段层**出现这两个 flag 就是
        自相矛盾 —— 复刻 sub_agent 那次「required 里必填、description 又禁止」
        的事故（模型在那个场景下每次都必须违规）。

        注意：description 里**有意**提醒模型"这两个参数无效"，这不算违规
        （与 sub_agent 的教训相反：那里是字段存在，这里是纯文字提醒）。
        """
        props = self.params["properties"]
        self.assertNotIn("parallel", props)
        self.assertNotIn("run_in_background", props)
        self.assertNotIn("parallel", self.params["required"])
        self.assertNotIn("run_in_background", self.params["required"])
        item_props = props["questions"]["items"]["properties"]
        for flag in ("parallel", "run_in_background"):
            self.assertNotIn(flag, item_props)
        # 正面要求：描述里必须说清这两个参数对本工具无效
        self.assertIn("run_in_background", self.desc)
        self.assertIn("parallel", self.desc)

    def test_header_mentions_12_char_limit(self):
        props = self.params["properties"]["questions"]["items"]["properties"]
        self.assertIn("12", props["header"]["description"])


class TestToolSetLayering(unittest.TestCase):
    """分层：主有、子无 —— 这条错了就是线程挂死的 bug。"""

    def setUp(self):
        self.reg = _bare_registry()

    def test_in_main_tool_set(self):
        self.assertIn("ask_user", _names(self.reg.tools))

    def test_absent_from_base_tools(self):
        """子智能体工具集绝不能含 ask_user。"""
        self.assertNotIn("ask_user", _names(self.reg.base_tools))

    def test_in_main_agent_tools_and_default_agent_tools(self):
        self.assertIn("ask_user", _names(self.reg.main_agent_tools))
        self.assertIn("ask_user", _names(self.reg.default_agent_tools))
        self.assertIn("ask_user", _names(self.reg.build_agent_tools(True)))

    def test_handler_registered(self):
        self.assertIsNotNone(self.reg.resolve_handler("ask_user"))

    def test_all_tool_defs_are_well_formed(self):
        """顺手守住整份工具集的信封形状（历史上有过漏 type/function 的写法）。"""
        for d in self.reg.tools:
            self.assertEqual(d["type"], "function")
            fn = d["function"]
            self.assertTrue(fn.get("name"), d)
            self.assertTrue(fn.get("description"), fn.get("name"))
            self.assertEqual(fn["parameters"]["type"], "object")


class _FakeBroker:
    def __init__(self, result="用户已完成选择：\n- [x] q? → a", exc=None):
        self.result = result
        self.exc = exc
        self.calls: list[dict] = []
        self.closed = False

    def ask(self, questions, tool_call_id="", stop_event=None):
        self.calls.append({
            "questions": questions,
            "tool_call_id": tool_call_id,
            "stop_event": stop_event,
        })
        if self.exc is not None:
            raise self.exc
        return self.result


class TestAskUserHandler(unittest.TestCase):
    """处理器契约：字符串进、字符串出，绝不抛异常。"""

    def setUp(self):
        self.reg = _bare_registry()

    def test_no_broker_returns_readable_error(self):
        out = self.reg.execute("ask_user", questions=[])
        self.assertIsInstance(out, str)
        self.assertIn("ask_user 当前不可用", out)
        self.assertIn("Error:", out)

    def test_holder_roundtrip(self):
        broker = _FakeBroker()
        self.assertIsNone(self.reg.get_interaction_broker())
        self.reg.set_interaction_broker(broker)
        self.assertIs(self.reg.get_interaction_broker(), broker)

    def test_transparent_passthrough(self):
        broker = _FakeBroker()
        self.reg.set_interaction_broker(broker)
        stop_evt = object()
        questions = [{"id": "q", "header": "h", "question": "q?", "options": [{"label": "a"}]}]
        out = self.reg.execute(
            "ask_user", questions=questions,
            _tool_call_id="toolu_1", _stop_event=stop_evt,
        )
        self.assertEqual(out, broker.result)
        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(broker.calls[0]["questions"], questions)
        self.assertEqual(broker.calls[0]["tool_call_id"], "toolu_1")
        self.assertIs(broker.calls[0]["stop_event"], stop_evt)

    def test_missing_questions_passes_empty_list(self):
        """模型漏传 questions 时不炸 —— 交给 broker 的参数校验出可读文案。"""
        broker = _FakeBroker()
        self.reg.set_interaction_broker(broker)
        self.reg.execute("ask_user")
        self.assertEqual(broker.calls[0]["questions"], [])

    def test_broker_exception_becomes_error_text(self):
        self.reg.set_interaction_broker(_FakeBroker(exc=RuntimeError("炸了")))
        out = self.reg.execute("ask_user", questions=[])
        self.assertIn("Error: ask_user 执行失败", out)
        self.assertIn("RuntimeError", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
