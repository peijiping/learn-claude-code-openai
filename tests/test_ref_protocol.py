#!/usr/bin/env python3
"""「引用文件/文件夹」（@-mention）协议面的端到端守护测试 —— 2026-09-21。

用一个假 WebSocket 把 `ws_bridge.handle` 真跑起来（不是 mock 内部函数），逐条喂
命令、收信封，验证**用户可见的行为**：

  1. `refs_list`           —— 列出工作空间内容；忽略清单生效；截断标记
  2. default 空间 / 空间不可用 —— 回 `disabled` 而**不是 error**，且**不去遍历 scratch**
  3. `chat` 带 refs        —— `start_turn` 收到多模态数组，且**只有路径没有文件内容**
  4. `chat` 不带 refs      —— 收到的仍是纯字符串（存量行为逐字节不变）
  5. 越界 / 伪造字段       —— 越界条目就地丢弃但不阻断发送；name/is_dir 以磁盘为准
  6. 回放                  —— `_history_to_ui` 从 content 里 harvest 出引用列表

全部用例在临时工作空间里跑（注册表注入临时根），**不触碰真实 `~/.aigent`**。

运行：`.venv/bin/python -m unittest discover -s tests -v`
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import project_registry  # noqa: E402
import refs as R  # noqa: E402
import ws_bridge  # noqa: E402
from project_registry import WorkspaceRegistry  # noqa: E402
from session_runtime import SessionRuntime, SessionRuntimeRegistry  # noqa: E402


class _FakeWS:
    """最小可用的 ws 替身（与 test_attachment_protocol 同款）。"""

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


class _RefCommandTestCase(unittest.TestCase):
    """与 test_attachment_protocol 同构的夹具：临时注册表 + 临时工作空间。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 必须 resolve：macOS 上 /var 是指向 /private/var 的符号链接，而 refs 侧
        # 所有路径都经过 resolve() —— 不统一会在断言里蹦出 /private 前缀差异。
        self.root = Path(self._tmp.name).resolve()
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

        # 自定义工作空间（真实目录在 tmp 内，绝不动用户的仓库）
        self.real = self.root / "real-project"
        self.real.mkdir(parents=True, exist_ok=True)
        self.pid = self.reg.create(str(self.real)).id
        self.ws_paths = ws_bridge._workspace_of(self.pid)
        self.sm = ws_bridge._ensure_session_manager(self.pid)
        self.sid, _ = self.sm.create_new_session()
        ws_bridge._SID_PROJECT[self.sid] = self.pid

        self.rt_registry = SessionRuntimeRegistry(
            lambda *a, **k: None, lambda: None, lambda n: {}
        )
        ws_bridge.registry = self.rt_registry

        # turn 只记录实参，绝不真跑（会联网调 LLM）
        self.turn_calls: list = []
        self._orig_start_turn = SessionRuntime.start_turn

        async def _record(_self, text, reasoning_effort=None, max_context=None):
            self.turn_calls.append(text)

        SessionRuntime.start_turn = _record
        self.addCleanup(lambda: setattr(
            SessionRuntime, "start_turn", self._orig_start_turn))

        # 标题生成会发网络请求 → 短路
        self._orig_title = ws_bridge._generate_session_title
        ws_bridge._generate_session_title = lambda *a, **k: None
        self.addCleanup(lambda: setattr(
            ws_bridge, "_generate_session_title", self._orig_title))

    def _restore(self):
        ws_bridge._MANAGER_CACHE.clear()
        ws_bridge._MANAGER_CACHE.update(self._managers)
        ws_bridge._SID_PROJECT.clear()
        ws_bridge._SID_PROJECT.update(self._sid_project)
        ws_bridge.registry = self._orig_runtime_registry

    # ── 便捷动作 ──────────────────────────────────────────────────
    def _file(self, name: str, text: str = "content") -> Path:
        p = self.real / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def _refs_payload(self, **payload) -> dict:
        sent = drive([{"kind": "refs_list", "payload": payload}])
        envs = _of_kind(sent, "refs")
        self.assertTrue(envs, sent)
        return envs[-1]["payload"]

    def _ref(self, path, name: str | None = None, is_dir: bool | None = None) -> dict:
        item = {"path": str(path)}
        if name is not None:
            item["name"] = name
        if is_dir is not None:
            item["is_dir"] = is_dir
        return item

    def _chat(self, text: str, refs: list | None = None) -> list[dict]:
        payload: dict = {"session_id": self.sid, "text": text}
        if refs:
            payload["refs"] = refs
        return drive([{"kind": "chat", "payload": payload}])


# ══════════════════════════════════════════════════════════════════
class RefsListTests(_RefCommandTestCase):

    def test_lists_workspace_content(self):
        self._file("alpha.ts", "export const a = 1")
        self._file("nested/beta.md", "# b")
        payload = self._refs_payload(project_id=self.pid)
        self.assertEqual(payload["project_id"], self.pid)
        self.assertEqual(payload["workdir"], str(self.real))
        self.assertFalse(payload["disabled"])
        self.assertFalse(payload["truncated"])
        names = [i["name"] for i in payload["items"]]
        self.assertIn("alpha.ts", names)
        self.assertIn("nested", names)
        self.assertIn("beta.md", names)
        kinds = {i["name"]: i["type"] for i in payload["items"]}
        self.assertEqual(kinds["nested"], "dir")
        self.assertEqual(kinds["alpha.ts"], "file")

    def test_ignore_list_prunes_dependency_dirs(self):
        self._file("src/app.ts", "code")
        self._file("node_modules/pkg/index.js", "module.exports = 1")
        self._file(".git/config", "[core]")
        payload = self._refs_payload(project_id=self.pid)
        names = [i["name"] for i in payload["items"]]
        self.assertIn("app.ts", names)
        for pruned in ("node_modules", ".git"):
            self.assertNotIn(pruned, names)
        self.assertNotIn("index.js", names)

    def test_truncation_is_flagged(self):
        for i in range(8):
            self._file(f"f{i:02d}.txt", str(i))
        with mock.patch.dict("os.environ", {"REF_LIST_MAX_ENTRIES": "3"}):
            payload = self._refs_payload(project_id=self.pid)
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["items"]), 3)

    def test_resolves_by_session_id(self):
        """有 session_id 时按会话归属解析（与其它按 sid 的操作同口径）。"""
        self._file("via-session.txt")
        payload = self._refs_payload(session_id=self.sid)
        self.assertEqual(payload["project_id"], self.pid)
        self.assertIn("via-session.txt", [i["name"] for i in payload["items"]])

    def test_default_project_is_disabled_without_scanning(self):
        """default 空间：回 disabled（常规态，不是 error），且**不去遍历 scratch**。"""
        with mock.patch.object(ws_bridge, "list_ref_workspace") as listed:
            sent = drive([{"kind": "refs_list", "payload": {"project_id": "default"}}])
        listed.assert_not_called()
        self.assertEqual(_of_kind(sent, "error"), [])
        payload = _of_kind(sent, "refs")[-1]["payload"]
        self.assertTrue(payload["disabled"])
        self.assertEqual(payload["items"], [])
        self.assertIn("草稿目录", payload["reason"])

    def test_unavailable_workspace_is_disabled(self):
        with mock.patch.object(ws_bridge, "_project_ready", return_value=False):
            payload = self._refs_payload(project_id=self.pid)
        self.assertTrue(payload["disabled"])
        self.assertIn("不可用", payload["reason"])

    def test_listing_failure_sends_error_not_crash(self):
        with mock.patch.object(ws_bridge, "list_ref_workspace",
                               side_effect=RuntimeError("boom")):
            sent = drive([{"kind": "refs_list", "payload": {"project_id": self.pid}}])
        self.assertTrue(_of_kind(sent, "error"))
        self.assertEqual(_of_kind(sent, "refs"), [])


# ══════════════════════════════════════════════════════════════════
class ChatWithRefsTests(_RefCommandTestCase):

    def test_chat_without_refs_keeps_plain_string(self):
        """存量零变化：不带引用（也不带附件）时 start_turn 收到的是纯字符串。"""
        self._chat("普通消息")
        self.assertTrue(self.turn_calls)
        self.assertIsInstance(self.turn_calls[0], str)
        self.assertEqual(self.turn_calls[0], "普通消息")

    def test_chat_with_refs_becomes_block_array(self):
        target = self._file("alpha.ts", "export const a = 1")
        self._chat("看看 @alpha.ts", [self._ref(target)])
        self.assertTrue(self.turn_calls)
        content = self.turn_calls[0]
        self.assertIsInstance(content, list)
        types = [b.get("type") for b in content]
        self.assertIn("text", types)
        self.assertIn(R.REF_BLOCK_TYPE, types)
        block = next(b for b in content if b.get("type") == R.REF_BLOCK_TYPE)
        self.assertEqual(block["ref"]["path"], str(target))
        self.assertEqual(block["ref"]["name"], "alpha.ts")
        self.assertFalse(block["ref"]["is_dir"])

    def test_reference_only_message_is_not_dropped(self):
        """正文为空、只有引用 —— 不能被静默丢弃（附件曾踩过同一个坑）。"""
        target = self._file("alpha.ts")
        self._chat("", [self._ref(target)])
        self.assertTrue(self.turn_calls)
        self.assertIsInstance(self.turn_calls[0], list)

    def test_directory_can_be_referenced(self):
        folder = self.real / "components"
        folder.mkdir()
        self._chat("看目录", [self._ref(folder)])
        block = next(b for b in self.turn_calls[0]
                     if b.get("type") == R.REF_BLOCK_TYPE)
        self.assertTrue(block["ref"]["is_dir"])

    def test_name_and_is_dir_are_taken_from_disk(self):
        """前端伪造的 name / is_dir 必须被磁盘事实覆盖。"""
        target = self._file("real.md", "# real")
        self._chat("看", [{"path": str(target), "name": "伪造.exe", "is_dir": True}])
        block = next(b for b in self.turn_calls[0]
                     if b.get("type") == R.REF_BLOCK_TYPE)
        self.assertEqual(block["ref"]["name"], "real.md")
        self.assertFalse(block["ref"]["is_dir"])

    def test_out_of_workspace_path_is_dropped_without_blocking(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        self._chat("偷看", [self._ref(outside / "secret.txt"),
                          self._ref("/etc/passwd")])
        # 越界条目全被丢弃 → content 退回纯字符串，且 turn 照常派发
        self.assertTrue(self.turn_calls)
        self.assertIsInstance(self.turn_calls[0], str)

    def test_mixed_valid_and_invalid_keeps_the_valid_one(self):
        good = self._file("good.ts")
        self._chat("看", [self._ref("/etc/passwd"), self._ref(good)])
        names = [b["ref"]["name"] for b in self.turn_calls[0]
                 if b.get("type") == R.REF_BLOCK_TYPE]
        self.assertEqual(names, ["good.ts"])

    def test_duplicate_paths_are_deduped(self):
        good = self._file("dup.ts")
        self._chat("看", [self._ref(good), self._ref(good)])
        blocks = [b for b in self.turn_calls[0]
                  if b.get("type") == R.REF_BLOCK_TYPE]
        self.assertEqual(len(blocks), 1)

    def test_ledger_block_carries_no_file_content(self):
        """账本形态只有路径与元数据 —— 磁盘上永远不出现文件内容或厂商线格式。"""
        self._file("secret.txt", "TOP-SECRET-CONTENT")
        self._chat("看", [self._ref(self.real / "secret.txt")])
        block = next(b for b in self.turn_calls[0]
                     if b.get("type") == R.REF_BLOCK_TYPE)
        self.assertEqual(set(block["ref"]), {"path", "name", "is_dir", "project_id"})
        self.assertNotIn("TOP-SECRET-CONTENT", json.dumps(block, ensure_ascii=False))


# ══════════════════════════════════════════════════════════════════
class ReplayTests(_RefCommandTestCase):

    def _append_user(self, text: str, refs: list[dict] | None = None) -> None:
        content: object = R.attach_ref_blocks(
            text, [R.normalize_ref(self.real, r) for r in (refs or [])])
        self.sm.append_message_to_session(
            self.sm.get_session_file(self.sid), {"role": "user", "content": content})

    def _ui_user_messages(self) -> list[dict]:
        history = self.sm.load_session_history(self.sm.get_session_file(self.sid))
        return [m for m in ws_bridge._history_to_ui(history) if m["role"] == "user"]

    def test_history_to_ui_harvests_refs(self):
        self._file("alpha.ts", "x")
        self._append_user("看看 @alpha.ts", [self._ref(self.real / "alpha.ts")])
        user_msg = self._ui_user_messages()[-1]
        self.assertEqual(user_msg["content"], "看看 @alpha.ts")
        self.assertEqual(user_msg["refs"], [{
            "path": str(self.real / "alpha.ts"), "name": "alpha.ts", "is_dir": False}])

    def test_history_to_ui_without_refs_has_no_field(self):
        self._append_user("普通消息")
        user_msg = self._ui_user_messages()[-1]
        self.assertNotIn("refs", user_msg, "无引用消息不应多出字段")

    def test_reference_only_message_replays_with_empty_text(self):
        self._file("alpha.ts")
        self._append_user("", [self._ref(self.real / "alpha.ts")])
        user_msg = self._ui_user_messages()[-1]
        self.assertEqual(user_msg["content"], "")
        self.assertEqual(len(user_msg["refs"]), 1)

    def test_expand_after_replay_keeps_history_as_ledger(self):
        """回放/展开之后，磁盘上的消息仍是账本形态（ref 块在、无展开残留）。"""
        self._file("alpha.ts")
        self._append_user("看看", [self._ref(self.real / "alpha.ts")])
        history = self.sm.load_session_history(self.sm.get_session_file(self.sid))
        user_msg = [m for m in history if m["role"] == "user"][-1]
        expanded = R.expand_ref_blocks_for_model(user_msg)
        self.assertIsInstance(expanded["content"], list)
        self.assertTrue(any(b.get("type") == "text" and "run_read" in b.get("text", "")
                            for b in expanded["content"]))
        # 原对象未被改写
        self.assertTrue(R.history_has_refs([user_msg]))


if __name__ == "__main__":
    unittest.main()
