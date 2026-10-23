#!/usr/bin/env python3
"""计划文书的落点、读取与生命周期守护测试 —— docs/frontend/22。

**2026-09-29 改版**：文书从"元数据目录 + sid 命名"
（`~/.aigent/projects/<id>/plans/session_<sid>.md`）搬到
**工作空间内 + 模型命名**（`<工作空间>/.aiagent/plan/<name>.md`）。
本文件随之重写，守护五件事：

1. **路径口径唯一**：`paths.plan_dir_for` / `plan_filename` / `plan_relpath` /
   `resolve_plan_path` 是唯一实现。名字来自模型 → 必须清洗（目录分隔符、保留字符、
   `..`、空名回退），且**同名不撞车**（别人那一份绝不覆盖）。
2. **读取降级层次**：`execution_mode.read_plan_file` 的 存在 / 不存在 / 非文件 /
   超限 四种结果互不混淆，且**绝不抛异常**（前端据此渲染不同状态）。
3. **回执形状**与既有 `file_content` 同族 —— 前端不必写第二套解析。
4. **生命周期变了**：会话删除 / 清空**不再删文书文件**（它此时是工作区里的项目
   文件），但 meta 的执行模式字段必须归零（否则留下"有计划状态、没有上下文"的僵尸壳）。
5. **目录不可被模型指定**：`plan_write` 的 `name` 只定文件名，schema 里**没有 path**；
   处理器把 (content, name) 委托给 Agent 注入的闭包（闭包拿 workspace/session_id，
   registry 拿不到）。

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
from paths import (  # noqa: E402
    PLAN_DIR_PARTS,
    plan_dir_for,
    plan_file_for_session,
    plan_filename,
    plan_relpath,
    resolve_plan_path,
)
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
#  一、路径口径（2026-09-29：工作空间内 + 模型命名）
# ══════════════════════════════════════════════════════════════════

class TestPlanPath(_ManagerTest):
    def test_plan_dir_is_inside_workspace(self):
        """文书目录在工作空间里（`.aiagent/plan/`），不再进元数据目录。"""
        self.assertEqual(plan_dir_for(self.root), self.root.joinpath(*PLAN_DIR_PARTS))
        self.assertEqual(plan_dir_for(self.root).parts[-2:], (".aiagent", "plan"))

    def test_relpath_is_posix_and_relative(self):
        self.assertEqual(plan_relpath("重构方案"), ".aiagent/plan/重构方案.md")
        # 交叉验证：目录前缀与 PLAN_DIR_PARTS 同源
        self.assertTrue(plan_relpath("x").startswith("/".join(PLAN_DIR_PARTS) + "/"))

    def test_workspace_paths_exposes_plan_dir(self):
        """`WorkspacePaths.plan_dir` 与 `plan_dir_for(workdir)` 必须同源。"""
        from paths import WorkspacePaths
        p = WorkspacePaths("pid", self.root / "data", self.root / "ws")
        self.assertEqual(p.plan_dir, plan_dir_for(self.root / "ws"))

    def test_legacy_derivation_still_available_for_existing_sessions(self):
        """旧口径（`<plans_dir>/session_<sid>.md`）保留 —— 升级前的会话靠它回退读取。"""
        p = plan_file_for_session(SID, "session_", self.root / "plans")
        self.assertEqual(p, self.root / "plans" / f"session_{SID}.md")
        self.assertEqual(self.sm.plan_file_for(SID), self.root / "plans" / f"session_{SID}.md")


class TestPlanFilenameSanitizing(unittest.TestCase):
    """名字来自模型 → 这里是唯一一道清洗。安全默认：清洗后永远有名字可用。"""

    def test_plain_name_gets_md_suffix(self):
        self.assertEqual(plan_filename("重构方案"), "重构方案.md")
        self.assertEqual(plan_filename("plan-1"), "plan-1.md")

    def test_md_suffix_not_doubled(self):
        self.assertEqual(plan_filename("x.md"), "x.md")
        self.assertEqual(plan_filename("x.MD"), "x.md")

    def test_directory_parts_are_stripped(self):
        """模型只能定名字、定不了目录（这是"落点不可指定"的落地处）。"""
        for raw in ("../evil", "a/b/c", "/etc/passwd", "C:\\Windows\\x",
                    "....//x", "sub/../../x"):
            out = plan_filename(raw)
            self.assertNotIn("/", out, msg=raw)
            self.assertNotIn("\\", out, msg=raw)
            self.assertFalse(out.startswith("."), msg=raw)

    def test_reserved_and_control_chars_replaced(self):
        self.assertEqual(plan_filename('a:b*c?d"e<f>g|h'), "a-b-c-d-e-f-g-h.md")
        self.assertEqual(plan_filename("a\x00b\nc"), "a-b-c.md")

    def test_blank_and_dot_names_fall_back(self):
        for raw in (None, "", "   ", "..", ".", "...", "/", "///", "   ..   "):
            self.assertEqual(plan_filename(raw), "plan.md", msg=repr(raw))

    def test_overlong_name_is_truncated(self):
        out = plan_filename("长" * 200)
        self.assertLessEqual(len(out), 60 + 3)      # PLAN_NAME_MAX + ".md"
        self.assertTrue(out.endswith(".md"))

    def test_uniqueness_prefix_comes_from_last_segment(self):
        self.assertEqual(plan_filename("a/b"), "b.md")

    def test_tampered_meta_name_is_neutralised_not_escaped(self):
        """**手改 meta** 也不能把读取引到工作空间外（联调实测过的那条）。

        `plan_filename` 先把名字清洗成纯文件名，于是 `plan_relpath` 拼出来的
        路径恒在 `.aiagent/plan/` 内；`ws_bridge.plan_read` 的 `resolve_within`
        只是第二道兜底。这里断言第一道就够了 —— 因为它是**唯一**会被模型/meta
        影响的那一段。
        """
        for raw in ("../../../../etc/hosts", "/etc/passwd", "~/.ssh/id_rsa",
                    "..\\..\\Windows\\system32"):
            rel = plan_relpath(raw)
            self.assertTrue(rel.startswith(".aiagent/plan/"), rel)
            self.assertNotIn("..", rel, rel)
            self.assertEqual(rel.count("/"), 2, rel)      # 只有 .aiagent/plan/ 两层


class TestResolvePlanPath(_ManagerTest):
    """同名不撞车：两个会话都可能取到同一个"重构方案"。"""

    def setUp(self):
        super().setUp()
        self.plan_dir = plan_dir_for(self.root)
        self.plan_dir.mkdir(parents=True, exist_ok=True)

    def test_free_name_used_as_is(self):
        self.assertEqual(resolve_plan_path(self.plan_dir, "重构方案"),
                         self.plan_dir / "重构方案.md")

    def test_previous_name_is_overwritten(self):
        """本会话上一版那一份**允许覆盖**（单份覆盖语义）。"""
        prev = self.plan_dir / "重构方案.md"
        prev.write_text("v1", encoding="utf-8")
        self.assertEqual(
            resolve_plan_path(self.plan_dir, "重构方案", "重构方案.md"), prev)

    def test_other_sessions_doc_is_never_clobbered(self):
        """别人的文书绝不覆盖 —— 改成 `-2` / `-3`。"""
        taken = self.plan_dir / "重构方案.md"
        taken.write_text("别人的", encoding="utf-8")
        got = resolve_plan_path(self.plan_dir, "重构方案", "另一个名字.md")
        self.assertEqual(got, self.plan_dir / "重构方案-2.md")
        self.assertEqual(taken.read_text(encoding="utf-8"), "别人的")

    def test_skips_until_free(self):
        for n in ("重构方案.md", "重构方案-2.md", "重构方案-3.md"):
            (self.plan_dir / n).write_text("x", encoding="utf-8")
        self.assertEqual(resolve_plan_path(self.plan_dir, "重构方案"),
                         self.plan_dir / "重构方案-4.md")


# ══════════════════════════════════════════════════════════════════
#  二、读取与降级层次
# ══════════════════════════════════════════════════════════════════

class TestReadPlanFile(_ManagerTest):
    """`read_plan_file` 是**纯路径**函数（与落点无关），故用临时目录验。"""

    def _path(self, name: str = "重构方案.md") -> Path:
        p = plan_dir_for(self.root) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def test_success(self):
        p = self._path()
        p.write_text("# 计划\n\n1. 做 A\n2. 做 B\n", encoding="utf-8")
        out = read_plan_file(p)
        self.assertEqual(out["reason"], "")
        self.assertEqual(out["name"], "重构方案.md")
        self.assertGreater(out["size"], 0)
        self.assertEqual(out["lines"], 5)
        self.assertIn("做 A", out["text"])
        self.assertEqual(out["encoding"], "utf-8")

    def test_missing_file_gives_reason(self):
        out = read_plan_file(self._path("never-written.md"))
        self.assertTrue(out["reason"])
        self.assertEqual(out["text"], "")
        self.assertFalse(out["too_large"])

    def test_directory_gives_reason(self):
        p = self._path("adir.md")
        p.mkdir(parents=True, exist_ok=True)
        out = read_plan_file(p)
        self.assertTrue(out["reason"])
        self.assertFalse(out["too_large"])

    def test_too_large_reads_nothing(self):
        p = self._path()
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
                                      plan_name="重构方案.md",
                                      goal_condition="条件 A")
        meta = self.sm.load_meta(SID)
        self.assertEqual(meta["execution_mode"], MODE_PLAN)
        self.assertEqual(meta["plan_status"], PLAN_STATUS_READY)
        self.assertEqual(meta["plan_name"], "重构方案.md")
        self.assertEqual(meta["goal_condition"], "条件 A")

        # 只改 plan_status，其余字段必须原样不动
        self.sm.set_session_execution(SID, plan_status=None)
        meta = self.sm.load_meta(SID)
        self.assertIsNone(meta["plan_status"])
        self.assertEqual(meta["execution_mode"], MODE_PLAN)
        self.assertEqual(meta["plan_name"], "重构方案.md")
        self.assertEqual(meta["goal_condition"], "条件 A")

    def test_plan_name_can_be_cleared_explicitly(self):
        self.sm.set_session_execution(SID, plan_name="x.md")
        self.sm.set_session_execution(SID, plan_name=None)
        self.assertIsNone(self.sm.load_meta(SID)["plan_name"])

    def test_goal_condition_can_be_cleared_explicitly(self):
        self.sm.set_session_execution(SID, goal_condition="X")
        self.sm.set_session_execution(SID, goal_condition=None)
        self.assertIsNone(self.sm.load_meta(SID)["goal_condition"])

    def test_touch_false_does_not_bump_updated_at(self):
        before = self.sm.load_meta(SID).get("updated_at")
        self.sm.set_session_execution(SID, execution_mode=MODE_PLAN)
        self.assertEqual(self.sm.load_meta(SID).get("updated_at"), before)

    def test_list_sessions_carries_exec_mode_fields(self):
        """断线重连后恢复 tag / 卡片外壳的**唯一通道**（sessions 列表载荷）。

        `plan_path` 是**相对工作空间**的路径（由 meta 的 `plan_name` 拼出）——
        右栏标签与 `file_read` 都用这个口径，所以列表载荷必须给同一种形态。
        """
        self.sm.set_session_execution(SID, execution_mode=MODE_PLAN,
                                      plan_status=PLAN_STATUS_READY,
                                      plan_name="重构方案.md")
        item = next(s for s in self.sm.list_sessions("active") if s["id"] == SID)
        self.assertEqual(item["execution_mode"], MODE_PLAN)
        self.assertEqual(item["plan_status"], PLAN_STATUS_READY)
        self.assertEqual(item["plan_path"], ".aiagent/plan/重构方案.md")
        self.assertIsNone(item["goal_condition"])

    def test_list_sessions_falls_back_for_legacy_sessions(self):
        """存量会话（有 plan_status、无 plan_name）→ 旧的元数据目录绝对路径。"""
        self.sm.set_session_execution(SID, plan_status=PLAN_STATUS_READY)
        item = next(s for s in self.sm.list_sessions("active") if s["id"] == SID)
        self.assertEqual(item["plan_path"], str(self.sm.plan_file_for(SID)))

    def test_list_sessions_defaults_to_normal_without_plan(self):
        item = next(s for s in self.sm.list_sessions("active") if s["id"] == SID)
        self.assertEqual(item["execution_mode"], "normal")
        self.assertIsNone(item["plan_status"])
        self.assertIsNone(item["plan_path"])


# ══════════════════════════════════════════════════════════════════
#  四、生命周期（2026-09-29 改：文书文件**不再**随会话删除）
# ══════════════════════════════════════════════════════════════════

class TestPlanArtifactLifecycle(_ManagerTest):
    """文书现在落在工作空间里 —— 它是**用户的项目文件**，删会话不该删它。

    meta 的执行模式字段反而必须归零：否则会留下"有计划状态、没有上下文"的僵尸壳。
    """

    def _make_plan_file(self) -> Path:
        p = plan_dir_for(self.root) / "重构方案.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# 计划", encoding="utf-8")
        return p

    def _arm(self) -> None:
        self.sm.set_session_execution(SID, execution_mode=MODE_PLAN,
                                      plan_status=PLAN_STATUS_READY,
                                      plan_name="重构方案.md")

    def test_delete_session_permanent_keeps_workspace_doc(self):
        p = self._make_plan_file()
        self._arm()
        self.assertTrue(self.sm.delete_session_permanent(SID))
        self.assertTrue(p.exists(), "工作区里的计划文档不该被删会话带走")

    def test_clear_session_keeps_workspace_doc(self):
        p = self._make_plan_file()
        self._arm()
        self.sm.clear_session(self.sm.get_session_file(SID))
        self.assertTrue(p.exists(), "清空会话内容不该删用户的项目文件")

    def test_clear_session_still_resets_exec_mode_fields(self):
        """文件留下，但状态必须归零（含新增的 plan_name）。"""
        self._make_plan_file()
        self._arm()
        self.sm.set_session_execution(SID, goal_condition="X")
        self.sm.clear_session(self.sm.get_session_file(SID))
        meta = self.sm.load_meta(SID)
        self.assertIn(meta["execution_mode"], (None, "normal"))
        self.assertIsNone(meta["plan_status"])
        self.assertIsNone(meta["plan_name"])
        self.assertIsNone(meta["goal_condition"])

    def test_trash_session_keeps_plan(self):
        """软删除（归档）同样保留 —— 还原后计划卡片仍要能读到正文。"""
        p = self._make_plan_file()
        self._arm()
        self.sm.trash_session(SID)
        self.assertTrue(p.exists())
        self.assertEqual(self.sm.load_meta(SID)["status"], "trashed")


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

    def test_sink_receives_content_and_name(self):
        seen = {}

        def sink(content, name=""):
            seen["content"] = content
            seen["name"] = name
            return "Plan written (3 chars)."

        self.reg.set_plan_sink(sink)
        out = self.reg._run_plan_write({"content": "# 计划", "name": "重构方案"})
        self.assertEqual(out, "Plan written (3 chars).")
        self.assertEqual(seen["content"], "# 计划")
        self.assertEqual(seen["name"], "重构方案")

    def test_missing_or_bad_name_becomes_empty_string(self):
        """名字**只做类型兜底**：真清洗在 `paths.plan_filename`，此处不重复规则。"""
        seen = {}

        def sink(content, name=""):
            seen["name"] = name
            return "ok"

        self.reg.set_plan_sink(sink)
        for bad in (None, 123, ["x"], {"a": 1}):
            self.reg._run_plan_write({"content": "# x", "name": bad})
            self.assertEqual(seen["name"], "", msg=repr(bad))
        self.reg._run_plan_write({"content": "# x"})       # 缺省也要可用
        self.assertEqual(seen["name"], "")

    def test_sink_exception_is_swallowed_to_error_string(self):
        """工具层铁律：**恒返回字符串、绝不向上抛**。"""
        def boom(content, name=""):
            raise RuntimeError("disk full")

        self.reg.set_plan_sink(boom)
        out = self.reg._run_plan_write({"content": "# x"})
        self.assertIsInstance(out, str)
        self.assertIn("Error", out)
        self.assertIn("disk full", out)


# ══════════════════════════════════════════════════════════════════
#  六、落盘闭包（Agent 侧）：落点、命名、覆盖与信封
# ══════════════════════════════════════════════════════════════════

class TestPlanWriteClosure(_SilenceAigentLogMixin, unittest.TestCase):
    """`Agent._bind_plan_sink` 的闭包 —— 唯一真正"写文件"的地方。

    用 `Agent.__new__(Agent)` + 手填字段的离线桩（项目既有范式）：构造整个 Agent
    会去读真实 `~/.aigent`（注册表 / 记忆 / 技能），与本次断言无关。
    桩里 `session_manager=None` / `execution_mode_sink=None` 是**刻意的**：
    前者让 `_persist_execution` 走"未接入会话"的短路返回，后者让信封推送静默跳过
    —— 两条都必须在闭包里被容忍（CLI / 单测路径）。
    """

    def setUp(self):
        self._silence_aigent_log()
        from agent_full_v2 import Agent
        from execution_mode import ExecutionGate
        from paths import WorkspacePaths

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()
        self.ws = self.root / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)

        agent = Agent.__new__(Agent)
        agent.session_id = SID
        agent.session_prefix = "session_"
        agent.workspace = WorkspacePaths("pid", self.root / "data", self.ws)
        agent.execution_gate = ExecutionGate()
        agent.session_manager = None
        agent.execution_mode_sink = None
        # `execution_state()` 会读它（`plan_path` 的存量回退分支还会用 workspace）——
        # 桩给 `active=None` 即"没有目标"，与构造后的初值同形。
        agent.goal_controller = mock.Mock(active=None)
        agent.tools = ToolRegistry.__new__(ToolRegistry)
        agent.tools._plan_sink = None
        agent._bind_plan_sink()
        self.agent = agent

    def _write(self, content: str, name: str = "") -> str:
        return self.agent.tools._plan_sink(content, name)

    def test_writes_inside_workspace_and_marks_ready(self):
        out = self._write("# 计划\n", "重构方案")
        path = self.ws / ".aiagent" / "plan" / "重构方案.md"
        self.assertTrue(path.is_file(), "文书必须落在 <工作空间>/.aiagent/plan/ 下")
        self.assertEqual(path.read_text(encoding="utf-8"), "# 计划\n")
        self.assertIn(".aiagent/plan/重构方案.md", out)   # 回执提到相对路径
        self.assertEqual(self.agent.execution_gate.plan_status, PLAN_STATUS_READY)
        self.assertEqual(self.agent.execution_gate.plan_name, "重构方案.md")

    def test_directory_cannot_be_escaped_by_name(self):
        """模型给的名字里带目录 → 只取最后一段，绝不越出 plan/。"""
        self._write("# 计划\n", "../../escape")
        self.assertFalse((self.ws / "escape.md").exists())
        self.assertFalse((self.root / "escape.md").exists())
        self.assertTrue((self.ws / ".aiagent" / "plan" / "escape.md").is_file())

    def test_same_session_rewrite_overwrites_and_drops_old_name(self):
        """同一会话换名重新产出 → 覆盖语义 + 清掉上一版（不留没人认领的死文件）。"""
        self._write("v1", "旧方案")
        old = self.ws / ".aiagent" / "plan" / "旧方案.md"
        self.assertTrue(old.is_file())
        self._write("v2", "新方案")
        new = self.ws / ".aiagent" / "plan" / "新方案.md"
        self.assertEqual(new.read_text(encoding="utf-8"), "v2")
        self.assertFalse(old.exists(), "换名重规划后上一版应被清掉")
        self.assertEqual(self.agent.execution_gate.plan_name, "新方案.md")

    def test_same_name_is_reused_without_suffix(self):
        """同名重规划 = 覆盖自己那一份，**不**生出 `-2`。"""
        self._write("v1", "方案")
        self._write("v2", "方案")
        p = self.ws / ".aiagent" / "plan" / "方案.md"
        self.assertEqual(p.read_text(encoding="utf-8"), "v2")
        self.assertFalse((self.ws / ".aiagent" / "plan" / "方案-2.md").exists())

    def test_blank_name_falls_back(self):
        self._write("# x", "")
        self.assertTrue((self.ws / ".aiagent" / "plan" / "plan.md").is_file())

    def test_missing_session_is_reported_not_raised(self):
        self.agent.session_id = None
        out = self._write("# x", "方案")
        self.assertIsInstance(out, str)
        self.assertIn("Error", out)


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
    """**目录不可被模型指定** —— schema 里只有 content / name，没有 path。

    `name` 是 2026-09-29 新增的：文件名由模型来取（"计划方案文档名字大模型来定"），
    但它只影响**文件名**，目录恒为 `<工作空间>/.aiagent/plan/`
    （清洗见 `paths.plan_filename`，测试见 `TestPlanFilenameSanitizing`）。
    """

    def _plan_write_schema(self) -> dict:
        tree = ast.parse((AGENTS_DIR / "tools.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            plain = _plain(node)
            if isinstance(plain, dict) and plain.get("name") == "plan_write":
                return plain
        self.fail("tools.py 里找不到 plan_write 的工具定义字典")

    def test_schema_properties(self):
        schema = self._plan_write_schema()
        self.assertIn("parameters", schema)
        props = schema["parameters"].get("properties") or {}
        self.assertEqual(set(props.keys()), {"content", "name"})
        # content 必填；name **可选**（缺省由后端回退一个名字，绝不因此失败）
        self.assertEqual(schema["parameters"].get("required"), ["content"])

    def test_no_path_parameter(self):
        schema = self._plan_write_schema()
        props = schema["parameters"].get("properties") or {}
        self.assertNotIn("path", props)
        self.assertNotIn("dir", props)
        self.assertNotIn("path", schema["parameters"].get("required") or [])


if __name__ == "__main__":
    unittest.main()
