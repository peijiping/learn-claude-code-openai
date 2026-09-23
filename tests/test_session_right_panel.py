#!/usr/bin/env python3
"""右侧面板（文件 + 变更）后端守护测试 —— 2026-09-23，docs/frontend/19。

守护四件事：

1. `session_manage.normalize_right_panel()` 的**逐项丢弃**语义 —— 这份数据来自
   前端、落在磁盘上、会被手改、会跨版本漂移，任何一处畸形都不许把前端打崩。
   这里逐条钉住归一化规则，同时它也是前端 `lib/rpanelTabs.ts::normalizeTabs`
   的**对齐基准**（两边规则漂移的后果是"内存里好好的，重启后标签莫名少一枚"）。
2. `SessionManager.set_session_ui()` 的往返与 `touch=False`（不刷新 updated_at
   —— 否则点一下标签就把会话顶到列表最前），以及 `list_sessions()` **不含**
   `right_panel`（列表广播不带会话级 UI 状态）。
3. `git_changes` 的取数：非仓库是**平级空态**而不是异常；porcelain `-z` 解析
   （含重命名双记录）；单文件 diff 的路径钉死（拒绝绝对路径与 `..` 穿越）。
4. `refs.read_workspace_file()` 的降级层次：越界 / 目录 / 二进制 / 字节超限 /
   行数截断 五种结果互不混淆（前端据此渲染**不同**状态，混了就是"文件太大"
   显示成"读取失败"这种误导）。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from git_changes import diff_file, status as git_status  # noqa: E402
from refs import read_workspace_file  # noqa: E402
from session_manage import (  # noqa: E402
    RIGHTPANEL_MAX_FILE_TABS,
    RIGHTPANEL_MAX_TABS,
    SessionManager,
    normalize_right_panel,
    right_panel_tab_key,
)

SYSTEM_PROMPT = "you are a test harness"


def _file_tab(i: int, *, pinned: bool = True) -> dict:
    """第 i 个文件标签（用稳定可读的路径，便于断言淘汰顺序）。"""
    return {"kind": "file", "path": f"/proj/src/f{i:02d}.ts", "pinned": pinned}


# ══════════════════════════════════════════════════════════════════
#  一、归一化纯函数
# ══════════════════════════════════════════════════════════════════

class TestNormalizeRightPanel(unittest.TestCase):
    def test_non_dict_returns_none(self):
        for bad in (None, "x", 3, [], [{"kind": "view", "view": "files"}]):
            self.assertIsNone(normalize_right_panel(bad), msg=repr(bad))

    def test_legal_roundtrip(self):
        ui = {
            "open": True,
            "tabs": [
                {"kind": "view", "view": "files"},
                {"kind": "file", "path": "/p/a.ts", "name": "a.ts", "pinned": False},
            ],
            "active": "file:/p/a.ts",
        }
        self.assertEqual(normalize_right_panel(ui), ui)

    def test_open_coerced_to_bool(self):
        self.assertFalse(normalize_right_panel({"open": 0})["open"])
        self.assertTrue(normalize_right_panel({"open": "y"})["open"])
        self.assertFalse(normalize_right_panel({})["open"])

    def test_tabs_non_list_becomes_empty(self):
        out = normalize_right_panel({"open": True, "tabs": "oops"})
        self.assertEqual(out["tabs"], [])
        self.assertIsNone(out["active"])

    def test_bad_items_dropped_one_by_one(self):
        """坏项**逐项**丢弃，好项必须留下 —— 整份拒绝是最糟的失败方式。"""
        out = normalize_right_panel({
            "open": True,
            "tabs": [
                {"kind": "view", "view": "files"},
                {"kind": "summary"},                       # kind 非法
                {"kind": "view", "view": "nope"},          # view 不在白名单
                {"kind": "file"},                          # 无 path
                {"kind": "file", "path": "   "},           # 空白 path
                {"kind": "file", "path": ["/p/a"]},        # 脏类型
                {"kind": "file", "path": "/p/keep.ts"},    # 合法
                {"kind": "view", "view": "changes"},
            ],
            "active": "view:changes",
        })
        self.assertEqual(out["tabs"], [
            {"kind": "view", "view": "files"},
            {"kind": "file", "path": "/p/keep.ts", "name": "keep.ts",
             "pinned": True},
            {"kind": "view", "view": "changes"},
        ])
        self.assertEqual(out["active"], "view:changes")

    def test_dedupe_view_and_path(self):
        out = normalize_right_panel({
            "tabs": [
                {"kind": "view", "view": "files"},
                {"kind": "view", "view": "files"},
                {"kind": "file", "path": "/p/a.ts"},
                {"kind": "file", "path": "/p/a.ts", "name": "别名.ts"},
            ],
        })
        self.assertEqual(len(out["tabs"]), 2)
        self.assertEqual(out["tabs"][1]["name"], "a.ts")

    def test_name_falls_back_to_basename(self):
        out = normalize_right_panel({"tabs": [{"kind": "file", "path": "/a/b/c.ts"}]})
        self.assertEqual(out["tabs"][0]["name"], "c.ts")
        for bad in (None, 7, "", "  "):
            out = normalize_right_panel(
                {"tabs": [{"kind": "file", "path": "/a/b/c.ts", "name": bad}]})
            self.assertEqual(out["tabs"][0]["name"], "c.ts", msg=repr(bad))

    def test_pinned_non_bool_defaults_true(self):
        """手改 meta 不该凭空造出"幽灵预览位"（下一会话点击会无声顶掉它）。"""
        for bad in (None, 0, 1, "no", [], {}):
            out = normalize_right_panel(
                {"tabs": [{"kind": "file", "path": "/p/a.ts", "pinned": bad}]})
            self.assertIs(out["tabs"][0]["pinned"], True, msg=repr(bad))

    def test_only_last_preview_survives(self):
        out = normalize_right_panel({"tabs": [
            _file_tab(1, pinned=False),
            _file_tab(2, pinned=False),
            _file_tab(3, pinned=False),
        ]})
        # 保留"最后一次会话点击"那一枚（f03），而不是最早那枚
        self.assertEqual([t["path"] for t in out["tabs"]], ["/proj/src/f03.ts"])

    def test_pinned_overflow_drops_oldest_inactive(self):
        tabs = [_file_tab(i) for i in range(RIGHTPANEL_MAX_FILE_TABS + 3)]
        active = right_panel_tab_key(tabs[1])   # 激活第 2 枚 → 它不能被丢
        out = normalize_right_panel({"tabs": tabs, "active": active})
        paths = [t["path"] for t in out["tabs"]]
        self.assertEqual(len(paths), RIGHTPANEL_MAX_FILE_TABS)
        self.assertIn(active, [right_panel_tab_key(t) for t in out["tabs"]])
        # 丢的是最旧的两枚（f00、f01 之外的那个），激活项与较新的都在
        self.assertNotIn("/proj/src/f00.ts", paths)
        self.assertNotIn("/proj/src/f03.ts", paths)
        self.assertIn("/proj/src/f01.ts", paths)

    def test_view_tabs_never_evicted(self):
        """超限时视图标签（与预览位）永远保留 —— 淘汰它们是"点了没反应"。"""
        tabs = [{"kind": "view", "view": v}
                for v in ("files", "changes", "terminal", "browser")]
        tabs += [_file_tab(i) for i in range(RIGHTPANEL_MAX_TABS + 5)]
        tabs.append(_file_tab(99, pinned=False))
        out = normalize_right_panel({"tabs": tabs})
        kinds = [t["kind"] for t in out["tabs"]]
        self.assertEqual(kinds.count("view"), 4)
        self.assertLessEqual(len(out["tabs"]), RIGHTPANEL_MAX_TABS)
        self.assertTrue(any(t["kind"] == "file" and not t["pinned"]
                            for t in out["tabs"]))

    def test_active_must_hit_a_tab(self):
        tabs = [{"kind": "view", "view": "files"}]
        for bad in ("view:changes", "file:/nope", 3, "", None, {}):
            out = normalize_right_panel({"tabs": tabs, "active": bad})
            self.assertIsNone(out["active"], msg=repr(bad))
        out = normalize_right_panel({"tabs": tabs, "active": "view:files"})
        self.assertEqual(out["active"], "view:files")

    def test_tab_key_shape(self):
        self.assertEqual(right_panel_tab_key({"kind": "view", "view": "files"}),
                         "view:files")
        self.assertEqual(right_panel_tab_key({"kind": "file", "path": "/a.ts"}),
                         "file:/a.ts")
        self.assertEqual(right_panel_tab_key({"kind": "nope"}), "")
        self.assertEqual(right_panel_tab_key(None), "")


# ══════════════════════════════════════════════════════════════════
#  二、SessionManager 落盘往返
# ══════════════════════════════════════════════════════════════════

class _ManagerTest(unittest.TestCase):
    """所有用例都在临时目录里造会话，绝不触碰真实 ~/.aigent。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rpanel-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sm = SessionManager(self.tmp / ".chathistory", SYSTEM_PROMPT)
        self.sid = "Sid0000001"
        # 复刻真实路径：桌面端建会话走 create_new_session → ensure_index_entry，
        # 即**一定有独立 meta 文件**。只有 meta 存在，_update_entry 才走单文件分支
        # （否则回落 index.jsonl，而 ws_bridge 的 session_history 只读 load_meta）。
        self.sm.get_session_file(self.sid).touch()
        self.sm.ensure_index_entry(self.sid)


class TestSetSessionUi(_ManagerTest):
    def test_roundtrip_and_normalized_on_disk(self):
        ui = {
            "open": True,
            "tabs": [
                {"kind": "view", "view": "files"},
                {"kind": "file", "path": "/p/a.ts", "name": "a.ts", "pinned": False},
                {"kind": "view", "view": "files"},          # 重复 → 落盘后只剩一枚
            ],
            "active": "file:/p/a.ts",
        }
        self.sm.set_session_ui(self.sid, ui)
        on_disk = json.loads(
            self.sm.meta_file(self.sid).read_text(encoding="utf-8"))["right_panel"]
        self.assertEqual(on_disk["tabs"], [
            {"kind": "view", "view": "files"},
            {"kind": "file", "path": "/p/a.ts", "name": "a.ts", "pinned": False},
        ])
        self.assertEqual(on_disk["active"], "file:/p/a.ts")
        # 读路径（ws_bridge 用的就是 load_meta）拿到的是同一份
        self.assertEqual(self.sm.load_meta(self.sid)["right_panel"], on_disk)

    def test_none_is_persisted_as_none(self):
        self.sm.set_session_ui(self.sid, {"open": True})
        self.sm.set_session_ui(self.sid, None)
        self.assertIsNone(self.sm.load_meta(self.sid)["right_panel"])

    def test_touch_false_keeps_updated_at(self):
        """点标签不算"会话内容变化"，不该把会话顶到列表最前。"""
        self.sm.set_session_ui(self.sid, {"open": True})
        before = self.sm.load_meta(self.sid)["updated_at"]
        self.sm.set_session_ui(self.sid, {"open": False})
        self.assertEqual(self.sm.load_meta(self.sid)["updated_at"], before)

    def test_missing_session_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.sm.set_session_ui("NoSuchSid1", {"open": True})

    def test_new_entry_carries_field(self):
        """新会话的默认元数据里就有这个键（可发现性），值为 None。"""
        entry = self.sm._new_entry(self.sid, "session_x.jsonl")
        self.assertIn("right_panel", entry)
        self.assertIsNone(entry["right_panel"])

    def test_list_sessions_does_not_carry_field(self):
        self.sm.set_session_ui(self.sid, {"open": True, "tabs": []})
        rows = {s["id"]: s for s in self.sm.list_sessions()}
        self.assertIn(self.sid, rows)
        self.assertNotIn("right_panel", rows[self.sid])


# ══════════════════════════════════════════════════════════════════
#  三、git_changes
# ══════════════════════════════════════════════════════════════════

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                   check=True)


class _GitTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:  # pragma: no cover - 环境缺 git
            self.skipTest("未安装 git")
        self.tmp = Path(tempfile.mkdtemp(prefix="gitchanges-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 非仓库用例需要一个**真的不是仓库**的目录：tmp 的父目录可能是仓库
        _git(self.tmp, "init", "-q", ".")
        _git(self.tmp, "config", "user.email", "t@example.com")
        _git(self.tmp, "config", "user.name", "test")
        (self.tmp / "a.txt").write_text("one\n", encoding="utf-8")
        (self.tmp / "中文 文件.txt").write_text("中\n", encoding="utf-8")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-qm", "init")


class TestGitStatus(_GitTest):
    def test_clean_repo(self):
        info = git_status(self.tmp)
        self.assertTrue(info["available"])
        self.assertEqual(info["branch"], "main")
        self.assertEqual(info["files"], [])
        self.assertEqual(info["reason"], "")

    def test_non_repo_is_flat_unavailable(self):
        plain = Path(tempfile.mkdtemp(prefix="plain-"))
        self.addCleanup(shutil.rmtree, plain, ignore_errors=True)
        info = git_status(plain)
        self.assertFalse(info["available"])
        self.assertTrue(info["reason"])
        self.assertEqual(info["files"], [])

    def test_modified_untracked_and_rename(self):
        (self.tmp / "a.txt").write_text("two\n", encoding="utf-8")
        (self.tmp / "new.txt").write_text("n\n", encoding="utf-8")
        _git(self.tmp, "mv", "中文 文件.txt", "改名 后.txt")
        info = git_status(self.tmp)
        by_path = {f["path"]: f for f in info["files"]}
        self.assertTrue(by_path["a.txt"]["has_working_changes"])
        self.assertFalse(by_path["a.txt"]["staged"])
        self.assertTrue(by_path["new.txt"]["untracked"])
        # 中文 + 空格的文件名必须原样回归（-z 解析的意义就在这里）
        self.assertIn("改名 后.txt", by_path)
        self.assertEqual(by_path["改名 后.txt"]["orig_path"], "中文 文件.txt")
        self.assertTrue(by_path["改名 后.txt"]["staged"])


class TestGitDiff(_GitTest):
    def test_modified_diff_readable_chinese(self):
        (self.tmp / "中文 文件.txt").write_text("改\n", encoding="utf-8")
        info = diff_file(self.tmp, "中文 文件.txt")
        self.assertTrue(info["available"])
        self.assertIn("中文 文件.txt", info["diff"])
        self.assertFalse(info["too_large"])

    def test_untracked_uses_no_index(self):
        (self.tmp / "new.txt").write_text("n\n", encoding="utf-8")
        info = diff_file(self.tmp, "new.txt")
        self.assertTrue(info["untracked"])
        self.assertIn("+n", info["diff"])

    def test_rejects_escape_paths(self):
        """路径会作为参数交给 git —— 两道防线（语法 + resolve 落点）都要在。"""
        for bad in ("/etc/passwd", "../outside.txt", "a/../../b.txt", ""):
            info = diff_file(self.tmp, bad)
            self.assertFalse(info["available"], msg=repr(bad))
            self.assertEqual(info["reason"], "路径不在当前工作空间内")

    def test_too_large_is_not_truncated(self):
        big = self.tmp / "big.txt"
        big.write_text("x\n" * 50, encoding="utf-8")
        _git(self.tmp, "add", "big.txt")
        _git(self.tmp, "commit", "-qm", "big")
        big.write_text("y\n" * 200, encoding="utf-8")
        # 上限读在调用点（不是导入点），所以用 env 覆盖即可，不必造一个真 200KB 文件
        with mock.patch.dict(os.environ, {"GIT_DIFF_MAX_CHARS": "80"}):
            info = diff_file(self.tmp, "big.txt")
        self.assertTrue(info["too_large"])
        # 半截 diff 会让用户以为"改动就这么点" → 宁可整份不发
        self.assertEqual(info["diff"], "")
        self.assertGreater(info["chars"], 0)

    def test_non_repo(self):
        plain = Path(tempfile.mkdtemp(prefix="plain2-"))
        self.addCleanup(shutil.rmtree, plain, ignore_errors=True)
        info = diff_file(plain, "x.txt")
        self.assertFalse(info["available"])
        self.assertIn("不是 Git 仓库", info["reason"])


# ══════════════════════════════════════════════════════════════════
#  四、refs.read_workspace_file（右栏文件预览）
# ══════════════════════════════════════════════════════════════════

class TestReadWorkspaceFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="preview-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_text_file(self):
        (self.tmp / "a.py").write_text("print(1)\nprint(2)\n", encoding="utf-8")
        out = read_workspace_file(self.tmp, "a.py")
        self.assertEqual(out["name"], "a.py")
        self.assertEqual(out["text"], "print(1)\nprint(2)\n")
        self.assertEqual(out["lines"], 3)   # 结尾换行产生的空行如实反映
        self.assertEqual(out["reason"], "")
        self.assertEqual(out["encoding"], "utf-8")

    def test_out_of_sandbox_and_missing(self):
        self.assertIn("工作空间", read_workspace_file(self.tmp, "/etc/hosts")["reason"])
        self.assertEqual(read_workspace_file(self.tmp, "nope.py")["reason"],
                         "文件不存在")

    def test_directory(self):
        (self.tmp / "sub").mkdir()
        self.assertIn("目录", read_workspace_file(self.tmp, "sub")["reason"])

    def test_binary(self):
        (self.tmp / "bin.dat").write_bytes(b"\x00\x01\x02\x03")
        out = read_workspace_file(self.tmp, "bin.dat")
        self.assertTrue(out["binary"])
        self.assertEqual(out["text"], "")
        self.assertEqual(out["reason"], "")

    def test_too_large_reads_nothing(self):
        (self.tmp / "big.log").write_text("x" * 500, encoding="utf-8")
        out = read_workspace_file(self.tmp, "big.log", max_bytes=100)
        self.assertTrue(out["too_large"])
        self.assertEqual(out["text"], "")
        self.assertEqual(out["lines"], 0)

    def test_truncated_keeps_content(self):
        (self.tmp / "many.txt").write_text("a\nb\nc\nd\n", encoding="utf-8")
        out = read_workspace_file(self.tmp, "many.txt", max_lines=2)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["text"], "a\nb")
        self.assertEqual(out["lines"], 2)

    def test_gbk_fallback(self):
        (self.tmp / "gbk.txt").write_bytes("中文内容\n".encode("gb18030"))
        out = read_workspace_file(self.tmp, "gbk.txt")
        self.assertEqual(out["encoding"], "gb18030")
        self.assertIn("中文内容", out["text"])


if __name__ == "__main__":
    unittest.main()
