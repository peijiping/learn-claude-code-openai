#!/usr/bin/env python3
"""会话生命周期 ↔ 任务板文件的级联清理守护测试（2026-09-16 两轮改造）。

约定（需求：task 不跨会话，会话被删除时任务文件同时删除）：

- **永久删除**会话 → 该会话的任务文件消失
- **清空**会话 → 任务文件消失，但会话文件保留
- **回收站**（trash / restore）→ 一律不动任务文件（还原后任务还在）
- **口径必须落在 SessionManager 这一层**：ws_bridge 的 session_clear 分支是
  直接调 `sm.clear_session` 的，不经过 `Agent.clear_session` —— 只做在 Agent
  那层会漏掉该路径（本文件正是这条的守门测试）。

存储布局：一个会话一个 JSON（`.tasks/<scope>.json`，文件内以组为 key）。
「paths 的路径口径」与「task_manager 的读写口径」必须同源 ——
`NamingContractTests` 用**真实的 TaskManager 写入**来验证，而不是复述字符串。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import paths  # noqa: E402
from paths import task_files_for_session  # noqa: E402
from session_manage import SessionManager  # noqa: E402
from task_manager import TaskManager  # noqa: E402


class _CascadeTestCase(unittest.TestCase):
    """公共夹具：临时 chat history 目录 + 临时 TASKS_DIR。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.tasks_dir = self.dir / ".tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        # task_files_for_session 在调用时从 paths 模块取全局 TASKS_DIR，
        # 因此临时改写即可（无需重载模块）。
        self._orig_tasks_dir = paths.TASKS_DIR
        paths.TASKS_DIR = self.tasks_dir
        self.addCleanup(setattr, paths, "TASKS_DIR", self._orig_tasks_dir)

        self.sm = SessionManager(self.dir, "sys")

    def make_session(self) -> str:
        sid, _ = self.sm.create_new_session()
        return sid

    def task_file(self, sid: str) -> Path:
        """该会话的任务文件（唯一一个）。"""
        return self.tasks_dir / f"{self.sm.session_prefix}{sid}.json"

    def seed_tasks(self, sid: str, count: int = 2) -> Path:
        """播下该会话的任务文件：一个文件、`count` 个组（组为 key）。"""
        scope = f"{self.sm.session_prefix}{sid}"
        groups = {
            f"g_seed_{i}": [{
                "id": f"t_17000000{i:02d}_{i:04d}",
                "subject": f"任务{i}", "description": "x", "status": "pending",
                "owner": None, "blockedBy": [],
            }] for i in range(count)
        }
        path = self.task_file(sid)
        path.write_text(json.dumps({
            "version": 1, "scope": scope, "updated_at": 0.0, "groups": groups,
        }, ensure_ascii=False), encoding="utf-8")
        return path


class PermanentDeleteCascadeTests(_CascadeTestCase):
    def test_delete_session_removes_task_file(self):
        sid = self.make_session()
        path = self.seed_tasks(sid, 3)
        self.assertTrue(path.exists())

        ok = self.sm.delete_session_permanent(sid)

        self.assertTrue(ok)
        self.assertEqual(task_files_for_session(sid, self.sm.session_prefix), [])
        self.assertFalse(path.exists())

    def test_delete_only_touches_own_session(self):
        sid_a = self.make_session()
        sid_b = self.make_session()
        self.seed_tasks(sid_a, 2)
        keep = self.seed_tasks(sid_b, 2)

        self.sm.delete_session_permanent(sid_a)

        self.assertEqual(task_files_for_session(sid_a, self.sm.session_prefix), [])
        self.assertEqual(len(task_files_for_session(sid_b, self.sm.session_prefix)), 1)
        self.assertTrue(keep.exists())

    def test_delete_without_task_files_is_fine(self):
        """没有任务文件的会话照常删除（不能因为"没东西可清"而失败）。"""
        sid = self.make_session()
        self.assertTrue(self.sm.delete_session_permanent(sid))


class ClearSessionCascadeTests(_CascadeTestCase):
    def test_clear_session_removes_tasks_but_keeps_session_file(self):
        sid = self.make_session()
        sess_file = self.sm.get_session_file(sid)
        path = self.seed_tasks(sid, 2)

        self.sm.clear_session(sess_file)

        self.assertTrue(sess_file.exists(), "清空会话不能把会话文件删掉")
        self.assertFalse(path.exists(),
                         "清空会话必须连带清掉任务板（否则任务板会跨轮残留）")

    def test_clear_only_touches_own_session(self):
        sid_a = self.make_session()
        sid_b = self.make_session()
        self.seed_tasks(sid_a, 1)
        keep = self.seed_tasks(sid_b, 1)

        self.sm.clear_session(self.sm.get_session_file(sid_a))

        self.assertTrue(keep.exists())


class TrashRestoreKeepsTasksTests(_CascadeTestCase):
    def test_trash_and_restore_do_not_touch_task_files(self):
        """回收站只改元数据标记；还原后任务必须还在。"""
        sid = self.make_session()
        path = self.seed_tasks(sid, 2)

        self.sm.trash_session(sid)
        self.assertTrue(path.exists(), "进回收站不应删任务文件")

        self.sm.restore_session(sid)
        self.assertTrue(path.exists(), "还原后任务必须还在")


class NamingContractTests(_CascadeTestCase):
    def test_task_files_for_session_matches_task_manager_writing(self):
        """paths 与 task_manager 的文件口径必须一致，否则清理会静默失效。"""
        sid = "abc123"
        scope = f"{self.sm.session_prefix}{sid}"
        expected = self.tasks_dir / f"{scope}.json"

        # 用真实 TaskManager 写一次，看它落到了哪个文件
        tm = TaskManager(self.tasks_dir)
        tm.set_scope(scope)
        tm._create_task("口径校验")

        self.assertTrue(expected.exists(), "TaskManager 没有写到约定的文件路径")
        self.assertEqual(task_files_for_session(sid, self.sm.session_prefix), [expected])
        self.assertEqual(tm._scope_file(), expected)

    def test_empty_result_for_unknown_session(self):
        self.assertEqual(task_files_for_session("nosuchid", self.sm.session_prefix), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
