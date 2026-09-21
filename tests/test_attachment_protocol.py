#!/usr/bin/env python3
"""附件协议面的端到端守护测试 —— 2026-09-20。

用一个假 WebSocket 把 `ws_bridge.handle` 真跑起来（不是 mock 内部函数），
逐条喂命令、收信封，验证**用户可见的行为**：

  1. `attachment_stage`  —— 登记成功/部分失败/坏空间各自回什么；文件落到哪
  2. `chat` 带附件        —— `start_turn` 收到的是多模态数组；草稿已归位；
                            **纯附件无正文也不能被丢弃**（历史 bug 是静默丢消息）
  3. 图片 + text-only 模型 —— 必须回可读的 error 且**不派发 turn**；
                            未知模型一律放过（本地目录没收录不等于不能用）
  4. `chat` 不带附件       —— 收到的是纯字符串（存量行为零变化）
  5. 回放                 —— `_history_to_ui` 从 content 里 harvest 出附件
  6. 级联清理             —— 清空/永久删除删附件目录；归档（trash）**不删**
  7. 启动 GC 接线          —— `_startup_attachment_gc` 会遍历工作空间并清理

全部用例在临时工作空间里跑（注册表注入临时根），**不触碰真实 `~/.aigent`**。

运行：`.venv/bin/python -m unittest discover -s tests -v`
"""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import attachments as A  # noqa: E402
import project_registry  # noqa: E402
import ws_bridge  # noqa: E402
from project_registry import WorkspaceRegistry  # noqa: E402
from session_runtime import SessionRuntime, SessionRuntimeRegistry  # noqa: E402


class _FakeWS:
    """最小可用的 ws 替身（与 test_workspace_commands 同款）。"""

    FLUSH_WAIT = 0.12

    def __init__(self, commands: list[dict]):
        self._lines = [json.dumps(c) for c in commands]
        self.sent: list[dict] = []
        self.remote_address = ("127.0.0.1", 45678)

    async def send(self, line: str) -> None:
        self.sent.append(json.loads(line))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line
            await asyncio.sleep(self.FLUSH_WAIT)
        await asyncio.sleep(self.FLUSH_WAIT)


def drive(commands: list[dict], timeout: float = 20.0) -> list[dict]:
    async def _run():
        ws = _FakeWS(commands)
        await asyncio.wait_for(ws_bridge.handle(ws), timeout=timeout)
        return ws.sent

    return asyncio.run(_run())


def _of_kind(sent: list[dict], kind: str) -> list[dict]:
    return [e for e in sent if e.get("kind") == kind]


class _AttachmentCommandTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reg = WorkspaceRegistry(
            index_path=self.root / "projects.json", projects_root=self.root
        )
        self._orig_registry = project_registry.get_registry()
        project_registry.set_registry(self.reg)
        self.addCleanup(project_registry.set_registry, self._orig_registry)

        self._managers = dict(ws_bridge._MANAGER_CACHE)
        self._sid_project = dict(ws_bridge._SID_PROJECT)
        self._orig_runtime_registry = ws_bridge.registry
        self.addCleanup(self._restore)

        # 自定义工作空间（注册表注入临时根 → 元数据目录也在 tmp，不碰真实 ~/.aigent）
        self.real = self.root / "real-project"
        self.real.mkdir(parents=True, exist_ok=True)
        self.pid = self.reg.create(str(self.real)).id
        self.ws_paths = ws_bridge._workspace_of(self.pid)
        self.sm = ws_bridge._ensure_session_manager(self.pid)
        self.sid, _ = self.sm.create_new_session()
        ws_bridge._SID_PROJECT[self.sid] = self.pid

        # 真实运行时注册表（生产里在 serve 之前建好）
        self.rt_registry = SessionRuntimeRegistry(
            lambda *a, **k: None, lambda: None, lambda n: {}
        )
        ws_bridge.registry = self.rt_registry

        # turn 只用记录实参，绝不真的跑（会联网调 LLM）
        self.turn_calls: list = []
        self._orig_start_turn = SessionRuntime.start_turn

        async def _record(_self, text, reasoning_effort=None, max_context=None):
            self.turn_calls.append(text)

        SessionRuntime.start_turn = _record
        self.addCleanup(lambda: setattr(
            SessionRuntime, "start_turn", self._orig_start_turn))

        # 标题生成会发网络请求 → 直接短路（标题本身不在本文件关注范围）
        self._orig_title = ws_bridge._generate_session_title
        ws_bridge._generate_session_title = lambda *a, **k: None
        self.addCleanup(lambda: setattr(
            ws_bridge, "_generate_session_title", self._orig_title))
        A.clear_expand_cache()
        self.addCleanup(A.clear_expand_cache)

    def _restore(self):
        ws_bridge._MANAGER_CACHE.clear()
        ws_bridge._MANAGER_CACHE.update(self._managers)
        ws_bridge._SID_PROJECT.clear()
        ws_bridge._SID_PROJECT.update(self._sid_project)
        ws_bridge.registry = self._orig_runtime_registry

    # ── 便捷动作 ──────────────────────────────────────────────────
    def _stage(self, name: str, text: str = "正文", binary: bytes | None = None) -> dict:
        p = self.real / name
        if binary is not None:
            p.write_bytes(binary)
        else:
            p.write_text(text, encoding="utf-8")
        sent = drive([{"kind": "attachment_stage",
                       "payload": {"paths": [str(p)], "project_id": self.pid}}])
        envs = _of_kind(sent, "attachments_staged")
        self.assertTrue(envs, sent)
        self.assertEqual(envs[-1]["payload"]["failed"], [])
        return envs[-1]["payload"]["items"][0]

    def _png(self, name: str = "shot.png") -> dict:
        from PIL import Image
        p = self.real / name
        Image.new("RGB", (24, 24), (30, 30, 200)).save(p)
        sent = drive([{"kind": "attachment_stage",
                       "payload": {"paths": [str(p)], "project_id": self.pid}}])
        envs = _of_kind(sent, "attachments_staged")
        self.assertEqual(envs[-1]["payload"]["failed"], [])
        return envs[-1]["payload"]["items"][0]

    @staticmethod
    def _ref(item: dict) -> dict:
        return {"att_id": item["att_id"], "kind": item["kind"],
                "name": item["name"], "mime": item["mime"],
                "ext": item["ext"], "size": item["size"],
                "project_id": item["project_id"]}

    def _chat(self, text: str, attachments: list | None = None,
              model_id: str | None = None) -> list[dict]:
        payload: dict = {"session_id": self.sid, "text": text}
        if attachments:
            payload["attachments"] = attachments
        if model_id:
            payload["model_id"] = model_id
        return drive([{"kind": "chat", "payload": payload}])


# ══════════════════════════════════════════════════════════════════
class StageCommandTests(_AttachmentCommandTestCase):
    def test_stage_writes_draft_under_workspace(self):
        item = self._stage("note.txt", "附件正文")
        self.assertEqual(item["kind"], "text")
        draft = self.ws_paths.attachments_dir / "_draft" / item["att_id"]
        self.assertTrue((draft / "meta.json").is_file())
        self.assertTrue((draft / f"{item['att_id']}.txt").is_file())

    def test_stage_reports_partial_failure(self):
        good = self.real / "ok.txt"
        bad = self.real / "old.doc"
        good.write_text("fine", encoding="utf-8")
        bad.write_text("x", encoding="utf-8")
        sent = drive([{"kind": "attachment_stage",
                       "payload": {"paths": [str(bad), str(good)],
                                   "project_id": self.pid}}])
        payload = _of_kind(sent, "attachments_staged")[-1]["payload"]
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(len(payload["failed"]), 1)
        self.assertIn(".docx", payload["failed"][0]["reason"])

    def test_stage_without_paths_returns_empty_ok(self):
        sent = drive([{"kind": "attachment_stage",
                       "payload": {"paths": [], "project_id": self.pid}}])
        payload = _of_kind(sent, "attachments_staged")[-1]["payload"]
        self.assertEqual((payload["items"], payload["failed"]), ([], []))

    def test_stage_on_unavailable_workspace_errors(self):
        sent = drive([{"kind": "attachment_stage",
                       "payload": {"paths": ["/tmp/x.png"],
                                   "project_id": "wsNotExist1"}}])
        msgs = [e["payload"]["msg"] for e in _of_kind(sent, "error")]
        self.assertTrue(msgs, sent)


# ══════════════════════════════════════════════════════════════════
class ChatWithAttachmentsTests(_AttachmentCommandTestCase):
    def test_chat_without_attachments_passes_plain_str(self):
        """存量行为零变化：不带附件时 start_turn 收到的是字符串。"""
        sent = self._chat("普通消息")
        self.assertEqual(_of_kind(sent, "error"), [])
        self.assertEqual(self.turn_calls, ["普通消息"])
        self.assertIsInstance(self.turn_calls[0], str)

    def test_chat_with_attachment_builds_multimodal_content(self):
        item = self._stage("note.txt", "附件里的正文")
        sent = self._chat("看看这个", [self._ref(item)])
        self.assertEqual(_of_kind(sent, "error"), [])
        self.assertEqual(len(self.turn_calls), 1)
        content = self.turn_calls[0]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "看看这个"})
        self.assertEqual(content[1]["type"], A.ATTACHMENT_BLOCK_TYPE)
        self.assertEqual(content[1]["attachment"]["id"], item["att_id"])
        # 账本里绝不能出现文件字节
        self.assertNotIn("base64", json.dumps(content, ensure_ascii=False))

    def test_chat_migrates_draft_to_session_dir(self):
        item = self._stage("note.txt", "正文")
        self._chat("看看", [self._ref(item)])
        draft = self.ws_paths.attachments_dir / "_draft" / item["att_id"]
        session_dir = self.ws_paths.attachments_dir / self.sid
        self.assertFalse(draft.exists(), "草稿必须在派发前归位")
        self.assertTrue((session_dir / f"{item['att_id']}.txt").is_file())

    def test_chat_with_attachments_only_is_not_dropped(self):
        """纯附件（正文为空）：不能被丢弃，也不能出现空文本块。"""
        item = self._stage("note.txt", "正文")
        sent = self._chat("", [self._ref(item)])
        self.assertEqual(_of_kind(sent, "error"), [])
        self.assertEqual(len(self.turn_calls), 1)
        content = self.turn_calls[0]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], A.ATTACHMENT_BLOCK_TYPE)

    def test_image_with_text_only_model_is_rejected_before_migration(self):
        item = self._png()
        with mock.patch.object(ws_bridge, "get_model_by_id",
                               lambda mid: {"capabilities": {"input": ["text"]}}):
            sent = self._chat("看图", [self._ref(item)], model_id="text-only-x")
        msgs = [e["payload"]["msg"] for e in _of_kind(sent, "error")]
        self.assertTrue(msgs and "不支持图片" in msgs[0], sent)
        self.assertEqual(self.turn_calls, [], "被拒时不得派发 turn")
        draft = self.ws_paths.attachments_dir / "_draft" / item["att_id"]
        self.assertTrue(draft.exists(), "拒绝发生在迁移之前，草稿应原样保留")

    def test_image_with_unknown_model_is_allowed(self):
        item = self._png()
        with mock.patch.object(ws_bridge, "get_model_by_id", lambda mid: None):
            sent = self._chat("看图", [self._ref(item)], model_id="brand-new-model")
        self.assertEqual(_of_kind(sent, "error"), [])
        self.assertEqual(len(self.turn_calls), 1)

    def test_document_attachment_not_blocked_by_text_only_model(self):
        """只有图片才受能力限制：文档走文本内联，text-only 模型完全可用。"""
        item = self._stage("note.txt", "正文")
        with mock.patch.object(ws_bridge, "get_model_by_id",
                               lambda mid: {"capabilities": {"input": ["text"]}}):
            sent = self._chat("看看", [self._ref(item)], model_id="text-only-x")
        self.assertEqual(_of_kind(sent, "error"), [])
        self.assertEqual(len(self.turn_calls), 1)

    def test_missing_attachment_file_does_not_block_turn(self):
        self._chat("随便")  # 先确保会话可跑
        self.turn_calls.clear()
        sent = self._chat("看看", [{"att_id": "att_deadbeef00", "kind": "text",
                                    "name": "gone.txt", "ext": ".txt"}])
        self.assertEqual(_of_kind(sent, "error"), [])
        self.assertEqual(len(self.turn_calls), 1)
        self.assertEqual(self.turn_calls[0][1]["attachment"]["missing"], True)


# ══════════════════════════════════════════════════════════════════
class CapabilityHelperTests(unittest.TestCase):
    def test_three_state_capability_check(self):
        with mock.patch.object(ws_bridge, "get_model_by_id",
                               lambda mid: {"capabilities": {"input": ["text", "image"]}}):
            self.assertTrue(ws_bridge._model_supports_image("vision-model"))
        with mock.patch.object(ws_bridge, "get_model_by_id",
                               lambda mid: {"capabilities": {"input": ["text"]}}):
            self.assertFalse(ws_bridge._model_supports_image("text-only"))
        with mock.patch.object(ws_bridge, "get_model_by_id", lambda mid: None):
            self.assertTrue(ws_bridge._model_supports_image("unknown"))
            self.assertTrue(ws_bridge._model_supports_image(None))
        with mock.patch.object(ws_bridge, "get_model_by_id",
                               lambda mid: {"capabilities": {}}):
            self.assertTrue(ws_bridge._model_supports_image("no-input-list"))

    def test_capability_lookup_failure_is_treated_as_supported(self):
        def _boom(_mid):
            raise RuntimeError("config broken")

        with mock.patch.object(ws_bridge, "get_model_by_id", _boom):
            self.assertTrue(ws_bridge._model_supports_image("x"))

    def test_title_hint_uses_first_attachment_name(self):
        self.assertEqual(ws_bridge._attachment_title_hint({}), "")
        self.assertEqual(
            ws_bridge._attachment_title_hint(
                {"attachments": [{"name": "季度报告.pdf"}]}),
            "[附件] 季度报告.pdf")


# ══════════════════════════════════════════════════════════════════
class ReplayHarvestTests(_AttachmentCommandTestCase):
    """回放：jsonl 里的中性引用块要能被 harvest 成前端可渲染的附件列表。

    这里**不走 chat**（本文件把 `start_turn` 短路了，turn 不会真跑、user 行不会
    落盘），而是用生产的落盘/读取两个函数自己走一遍往返 —— 这恰好覆盖了最关键的
    一条断言：**附件元数据放在 content 块里能穿过 append→load，而放消息兄弟字段
    会被 `load_session_history` 丢掉**。
    """

    def _append_user(self, text: str, refs: list) -> None:
        records = A.migrate_to_session(self.ws_paths, refs, self.sid)
        self.sm.append_message_to_session(
            self.sm.get_session_file(self.sid),
            {"role": "user", "content": A.build_user_content(text, records)},
        )

    def _ui_user_messages(self) -> list[dict]:
        history = self.sm.load_session_history(self.sm.get_session_file(self.sid))
        return [m for m in ws_bridge._history_to_ui(history) if m["role"] == "user"]

    def test_history_to_ui_harvests_attachments(self):
        item = self._stage("note.txt", "正文")
        self._append_user("看看", [self._ref(item)])

        user_msgs = self._ui_user_messages()
        self.assertTrue(user_msgs)
        atts = user_msgs[-1].get("attachments")
        self.assertTrue(atts, user_msgs[-1])
        self.assertEqual(atts[0]["id"], item["att_id"])
        self.assertEqual(atts[0]["name"], "note.txt")
        self.assertEqual(atts[0]["kind"], "text")
        # content 仍是纯文本视图（附件块不进 UI 正文）
        self.assertEqual(user_msgs[-1]["content"], "看看")

    def test_history_to_ui_without_attachments_has_no_field(self):
        self.sm.append_message_to_session(
            self.sm.get_session_file(self.sid),
            {"role": "user", "content": "普通消息"},
        )
        user_msgs = self._ui_user_messages()
        self.assertTrue(user_msgs)
        self.assertEqual(user_msgs[-1]["content"], "普通消息")
        self.assertNotIn("attachments", user_msgs[-1], "无附件消息不应多出字段")

    def test_attachment_only_message_replays_with_empty_text(self):
        item = self._stage("note.txt", "正文")
        self._append_user("", [self._ref(item)])
        user_msgs = self._ui_user_messages()
        self.assertEqual(user_msgs[-1]["content"], "")
        self.assertEqual(len(user_msgs[-1]["attachments"]), 1)


# ══════════════════════════════════════════════════════════════════
class LifecycleCascadeTests(_AttachmentCommandTestCase):
    def _session_dir(self) -> Path:
        return self.ws_paths.attachments_dir / self.sid

    def test_clear_session_removes_attachments(self):
        self._chat("看看", [self._ref(self._stage("a.txt"))])
        self.assertTrue(self._session_dir().is_dir())
        drive([{"kind": "session_clear", "payload": {"session_id": self.sid}}])
        self.assertFalse(self._session_dir().exists())

    def test_trash_keeps_attachments(self):
        """归档是软删除：还原后消息里的附件必须还在。"""
        self._chat("看看", [self._ref(self._stage("a.txt"))])
        drive([{"kind": "session_trash", "payload": {"session_id": self.sid}}])
        self.assertTrue(self._session_dir().is_dir())

    def test_permanent_delete_removes_attachments(self):
        self._chat("看看", [self._ref(self._stage("a.txt"))])
        drive([{"kind": "session_trash", "payload": {"session_id": self.sid}}])
        try:
            drive([{"kind": "session_delete", "payload": {"ids": [self.sid]}}])
        finally:
            # 生产里运行时会随会话下线；测试里手动摘掉，避免影响后续用例
            self.rt_registry.remove(self.sid)
        self.assertFalse(self._session_dir().exists())

    def test_restore_keeps_attachments(self):
        self._chat("看看", [self._ref(self._stage("a.txt"))])
        drive([{"kind": "session_trash", "payload": {"session_id": self.sid}}])
        drive([{"kind": "session_restore", "payload": {"session_id": self.sid}}])
        self.assertTrue(self._session_dir().is_dir())


# ══════════════════════════════════════════════════════════════════
class StartupGCTests(_AttachmentCommandTestCase):
    def test_startup_gc_iterates_workspaces_and_cleans(self):
        orphan = self.ws_paths.attachments_dir / "GONE000001"
        orphan.mkdir(parents=True)
        expired_draft = self.ws_paths.attachments_dir / "_draft" / "att_old0000001"
        expired_draft.mkdir(parents=True)
        (expired_draft / "meta.json").write_text("{}", encoding="utf-8")
        old = time.time() - 5 * 24 * 3600
        for p in (orphan, expired_draft):
            import os
            os.utime(p, (old, old))

        with mock.patch.object(ws_bridge, "_all_workspaces",
                               lambda: [self.ws_paths]):
            ws_bridge._startup_attachment_gc()

        self.assertFalse(orphan.exists())
        self.assertFalse(expired_draft.exists())

    def test_all_workspaces_includes_default_and_custom(self):
        ids = {ws.id for ws in ws_bridge._all_workspaces()}
        self.assertIn("default", ids)
        self.assertIn(self.pid, ids)


if __name__ == "__main__":
    unittest.main()
