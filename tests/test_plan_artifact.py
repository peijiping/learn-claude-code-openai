#!/usr/bin/env python3
"""计划文书的落点、读取与级联清理守护测试 —— 2026-09-25，docs/frontend/22。

守护五件事：

1. **路径口径唯一**：`paths.plan_file_for_session` 是唯一实现，
   `SessionManager.plan_file_for` / `execution_mode.plan_file_for` 都委托它。
   写入端与清理端同源 —— 不同源就会出现"删了会话却把文书永久留在磁盘上"
   （清理静默失效，无任何报错）。
2. **读取降级层次**：`execution_mode.read_plan_file` 的 存在 / 不存在 / 非文件 /
   超限 四种结果互不混淆，且**绝不抛异常**（前端据此渲染不同状态）。
3. **回执形状**与既有 `file_content` 同族 —— 前端不必写第二套解析。
4. **级联清理恰好两处**：`delete_session_permanent` / `clear_session` 清文书；
   `trash_session`（软删除）**刻意保留**。
5. **落点不可被模型指定**：`plan_write` 工具 schema 只有 `content`，没有 path；
   处理器委托 Agent 注入的闭包（闭包拿 session_id/data_root，registry 拿不到）。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import ast
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import execution_mode  # noqa: E402
from execution_mode import (  # noqa: E402
    MODE_PLAN,
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_READY,
    plan_content_payload,
    read_plan_file,
)
from paths import plan_file_for_session  # noqa: E402
from session_manage import SessionManager  # noqa: E402
from tools import ToolRegistry  # noqa: E402

SYSTEM_PROMPT = "you are a test harness"
SID = "Sid0000001"

# `plan_content` 的字段契约（与 ws_bridge.file_content_disabled 同族）。
# 写死一份放进断言：形状漂移必须**显式**改测试，而不是悄悄漂。
EXPECTED_PLAN_CONTENT_KEYS = {
    "project_id", "session_id", "path", "name", "size", "mtime", "encoding",
    "binary", "too_large", "truncated", "lines", "text", "reason",
}


class _SilenceAigentLogMixin:
    """把 `aigent` 日志的处理器换成 NullHandler，避免测试污染真实
    `~/.aigent/logs/`。

    本模块有多处**刻意**触发失败路径（文书不存在 / too_large / sink 抛异常），
    这些都会走 `log.warning` / `log.error`；不静音就会往用户真实日志里写垃圾。
    处理器在 tearDown 原样还原，不影响其它测试模块。
    """

    def _silence_aigent_log(self) -> None:
        lg = logging.getLogger("aigent")
        saved = list(lg.handlers)
        lg.handlers = [logging.NullHandler()]
        self.addCleanup(lambda: setattr(lg, "handlers", saved))


class _ManagerTest(_SilenceAigentLogMixin, unittest.TestCase):
    def setUp(self):
        self._silence_aigent_log()
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()
        self.sm = SessionManager(self.root / ".chathistory", SYSTEM_PROMPT)
        # 复刻真实路径：桌面端建会话一定有独立 meta 文件（_update_entry 才走
        # 单文件分支；否则回落 index.jsonl，而 ws_bridge 只读 load_meta）。
        self.sm.get_session_file(SID).touch()
        self.sm.ensure_index_entry(SID)


# ══════════════════════════════════════════════════════════════════
#  一、路径口径
# ══════════════════════════════════════════════════════════════════

class TestPlanPath(_ManagerTest):
    def test_paths_module_is_single_source(self):
        p = plan_file_for_session(SID, "session_", self.root / "plans")
        self.assertEqual(p, self.root / "plans" / f"session_{SID}.md")

    def test_session_manager_delegates(self):
        self.assertEqual(self.sm.plan_file_for(SID),
                         self.root / "plans" / f"session_{SID}.md")

    def test_prefix_respected(self):
        self.assertEqual(plan_file_for_session("x", "s_", Path("/tmp/pl")),
                         Path("/tmp/pl/s_x.md"))

    def test_plans_dir_is_sibling_of_chathistory(self):
        """文书落在**元数据目录**下的 plans/，不是 .chathistory 里面。"""
        self.assertEqual(self.sm.plan_file_for(SID).parent,
                         self.root / "plans")


# ══════════════════════════════════════════════════════════════════
#  二、读取与降级层次
# ══════════════════════════════════════════════════════════════════

class TestReadPlanFile(_ManagerTest):
    def test_success(self):
        p = self.sm.plan_file_for(SID)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# 计划\n\n1. 做 A\n2. 做 B\n", encoding="utf-8")
        out = read_plan_file(p)
        self.assertEqual(out["reason"], "")
        self.assertEqual(out["name"], f"session_{SID}.md")
        self.assertGreater(out["size"], 0)
        self.assertEqual(out["lines"], 5)
        self.assertIn("做 A", out["text"])
        self.assertEqual(out["encoding"], "utf-8")

    def test_missing_file_gives_reason(self):
        out = read_plan_file(self.sm.plan_file_for(SID))
        self.assertTrue(out["reason"])
        self.assertEqual(out["text"], "")
        self.assertFalse(out["too_large"])

    def test_directory_gives_reason(self):
        p = self.sm.plan_file_for(SID)
        p.mkdir(parents=True, exist_ok=True)
        out = read_plan_file(p)
        self.assertTrue(out["reason"])
        self.assertFalse(out["too_large"])

    def test_too_large_reads_nothing(self):
        p = self.sm.plan_file_for(SID)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * 5000, encoding="utf-8")
        # 把上限压到 100 字节（上限口径在 refs.file_preview_max_bytes）
        with mock.patch.object(execution_mode, "file_preview_max_bytes",
                               lambda: 100):
            out = read_plan_file(p)
        self.assertTrue(out["too_large"])
        self.assertEqual(out["text"], "")   # 超限**一点内容都不读**
        self.assertTrue(out["reason"])

    def test_never_raises_on_weird_path(self):
        for bad in (Path("/\x00bad"), Path("/proc/1/mem")):
            out = read_plan_file(bad)
            self.assertTrue(out["reason"], msg=str(bad))

    def test_payload_shape_matches_contract(self):
        out = plan_content_payload("/x/y.md", reason="boom")
        self.assertEqual(set(out.keys()), EXPECTED_PLAN_CONTENT_KEYS)
        self.assertEqual(out["encoding"], "")   # 失败时不给编码
        ok = plan_content_payload("/x/y.md", text="hi")
        self.assertEqual(ok["encoding"], "utf-8")


# ══════════════════════════════════════════════════════════════════
#  三、会话执行模式字段落盘与列表载荷
# ══════════════════════════════════════════════════════════════════

class TestSessionExecutionPersistence(_ManagerTest):
    def test_partial_update_semantics(self):
        """省略参数 = 不改；显式传 None = 写 None（哨兵 _UNSET 的意义）。"""
        self.sm.set_session_execution(SID, execution_mode=MODE_PLAN,
                                      plan_status=PLAN_STATUS_READY,
                                      goal_condition="条件 A")
        meta = self.sm.load_meta(SID)
        self.assertEqual(meta["execution_mode"], MODE_PLAN)
        self.assertEqual(meta["plan_status"], PLAN_STATUS_READY)
        self.assertEqual(meta["goal_condition"], "条件 A")

        # 只改 plan_status，其余字段必须原样不动
        self.sm.set_session_execution(SID, plan_status=None)
        meta = self.sm.load_meta(SID)
        self.assertIsNone(meta["plan_status"])
        self.assertEqual(meta["execution_mode"], MODE_PLAN)
        self.assertEqual(meta["goal_condition"], "条件 A")

    def test_goal_condition_can_be_cleared_explicitly(self):
        self.sm.set_session_execution(SID, goal_condition="X")
        self.sm.set_session_execution(SID, goal_condition=None)
        self.assertIsNone(self.sm.load_meta(SID)["goal_condition"])

    def test_touch_false_does_not_bump_updated_at(self):
        before = self.sm.load_meta(SID).get("updated_at")
        self.sm.set_session_execution(SID, execution_mode=MODE_PLAN)
        self.assertEqual(self.sm.load_meta(SID).get("updated_at"), before)

    def test_list_sessions_carries_exec_mode_fields(self):
        """断线重连后恢复 tag / 卡片外壳的**唯一通道**（sessions 列表载荷）。"""
        self.sm.set_session_execution(SID, execution_mode=MODE_PLAN,
                                      plan_status=PLAN_STATUS_READY)
        item = next(s for s in self.sm.list_sessions("active") if s["id"] == SID)
        self.assertEqual(item["execution_mode"], MODE_PLAN)
        self.assertEqual(item["plan_status"], PLAN_STATUS_READY)
        # plan_path **派生**（不落 meta），仅在有计划状态时给出
        self.assertEqual(item["plan_path"], str(self.sm.plan_file_for(SID)))
        self.assertIsNone(item["goal_condition"])

    def test_list_sessions_defaults_to_normal_without_plan(self):
        item = next(s for s in self.sm.list_sessions("active") if s["id"] == SID)
        self.assertEqual(item["execution_mode"], "normal")
        self.assertIsNone(item["plan_status"])
        self.assertIsNone(item["plan_path"])


# ══════════════════════════════════════════════════════════════════
#  四、级联清理（恰好两处）
# ══════════════════════════════════════════════════════════════════

class TestCascadeCleanup(_ManagerTest):
    def _make_plan_file(self) -> Path:
        p = self.sm.plan_file_for(SID)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# 计划", encoding="utf-8")
        return p

    def test_delete_session_permanent_removes_plan(self):
        p = self._make_plan_file()
        self.assertTrue(self.sm.delete_session_permanent(SID))
        self.assertFalse(p.exists())

    def test_clear_session_removes_plan(self):
        p = self._make_plan_file()
        self.sm.clear_session(self.sm.get_session_file(SID))
        self.assertFalse(p.exists())

    def test_trash_session_keeps_plan(self):
        """软删除**刻意保留**文书 —— 还原会话后计划卡片仍要能读到正文。"""
        p = self._make_plan_file()
        self.sm.trash_session(SID)
        self.assertTrue(p.exists())
        self.assertEqual(self.sm.load_meta(SID)["status"], "trashed")

    def test_clear_session_resets_exec_mode_fields(self):
        self._make_plan_file()
        self.sm.set_session_execution(SID, execution_mode=MODE_PLAN,
                                      plan_status=PLAN_STATUS_APPROVED,
                                      goal_condition="X")
        self.sm.clear_session(self.sm.get_session_file(SID))
        meta = self.sm.load_meta(SID)
        self.assertIn(meta["execution_mode"], (None, "normal"))
        self.assertIsNone(meta["plan_status"])
        self.assertIsNone(meta["goal_condition"])


# ══════════════════════════════════════════════════════════════════
#  五、plan_write 处理器与工具 schema
# ══════════════════════════════════════════════════════════════════

class TestPlanWriteHandler(_SilenceAigentLogMixin, unittest.TestCase):
    def setUp(self):
        self._silence_aigent_log()
        # 用 `__new__` 绕过 ToolRegistry 构造：本组只测 `_run_plan_write` 自身的
        # 契约（内容校验 / sink 缺失 / 委托 / 异常兜底），构造整个 registry 会去
        # 读真实 ~/.aigent 下的记忆与技能目录，属于无谓副作用。
        self.reg = ToolRegistry.__new__(ToolRegistry)
        self.reg._plan_sink = None

    def test_empty_content_rejected(self):
        for bad in ("", "   ", "\n", None, 123):
            out = self.reg._run_plan_write({"content": bad})
            self.assertIn("Error", out, msg=repr(bad))

    def test_no_sink_is_reported(self):
        out = self.reg._run_plan_write({"content": "# 计划"})
        self.assertIn("Error", out)
        self.assertIn("plan_write", out)

    def test_sink_receives_content_and_return_is_passed_through(self):
        seen = {}

        def sink(content):
            seen["content"] = content
            return "Plan written (3 chars)."

        self.reg.set_plan_sink(sink)
        out = self.reg._run_plan_write({"content": "# 计划"})
        self.assertEqual(out, "Plan written (3 chars).")
        self.assertEqual(seen["content"], "# 计划")

    def test_sink_exception_is_swallowed_to_error_string(self):
        """工具层铁律：**恒返回字符串、绝不向上抛**。"""
        def boom(content):
            raise RuntimeError("disk full")

        self.reg.set_plan_sink(boom)
        out = self.reg._run_plan_write({"content": "# x"})
        self.assertIsInstance(out, str)
        self.assertIn("Error", out)
        self.assertIn("disk full", out)


def _plain(node):
    """ast 字面量 → Python 值（只处理 dict/list/tuple/常量）。"""
    if isinstance(node, ast.Dict):
        return {_plain(k): _plain(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_plain(e) for e in node.elts]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.JoinedStr):     # f-string（description 里可能有）
        return "<fstring>"
    return ast.dump(node)


class TestPlanWriteToolSchema(unittest.TestCase):
    """落点**不可被模型指定** —— schema 里只有 content，没有 path。"""

    def _plan_write_schema(self) -> dict:
        tree = ast.parse((AGENTS_DIR / "tools.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            plain = _plain(node)
            if isinstance(plain, dict) and plain.get("name") == "plan_write":
                return plain
        self.fail("tools.py 里找不到 plan_write 的工具定义字典")

    def test_schema_has_content_only(self):
        schema = self._plan_write_schema()
        self.assertIn("parameters", schema)
        props = schema["parameters"].get("properties") or {}
        self.assertEqual(set(props.keys()), {"content"})
        self.assertEqual(schema["parameters"].get("required"), ["content"])

    def test_no_path_parameter(self):
        schema = self._plan_write_schema()
        self.assertNotIn("path", schema["parameters"].get("properties") or {})
        self.assertNotIn("path", schema["parameters"].get("required") or [])


if __name__ == "__main__":
    unittest.main()
