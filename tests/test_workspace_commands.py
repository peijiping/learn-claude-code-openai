#!/usr/bin/env python3
"""工作空间命令面的端到端守护测试 —— 2026-09-18。

这里用一个假 WebSocket 把 `ws_bridge.handle` 真跑起来（不是 mock 内部函数），
逐条喂命令、收信封，验证**用户可见的行为**：

- `projects_list` / 连接建立时的重放：default 恒第一，其余带 exists
- `project_add`：落索引 + 建同构元数据目录 + 广播 projects 与 sessions；
  坏路径（不存在 / 指向 ~/.aigent）必须回**可读的 error**，不是静默失败
- `project_open` / `project_rename`：active 与名称变更广播；default 拒绝改
- `project_remove`：条目与元数据目录消失，**真实目录必须保留**；
  default 拒绝；该空间有会话在跑时拒绝
- `trash_list`：跨空间汇总回收站

为什么不 mock：这批分支的价值全在"命令 → 落盘 → 广播"这条链上，
只测注册表（见 test_workspace_registry）覆盖不到接线错误。
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import project_registry  # noqa: E402
import ws_bridge  # noqa: E402
from config import AIGENT_HOME  # noqa: E402
from paths import WORKSPACE_SUBDIRS  # noqa: E402
from project_registry import WorkspaceRegistry  # noqa: E402


class _FakeWS:
    """最小可用的 ws 替身：能 send、能被 async for 迭代。

    迭代器在每条命令之后停一下 —— `handle` 正常退出时会 `finally` 注销连接并
    cancel writer（生产语义：客户端断开就不再往外发），所以必须在**连接仍在线**
    期间让 writer 把信封 flush 出去，否则一条都收不到。
    """

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
        await asyncio.sleep(self.FLUSH_WAIT)  # 空命令场景也要收连接建立时的重放


def drive(commands: list[dict], timeout: float = 20.0) -> list[dict]:
    """跑一遍 handle()，返回收到的全部信封（含连接建立时的 projects 重放）。"""

    async def _run():
        ws = _FakeWS(commands)
        await asyncio.wait_for(ws_bridge.handle(ws), timeout=timeout)
        return ws.sent

    return asyncio.run(_run())


def _envelopes_after_replay(sent: list[dict]) -> list[dict]:
    """去掉连接建立时的重放（projects），只留命令产生的信封。"""
    out = []
    for e in sent:
        if e.get("kind") == "projects" and not out:
            continue
        out.append(e)
    return out


class _CommandTestCase(unittest.TestCase):
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

        self.real = self.root / "real-project"
        self.real.mkdir(parents=True, exist_ok=True)

        # 模拟 main() 的服务器状态：handle() 读全局 registry 做状态重放。
        # 生产里这张表在 serve 之前就建好了，测试必须补上（否则不是"生产等价"）。
        from session_runtime import SessionRuntimeRegistry

        self.rt_registry = SessionRuntimeRegistry(
            lambda *a, **k: None, lambda: None, lambda n: {}
        )
        ws_bridge.registry = self.rt_registry

    def _restore(self):
        ws_bridge._MANAGER_CACHE.clear()
        ws_bridge._MANAGER_CACHE.update(self._managers)
        ws_bridge._SID_PROJECT.clear()
        ws_bridge._SID_PROJECT.update(self._sid_project)
        ws_bridge.registry = self._orig_runtime_registry

    def _projects(self, sent: list[dict]) -> dict:
        for e in reversed(sent):
            if e.get("kind") == "projects":
                return e["payload"]
        self.fail("没有收到 projects 信封")

    def _errors(self, sent: list[dict]) -> list[str]:
        return [e["payload"]["msg"] for e in sent if e.get("kind") == "error"]


class ProjectCommandTests(_CommandTestCase):
    def test_replay_on_connect_sends_projects_first(self):
        sent = drive([])
        self.assertEqual(sent[0]["kind"], "projects")
        payload = sent[0]["payload"]
        self.assertEqual([p["id"] for p in payload["projects"]], ["default"])
        self.assertTrue(payload["projects"][0]["system"])

    def test_add_creates_meta_dir_and_broadcasts(self):
        sent = drive([
            {"kind": "projects_list"},
            {"kind": "project_add", "payload": {"path": str(self.real)}},
        ])
        payload = self._projects(sent)
        ids = [p["id"] for p in payload["projects"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], "default")
        pid = ids[1]
        self.assertTrue(pid.startswith("ws"))
        # 元数据目录与同构子目录
        for name in WORKSPACE_SUBDIRS:
            self.assertTrue((self.root / pid / name).is_dir(), name)
        # 新增后同时刷新会话列表（新空间还没有会话，但要保证前端拿到列表结构）
        self.assertIn("sessions", [e["kind"] for e in sent])
        self.assertEqual(payload["active"], pid)
        # 真实目录原样
        self.assertTrue(self.real.is_dir())

    def test_add_rejects_missing_path_with_readable_error(self):
        sent = drive([
            {"kind": "project_add", "payload": {"path": str(self.root / "nope")}},
        ])
        msgs = self._errors(sent)
        self.assertTrue(msgs and "目录不存在" in msgs[0], msgs)
        self.assertEqual(len(self._projects(sent)["projects"]), 1)

    def test_add_rejects_aigent_home(self):
        sent = drive([
            {"kind": "project_add", "payload": {"path": str(AIGENT_HOME)}},
        ])
        msgs = self._errors(sent)
        self.assertTrue(msgs and "~/.aigent" in msgs[0], msgs)

    def test_open_sets_active(self):
        info = self.reg.create(str(self.real))
        sent = drive([
            {"kind": "project_open", "payload": {"project_id": "default"}},
        ])
        self.assertEqual(self._projects(sent)["active"], "default")
        # 再切回去
        sent = drive([
            {"kind": "project_open", "payload": {"project_id": info.id}},
        ])
        self.assertEqual(self._projects(sent)["active"], info.id)

    def test_open_unknown_project_errors(self):
        sent = drive([
            {"kind": "project_open", "payload": {"project_id": "wsNOPE"}},
        ])
        self.assertTrue(self._errors(sent))

    def test_rename_and_default_guard(self):
        info = self.reg.create(str(self.real))
        sent = drive([
            {"kind": "project_rename",
             "payload": {"project_id": info.id, "name": "新名字"}},
        ])
        names = {p["id"]: p["name"] for p in self._projects(sent)["projects"]}
        self.assertEqual(names[info.id], "新名字")
        # default 不可改名
        sent = drive([
            {"kind": "project_rename",
             "payload": {"project_id": "default", "name": "随便"}},
        ])
        self.assertTrue(any("默认工作空间" in m for m in self._errors(sent)))

    def test_remove_deletes_entry_and_meta_dir_but_keeps_real_dir(self):
        info = self.reg.create(str(self.real))
        (self.root / info.id / ".chathistory" / "session_x.jsonl").write_text(
            "{}", encoding="utf-8")
        sent = drive([
            {"kind": "project_remove", "payload": {"project_id": info.id}},
        ])
        self.assertEqual([p["id"] for p in self._projects(sent)["projects"]], ["default"])
        self.assertFalse((self.root / info.id).exists())
        self.assertTrue(self.real.is_dir(), "用户真实目录必须保留")
        self.assertIn("sessions", [e["kind"] for e in sent])

    def test_remove_default_rejected(self):
        sent = drive([
            {"kind": "project_remove", "payload": {"project_id": "default"}},
        ])
        self.assertTrue(any("不可删除" in m for m in self._errors(sent)))

    def test_remove_blocked_while_session_running(self):
        info = self.reg.create(str(self.real))
        rt = self.rt_registry.get_or_create(
            "BUSYSESS01", workspace=ws_bridge._workspace_of(info.id))
        rt.busy = True

        sent = drive([
            {"kind": "project_remove", "payload": {"project_id": info.id}},
        ])
        self.assertTrue(any("正在执行" in m for m in self._errors(sent)), sent)
        self.assertTrue((self.root / info.id).is_dir(), "被拒绝时不能删任何东西")
        self.assertIsNotNone(self.reg.get(info.id))

    def test_trash_list_aggregates_spaces(self):
        info = self.reg.create(str(self.real))
        sm = ws_bridge._ensure_session_manager(info.id)
        sid, _ = sm.create_new_session()
        sm.trash_session(sid)
        sent = drive([{"kind": "trash_list"}])
        trashed = [e for e in sent if e.get("kind") == "sessions_trashed"]
        self.assertTrue(trashed)
        rows = {s["id"]: s for s in trashed[-1]["payload"]["sessions"]}
        self.assertIn(sid, rows)
        self.assertEqual(rows[sid]["project"], info.id)


if __name__ == "__main__":
    unittest.main()
