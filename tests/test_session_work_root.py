#!/usr/bin/env python3
"""work_root 会话级沙箱根快照守护测试 —— 2026-09-20。

「会话建成即锁空间」的姊妹规则：沙箱根在**新建时**解析一次写进会话元数据，
之后不可变。这里钉死四条契约：

1. **路径束规则收口**：default(CLI/存量回退) = WORKDIR + bash 进程 cwd；
   自定义空间 = 选定目录 + bash 同根；桌面端新建 default 会话 = scratch 草稿区。
2. **meta 快照**：新建会话 meta 的 work_root 默认 None（存量兼容）；
   set_session_work_root 写入后 load_meta 可读回。
3. **读侧回退**：meta 无 work_root → 按空间现值（零迁移）；有 → 以快照为准，
   bash 与文件工具随快照同根。
4. **Agent 接线**：workspace=default_scratch_paths() 的 Agent，工具沙箱根与
   bash cwd 都落在 scratch（桌面端新 default 会话的新行为）。

测试全部用临时目录，不碰真实 ~/.aigent/projects。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import paths  # noqa: E402
import project_registry  # noqa: E402
import ws_bridge  # noqa: E402
from agent_full_v2 import Agent  # noqa: E402
from paths import default_scratch_paths, workspace_paths  # noqa: E402
from project_registry import WorkspaceRegistry  # noqa: E402
from session_manage import SessionManager  # noqa: E402


class _TempRegistryFixture(unittest.TestCase):
    """临时 projects 注册表 + 桥层缓存复原（不得污染真实 ~/.aigent）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.reg = WorkspaceRegistry(
            index_path=self.root / "projects.json", projects_root=self.root
        )
        self._orig_registry = project_registry.get_registry()
        project_registry.set_registry(self.reg)
        self.addCleanup(project_registry.set_registry, self._orig_registry)
        # 桥层模块级缓存：测试前后必须复原，否则用例互相污染
        self._managers = dict(ws_bridge._MANAGER_CACHE)
        self._sid_project = dict(ws_bridge._SID_PROJECT)
        self.addCleanup(self._restore_caches)
        self.real = self.root / "real-project"
        self.real.mkdir(parents=True, exist_ok=True)
        self.info = self.reg.create(str(self.real))

    def _restore_caches(self):
        ws_bridge._MANAGER_CACHE.clear()
        ws_bridge._MANAGER_CACHE.update(self._managers)
        ws_bridge._SID_PROJECT.clear()
        ws_bridge._SID_PROJECT.update(self._sid_project)


class PathsBundleRulesTests(unittest.TestCase):
    """路径束的 bash_cwd 规则收口（2026-09-20，原在 Agent.__init__ 推导）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()

    def test_scratch_dir_is_under_default_metadata_root(self):
        self.assertEqual(paths.DEFAULT_SCRATCH_DIR, paths.DATA_ROOT / "scratch")

    def test_default_scratch_paths_bundle(self):
        ws = default_scratch_paths()
        self.assertTrue(ws.is_default)
        self.assertEqual(ws.workdir, paths.DEFAULT_SCRATCH_DIR)
        # 桌面端新 default 会话：bash 与文件工具同根（两个落点收口）
        self.assertEqual(ws.bash_cwd, paths.DEFAULT_SCRATCH_DIR)

    def test_legacy_default_bundle_unchanged(self):
        ws = workspace_paths("default")
        self.assertEqual(ws.workdir, paths.WORKDIR)
        # CLI / 存量回退：bash 仍跑进程 cwd（历史行为，零迁移）
        self.assertIsNone(ws.bash_cwd)

    def test_custom_bundle_bash_same_root(self):
        ws = workspace_paths("ws000000001", self.tmp_path)
        self.assertEqual(ws.workdir, self.tmp_path)
        self.assertEqual(ws.bash_cwd, self.tmp_path)


class SessionMetaWorkRootTests(_TempRegistryFixture):
    """meta 的 work_root 字段：默认 None、写入可读回。"""

    def _sm(self, pid: str) -> SessionManager:
        return ws_bridge._ensure_session_manager(pid)

    def test_new_session_meta_work_root_defaults_to_none(self):
        sm = self._sm(self.info.id)
        sid, _ = sm.create_new_session()
        self.assertIsNone(sm.load_meta(sid).get("work_root"))

    def test_set_session_work_root_persists(self):
        sm = self._sm(self.info.id)
        sid, _ = sm.create_new_session()
        wr = self.root / "scratch-x"
        sm.set_session_work_root(sid, str(wr))
        self.assertEqual(sm.load_meta(sid).get("work_root"), str(wr))
        # 独立 meta 文件落盘（O(1) 轨道），非 index 兜底
        self.assertTrue(sm.meta_file(sid).exists())

    def test_work_root_survives_other_meta_updates(self):
        sm = self._sm(self.info.id)
        sid, _ = sm.create_new_session()
        sm.set_session_work_root(sid, "/tmp/wr")
        sm.set_session_model(sid, model_id="m-1")
        meta = sm.load_meta(sid)
        self.assertEqual(meta.get("work_root"), "/tmp/wr")
        self.assertEqual(meta.get("model_id"), "m-1")


class WorkspaceForSessionTests(_TempRegistryFixture):
    """读侧解析：无快照按空间现值（零迁移），有快照以快照为准。"""

    def test_meta_none_falls_back_to_legacy_default(self):
        ws = ws_bridge._workspace_for_session("default", None)
        self.assertEqual(ws.workdir, paths.WORKDIR)
        self.assertIsNone(ws.bash_cwd)

    def test_meta_without_work_root_falls_back(self):
        ws = ws_bridge._workspace_for_session("default", {"project": "default"})
        self.assertEqual(ws.workdir, paths.WORKDIR)

    def test_default_snapshot_switches_to_work_root(self):
        wr = self.root / "scratch-x"
        ws = ws_bridge._workspace_for_session(
            "default", {"work_root": str(wr)})
        self.assertEqual(ws.workdir, wr)
        # bash 与文件工具随快照同根
        self.assertEqual(ws.bash_cwd, wr)
        # 其余路径束字段不受影响（会话历史 / 任务仍在 default 元数据目录）
        self.assertEqual(ws.chat_history_dir, ws_bridge._workspace_of("default").chat_history_dir)
        self.assertTrue(ws.is_default)

    def test_custom_snapshot_overrides_dir(self):
        old_dir = self.info.path
        wr = self.root / "moved-dir"  # 模拟空间目录后续变化
        ws = ws_bridge._workspace_for_session(
            self.info.id, {"work_root": str(wr)})
        self.assertEqual(ws.workdir, wr)
        self.assertEqual(ws.bash_cwd, wr)
        self.assertNotEqual(ws.workdir, old_dir)


class AgentScratchWiringTests(_TempRegistryFixture):
    """Agent 接线：桌面端新 default 会话的沙箱根与 bash 都落在 scratch。"""

    def test_agent_tools_use_scratch(self):
        agent = Agent(silent=True, workspace=default_scratch_paths())
        self.assertEqual(agent.tools.workdir, paths.DEFAULT_SCRATCH_DIR)
        self.assertEqual(agent.tools.bash_cwd, paths.DEFAULT_SCRATCH_DIR)
        self.assertEqual(agent.system_prompt.workdir, paths.DEFAULT_SCRATCH_DIR)
        self.assertEqual(agent.hook_system.workdir, paths.DEFAULT_SCRATCH_DIR)

    def test_agent_legacy_default_keeps_process_cwd(self):
        agent = Agent(silent=True)  # 不传 workspace = CLI / 存量语义
        self.assertEqual(agent.tools.workdir, paths.WORKDIR)
        self.assertIsNone(agent.tools.bash_cwd)


if __name__ == "__main__":
    unittest.main()
