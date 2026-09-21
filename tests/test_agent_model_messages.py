#!/usr/bin/env python3
"""发送边界的附件展开守卫 —— 2026-09-20。

`Agent._model_messages()` 是 jsonl（账本形态）与 provider 线格式之间的**唯一
转换点**。本文件守两条硬约定：

  1. **无附件会话逐字节等价**：对任何不含附件块的历史，输出必须与改造前的实现
     完全一致（本项目"改动不得污染存量行为"的硬要求，也是这次引擎层改动的
     安全保险丝）。断言方式是**内联复刻旧实现**做逐元素比较，而不是靠人工检查。
  2. **展开不回写历史**：`history_messages` 在调用后必须仍是账本形态
     （内存与 jsonl 同量级；base64 只存在于那一次请求里）。

顺带覆盖：附件块展开成线格式、未知字段仍被白名单剔除、会话未建立时不展开。

运行：`.venv/bin/python -m unittest discover -s tests -v`
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import agent_full_v2  # noqa: E402
import attachments as A  # noqa: E402
import refs as R  # noqa: E402
from agent_full_v2 import MODEL_MSG_FIELDS, Agent  # noqa: E402
from paths import WorkspacePaths  # noqa: E402


def _legacy_model_messages(history: list) -> list:
    """改造前的 `_model_messages` 实现（逐字复制 → 作为等价性基准）。"""
    return [
        {k: m[k] for k in MODEL_MSG_FIELDS if k in m}
        for m in history
    ]


def _stub_agent(history: list, workspace: WorkspacePaths | None,
                session_id: str | None) -> Agent:
    """`Agent.__new__` + 只填本用例用到的字段（不触发构造流程/网络/工具）。"""
    agent = Agent.__new__(Agent)
    agent.history_messages = history
    agent.workspace = workspace
    agent.session_id = session_id
    return agent


class ModelMessagesEquivalenceTests(unittest.TestCase):
    """核心：无附件时与旧实现逐字节相等。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = WorkspacePaths("default", self.root, self.root, bash_cwd=self.root)

    def _history(self) -> list:
        """覆盖真实落盘行的各种形态（含无附件、缺字段、多模态文本块）。"""
        return [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "纯文本消息",
             "created_at": "2026-09-20T10:00:00"},
            {"role": "assistant", "content": "回复",
             "reasoning_content": "think", "tool_calls": [],
             "usage": {"prompt_tokens": 1}, "model_info": {"id": "m"}},
            {"role": "tool", "content": "结果", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_2", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "user", "content": [{"type": "text", "text": "只有文本块"}]},
        ]

    def test_no_attachment_output_is_byte_identical(self):
        history = self._history()
        agent = _stub_agent(list(history), self.ws, "SESS000001")
        self.assertEqual(agent._model_messages(), _legacy_model_messages(history))

    def test_equivalence_holds_without_session(self):
        """会话未建立（CLI / cron 首轮前）：同样必须一比一等价。"""
        history = self._history()
        agent = _stub_agent(list(history), None, None)
        self.assertEqual(agent._model_messages(), _legacy_model_messages(history))

    def test_no_ref_history_is_identical_and_passes_through(self):
        """引用（@-mention，2026-09-21）不得污染存量。

        两道断言缺一不可：
        - 等值：无引用历史的输出与旧实现完全一致；
        - **透传（身份）**：引用展开这一步必须把附件展开的产物**原对象**交出去。
          只要将来有人把它改成"总是返回新 dict"，等值断言照样通过，而"零复制、
          无引用时逐字节等价"的结构保证就没了 —— 这条才是真正的保险丝。
        """
        history = self._history()
        agent = _stub_agent(list(history), self.ws, "SESS000001")

        seen: list = []
        real_expand = agent_full_v2.expand_content_for_model

        def _spy(msg, session_dir, **kw):
            out = real_expand(msg, session_dir, **kw)
            seen.append(out)
            return out

        with mock.patch.object(agent_full_v2, "expand_content_for_model", _spy):
            projected = agent._model_messages()

        self.assertEqual(projected, _legacy_model_messages(history))
        self.assertEqual(len(seen), len(projected))
        for got, mid in zip(projected, seen):
            self.assertIs(got, mid,
                          "无引用块时引用展开必须是恒等透传（返回同一对象）")
        # 历史本身一字未改
        self.assertEqual(agent.history_messages, list(history))

    def test_attachment_session_dir_none_without_session_id(self):
        self.assertIsNone(_stub_agent([], self.ws, None)._attachment_session_dir())
        self.assertIsNone(_stub_agent([], self.ws, "")._attachment_session_dir())
        self.assertIsNone(_stub_agent([], None, "SESS000001")._attachment_session_dir())
        self.assertEqual(
            _stub_agent([], self.ws, "SESS000001")._attachment_session_dir(),
            self.ws.attachments_dir / "SESS000001")


class ModelMessagesExpandTests(unittest.TestCase):
    """有附件时展开为线格式，且不回写历史。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = WorkspacePaths("default", self.root, self.root, bash_cwd=self.root)
        self.sid = "SESS000002"
        A.clear_expand_cache()
        self.addCleanup(A.clear_expand_cache)
        self.src = self.root / "src"
        self.src.mkdir(parents=True, exist_ok=True)

    def _stage(self, name: str, text: str) -> dict:
        p = self.src / name
        p.write_text(text, encoding="utf-8")
        result = A.stage(self.ws, [str(p)])
        self.assertEqual(result["failed"], [])
        return result["items"][0]

    def _ledger_message(self, item: dict, text: str) -> dict:
        records = A.migrate_to_session(
            self.ws, [{"att_id": item["att_id"], "kind": item["kind"],
                       "name": item["name"], "ext": item["ext"]}], self.sid)
        return {"role": "user", "content": A.build_user_content(text, records),
                "created_at": "2026-09-20T10:00:00"}

    def test_text_attachment_expands_to_text_block(self):
        item = self._stage("notes.txt", "附件里的正文")
        msg = self._ledger_message(item, "看看这个")
        agent = _stub_agent([msg], self.ws, self.sid)

        sent = agent._model_messages()[0]
        self.assertIsInstance(sent["content"], list)
        self.assertEqual(sent["content"][0], {"type": "text", "text": "看看这个"})
        self.assertEqual(sent["content"][1]["type"], "text")
        self.assertIn("附件里的正文", sent["content"][1]["text"])
        # 非白名单字段（created_at）仍被剔除
        self.assertNotIn("created_at", sent)

    def test_expansion_does_not_write_back_to_history(self):
        item = self._stage("notes.txt", "正文")
        msg = self._ledger_message(item, "看看")
        history = [msg]
        agent = _stub_agent(history, self.ws, self.sid)

        agent._model_messages()
        agent._model_messages()

        self.assertIs(agent.history_messages[0], msg)
        self.assertEqual(agent.history_messages[0]["content"][1]["type"],
                         A.ATTACHMENT_BLOCK_TYPE)
        self.assertNotIn("base64", str(agent.history_messages))

    def test_ref_blocks_expand_at_send_boundary_and_stay_out_of_history(self):
        """引用（@-mention，2026-09-21）：jsonl 存中性路径块，请求体里变成说明文本块。"""
        target = self.src / "a.ts"
        target.write_text("export const a = 1", encoding="utf-8")
        ledger = {"role": "user", "content": R.attach_ref_blocks(
            "看看 @a.ts",
            [R.normalize_ref(self.ws.workdir, {"path": str(target)})])}
        agent = _stub_agent([ledger], self.ws, self.sid)

        sent = agent._model_messages()[0]
        self.assertEqual(sent["content"][0], {"type": "text", "text": "看看 @a.ts"})
        self.assertEqual(sent["content"][1]["type"], "text")
        self.assertIn(str(target), sent["content"][1]["text"])
        self.assertIn("run_read", sent["content"][1]["text"])
        # 不回写历史：账本形态里 ref 块还在，说明文本块没被写回
        self.assertTrue(R.history_has_refs(agent.history_messages))
        self.assertNotIn("run_read", str(agent.history_messages))

    def test_image_attachment_expands_to_image_url(self):
        from PIL import Image
        p = self.src / "shot.png"
        Image.new("RGB", (24, 24), (10, 10, 200)).save(p)
        result = A.stage(self.ws, [str(p)])
        item = result["items"][0]
        msg = self._ledger_message(item, "看图")
        agent = _stub_agent([msg], self.ws, self.sid)

        block = agent._model_messages()[0]["content"][1]
        self.assertEqual(block["type"], "image_url")
        self.assertTrue(block["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_missing_file_degrades_without_raising(self):
        """附件文件被手工删掉后追问：请求必须仍然拼得出来（降级为占位文本）。"""
        item = self._stage("notes.txt", "正文")
        msg = self._ledger_message(item, "看看")
        for leftover in (self.ws.attachments_dir / self.sid).glob(f"{item['att_id']}*"):
            leftover.unlink()
        A.clear_expand_cache()
        agent = _stub_agent([msg], self.ws, self.sid)

        sent = agent._model_messages()[0]          # 不抛即通过
        self.assertEqual(sent["content"][1]["type"], "text")
        self.assertIn("缺失", sent["content"][1]["text"])

    def test_mixed_history_only_expands_attachment_messages(self):
        item = self._stage("notes.txt", "正文")
        plain = {"role": "user", "content": "普通消息"}
        ledger = self._ledger_message(item, "带附件")
        history = [plain, ledger]
        agent = _stub_agent(history, self.ws, self.sid)

        out = agent._model_messages()
        self.assertEqual(out[0]["content"], "普通消息")     # 原样
        self.assertIsInstance(out[1]["content"], list)      # 已展开


class VisionCapabilityGateTests(unittest.TestCase):
    """图片能力门控必须落在发送边界。

    改前唯一的校验是 ws_bridge 的 chat 预检，且只扫**当轮新上传**的图片：
    会话中途换成 text-only 模型、或历史回放时，图片会被原样发给不支持图片的模型
    （provider 报错，或内容被静默忽略）。`_model_messages` 是全部路径的必经之处。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = WorkspacePaths("default", self.root, self.root, bash_cwd=self.root)
        self.sid = "SESS000003"
        A.clear_expand_cache()
        self.addCleanup(A.clear_expand_cache)
        self.src = self.root / "src"
        self.src.mkdir(parents=True, exist_ok=True)

    def _record(self, path: Path) -> dict:
        result = A.stage(self.ws, [str(path)])
        self.assertEqual(result["failed"], [])
        item = result["items"][0]
        return A.migrate_to_session(
            self.ws, [{"att_id": item["att_id"], "kind": item["kind"],
                       "name": item["name"], "ext": item["ext"]}], self.sid)

    def _image_agent(self, text: str = "看图") -> Agent:
        from PIL import Image
        p = self.src / "shot.png"
        Image.new("RGB", (20, 20), (0, 80, 160)).save(p)
        msg = {"role": "user", "content": A.build_user_content(text, self._record(p))}
        return _stub_agent([msg], self.ws, self.sid)

    def test_image_degrades_when_model_has_no_vision(self):
        agent = self._image_agent()
        with mock.patch.object(agent_full_v2, "model_supports_image",
                               lambda mid: False):
            sent = agent._model_messages()[0]
        types = [b["type"] for b in sent["content"]]
        self.assertNotIn("image_url", types)
        self.assertTrue(any("未发送" in b.get("text", "") for b in sent["content"]))

    def test_image_sent_when_model_has_vision(self):
        agent = self._image_agent()
        with mock.patch.object(agent_full_v2, "model_supports_image",
                               lambda mid: True):
            sent = agent._model_messages()[0]
        self.assertIn("image_url", [b["type"] for b in sent["content"]])

    def test_capability_lookup_failure_does_not_kill_turn(self):
        """能力查询抛异常 → 按"支持"放过。本方法在 retry lambda 内，
        异常穿透会打死整轮（本轮 tool_result 全缺）。"""
        agent = self._image_agent()

        def boom(mid):
            raise RuntimeError("配置读坏了")

        with mock.patch.object(agent_full_v2, "model_supports_image", boom):
            sent = agent._model_messages()[0]           # 不抛即通过
        self.assertIn("image_url", [b["type"] for b in sent["content"]])

    def test_documents_unaffected_by_image_capability(self):
        """文档走文本内联，与图片能力无关 —— text-only 模型完全可用。"""
        p = self.src / "notes.txt"
        p.write_text("文档正文", encoding="utf-8")
        msg = {"role": "user", "content": A.build_user_content("看看", self._record(p))}
        agent = _stub_agent([msg], self.ws, self.sid)
        with mock.patch.object(agent_full_v2, "model_supports_image",
                               lambda mid: False):
            sent = agent._model_messages()[0]
        self.assertEqual([b["type"] for b in sent["content"]], ["text", "text"])
        self.assertIn("文档正文", sent["content"][1]["text"])

    def test_no_attachment_history_never_consults_capability(self):
        """无附件时不该去查能力（也就不会因它出问题）—— 逐字节等价仍成立。"""
        history = [{"role": "user", "content": "纯文本"}]
        agent = _stub_agent(history, None, None)

        def boom(mid):
            raise AssertionError("无附件会话不应查询模型能力")

        with mock.patch.object(agent_full_v2, "model_supports_image", boom):
            out = agent._model_messages()
        self.assertEqual(out, _legacy_model_messages(history))


if __name__ == "__main__":
    unittest.main()
