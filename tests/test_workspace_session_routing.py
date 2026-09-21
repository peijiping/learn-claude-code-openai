#!/usr/bin/env python3
"""多工作空间**会话路由**守护测试 —— 2026-09-18。

桥层要回答的核心问题只有一个：「这个 session_id 属于哪个工作空间？」
答错了不会报错，只会静默写到别的空间去。所以这里把三条契约钉死：

1. 新建会话的归属按 `project_id` 落定，并进 `_SID_PROJECT` 缓存；
2. 按 session_id 的解析（`_project_of_session` / `_manager_for_session`）认得出
   自定义空间的会话，且**读到的会话文件确实在该空间的元数据目录下**；
3. 会话 id **跨空间唯一**（`set_session_id_guard`）—— 单空间查重挡不住
   "两个空间各自生成同一个 id"，而 id 是前端事件路由键。

测试通过注入临时 projects 根来跑，不碰真实 `~/.aigent/projects`。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import project_registry  # noqa: E402
import session_manage  # noqa: E402
import ws_bridge  # noqa: E402
from project_registry import WorkspaceRegistry  # noqa: E402


class SessionRoutingTests(unittest.TestCase):
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

        # 桥层三个缓存是模块级状态：测试前后必须清干净，否则用例互相污染
        self._managers = dict(ws_bridge._MANAGER_CACHE)
        self._sid_project = dict(ws_bridge._SID_PROJECT)
        self.addCleanup(self._restore_caches)

        real = self.root / "real-project"
        real.mkdir(parents=True, exist_ok=True)
        self.info = self.reg.create(str(real))

    def _restore_caches(self):
        ws_bridge._MANAGER_CACHE.clear()
        ws_bridge._MANAGER_CACHE.update(self._managers)
        ws_bridge._SID_PROJECT.clear()
        ws_bridge._SID_PROJECT.update(self._sid_project)

    def _new_session_in(self, pid: str) -> str:
        sm = ws_bridge._ensure_session_manager(pid)
        sid, _ = sm.create_new_session()
        ws_bridge._SID_PROJECT[sid] = pid
        return sid

    # ── 归属解析 ──────────────────────────────────────────────
    def test_custom_space_session_resolves_to_its_space(self):
        sid = self._new_session_in(self.info.id)
        self.assertEqual(ws_bridge._project_of_session(sid), self.info.id)
        sm = ws_bridge._manager_for_session(sid)
        self.assertEqual(sm.project_id, self.info.id)
        # 读到的会话文件必须落在该空间的元数据目录下
        self.assertTrue(
            sm.get_session_file(sid).is_relative_to(self.root / self.info.id)
        )

    def test_default_space_session_resolves_to_default(self):
        # 只走缓存路径，**不在真实 default 空间建文件**（测试不得污染用户数据）
        ws_bridge._SID_PROJECT["FAKESID001"] = "default"
        self.assertEqual(ws_bridge._project_of_session("FAKESID001"), "default")
        self.assertEqual(
            ws_bridge._manager_for_session("FAKESID001").project_id, "default"
        )

    def test_unknown_session_falls_back_to_default(self):
        # 陌生 id（老数据 / 已删除）不得抛异常，按 default 处理（改造前行为）
        self.assertEqual(ws_bridge._project_of_session("NOSUCHID123"), "default")

    def test_probe_finds_space_without_cache(self):
        """缓存被清掉时靠磁盘探测仍能认领（重启后第一次操作就走这条路）。"""
        sid = self._new_session_in(self.info.id)
        ws_bridge._SID_PROJECT.clear()
        self.assertEqual(ws_bridge._project_of_session(sid), self.info.id)

    def test_session_list_carries_project(self):
        custom = self._new_session_in(self.info.id)
        items = {s["id"]: s for s in ws_bridge._list_all_sessions()}
        self.assertIn(custom, items)
        self.assertEqual(items[custom]["project"], self.info.id)
        # 每条都必须能归属到某个空间（前端按它分组；缺字段会掉出侧边栏树）
        self.assertTrue(all(s.get("project") for s in items.values()))

    def test_projects_payload_shape(self):
        payload = ws_bridge._projects_payload()
        ids = [p["id"] for p in payload["projects"]]
        self.assertEqual(ids[0], "default")          # default 恒第一
        self.assertIn(self.info.id, ids)
        self.assertEqual(payload["active"], self.info.id)

    # ── 会话 id 跨空间唯一 ────────────────────────────────────
    def test_global_guard_detects_id_in_other_space(self):
        sid = self._new_session_in(self.info.id)
        self.assertTrue(ws_bridge._session_id_taken_globally(sid))
        self.assertFalse(ws_bridge._session_id_taken_globally("NOSUCHID123"))

    def test_new_session_avoids_id_taken_by_another_space(self):
        """核心：别的空间占用的 id 必须被跳过（单空间查重挡不住这种碰撞）。

        两个空间都是临时目录；第二个空间的新建请求被喂入"已被占用的 id + 一个
        全新 id"，必须落到后者。
        """
        taken = self._new_session_in(self.info.id)
        other_real = self.root / "another-project"
        other_real.mkdir(parents=True, exist_ok=True)
        other = self.reg.create(str(other_real))

        fresh = self._unique_id()
        seq = iter([taken, fresh])
        orig_new = session_manage.new_session_id
        orig_guard = session_manage._SESSION_ID_TAKEN
        session_manage.new_session_id = lambda: next(seq)
        session_manage.set_session_id_guard(ws_bridge._session_id_taken_globally)
        try:
            sm = ws_bridge._ensure_session_manager(other.id)  # 另一个空间
            sid, _ = sm.create_new_session()
        finally:
            session_manage.new_session_id = orig_new
            session_manage.set_session_id_guard(orig_guard)
        self.assertEqual(sid, fresh)
        # 新会话确实落在第二个空间里（没串到第一个空间）
        self.assertTrue(
            sm.get_session_file(sid).is_relative_to(self.root / other.id)
        )

    def _unique_id(self) -> str:
        """本测试专用的会话 id（用随机后缀，避免与历史遗留文件撞上）。"""
        import secrets
        import string

        alphabet = string.ascii_letters + string.digits
        while True:
            sid = "T" + "".join(secrets.choice(alphabet) for _ in range(9))
            if not self.reg.paths(self.info.id).chat_history_dir.joinpath(
                f"session_{sid}.jsonl"
            ).exists():
                return sid

    # ── 空间就绪判定（目录被删的场景）──────────────────────────
    def test_project_ready_reflects_real_dir(self):
        self.assertTrue(ws_bridge._project_ready(self.info.id))
        self.assertTrue(ws_bridge._project_ready("default"))  # 无真实目录，恒可用
        import shutil

        shutil.rmtree(self.root / "real-project")
        self.assertFalse(ws_bridge._project_ready(self.info.id))

    def test_busy_guard_for_project_removal(self):
        """删除工作空间的守卫：只有**本空间**在跑的会话才算数（别的空间不算）。"""
        from session_runtime import SessionRuntime, SessionRuntimeRegistry

        reg = SessionRuntimeRegistry(lambda *a, **k: None, lambda: None, lambda n: {})
        ws_paths = self.reg.paths(self.info.id)
        other_paths = ws_bridge._workspace_of("default")
        busy = reg.get_or_create("BUSY000001", workspace=ws_paths)
        busy.busy = True
        reg.get_or_create("IDLE000001", workspace=ws_paths)
        other = reg.get_or_create("BUSY000002", workspace=other_paths)
        other.busy = True

        self._orig = ws_bridge.registry
        ws_bridge.registry = reg
        self.addCleanup(setattr, ws_bridge, "registry", self._orig)

        self.assertEqual(ws_bridge._busy_sessions_of_project(self.info.id), ["BUSY000001"])
        self.assertEqual(ws_bridge._busy_sessions_of_project("default"), ["BUSY000002"])
        self.assertEqual(
            sorted(ws_bridge._sessions_of_project(self.info.id)),
            ["BUSY000001", "IDLE000001"],
        )
        self.assertEqual(ws_bridge._busy_sessions_of_project("default"), ["BUSY000002"])
        self.assertEqual(ws_bridge._busy_sessions_of_project("wsNOSUCH"), [])
        self.assertIsInstance(busy, SessionRuntime)


if __name__ == "__main__":
    unittest.main()
