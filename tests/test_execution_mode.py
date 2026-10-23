#!/usr/bin/env python3
"""任务执行模式（`agents/execution_mode.py`）守护测试 —— 2026-09-25，docs/frontend/22。

覆盖三层：

1. `bash_is_read_only()` —— plan 专用的**窄只读判定**。这是 P0-3 的落点：权限链的
   安全白名单语义是「够安全可免审批」而不是「只读」，直接复用它会把
   `find . -exec rm -rf {} +` / `env X=1 rm -rf /` / `git branch -D main` /
   `echo "$(rm -rf x)"` 全部放行。这里逐条钉住否决位，并断言**不变量**
   「plan 放行集合 ⊂ 权限链 allow 集合」。
2. `ExecutionGate` 状态机（plan 的**真源**）与 `blocked_reason()` 判定矩阵。
3. `permission.py` 的**零依赖**：execution_mode 复用原语但不 import 策略数据，
   且 permission 模块不许反向 import execution_mode（两条轴不合并判定链）。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from execution_mode import (  # noqa: E402
    MODE_GOAL,
    MODE_NORMAL,
    MODE_PLAN,
    PLAN_BASH_READONLY_HEADS,
    PLAN_BASH_DENY_TOKENS,
    PLAN_BASH_GIT_BRANCH_SAFE_FLAGS,
    PLAN_BASH_GIT_SUBCOMMANDS,
    PLAN_READONLY_TOOLS,
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_READY,
    PLAN_WRITE_TOOL,
    ExecutionGate,
    bash_is_read_only,
    block_message,
    split_command_segments,
    tokenize,
)
from permission import (  # noqa: E402
    BUILTIN_DANGEROUS,
    BUILTIN_DENY,
    BUILTIN_SAFE_COMMANDS,
    cross_pattern_matches,
    pattern_matches,
)


# ══════════════════════════════════════════════════════════════════
#  一、bash 窄只读判定
# ══════════════════════════════════════════════════════════════════

class TestBashIsReadOnly(unittest.TestCase):
    def test_simple_readonly_commands_allowed(self):
        for cmd in (
            "ls", "ls -la", "pwd", "cat notes.md", "head -20 a.py",
            "tail -f x", "wc -l a.txt", "grep -i foo bar.txt", "rg TODO",
            "find . -name '*.py'", "which python3", "file a.bin",
            "stat a.txt", "du -sh .", "df -h", "echo hello", "date",
            "date +%s", "git status", "git diff", "git log --oneline -5",
            "git show HEAD", "git branch", "git branch -a",
            "git branch -vv", "env",
        ):
            self.assertTrue(bash_is_read_only(cmd), msg=cmd)

    def test_pipeline_and_chain_of_readonly_allowed(self):
        self.assertTrue(bash_is_read_only("cat a.txt | grep -i foo | wc -l"))
        self.assertTrue(bash_is_read_only("ls && git status"))
        self.assertTrue(bash_is_read_only("ls; pwd"))

    # ── P0-3 的四个反例（评审钉死的必测项）──────────────────────────
    def test_p0_3_counterexamples_blocked(self):
        for cmd in (
            "find . -exec rm -rf {} +",   # find 是安全白名单头，但 -exec 是执行位
            "env X=1 rm -rf /",           # env 是启动器语义，会换掉实际二进制
            "git branch -D main",         # git branch 是白名单，但 -D 删分支
            "echo \"$(rm -rf x)\"",       # 命令替换以 echo 为头绕过
        ):
            self.assertFalse(bash_is_read_only(cmd), msg=cmd)

    def test_redirection_and_substitution_blocked(self):
        for cmd in (
            "cat x > /etc/passwd", "echo a >> f", "cat <<EOF",
            "echo `id`", "echo $(id)", "ls > out.txt",
        ):
            self.assertFalse(bash_is_read_only(cmd), msg=cmd)

    def test_write_channel_tokens_blocked(self):
        for cmd in (
            "ls | tee out.txt", "dd if=/dev/zero of=f", "ls | xargs rm",
            "find . -delete", "find . -fprint list", "rg --pre 'sh -c x'",
            "git diff --output=patch.diff", "truncate -s 0 a.txt",
        ):
            self.assertFalse(bash_is_read_only(cmd), msg=cmd)

    def test_per_head_deny_blocked(self):
        # `date -s` 改系统时钟；但 `date +%s` 是只读（上面已断言）
        self.assertFalse(bash_is_read_only("date -s '2020-01-01'"))
        self.assertFalse(bash_is_read_only("date --set=now"))

    def test_git_subcommand_whitelist(self):
        for sub in PLAN_BASH_GIT_SUBCOMMANDS:
            ok = bash_is_read_only(f"git {sub}")
            self.assertTrue(ok, msg=f"git {sub}")
        for bad in ("git commit -m x", "git push", "git checkout .",
                    "git reset --hard", "git clean -fd", "git stash",
                    "git add .", "git tag v1", "git remote add o url"):
            self.assertFalse(bash_is_read_only(bad), msg=bad)

    def test_git_branch_extra_tokens(self):
        # 只允许安全开关；`git branch foo` 会**创建分支**
        for flag in sorted(PLAN_BASH_GIT_BRANCH_SAFE_FLAGS):
            self.assertTrue(bash_is_read_only(f"git branch {flag}"), msg=flag)
        for bad in ("git branch new-branch", "git branch -d old",
                    "git branch -m a b", "git branch --edit-description"):
            self.assertFalse(bash_is_read_only(bad), msg=bad)

    def test_env_only_bare(self):
        self.assertTrue(bash_is_read_only("env"))
        for bad in ("env -i ls", "env PATH=/tmp/evil ls", "env X=1 ls"):
            self.assertFalse(bash_is_read_only(bad), msg=bad)

    def test_non_whitelisted_head_blocked(self):
        for cmd in ("rm -rf /", "sed -i s/a/b/ f", "python -c print(1)",
                    "curl http://x", "ssh host", "nc -l 1234",
                    "touch newfile", "mkdir d", "chmod 777 f"):
            self.assertFalse(bash_is_read_only(cmd), msg=cmd)

    def test_case_insensitive_readonly_switches_not_blocked(self):
        """`-i` / `-o` 是**放行头的只读开关**，刻意不做全局否决位。

        它们的"写"语义只对 `sed -i` / `curl -o` 这类**不在白名单内**的命令头成立，
        head 检查已先拦下；对 `grep -i`（最常用的探索方式）拦它只会误伤。
        """
        self.assertTrue(bash_is_read_only("grep -i foo a.txt"))
        self.assertTrue(bash_is_read_only("grep -o 'x.*' a.txt"))
        self.assertTrue(bash_is_read_only("ls -i"))
        self.assertTrue(bash_is_read_only("df -i"))
        # 但 `-i` 对非白名单头一律先被 head 拦下
        self.assertFalse(bash_is_read_only("sed -i 's/a/b/' a.txt"))
        self.assertFalse(bash_is_read_only("curl -o out http://x"))

    def test_empty_or_unparseable_is_fail_closed(self):
        for cmd in ("", "   ", "\n", None, "|", "&&"):
            self.assertFalse(bash_is_read_only(cmd), msg=repr(cmd))

    def test_store_deny_and_dangerous_patterns_honoured(self):
        """目录/危险模式必须叠加生效 —— 否则会破坏"⊂ 权限链 allow 集合"的不变量。"""
        self.assertTrue(bash_is_read_only("ls", {}))
        self.assertFalse(bash_is_read_only(
            "ls", {"deny_patterns": ["ls"]}))
        self.assertFalse(bash_is_read_only(
            "git status", {"dangerous_patterns": ["git status"]}))
        # 不相干的规则不影响
        self.assertTrue(bash_is_read_only(
            "ls", {"deny_patterns": ["rm -rf"], "dangerous_patterns": ["dd "]}))


class TestPlanBashInvariant(unittest.TestCase):
    """铁律：plan 放行集合 **严格小于** 权限链 allow 集合。"""

    def test_readonly_heads_subset_of_permission_safe_commands(self):
        allowed_heads = {entry.split()[0] for entry in BUILTIN_SAFE_COMMANDS}
        self.assertTrue(
            PLAN_BASH_READONLY_HEADS <= allowed_heads,
            msg=sorted(PLAN_BASH_READONLY_HEADS - allowed_heads),
        )

    def test_env_intentionally_excluded_from_plan_heads(self):
        """`env` 在权限链白名单里，但**刻意**不在 plan 只读头里（启动器语义）。"""
        self.assertIn("env", {e.split()[0] for e in BUILTIN_SAFE_COMMANDS})
        self.assertNotIn("env", PLAN_BASH_READONLY_HEADS)

    def test_every_plan_allowed_command_passes_permission_deny_layers(self):
        """凡是 plan 判为只读的命令，都不得命中权限链的硬拒绝/危险模式。"""
        samples = [
            "ls -la", "cat a", "grep -i x a", "find . -name '*.py'",
            "git status", "git diff", "git log", "git branch -a",
            "env", "date", "du -sh .", "tail -5 f",
        ]
        patterns = list(BUILTIN_DENY) + list(BUILTIN_DANGEROUS)
        for cmd in samples:
            self.assertTrue(bash_is_read_only(cmd), msg=cmd)
            segs = split_command_segments(cmd)
            token_lists = [tokenize(s) for s in segs]
            for seg in segs:
                for p in patterns:
                    self.assertFalse(
                        pattern_matches(p, seg)
                        or cross_pattern_matches(p, token_lists),
                        msg=f"{cmd!r} 命中权限链模式 {p!r} 却仍被 plan 放行",
                    )

    def test_deny_tokens_are_documented_set(self):
        # 否决位集合是显式枚举的；`-i` / `-o` 刻意不在其中（见上方测试）
        self.assertNotIn("-i", PLAN_BASH_DENY_TOKENS)
        self.assertNotIn("-o", PLAN_BASH_DENY_TOKENS)
        self.assertIn("-exec", PLAN_BASH_DENY_TOKENS)
        self.assertIn("-delete", PLAN_BASH_DENY_TOKENS)


# ══════════════════════════════════════════════════════════════════
#  二、ExecutionGate 状态机
# ══════════════════════════════════════════════════════════════════

class TestExecutionGateState(unittest.TestCase):
    def test_initial_state(self):
        g = ExecutionGate()
        self.assertEqual(g.snapshot(), {
            "mode": MODE_NORMAL, "plan_status": None, "plan_path": None,
            "plan_name": None})

    def test_plan_roundtrip(self):
        g = ExecutionGate()
        g.set_plan_mode()
        self.assertEqual(g.snapshot()["mode"], MODE_PLAN)
        self.assertIsNone(g.snapshot()["plan_status"])

        g.mark_plan_ready("/tmp/x/plans/session_s1.md")
        snap = g.snapshot()
        self.assertEqual(snap["plan_status"], PLAN_STATUS_READY)
        self.assertEqual(snap["plan_path"], "/tmp/x/plans/session_s1.md")
        self.assertTrue(g.plan_blocks_writes())

        g.approve_plan()
        self.assertEqual(g.plan_status, PLAN_STATUS_APPROVED)
        self.assertFalse(g.plan_blocks_writes())

        g.clear_plan()
        self.assertIsNone(g.plan_status)
        self.assertIsNone(g.plan_path)

    def test_entering_plan_clears_previous_status(self):
        """重进计划模式时清掉上一份文书的**状态**（文件留给下次覆盖）。"""
        g = ExecutionGate()
        g.mark_plan_ready("/tmp/a.md")
        self.assertEqual(g.plan_status, PLAN_STATUS_READY)
        g.set_plan_mode()
        self.assertIsNone(g.plan_status)

    def test_mark_plan_ready_without_path_keeps_old_path(self):
        g = ExecutionGate()
        g.mark_plan_ready("/tmp/a.md")
        g.mark_plan_ready(None)
        self.assertEqual(g.plan_path, "/tmp/a.md")
        self.assertEqual(g.plan_status, PLAN_STATUS_READY)

    def test_set_mode_ignores_invalid(self):
        g = ExecutionGate()
        g.set_mode("bogus")
        self.assertEqual(g.mode, MODE_NORMAL)
        g.set_mode(MODE_GOAL)
        self.assertEqual(g.mode, MODE_GOAL)

    def test_restore_plan_from_meta(self):
        g = ExecutionGate()
        g.restore_plan_from_meta({"plan_status": PLAN_STATUS_READY})
        self.assertEqual(g.plan_status, PLAN_STATUS_READY)
        for bad in ({"plan_status": "nope"}, {}, None, "not-a-dict"):
            g.restore_plan_from_meta(bad)
            self.assertIsNone(g.plan_status, msg=repr(bad))

    def test_snapshot_is_atomic_copy(self):
        g = ExecutionGate()
        snap = g.snapshot()
        snap["mode"] = "tampered"
        self.assertEqual(g.mode, MODE_NORMAL)


# ══════════════════════════════════════════════════════════════════
#  三、blocked_reason 判定矩阵（docs/frontend/22 §5.10）
# ══════════════════════════════════════════════════════════════════

class TestBlockedReason(unittest.TestCase):
    def setUp(self):
        self.g = ExecutionGate()

    def _blocked(self, name, args=None):
        return self.g.blocked_reason(name, args or {})

    def test_normal_and_goal_never_block(self):
        for mode in (MODE_NORMAL, MODE_GOAL):
            self.g.set_mode(mode)
            for tool in ("run_write", "run_edit", "bash", "write_memory",
                         "sub_agent", "schedule_cron"):
                self.assertIsNone(self._blocked(tool), msg=f"{mode}/{tool}")

    def test_plan_blocks_writes_allows_reads(self):
        self.g.set_plan_mode()
        for tool in ("run_write", "run_edit", "write_memory", "forget_memory",
                     "create_task", "claim_task", "spawn_teammate",
                     "schedule_cron", "create_worktree", "remove_worktree",
                     "send_message", "sub_agent", "connect_mcp",
                     "mcp__fs__write_file"):
            self.assertIsNotNone(self._blocked(tool), msg=tool)
        for tool in PLAN_READONLY_TOOLS:
            self.assertIsNone(self._blocked(tool), msg=tool)

    def test_plan_write_is_the_documented_exit(self):
        self.g.set_plan_mode()
        self.assertIsNone(self._blocked(PLAN_WRITE_TOOL))

    def test_bash_conditionally_allowed_in_plan(self):
        self.g.set_plan_mode()
        self.assertIsNone(self._blocked("bash", {"command": "git status"}))
        self.assertIsNotNone(self._blocked("bash", {"command": "rm -rf /"}))
        self.assertIsNotNone(self._blocked(
            "bash", {"command": "find . -exec rm -rf {} +"}))
        # 缺 command 参数 → 空串 → fail-closed 阻断
        self.assertIsNotNone(self._blocked("bash", {}))

    def test_approved_plan_unblocks_everything(self):
        self.g.set_plan_mode()
        self.g.mark_plan_ready("/tmp/a.md")
        self.g.approve_plan()
        for tool in ("run_write", "run_edit", "bash", "write_memory"):
            self.assertIsNone(self._blocked(tool), msg=tool)

    def test_block_message_is_actionable(self):
        msg = block_message("run_write")
        self.assertIn(PLAN_WRITE_TOOL, msg)
        self.assertIn("Plan mode", msg)
        self.assertIn("run_write", msg)


class TestPlanDisplayPath(unittest.TestCase):
    """`paths.plan_display_path`：三个出口（信封 / session_history / 列表）共用的判据。"""

    def setUp(self):
        from paths import plan_display_path
        self.f = plan_display_path

    def test_new_artifact_gives_relative_path(self):
        self.assertEqual(self.f(PLAN_STATUS_READY, "重构方案.md"),
                         ".aiagent/plan/重构方案.md")

    def test_legacy_session_falls_back_to_absolute(self):
        """存量会话（meta 无 plan_name）→ 旧的元数据目录绝对路径。"""
        self.assertEqual(self.f(PLAN_STATUS_READY, None, "/tmp/plans/session_a.md"),
                         "/tmp/plans/session_a.md")

    def test_no_plan_status_gives_none(self):
        """没有计划状态就没有路径 —— 即使名字还在（重规划期间的状态）。"""
        self.assertIsNone(self.f(None, "重构方案.md", "/tmp/plans/x.md"))
        self.assertIsNone(self.f(None, None))
        self.assertIsNone(self.f(PLAN_STATUS_READY, None))


class TestExecutionGateHoldsPlanName(unittest.TestCase):
    """gate 必须记住**模型给的文件名** —— 它是 meta `plan_name` 的唯一来源。"""

    def test_mark_ready_records_path_and_name(self):
        g = ExecutionGate()
        g.set_plan_mode()
        g.mark_plan_ready("/ws/.aiagent/plan/方案.md", "方案.md")
        self.assertEqual(g.plan_status, PLAN_STATUS_READY)
        self.assertEqual(g.plan_name, "方案.md")
        self.assertEqual(g.snapshot()["plan_name"], "方案.md")

    def test_set_plan_mode_keeps_previous_name(self):
        """重规划要覆盖**自己**那一份 → 进入可写态不清名字（否则会被当成别人的文件）。"""
        g = ExecutionGate()
        g.set_plan_mode()
        g.mark_plan_ready("/ws/.aiagent/plan/方案.md", "方案.md")
        g.set_plan_mode()
        self.assertIsNone(g.plan_status)
        self.assertEqual(g.plan_name, "方案.md")

    def test_clear_plan_drops_name(self):
        g = ExecutionGate()
        g.set_plan_mode()
        g.mark_plan_ready("/ws/.aiagent/plan/方案.md", "方案.md")
        g.clear_plan()
        self.assertIsNone(g.plan_name)
        self.assertIsNone(g.plan_path)

    def test_restore_from_meta_round_trip(self):
        g = ExecutionGate()
        g.restore_plan_from_meta({"plan_status": PLAN_STATUS_READY,
                                  "plan_name": "方案.md"})
        self.assertEqual(g.plan_status, PLAN_STATUS_READY)
        self.assertEqual(g.plan_name, "方案.md")

    def test_restore_ignores_bad_name(self):
        for bad in (None, "", "   ", 123, ["x"]):
            g = ExecutionGate()
            g.restore_plan_from_meta({"plan_status": PLAN_STATUS_READY,
                                      "plan_name": bad})
            self.assertIsNone(g.plan_name, msg=repr(bad))


# ══════════════════════════════════════════════════════════════════
#  四、两条轴不合并（静态不变量）
# ══════════════════════════════════════════════════════════════════

def _has_import(src: str, module: str) -> bool:
    """源码里是否出现 `import <module>` / `from <module> import`（粗粒度但够用）。"""
    import re
    return bool(re.search(rf"^\s*(from\s+{module}\s+import|import\s+{module}\b)",
                          src, re.MULTILINE))


class TestAxisSeparation(unittest.TestCase):
    def test_permission_module_does_not_depend_on_execution_mode(self):
        """`permission.py` 是权限策略的唯一出处，不许反向依赖执行模式。

        两条轴**正交**：同一事件上各判一次，判定链不合并。若 permission 反过来
        import execution_mode，"能不能做"与"以什么方式做"就会缠在一起。
        """
        src = (AGENTS_DIR / "permission.py").read_text(encoding="utf-8")
        self.assertFalse(_has_import(src, "execution_mode"),
                         "permission.py 不得 import execution_mode")

    def test_execution_mode_does_not_hold_goal_state(self):
        """execution_mode 只持有 plan 状态；goal 的真相恒在 GoalController。"""
        src = (AGENTS_DIR / "execution_mode.py").read_text(encoding="utf-8")
        self.assertFalse(_has_import(src, "goal"),
                         "execution_mode.py 不得 import goal（避免第二真相源）")
        # 允许出现 MODE_GOAL 常量（只是 UI 投影值），但不得持有 goal 条件
        self.assertNotIn("goal_condition", src)

    def test_hooks_delegates_without_holding_policy(self):
        """`hooks.py` 只有注入点 + 一行委托，不许内联判定策略。"""
        src = (AGENTS_DIR / "hooks.py").read_text(encoding="utf-8")
        self.assertFalse(_has_import(src, "permission"),
                         "hooks.py 不得 import permission（策略在其门内）")
        self.assertFalse(_has_import(src, "execution_mode"),
                         "hooks.py 不得 import execution_mode（策略在其门内）")


if __name__ == "__main__":
    unittest.main()
