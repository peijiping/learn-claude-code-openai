#!/usr/bin/env python3
"""
test_session_id_naming.py - 会话短 id 命名与「按最后修改时间排序」的回归测试

覆盖（2026-09-14 会话存储改造）：
- new_session_id：10 字符 base62、绝不全数字（防与存量编号 stem 歧义）
- create_new_session：随机 id 命名 + 四件套（jsonl/meta）关联 + id 唯一
- 新旧共存：存量 session_N.jsonl 的 id 为编号字符串，可被 switch/删除/列表寻址
- list_sessions：按 updated_at（mtime 兜底）降序，最近修改在前
- append_message_to_session：追加消息后刷新 meta.updated_at
- get_latest_session：取最近使用（updated_at/mtime 最大）的会话
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from session_manage import SESSION_ID_LEN, SessionManager, new_session_id  # noqa: E402
from paths import todo_file_for_session  # noqa: E402


class SessionIdNamingTestCase(unittest.TestCase):
    """公共夹具：临时目录 + 纯 SessionManager（不注入 subagent store）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.sm = SessionManager(self.dir, "sys")

    def tearDown(self):
        self._tmp.cleanup()

    def write_session(self, name, *objects):
        f = self.dir / name
        f.write_text(
            "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objects),
            encoding="utf-8",
        )
        return f


# ── 1. 短 id 生成 ─────────────────────────────────────────────
class TestNewSessionId(unittest.TestCase):

    def test_format_base62_fixed_len(self):
        for _ in range(200):
            sid = new_session_id()
            self.assertEqual(len(sid), SESSION_ID_LEN)
            self.assertTrue(sid.isalnum(), "base62 只含字母数字")
            self.assertFalse(sid.isdigit(), "全数字会与存量编号 stem 歧义，必须重掷")

    def test_randomness_no_repeat(self):
        ids = {new_session_id() for _ in range(500)}
        self.assertEqual(len(ids), 500, "随机 id 不应出现重复")


# ── 2. 新会话创建与命名 ───────────────────────────────────────
class TestCreateNewSession(SessionIdNamingTestCase):

    def test_create_uses_short_id_and_meta(self):
        sid, f = self.sm.create_new_session()
        self.assertEqual(f.name, f"session_{sid}.jsonl")
        self.assertTrue(f.exists())
        meta = self.sm.load_meta(sid)
        self.assertIsNotNone(meta, "新会话必须写独立 meta 文件")
        self.assertEqual(meta["id"], sid)
        self.assertEqual(meta["file"], f.name)
        self.assertIn("updated_at", meta)

    def test_created_ids_unique(self):
        ids = {self.sm.create_new_session()[0] for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_todo_file_follows_session_id(self):
        sid, _ = self.sm.create_new_session()
        todo = todo_file_for_session(sid)
        self.assertEqual(todo.name, f"session_{sid}.todo.json")


# ── 3. 新旧共存（存量编号字符串即 id）─────────────────────────
class TestLegacyCoexistence(SessionIdNamingTestCase):

    def test_legacy_file_addressable_by_numeric_string_id(self):
        self.write_session("session_6.jsonl", {"role": "system", "content": "s"})
        sid, f = self.sm.get_latest_session()
        self.assertEqual(sid, "6")
        self.assertEqual(f.name, "session_6.jsonl")
        _, _, messages = self.sm.switch_session("6")
        self.assertEqual(len(messages), 1)

    def test_legacy_delete_permanent(self):
        self.write_session("session_7.jsonl", {"role": "system", "content": "s"})
        self.assertTrue(self.sm.delete_session_permanent("7"))
        self.assertFalse((self.dir / "session_7.jsonl").exists())

    def test_sidecar_files_excluded_from_listing(self):
        self.write_session("session_1.jsonl", {"role": "system", "content": "s"})
        # 旁路文件 stem 含 "."，不能混进会话列表/最新会话判定
        self.write_session("session_1.subagents.jsonl", {"v": 1, "subagent_id": "sub_1"})
        sid, f = self.sm.get_latest_session()
        self.assertEqual((sid, f.name), ("1", "session_1.jsonl"))
        self.assertEqual([s["id"] for s in self.sm.list_sessions()], ["1"])

    def test_legacy_trash_restore_via_index(self):
        f = self.write_session("session_8.jsonl", {"role": "system", "content": "s"})
        self.sm.trash_session("8")
        self.assertEqual([s["id"] for s in self.sm.list_sessions()], [])
        self.assertEqual([s["id"] for s in self.sm.list_sessions("trashed")], ["8"])
        self.sm.restore_session("8")
        self.assertEqual([s["id"] for s in self.sm.list_sessions()], ["8"])
        self.assertTrue(f.exists(), "软删除不动 jsonl")


# ── 4. 排序与 updated_at 刷新 ────────────────────────────────
class TestOrderingAndUpdatedAt(SessionIdNamingTestCase):

    def test_append_refreshes_meta_updated_at(self):
        sid, f = self.sm.create_new_session()
        before = self.sm.load_meta(sid)["updated_at"]
        time.sleep(1.1)  # updated_at 秒级精度，跨秒才能断言变化
        self.sm.append_message_to_session(f, {"role": "user", "content": "hi"})
        after = self.sm.load_meta(sid)["updated_at"]
        self.assertGreater(after, before, "追加消息必须刷新 meta.updated_at")

    def test_list_sorted_by_last_modified(self):
        # 存量三兄弟：session_1 最老，session_3 最新（mtime 顺序）
        f1 = self.write_session("session_1.jsonl", {"role": "system", "content": "s"})
        time.sleep(0.05)
        f2 = self.write_session("session_2.jsonl", {"role": "system", "content": "s"})
        time.sleep(0.05)
        self.write_session("session_3.jsonl", {"role": "system", "content": "s"})
        self.assertEqual(
            [s["id"] for s in self.sm.list_sessions()], ["3", "2", "1"],
            "默认按最后修改时间降序",
        )
        # 触碰最老的 session_1 → 它应排到最前（编号顺序与修改时间顺序解耦）
        time.sleep(0.05)
        f1.write_text(
            json.dumps({"role": "user", "content": "new activity"}) + "\n", encoding="utf-8"
        )
        self.assertEqual(
            [s["id"] for s in self.sm.list_sessions()], ["1", "3", "2"]
        )
        self.assertTrue(f2.exists())

    def test_get_latest_session_follows_activity(self):
        self.write_session("session_1.jsonl", {"role": "system", "content": "s"})
        time.sleep(0.05)
        self.write_session("session_2.jsonl", {"role": "system", "content": "s"})
        sid, _ = self.sm.get_latest_session()
        self.assertEqual(sid, "2")
        time.sleep(0.05)
        f1 = self.dir / "session_1.jsonl"
        f1.write_text(
            json.dumps({"role": "user", "content": "later"}) + "\n", encoding="utf-8"
        )
        sid, _ = self.sm.get_latest_session()
        self.assertEqual(sid, "1", "最近使用（mtime 更新）的会话应成为 latest")


if __name__ == "__main__":
    unittest.main()
