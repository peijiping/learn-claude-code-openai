#!/usr/bin/env python3
"""「工作空间引用」（@-mention）的离线回归测试 —— 2026-09-21。

覆盖 `agents/refs.py` 的全部对外契约（设计见
`docs/frontend/13-引用文件与文件夹（@-mention）.md`）：

  1. 列目录   —— BFS 顺序、忽略清单整棵剪枝、跳 symlink、上限截断、异常不穿透
  2. 路径安全 —— `resolve_within` 越界必拒，且与 `tools.safe_path` 判定一致
  3. 规范化   —— name / is_dir 以磁盘为准（前端字段不可伪造）
  4. 账本块   —— `attach_ref_blocks` **无引用时返回同一对象**（零行为变化的硬保证）
  5. 展开     —— 发送边界合并成一个说明块；无引用块时返回**同一对象**；绝不抛
  6. 回放     —— `harvest_refs` / `ref_title_hint`

全部用例用临时目录承载，**不触碰真实 `~/.aigent`**。
运行：`.venv/bin/python -m unittest discover -s tests -v`（pytest 未安装，用内置 unittest）
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import refs as R  # noqa: E402
from tools import ToolRegistry  # noqa: E402


def _tree(base: Path, spec: dict) -> None:
    """按 {相对路径: 内容} 建目录树（内容为 None 表示建目录）。"""
    for rel, content in spec.items():
        target = base / rel
        if content is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


class _TempWorkspace(unittest.TestCase):
    """所有用例共用的临时工作空间夹具。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def _names(self, items) -> list[str]:
        return [i["name"] for i in items]


# ══════════════════════════════════════════════════════════════════
#  1. 扁平列目录
# ══════════════════════════════════════════════════════════════════

class ListWorkspaceTests(_TempWorkspace):

    def test_flat_listing_has_both_dirs_and_files(self):
        _tree(self.root, {
            "src": None,
            "src/a.py": "print(1)",
            "README.md": "# hi",
        })
        result = R.list_workspace(self.root)
        self.assertEqual(result["workdir"], str(self.root))
        self.assertFalse(result["truncated"])
        self.assertEqual(result["skipped"], 0)
        by_name = {i["name"]: i["type"] for i in result["items"]}
        self.assertEqual(by_name, {
            "src": "dir", "a.py": "file", "README.md": "file",
        })
        # path 是绝对路径，前端靠 path 去掉 name 还原所在目录
        for item in result["items"]:
            self.assertTrue(Path(item["path"]).is_absolute())
            self.assertTrue(item["path"].endswith(item["name"]))

    def test_within_directory_dirs_come_before_files_sorted_casefold(self):
        _tree(self.root, {
            "zeta": None,
            "alpha": None,
            "b.txt": "b",
            "A.txt": "A",
        })
        names = self._names(R.list_workspace(self.root)["items"])
        # 目录排在前、各自按 casefold 升序（A.txt 与 b.txt 不区分大小写比较）
        self.assertEqual(names, ["alpha", "zeta", "A.txt", "b.txt"])

    def test_bfs_lists_shallower_entries_first(self):
        _tree(self.root, {
            "dir1": None,
            "dir2": None,
            "top.txt": "t",
            "dir1/inner.txt": "i",
            "dir2/deep": None,
            "dir2/deep/deepest.txt": "d",
        })
        items = R.list_workspace(self.root)["items"]
        depths = {}
        for item in items:
            rel = Path(item["path"]).relative_to(self.root)
            depths[item["name"]] = len(rel.parts)
        # BFS：任何浅层条目都排在更深条目之前
        ordered = [depths[i["name"]] for i in items]
        self.assertEqual(ordered, sorted(ordered))
        self.assertEqual(self._names(items),
                         ["dir1", "dir2", "top.txt", "inner.txt", "deep", "deepest.txt"])

    def test_ignore_list_prunes_directory_entirely(self):
        _tree(self.root, {
            "node_modules/pkg/index.js": "module.exports = 1",
            ".git/config": "[core]",
            "__pycache__/x.pyc": "bytecode",
            "keep/ok.txt": "ok",
        })
        names = self._names(R.list_workspace(self.root)["items"])
        # 被忽略的目录**连自身都不出现**，其中的内容更不会出现
        self.assertEqual(names, ["keep", "ok.txt"])
        self.assertNotIn("node_modules", names)
        self.assertNotIn(".git", names)
        self.assertNotIn("index.js", names)

    def test_ds_store_is_ignored(self):
        _tree(self.root, {".DS_Store": "junk", "a.txt": "a"})
        self.assertEqual(self._names(R.list_workspace(self.root)["items"]), ["a.txt"])

    def test_extra_ignore_via_env(self):
        _tree(self.root, {"vendor/lib.js": "1", "src/a.js": "2"})
        with mock.patch.dict(os.environ, {"REF_LIST_IGNORE": "vendor, custom"}):
            self.assertIn("vendor", R.ignore_dirs())
            names = self._names(R.list_workspace(self.root)["items"])
        self.assertEqual(names, ["src", "a.js"])

    def test_symlinks_are_skipped(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        _tree(self.root, {"real.txt": "r", "sub": None})
        os.symlink(outside / "secret.txt", self.root / "escape.txt")
        os.symlink(outside, self.root / "escape_dir")
        os.symlink(self.root / "sub", self.root / "loop")
        names = self._names(R.list_workspace(self.root)["items"])
        self.assertNotIn("escape.txt", names)
        self.assertNotIn("escape_dir", names)
        self.assertNotIn("loop", names)
        self.assertIn("real.txt", names)

    def test_permission_error_is_swallowed_and_counted(self):
        _tree(self.root, {"locked/x.txt": "x", "open.txt": "o"})
        real_scandir = os.scandir

        def fake_scandir(path):
            if Path(path).name == "locked":
                raise PermissionError(13, "Permission denied")
            return real_scandir(path)

        with mock.patch("os.scandir", side_effect=fake_scandir):
            result = R.list_workspace(self.root)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("open.txt", self._names(result["items"]))

    def test_missing_or_invalid_workdir_never_raises(self):
        for bad in (self.root / "does-not-exist", "", None, "\x00bad"):
            result = R.list_workspace(bad)
            self.assertEqual(result["items"], [])
            self.assertFalse(result["truncated"])

    def test_total_seen_counts_scanned_entries(self):
        _tree(self.root, {"a.txt": "a", "node_modules/x.js": "x", "b.txt": "b"})
        result = R.list_workspace(self.root)
        # 扫到 3 条（含被忽略的 node_modules 目录本身），列出 2 条
        self.assertEqual(result["total_seen"], 3)
        self.assertEqual(len(result["items"]), 2)


class TruncationTests(_TempWorkspace):

    def test_limit_truncates_and_flags(self):
        _tree(self.root, {f"f{i:02d}.txt": str(i) for i in range(10)})
        result = R.list_workspace(self.root, limit=3)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(self._names(result["items"]), ["f00.txt", "f01.txt", "f02.txt"])

    def test_truncation_stops_descending(self):
        """截断发生在浅层时，不应再去扫子目录（省时间也省得列出一堆深层条目）。"""
        _tree(self.root, {"a.txt": "a", "b.txt": "b", "sub/deep.txt": "d"})
        calls: list[str] = []
        real_scandir = os.scandir

        def counting_scandir(path):
            calls.append(str(path))
            return real_scandir(path)

        with mock.patch("os.scandir", side_effect=counting_scandir):
            result = R.list_workspace(self.root, limit=2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("deep.txt", self._names(result["items"]))

    def test_no_truncation_when_under_limit(self):
        _tree(self.root, {"a.txt": "a"})
        self.assertFalse(R.list_workspace(self.root, limit=10)["truncated"])

    def test_max_entries_is_env_driven(self):
        with mock.patch.dict(os.environ, {"REF_LIST_MAX_ENTRIES": "7"}):
            self.assertEqual(R.max_entries(), 7)
        with mock.patch.dict(os.environ, {"REF_LIST_MAX_ENTRIES": "0"}):
            # 非法（< minimum）→ 回落默认值，绝不允许 0 导致空列表
            self.assertEqual(R.max_entries(), 3000)


# ══════════════════════════════════════════════════════════════════
#  2. 路径安全
# ══════════════════════════════════════════════════════════════════

class ResolveWithinTests(_TempWorkspace):

    def test_accepts_paths_inside_workspace(self):
        _tree(self.root, {"src/a.txt": "a", "src/nested/b.txt": "b"})
        for rel in ("src/a.txt", "src/nested/b.txt", "./src/a.txt",
                    "src/../src/a.txt", str(self.root)):
            self.assertIsNotNone(R.resolve_within(self.root, rel), rel)

    def test_accepts_missing_target_inside_workspace(self):
        # 不要求存在：resolve 用非严格模式，是否有效交给上层判断
        self.assertIsNotNone(R.resolve_within(self.root, "not/created/yet.txt"))

    def test_rejects_escapes(self):
        for bad in ("../escape.txt", "../../../../etc/hosts", "/etc/passwd",
                    "src/../../outside.txt", "", "   ", None):
            self.assertIsNone(R.resolve_within(self.root, bad), bad)

    def test_rejects_symlink_escaping_workspace(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        _tree(self.root, {"sub": None})
        os.symlink(outside / "secret.txt", self.root / "link.txt")
        # 形状上在工作空间内，但 resolve() 之后链到外面 → 必须拒绝
        self.assertIsNone(R.resolve_within(self.root, "link.txt"))

    def test_matches_tools_safe_path_verdict(self):
        """与 `tools.safe_path` 的判定必须一致 —— 否则"列表里能选"与
        "模型读得到"会脱节（引用功能的整个前提就是沙箱内可读）。"""
        _tree(self.root, {"src/a.txt": "a"})
        registry = ToolRegistry(workdir=self.root, bash_cwd=self.root)
        cases = [
            "src/a.txt", "src", ".", "src/nested/new.txt",
            "../escape.txt", "/etc/passwd", "src/../../outside.txt",
            "../../../../etc/hosts",
        ]
        for rel in cases:
            try:
                registry.safe_path(rel)
                allowed_by_tools = True
            except ValueError:
                allowed_by_tools = False
            self.assertEqual(R.resolve_within(self.root, rel) is not None,
                             allowed_by_tools, f"判定不一致: {rel}")


# ══════════════════════════════════════════════════════════════════
#  3. 规范化（以磁盘为准）
# ══════════════════════════════════════════════════════════════════

class NormalizeRefTests(_TempWorkspace):

    def test_name_and_is_dir_come_from_disk_not_frontend(self):
        _tree(self.root, {"real.md": "content", "folder": None})
        forged = R.normalize_ref(self.root, {
            "path": str(self.root / "real.md"),
            "name": "伪造的名字.exe",     # 前端伪造的名字必须被覆盖
            "is_dir": True,               # 前端伪造的类型必须被覆盖
            "project_id": "ws123",
        })
        self.assertEqual(forged["name"], "real.md")
        self.assertFalse(forged["is_dir"])
        self.assertEqual(forged["project_id"], "ws123")

        folder = R.normalize_ref(self.root, {"path": str(self.root / "folder")})
        self.assertEqual(folder["name"], "folder")
        self.assertTrue(folder["is_dir"])

    def test_out_of_workspace_is_dropped(self):
        self.assertIsNone(R.normalize_ref(self.root, {"path": "/etc/passwd"}))
        self.assertIsNone(R.normalize_ref(self.root, {"path": "../../x"}))
        self.assertIsNone(R.normalize_ref(self.root, {"path": ""}))
        self.assertIsNone(R.normalize_ref(self.root, "not-a-dict"))
        self.assertIsNone(R.normalize_ref(self.root, None))

    def test_missing_file_is_still_accepted(self):
        """文件在挑选后被删掉：仍接受并标注，不静默丢弃用户意图。"""
        record = R.normalize_ref(self.root, {"path": "gone.txt"})
        self.assertIsNotNone(record)
        self.assertEqual(record["name"], "gone.txt")
        self.assertFalse(record["is_dir"])

    def test_normalize_refs_dedupes_by_path(self):
        _tree(self.root, {"a.txt": "a", "b.txt": "b"})
        records = R.normalize_refs(self.root, [
            {"path": "a.txt"},
            {"path": "a.txt"},
            {"path": "b.txt"},
            {"path": "/etc/passwd"},     # 越界，丢弃
            {"path": "a.txt"},
        ])
        self.assertEqual([r["name"] for r in records], ["a.txt", "b.txt"])


# ══════════════════════════════════════════════════════════════════
#  4. 账本块组装（零行为变化的身份断言）
# ══════════════════════════════════════════════════════════════════

class AttachRefBlocksTests(_TempWorkspace):

    def _record(self, name="a.txt", is_dir=False):
        return {"path": str(self.root / name), "name": name,
                "is_dir": is_dir, "project_id": "ws1"}

    def test_block_shape(self):
        block = R.build_ref_block(self._record())
        self.assertEqual(block["type"], R.REF_BLOCK_TYPE)
        self.assertEqual(block["ref"], {
            "path": str(self.root / "a.txt"), "name": "a.txt",
            "is_dir": False, "project_id": "ws1",
        })

    def test_no_refs_returns_same_object_str(self):
        self.assertIs(R.attach_ref_blocks("hello", []), R.attach_ref_blocks("hello", []))
        text = "hello"
        self.assertIs(R.attach_ref_blocks(text, []), text)

    def test_no_refs_returns_same_object_list(self):
        content = [{"type": "text", "text": "hi"}]
        self.assertIs(R.attach_ref_blocks(content, []), content)

    def test_str_content_with_refs_becomes_blocks(self):
        result = R.attach_ref_blocks("看看这个", [self._record()])
        self.assertIsInstance(result, list)
        self.assertEqual(result[0], {"type": "text", "text": "看看这个"})
        self.assertEqual(result[1]["type"], R.REF_BLOCK_TYPE)

    def test_empty_text_with_refs_omits_text_block(self):
        result = R.attach_ref_blocks("   ", [self._record()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], R.REF_BLOCK_TYPE)

    def test_list_content_is_appended_without_mutating_original(self):
        original = [{"type": "text", "text": "hi"}]
        result = R.attach_ref_blocks(original, [self._record()])
        self.assertEqual(len(original), 1)          # 原对象未被改动
        self.assertEqual(len(result), 2)
        self.assertIsNot(result, original)

    def test_records_without_path_are_dropped(self):
        content = [{"type": "text", "text": "hi"}]
        self.assertIs(R.attach_ref_blocks(content, [{"name": "no-path"}]), content)


class HistoryHasRefsTests(_TempWorkspace):

    def test_detects_ref_blocks(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "hi"},
            R.build_ref_block({"path": "/w/a.txt", "name": "a.txt"}),
        ]}]
        self.assertTrue(R.history_has_refs(messages))

    def test_false_for_plain_content(self):
        self.assertFalse(R.history_has_refs([{"role": "user", "content": "hi"}]))
        self.assertFalse(R.history_has_refs([{"role": "user", "content": [
            {"type": "attachment", "attachment": {"id": "att_x"}}]}]))
        self.assertFalse(R.history_has_refs([]))
        self.assertFalse(R.history_has_refs(None))


# ══════════════════════════════════════════════════════════════════
#  5. 发送边界展开
# ══════════════════════════════════════════════════════════════════

class ExpandRefBlocksTests(_TempWorkspace):

    def setUp(self):
        super().setUp()
        # 被引用的目标真实存在 —— 「内容不在上下文中」的整条前提就是它们读得到
        _tree(self.root, {"a.txt": "a", "sub": None})

    def _message(self):
        return {"role": "user", "content": [
            {"type": "text", "text": "看看 @a.txt"},
            R.build_ref_block({"path": str(self.root / "a.txt"), "name": "a.txt"}),
            R.build_ref_block({"path": str(self.root / "sub"), "name": "sub",
                               "is_dir": True}),
        ]}

    def test_expands_to_single_text_block_with_paths_and_instructions(self):
        expanded = R.expand_ref_blocks_for_model(self._message())
        self.assertIsNot(expanded, self._message())
        content = expanded["content"]
        self.assertEqual(len(content), 2)                 # 正文 + 一个引用说明块
        self.assertEqual(content[0], {"type": "text", "text": "看看 @a.txt"})
        text = content[1]["text"]
        self.assertIn(str(self.root / "a.txt"), text)
        self.assertIn(str(self.root / "sub"), text)
        self.assertIn("（文件）", text)
        self.assertIn("（目录）", text)
        self.assertIn("run_read", text)
        self.assertIn("不在上下文中", text)

    def test_does_not_mutate_original_message(self):
        message = self._message()
        R.expand_ref_blocks_for_model(message)
        # 内存与 jsonl 恒为账本形态：原 message 必须仍带 ref 块
        self.assertTrue(R.history_has_refs([message]))
        self.assertEqual(len(message["content"]), 3)

    def test_same_object_when_no_ref_blocks(self):
        message = {"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "attachment", "attachment": {"id": "att_1"}},
        ]}
        self.assertIs(R.expand_ref_blocks_for_model(message), message)

    def test_same_object_for_str_content(self):
        message = {"role": "user", "content": "plain text"}
        self.assertIs(R.expand_ref_blocks_for_model(message), message)

    def test_same_object_for_non_dict_message(self):
        self.assertIsNone(R.expand_ref_blocks_for_model(None))
        message = {"role": "assistant"}
        self.assertIs(R.expand_ref_blocks_for_model(message), message)

    def test_missing_file_is_flagged(self):
        message = {"role": "user", "content": [
            R.build_ref_block({"path": str(self.root / "gone.txt"), "name": "gone.txt"}),
        ]}
        text = R.expand_ref_blocks_for_model(message)["content"][0]["text"]
        self.assertIn("当前不存在", text)

    def test_existing_file_is_not_flagged(self):
        _tree(self.root, {"a.txt": "a"})
        message = {"role": "user", "content": [
            R.build_ref_block({"path": str(self.root / "a.txt"), "name": "a.txt"}),
        ]}
        text = R.expand_ref_blocks_for_model(message)["content"][0]["text"]
        self.assertNotIn("当前不存在", text)

    def test_malformed_block_does_not_raise(self):
        message = {"role": "user", "content": [
            {"type": R.REF_BLOCK_TYPE},                      # 缺 ref
            {"type": R.REF_BLOCK_TYPE, "ref": "not-a-dict"},  # 类型错
            {"type": R.REF_BLOCK_TYPE, "ref": {"path": ""}},  # 空路径
        ]}
        expanded = R.expand_ref_blocks_for_model(message)
        # 全是畸形块 → 无可渲染内容，块被全部剥掉但不抛异常
        self.assertEqual(expanded["content"], [])

    def test_never_raises_on_exception(self):
        message = {"role": "user", "content": object()}
        self.assertIs(R.expand_ref_blocks_for_model(message), message)


# ══════════════════════════════════════════════════════════════════
#  6. 回放与标题兜底
# ══════════════════════════════════════════════════════════════════

class HarvestTests(_TempWorkspace):

    def test_harvests_ui_shape(self):
        content = [
            {"type": "text", "text": "hi"},
            R.build_ref_block({"path": "/w/a.txt", "name": "a.txt"}),
            R.build_ref_block({"path": "/w/sub", "name": "sub", "is_dir": True,
                               "project_id": "ws1"}),
        ]
        self.assertEqual(R.harvest_refs(content), [
            {"path": "/w/a.txt", "name": "a.txt", "is_dir": False},
            {"path": "/w/sub", "name": "sub", "is_dir": True},
        ])

    def test_non_list_and_malformed_yield_empty(self):
        self.assertEqual(R.harvest_refs("plain"), [])
        self.assertEqual(R.harvest_refs(None), [])
        self.assertEqual(R.harvest_refs([{"type": "attachment"}]), [])
        self.assertEqual(R.harvest_refs([{"type": R.REF_BLOCK_TYPE}]), [])
        self.assertEqual(R.harvest_refs([{"type": R.REF_BLOCK_TYPE, "ref": {"path": ""}}]), [])


class TitleHintTests(_TempWorkspace):

    def test_uses_name(self):
        self.assertEqual(R.ref_title_hint([{"name": "a.ts", "path": "/w/a.ts"}]),
                         "[引用] a.ts")

    def test_falls_back_to_path_basename(self):
        self.assertEqual(R.ref_title_hint([{"path": "/w/x/y.ts"}]), "[引用] y.ts")

    def test_empty_for_nothing(self):
        for bad in ([], None, "x", [{"name": ""}], [{}]):
            self.assertEqual(R.ref_title_hint(bad), "")


if __name__ == "__main__":
    unittest.main()
