#!/usr/bin/env python3
"""计划模式守卫钩子（`agents/hooks.py::plan_guard_hook`）守护测试 —— 2026-09-25。

守护四件事（docs/frontend/22 §5.10 / §5.11）：

1. **顺序即语义**：PreToolUse 内 `plan_guard_hook` 必须排在 `permission_hook`
   之前 —— 模式级限制给出的理由（"改用 plan_write"）比单条规则更可行动，
   且 plan 拦下的集合本就是权限链 allow 集合的子集，不会掩盖硬拒绝语义。
2. **判定矩阵**：plan 激活且文书未批准时，写工具阻断、只读工具放行、bash 条件放行。
3. **不变量**：`full_access` + plan 组合下写操作**仍被拦** —— 权限档位免的是
   "审批"，不是"计划模式的写禁令"。
4. **store 接线**：plan 守卫复用权限门的 store（同一份 permissions.json 热加载
   缓存）叠加 deny / dangerous 模式，否则会破坏上述子集不变量。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from execution_mode import ExecutionGate, PLAN_WRITE_TOOL  # noqa: E402
from hooks import HookSystem  # noqa: E402
from permission import (  # noqa: E402
    MODE_FULL_ACCESS,
    PermissionGate,
    PermissionStore,
)


@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _Call:
    id: str
    function: _Fn


def _call(tool: str, **args) -> _Call:
    return _Call("toolu_1", _Fn(tool, json.dumps(args)))


class _HookTest(unittest.TestCase):
    """临时工作区 + 独立 store + 已注入两个门的 HookSystem。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()
        self.workdir = self.root / "ws"
        self.workdir.mkdir()

        self.store = PermissionStore(path=self.root / "permissions.json")
        self.permission_gate = PermissionGate(
            self.workdir, silent=True, store=self.store)

        self.hooks = HookSystem(silent=True, workdir=self.workdir)
        self.hooks.set_permission_gate(self.permission_gate)
        self.gate = ExecutionGate()
        self.hooks.set_execution_gate(self.gate)
        self.hooks.register_default_hooks()

    def _pre(self, call: _Call):
        """走完整 PreToolUse 链路（plan 守卫 → 权限 → 日志）。"""
        return self.hooks.trigger(HookSystem.PRE_TOOL_USE, call)

    def _guard(self, call: _Call):
        """只调 plan 守卫本身。

        需要**隔离** plan 语义时必须用它 —— 权限链在 default 模式下同样会拦下
        `rm -rf /` 这类命令，走 `trigger` 无法区分"被谁拦的"。
        """
        return self.hooks.plan_guard_hook(call)

    def _in_plan(self):
        self.gate.set_plan_mode()


# ══════════════════════════════════════════════════════════════════
#  一、注册顺序
# ══════════════════════════════════════════════════════════════════

class TestHookOrder(unittest.TestCase):
    def test_plan_guard_registered_before_permission(self):
        h = HookSystem(silent=True)
        h.register_default_hooks()
        names = [getattr(cb, "__name__", "") for cb in h._hooks[h.PRE_TOOL_USE]]
        self.assertEqual(names[:3], ["plan_guard_hook", "permission_hook", "log_hook"],
                         msg=f"PreToolUse 顺序错误: {names}")


# ══════════════════════════════════════════════════════════════════
#  二、判定矩阵
# ══════════════════════════════════════════════════════════════════

class TestPlanGuardMatrix(_HookTest):
    def test_write_tools_blocked(self):
        self._in_plan()
        for name, args in (
            ("run_write", {"path": "a.txt", "content": "x"}),
            ("run_edit", {"path": "a.txt", "old": "a", "new": "b"}),
            ("write_memory", {"type": "project", "content": "x"}),
            ("create_task", {"title": "t"}),
            ("spawn_teammate", {"name": "n", "prompt": "p"}),
            ("schedule_cron", {"prompt": "p", "rrule": "FREQ=DAILY"}),
        ):
            out = self._guard(_call(name, **args))
            self.assertIsNotNone(out, msg=name)
            self.assertIn(PLAN_WRITE_TOOL, out, msg=f"{name} 的阻断文案必须可行动")

    def test_readonly_tools_allowed(self):
        self._in_plan()
        for name in ("run_read", "run_glob", "ask_user", "load_skill",
                     "list_skills", "list_tasks", "check_background"):
            self.assertIsNone(self._guard(_call(name)), msg=name)

    def test_plan_write_allowed(self):
        self._in_plan()
        self.assertIsNone(self._guard(_call(PLAN_WRITE_TOOL, content="# 计划")))

    def test_bash_conditionally_allowed(self):
        self._in_plan()
        self.assertIsNone(self._guard(_call("bash", command="git status")))
        self.assertIsNone(self._guard(_call("bash", command="ls -la")))
        self.assertIsNotNone(self._guard(_call("bash", command="rm -rf /")))
        self.assertIsNotNone(self._guard(_call("bash", command="cat a > b")))

    # ── P0-3 的四个反例 ────────────────────────────────────────────
    def test_p0_3_counterexamples_blocked(self):
        """这四条在**权限链**里全数命中安全白名单（find / env / git branch /
        echo 都是"够安全可免审批"的），只有 plan 的窄只读判定能拦住。"""
        not_in_plan = [
            "find . -exec rm -rf {} +",
            "env X=1 rm -rf /",
            "git branch -D main",
            "echo \"$(rm -rf x)\"",
        ]
        for cmd in not_in_plan:
            self.assertIsNone(self._guard(_call("bash", command=cmd)),
                              msg=f"非 plan 时不该由守卫拦下: {cmd}")
        self._in_plan()
        for cmd in not_in_plan:
            self.assertIsNotNone(self._guard(_call("bash", command=cmd)), msg=cmd)

    def test_p0_3_counterexamples_survive_full_chain(self):
        """走完整链路（plan 守卫 + 权限）也必须是阻断。"""
        self._in_plan()
        for cmd in (
            "find . -exec rm -rf {} +",
            "env X=1 rm -rf /",
            "git branch -D main",
            "echo \"$(rm -rf x)\"",
        ):
            self.assertIsNotNone(self._pre(_call("bash", command=cmd)), msg=cmd)

    def test_not_in_plan_allows_everything(self):
        for name, args in (
            ("run_write", {"path": "a.txt", "content": "x"}),
            ("bash", {"command": "find . -exec rm -rf {} +"}),
            ("create_task", {"title": "t"}),
            ("spawn_teammate", {"name": "n", "prompt": "p"}),
        ):
            self.assertIsNone(self._guard(_call(name, **args)), msg=name)

    def test_approved_plan_unblocks_writes(self):
        self._in_plan()
        self.gate.mark_plan_ready("/tmp/a.md")
        self.gate.approve_plan()
        # 文书已批准 → plan 的写禁令解除（随后由权限链接管，与改造前一致）
        self.assertIsNone(self._guard(_call("run_write", path="a", content="x")))
        self.assertIsNone(self._guard(_call("bash", command="ls -la")))


# ══════════════════════════════════════════════════════════════════
#  三、与权限档位正交（full_access 不免 plan 的写禁令）
# ══════════════════════════════════════════════════════════════════

class TestPlanBeatsFullAccess(_HookTest):
    def test_full_access_still_blocked_by_plan(self):
        self.permission_gate.set_mode(MODE_FULL_ACCESS)
        # 前提：full_access 下权限链本身放行 run_write（免审批）
        self.assertIsNone(
            self.hooks.permission_hook(_call("run_write", path="a.txt", content="x")),
            msg="full_access 下权限链应放行 run_write（否则本用例前提不成立）")
        # 但 plan 激活时守卫仍然拦下 —— 两条轴独立
        self._in_plan()
        self.assertIsNotNone(
            self._guard(_call("run_write", path="a.txt", content="x")))


# ══════════════════════════════════════════════════════════════════
#  四、store 接线与 fail-closed
# ══════════════════════════════════════════════════════════════════

class TestStorePlumbingAndFailClosed(_HookTest):
    def test_user_deny_pattern_blocks_in_plan(self):
        """plan 守卫必须叠加用户 deny_patterns，否则会破坏子集不变量。"""
        self._in_plan()
        self.assertIsNone(self._guard(_call("bash", command="git status")),
                          msg="前提：plan 本应放行 git status")
        self.store.save_reporting({"deny_patterns": ["git"]})
        self.assertIsNotNone(self._guard(_call("bash", command="git status")),
                             msg="store 的 deny_patterns 未被 plan 守卫读取")

    def test_parse_failure_is_fail_closed_in_plan(self):
        self._in_plan()
        bad = _Call("toolu_1", _Fn("run_write", "{not json"))
        self.assertIsNotNone(self._guard(bad))

    def test_no_execution_gate_allows(self):
        """未注入执行模式门（子智能体兜底 / 单测直连）→ 放行，行为与改造前一致。"""
        h = HookSystem(silent=True, workdir=self.workdir)
        h.set_permission_gate(self.permission_gate)
        self.assertIsNone(h.plan_guard_hook(_call("run_write", path="a")))

    def test_non_plan_does_not_parse_args(self):
        """非 plan 模式零开销放行：即使 arguments 是坏 JSON 也不该被拦。"""
        bad = _Call("toolu_1", _Fn("run_write", "{not json"))
        self.assertIsNone(self._guard(bad))


if __name__ == "__main__":
    unittest.main()
