#!/usr/bin/env python3
"""目标评估器稳定性守护测试 —— 2026-09-30。

用户报的原始问题："我的目标模式测试报错了（`GoalError: goal evaluator returned
invalid JSON`），而且对话框上那条目标提示一直停在第一轮"。

根因（实测复现）：评估器默认**开着思考**且 `max_tokens=512`，而 deepseek-flash 这类
思考模型会把 completion 预算先烧在 `reasoning_content` 上 —— 一旦烧满就
`finish_reason=length` / `content=None`，`json.loads("")` 必然抛错。约 1/5 概率命中。
评估器是"会话能否自动续跑"的唯一判据，它一报错整轮就以 error 结束（`active.iterations`
不增 → 前端 `round+1` 永远显示"第 1 轮"）。

本文件钉住四层修复：

1. `_parse_json_object` 容错：整段解析失败时截取**第一个配对完整的 JSON 对象**
   （括号深度扫描，跳过字符串内部/转义），容忍前后杂字；截断/空仍必须抛错。
2. `_extract_json_object` 的配对扫描本身（含字符串里的 `{` `}` 干扰）。
3. 评估器调用**强制关思考**：`extra_body={"thinking": {"type": "disabled"}}`
   必须真的传进 `streamed_create`（这是根因修复，回归即"又会随机报 invalid JSON"）。
4. 失败**重试一次**并放大输出预算；全部失败才抛 `GoalError`（契约不变）。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）。
本文件不构造 Agent、不联网，只需 dummy `OPENAI_API_KEY`（仓库既有测试约定）。
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import goal as G  # noqa: E402


class _FakeMessage:
    """streamed_create 聚合结果的替身（只用到 content）。"""

    def __init__(self, content):
        self.content = content
        self.reasoning_content = None
        self.tool_calls = None


def _patch_create(content, finish="stop"):
    """把 goal.streamed_create 换成假实现，返回 (mock, calls) 以便查参数。"""
    calls = []

    def fake(llm, **kwargs):
        calls.append(kwargs)
        return _FakeMessage(content), finish, {}

    return mock.patch.object(G, "streamed_create", side_effect=fake), calls


class ParseJsonObjectTest(unittest.TestCase):
    """解析与校验：该容的容、该拒的拒。"""

    def test_plain_json(self):
        value = G._parse_json_object('{"ok": true, "reason": "done", "impossible": false}')
        self.assertEqual(value, {"ok": True, "reason": "done", "impossible": False})

    def test_markdown_fence(self):
        text = '```json\n{"ok": false, "reason": "缺测试", "impossible": false}\n```'
        self.assertEqual(G._parse_json_object(text)["reason"], "缺测试")

    def test_prose_before_and_after(self):
        """模型爱在 JSON 前后加一句解释 —— 截取配对对象即可。"""
        text = '好的，我的判定如下：\n{"ok": true, "reason": "都做了", "impossible": false}\n以上。'
        self.assertEqual(G._parse_json_object(text)["ok"], True)

    def test_braces_inside_string_do_not_break_scan(self):
        text = '{"ok": false, "reason": "未看到 {a} 的结果", "impossible": false}'
        self.assertEqual(G._parse_json_object(text)["reason"], "未看到 {a} 的结果")

    def test_escaped_quote_in_string(self):
        text = r'{"ok": false, "reason": "他说 \"还没做\"", "impossible": false}'
        self.assertIn("还没做", G._parse_json_object(text)["reason"])

    def test_truncated_still_raises(self):
        """输出被截断（括号不配对）必须仍按失败处理，不能"猜到一半"。"""
        with self.assertRaises(G.GoalError) as ctx:
            G._parse_json_object('{"ok": true, "reason": "done"')
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_empty_still_raises(self):
        """content 为空的原始报错场景，必须保留 GoalError 契约。"""
        with self.assertRaises(G.GoalError):
            G._parse_json_object("")

    def test_field_validation(self):
        with self.assertRaises(G.GoalError):
            G._parse_json_object('{"ok": "true", "reason": "x"}')  # ok 非 bool
        with self.assertRaises(G.GoalError):
            G._parse_json_object('{"ok": true, "reason": "   "}')  # reason 空
        with self.assertRaises(G.GoalError):
            G._parse_json_object('{"ok": true, "reason": "x", "impossible": "no"}')
        with self.assertRaises(G.GoalError):
            G._parse_json_object('{"ok": true, "reason": "x", "impossible": true}')


class ExtractJsonObjectTest(unittest.TestCase):
    """配对扫描本身。"""

    def test_returns_first_balanced_object(self):
        text = '前言 {"a": {"b": 1}} 后缀 {"c": 2}'
        self.assertEqual(G._extract_json_object(text), '{"a": {"b": 1}}')

    def test_none_when_unclosed(self):
        self.assertIsNone(G._extract_json_object('{"a": 1'))
        self.assertIsNone(G._extract_json_object("no object here"))


class EvaluatorCallTest(unittest.TestCase):
    """评估器调用参数与重试：这两条是"不再随机报 invalid JSON"的根。"""

    def _evaluator(self, **kwargs):
        return G.PromptGoalEvaluator(llm_client=object(), model="m", **kwargs)

    def test_thinking_is_disabled(self):
        """根因修复：必须显式关思考，否则 reasoning 会吃掉输出预算。"""
        patcher, calls = _patch_create('{"ok": true, "reason": "done", "impossible": false}')
        with patcher:
            self._evaluator(max_tokens=512).evaluate("cond", [])
        self.assertEqual(calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(calls[0]["max_tokens"], 512)

    def test_retry_with_larger_budget(self):
        """首次解析失败 → 第二次放大预算（关不掉思考的端点兜底）。"""
        results = [
            (None, "length"),  # 预算被思考吃满：content 为空
            ('{"ok": true, "reason": "重试成功", "impossible": false}', "stop"),
        ]
        calls = []

        def fake(llm, **kwargs):
            calls.append(kwargs["max_tokens"])
            content, finish = results[len(calls) - 1]
            return _FakeMessage(content), finish, {}

        with mock.patch.object(G, "streamed_create", side_effect=fake):
            evaluation = self._evaluator(max_tokens=512).evaluate("cond", [])
        self.assertTrue(evaluation.ok)
        self.assertEqual(calls, [512, 512 * G.EVALUATOR_RETRY_TOKEN_FACTOR])

    def test_all_attempts_failed_raises_goal_error(self):
        """契约不变：全失败仍抛 GoalError，由 controller 转成 error 态。"""
        with _patch_create("抱歉，我无法给出 JSON")[0]:
            with self.assertRaises(G.GoalError):
                self._evaluator(max_tokens=512).evaluate("cond", [])

    def test_max_tokens_env_override(self):
        """GOAL_EVALUATOR_MAX_TOKENS 可覆盖默认预算（可调参数走配置的约定）。"""
        with mock.patch.dict(os.environ, {"GOAL_EVALUATOR_MAX_TOKENS": "2048"}):
            self.assertEqual(self._evaluator().max_tokens, 2048)
        self.assertEqual(
            self._evaluator().max_tokens, G.DEFAULT_EVALUATOR_MAX_TOKENS
        )

    def test_explicit_max_tokens_wins(self):
        with mock.patch.dict(os.environ, {"GOAL_EVALUATOR_MAX_TOKENS": "2048"}):
            self.assertEqual(self._evaluator(max_tokens=64).max_tokens, 64)


if __name__ == "__main__":
    unittest.main()
