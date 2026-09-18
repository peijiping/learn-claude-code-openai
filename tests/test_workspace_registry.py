#!/usr/bin/env python3
"""工作空间注册表（projects.json）守护测试 —— 2026-09-18 多工作空间改造。

这里的每一条都对应一个**数据事故**或**用户可见的行为约定**，不是覆盖率凑数：

- default 恒在、恒第一、不可删、不可改名（索引被手改成空也只是"归一化回默认"，
  绝不能把老用户的 default 空间弄丢 —— 那等于所有历史会话变成孤儿）
- 索引原子写 + 读-改-写互斥（并发建空间不能丢条目）
- 删除**只删元数据目录**，用户选定的真实目录必须原样保留
- 重复选同一目录 → 复用条目（不新建、不重复占 id）
- 同名目录 → 展示名去重，但**元数据目录名用短码**（所以不会撞）
- 选 `~/.aigent` 内部目录要被拒（否则元数据会被写进应用自身数据目录）
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import paths  # noqa: E402
from config import AIGENT_HOME  # noqa: E402
from paths import WORKSPACE_SUBDIRS  # noqa: E402
from project_registry import (  # noqa: E402
    DEFAULT_PROJECT_ID,
    WorkspaceError,
    WorkspaceRegistry,
)


class _RegistryTestCase(unittest.TestCase):
    """公共夹具：临时 projects 根（索引与元数据目录都在临时目录里）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reg = WorkspaceRegistry(
            index_path=self.root / "projects.json", projects_root=self.root
        )

    # ── helpers ──────────────────────────────────────────────
    def make_dir(self, name: str) -> Path:
        p = self.root / "real" / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def index(self) -> dict:
        return json.loads((self.root / "projects.json").read_text(encoding="utf-8"))

    def custom_ids(self) -> list[str]:
        return [i.id for i in self.reg.list_infos() if not i.system]


class BootstrapTests(_RegistryTestCase):
    def test_ensure_creates_index_with_default_only(self):
        self.assertFalse((self.root / "projects.json").exists())
        self.reg.ensure()
        data = self.index()
        self.assertEqual(len(data["projects"]), 1)
        self.assertEqual(data["projects"][0]["id"], DEFAULT_PROJECT_ID)
        self.assertEqual(data["active"], DEFAULT_PROJECT_ID)
        # 重复 ensure 幂等
        self.reg.ensure()
        self.assertEqual(len(self.index()["projects"]), 1)

    def test_default_first_even_if_index_lacks_it(self):
        (self.root / "projects.json").write_text(
            json.dumps({"projects": [{"id": "wsABC", "name": "x", "path": "/tmp/x"}]}),
            encoding="utf-8",
        )
        infos = self.reg.list_infos()
        self.assertEqual(infos[0].id, DEFAULT_PROJECT_ID)
        self.assertTrue(infos[0].system)
        # default 被补回后也应落盘（下次读盘同样完整）
        self.reg.ensure()
        self.assertEqual(self.index()["projects"][0]["id"], DEFAULT_PROJECT_ID)

    def test_broken_index_backed_up_and_rebuilt(self):
        (self.root / "projects.json").write_text("{ 这不是 json", encoding="utf-8")
        infos = self.reg.list_infos()
        self.assertEqual([i.id for i in infos], [DEFAULT_PROJECT_ID])
        self.assertTrue((self.root / "projects.json.bak").exists())

    def test_active_falls_back_when_pointing_to_unknown(self):
        (self.root / "projects.json").write_text(
            json.dumps({"active": "wsGONE", "projects": []}), encoding="utf-8"
        )
        self.assertEqual(self.reg.active_id(), DEFAULT_PROJECT_ID)


class CreateTests(_RegistryTestCase):
    def test_create_builds_meta_dir_with_same_subdirs_as_default(self):
        real = self.make_dir("my-project")
        info = self.reg.create(str(real))
        self.assertEqual(info.name, "my-project")
        self.assertEqual(info.path, str(real.resolve()))
        meta = self.root / info.id
        self.assertTrue(meta.is_dir())
        for name in WORKSPACE_SUBDIRS:
            self.assertTrue((meta / name).is_dir(), f"缺少子目录 {name}")
        # 与 default 同构 = 用的是同一份清单（不是另抄一份）
        self.assertIn(".chathistory", WORKSPACE_SUBDIRS)
        self.assertIn(".tasks", WORKSPACE_SUBDIRS)

    def test_create_generates_ws_prefixed_base62_id(self):
        info = self.reg.create(str(self.make_dir("a")))
        self.assertTrue(info.id.startswith("ws"))
        self.assertEqual(len(info.id), 12)
        self.assertTrue(all(c.isalnum() and c.isascii() for c in info.id))
        self.assertNotEqual(info.id, DEFAULT_PROJECT_ID)
        self.assertFalse(info.system)

    def test_create_same_dir_is_idempotent(self):
        real = self.make_dir("dup")
        first = self.reg.create(str(real))
        second = self.reg.create(str(real))
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.custom_ids()), 1)

    def test_create_same_dir_via_relative_or_symlink_path_reuses_entry(self):
        real = self.make_dir("link-target")
        first = self.reg.create(str(real))
        link = self.root / "link-alias"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前文件系统不支持符号链接")
        second = self.reg.create(str(link))
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.custom_ids()), 1)

    def test_create_duplicate_folder_names_get_suffixed(self):
        a = self.reg.create(str(self.make_dir("app")))
        nested = self.root / "real" / "nested" / "app"
        nested.mkdir(parents=True, exist_ok=True)
        b = self.reg.create(str(nested))
        self.assertEqual(a.name, "app")
        self.assertEqual(b.name, "app (2)")
        # 元数据目录名互不相同（用短码，不跟随名称）
        self.assertNotEqual(a.id, b.id)

    def test_create_rejects_missing_and_file_paths(self):
        with self.assertRaises(WorkspaceError):
            self.reg.create(str(self.root / "not-there"))
        f = self.root / "a.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            self.reg.create(str(f))

    def test_create_rejects_empty_path(self):
        with self.assertRaises(WorkspaceError):
            self.reg.create("  ")

    def test_create_rejects_aigent_home_and_its_children(self):
        with self.assertRaises(WorkspaceError):
            self.reg.create(str(AIGENT_HOME))
        with self.assertRaises(WorkspaceError):
            self.reg.create(str(AIGENT_HOME / "skills"))

    def test_create_rejects_unwritable_dir(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root 下权限位无效")
        real = self.make_dir("readonly")
        os.chmod(real, 0o500)
        self.addCleanup(os.chmod, real, 0o700)
        with self.assertRaises(WorkspaceError):
            self.reg.create(str(real))

    def test_round_trip_persists_to_disk(self):
        info = self.reg.create(str(self.make_dir("persist")))
        fresh = WorkspaceRegistry(
            index_path=self.root / "projects.json", projects_root=self.root
        )
        got = fresh.get(info.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.path, info.path)
        self.assertEqual(fresh.active_id(), info.id)

    def test_concurrent_create_does_not_lose_entries(self):
        dirs = [str(self.make_dir(f"c{i}")) for i in range(8)]
        errors: list[Exception] = []

        def worker(p: str) -> None:
            try:
                self.reg.create(p)
            except Exception as e:  # noqa: BLE001 - 线程内异常需带回主线程断言
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(d,)) for d in dirs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        ids = self.custom_ids()
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 8)
        # 索引里也必须真的是 8 条（锁失效会丢更新 → 少条目）
        self.assertEqual(len([e for e in self.index()["projects"] if e["id"] != "default"]), 8)


class RenameTests(_RegistryTestCase):
    def test_rename_default_rejected(self):
        with self.assertRaises(WorkspaceError):
            self.reg.rename(DEFAULT_PROJECT_ID, "新名字")

    def test_rename_updates_name_and_keeps_id_dir(self):
        info = self.reg.create(str(self.make_dir("old")))
        renamed = self.reg.rename(info.id, "新名字")
        self.assertEqual(renamed.name, "新名字")
        self.assertEqual(renamed.id, info.id)
        self.assertTrue((self.root / info.id).is_dir())

    def test_rename_empty_rejected(self):
        info = self.reg.create(str(self.make_dir("x")))
        with self.assertRaises(WorkspaceError):
            self.reg.rename(info.id, "   ")

    def test_rename_to_existing_name_rejected(self):
        a = self.reg.create(str(self.make_dir("a")))
        self.reg.create(str(self.make_dir("b")))
        with self.assertRaises(WorkspaceError):
            self.reg.rename(a.id, "b")

    def test_rename_unknown_rejected(self):
        with self.assertRaises(WorkspaceError):
            self.reg.rename("wsNOPE", "x")


class RemoveTests(_RegistryTestCase):
    def test_remove_default_rejected(self):
        self.reg.ensure()
        with self.assertRaises(WorkspaceError):
            self.reg.remove(DEFAULT_PROJECT_ID)

    def test_remove_deletes_meta_dir_but_keeps_real_dir(self):
        real = self.make_dir("keepme")
        info = self.reg.create(str(real))
        (self.root / info.id / ".chathistory" / "session_x.jsonl").write_text(
            "{}", encoding="utf-8"
        )
        self.reg.remove(info.id)
        self.assertFalse((self.root / info.id).exists())
        self.assertTrue(real.is_dir(), "用户的真实目录绝不能被删")
        self.assertTrue((real / "").exists())
        self.assertEqual(self.custom_ids(), [])

    def test_remove_switches_active_back_to_default(self):
        info = self.reg.create(str(self.make_dir("cur")))
        self.assertEqual(self.reg.active_id(), info.id)
        self.reg.remove(info.id)
        self.assertEqual(self.reg.active_id(), DEFAULT_PROJECT_ID)

    def test_remove_survives_missing_meta_dir(self):
        info = self.reg.create(str(self.make_dir("gone")))
        import shutil

        shutil.rmtree(self.root / info.id)
        self.reg.remove(info.id)  # 不应抛
        self.assertEqual(self.custom_ids(), [])

    def test_remove_unknown_rejected(self):
        with self.assertRaises(WorkspaceError):
            self.reg.remove("wsNOPE")


class PathsTests(_RegistryTestCase):
    def test_default_paths_use_legacy_workdir_and_data_root(self):
        ws = self.reg.paths(DEFAULT_PROJECT_ID)
        self.assertTrue(ws.is_default)
        self.assertEqual(ws.data_root, paths.DATA_ROOT)
        self.assertEqual(ws.workdir, paths.WORKDIR)
        self.assertEqual(ws.chat_history_dir, paths.CHAT_HISTORY_DIR)

    def test_custom_paths_point_at_real_dir_and_temp_meta_root(self):
        real = self.make_dir("ws-real")
        info = self.reg.create(str(real))
        ws = self.reg.paths(info.id)
        self.assertEqual(ws.id, info.id)
        self.assertEqual(ws.workdir, real.resolve())
        self.assertEqual(ws.data_root, self.root / info.id)
        self.assertEqual(ws.chat_history_dir, self.root / info.id / ".chathistory")
        self.assertEqual(ws.tasks_dir, self.root / info.id / ".tasks")
        self.assertEqual(ws.durable_path,
                         self.root / info.id / ".scheduler" / "scheduled_tasks.json")

    def test_paths_of_unknown_rejected(self):
        with self.assertRaises(WorkspaceError):
            self.reg.paths("wsNOPE")

    def test_workspace_paths_requires_root_for_custom_id(self):
        # 防"静默把沙箱根落到元数据目录"：宁响亮失败
        with self.assertRaises(ValueError):
            paths.workspace_paths("wsABC")


class ProbeTests(_RegistryTestCase):
    def test_exists_false_after_real_dir_disappears(self):
        real = self.make_dir("vanish")
        info = self.reg.create(str(real))
        self.assertTrue(self.reg.get(info.id).exists)
        import shutil

        shutil.rmtree(real)
        self.assertFalse(self.reg.get(info.id).exists)
        # default 无真实目录，恒视为可用
        self.assertTrue(self.reg.get(DEFAULT_PROJECT_ID).exists)

    def test_payload_shape(self):
        info = self.reg.create(str(self.make_dir("payload")))
        payload = info.to_payload(session_count=3)
        self.assertEqual(payload["id"], info.id)
        self.assertEqual(payload["session_count"], 3)
        self.assertIn("exists", payload)
        self.assertFalse(payload["system"])
        default_payload = self.reg.get(DEFAULT_PROJECT_ID).to_payload()
        self.assertTrue(default_payload["system"])
        self.assertIsNone(default_payload["path"])


if __name__ == "__main__":
    unittest.main()
