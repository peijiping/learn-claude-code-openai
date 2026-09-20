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

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import attachments as A  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
