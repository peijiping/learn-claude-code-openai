#!/usr/bin/env python3
"""附件块在「压缩 / token 估算」侧的降级守卫 —— 2026-09-20。

被守护的约定（`agents/context_compact.py`）：

  附件块（`{"type":"attachment",...}`）与已是线格式的图片块
  （`image_url` / `image` / `input_image`）在 `content_to_str()` 里**只降级为
  可读占位**（`[图片: x.png]` / `[图片]`），**绝不把块本身序列化进结果**。

为什么这条必须守死：`content_to_str` 是 L4 摘要与 token 估算的共同入口。

- L4 摘要把消息拼进摘要 prompt（预算 4000 token）—— 一张内联图片的 base64
  是几十万字符，`str(block)` 会让整个摘要请求直接报废，且**原图信息永久丢失**
  （摘要结果回写进 history，原消息被替换掉）；
- `estimate_tokens` 按字符数启发式估算 —— 被 base64 污染后单轮就"超预算"，
  上下文圆圈提前触顶 → 反复触发压缩 → 雪崩。

同时守住：**无附件块的消息行为与改造前逐字相同**（回归防线）。

运行：`.venv/bin/python -m unittest discover -s tests -v`
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from context_compact import ContextCompact  # noqa: E402

# 一段"像真的一样"的 base64（足够长才能暴露污染）
FAKE_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42m" + "A" * 4000


class AttachmentDowngradeTests(unittest.TestCase):
    def setUp(self):
        self.c = ContextCompact()

    # ── 附件块 ────────────────────────────────────────────────────
    def test_image_attachment_becomes_label(self):
        got = self.c.content_to_str([
            {"type": "attachment",
             "attachment": {"kind": "image", "name": "截图.png"}}])
        self.assertEqual(got, "[图片: 截图.png]")

    def test_document_attachment_becomes_label(self):
        got = self.c.content_to_str([
            {"type": "attachment",
             "attachment": {"kind": "document", "name": "季度报告.pdf"}}])
        self.assertEqual(got, "[附件: 季度报告.pdf]")

    def test_attachment_without_name_is_still_labeled(self):
        self.assertEqual(
            self.c.content_to_str([{"type": "attachment", "attachment": {}}]), "[附件]")
        self.assertEqual(self.c.content_to_str([{"type": "attachment"}]), "[附件]")

    def test_attachment_block_never_leaks_dict_repr(self):
        got = self.c.content_to_str([
            {"type": "attachment",
             "attachment": {"kind": "image", "name": "a.png", "source_path": "/x/a.png"}}])
        self.assertNotIn("{", got)
        self.assertNotIn("source_path", got)
        self.assertNotIn("/x/a.png", got)

    # ── 线格式图片块（历史数据 / 手工构造）────────────────────────
    def test_inline_image_url_becomes_placeholder_without_base64(self):
        for btype in ("image_url", "image", "input_image"):
            with self.subTest(btype=btype):
                got = self.c.content_to_str([{
                    "type": btype,
                    "image_url": {"url": f"data:image/png;base64,{FAKE_B64}"},
                }])
                self.assertEqual(got, "[图片]")
                self.assertNotIn("base64", got)
                self.assertNotIn("iVBOR", got)
                self.assertLess(len(got), 20)

    def test_mixed_content_keeps_text_and_degrades_images(self):
        got = self.c.content_to_str([
            {"type": "text", "text": "看看这两张图"},
            {"type": "attachment", "attachment": {"kind": "image", "name": "a.png"}},
            {"type": "attachment", "attachment": {"kind": "document", "name": "b.pdf"}},
        ])
        self.assertEqual(got, "看看这两张图\n[图片: a.png]\n[附件: b.pdf]")

    # ── token 估算不被污染 ────────────────────────────────────────
    def test_estimate_tokens_does_not_explode_on_inline_image(self):
        ledger = [{"role": "user", "content": [
            {"type": "text", "text": "看看这张图"},
            {"type": "attachment", "attachment": {"kind": "image", "name": "a.png"}},
        ]}]
        wire = [{"role": "user", "content": [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{FAKE_B64}"}},
        ]}]
        self.assertLess(self.c.estimate_tokens(wire), 100)
        self.assertLess(abs(self.c.estimate_tokens(wire)
                            - self.c.estimate_tokens(ledger)), 40)

    # ── L4 摘要 prompt 不含字节 ───────────────────────────────────
    def test_summary_prompt_contains_no_base64(self):
        msg = {"role": "user", "content": [
            {"type": "text", "text": "看看"},
            {"type": "attachment",
             "attachment": {"kind": "image", "name": "a.png"}},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{FAKE_B64}"}},
        ]}
        formatted = self.c._format_message_for_summary(msg)
        self.assertNotIn("base64", formatted)
        self.assertNotIn("iVBOR", formatted)
        self.assertIn("[图片: a.png]", formatted)

    def test_message_to_text_handles_attachment_only_message(self):
        text = self.c.message_to_text({"role": "user", "content": [
            {"type": "attachment", "attachment": {"kind": "image", "name": "a.png"}}]})
        self.assertEqual(text, "[图片: a.png]")

    # ── 回归防线：无附件块的行为逐字不变 ──────────────────────────
    def test_non_attachment_shapes_are_byte_identical(self):
        cases = [
            "纯字符串",
            "",
            None,
            ["裸字符串块", "第二个"],
            [{"type": "text", "text": "hi"}],
            [{"text": "有 text 键但没 type"}],
            [{"foo": 1}],
            [{"type": "tool_result", "content": "x"}],
            123,
        ]
        for case in cases:
            with self.subTest(case=repr(case)[:40]):
                expected = self._legacy_content_to_str(case)
                self.assertEqual(self.c.content_to_str(case), expected)

    @staticmethod
    def _legacy_content_to_str(content) -> str:
        """改造前的实现（逐字复制）—— 只有不带附件/图片块时才允许被比较。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    parts.append(str(block.get("text", block)))
                else:
                    parts.append(getattr(block, "text", str(block)))
            return "\n".join(parts)
        return str(content)


class SummaryRoundTripTests(unittest.TestCase):
    """摘要拼装路径上再确认一次：真实形状的消息不会把字节带进去。"""

    def test_compact_payload_has_no_attachment_bytes(self):
        c = ContextCompact()
        # 消息形状按**落盘后的真实形状**构造：assistant 行由
        # `session_manage._message_to_json_row` 归一化，恒带 tool_calls 键
        # （`is_ai_with_tool_use` 会直接索引它，缺键会 KeyError）。
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [
                {"type": "text", "text": "帮我看看"},
                {"type": "attachment",
                 "attachment": {"kind": "image", "name": "shot.png",
                                "source_path": "/Users/x/shot.png"}},
            ]},
            {"role": "assistant", "content": "好的", "tool_calls": []},
        ]
        dump = json.dumps(
            [c._format_message_for_summary(m) for m in messages], ensure_ascii=False)
        self.assertIn("[图片: shot.png]", dump)
        self.assertNotIn("base64", dump)
        self.assertNotIn("{'type'", dump)


if __name__ == "__main__":
    unittest.main()
