#!/usr/bin/env python3
"""多工作空间路径注入守护测试 —— 2026-09-18。

这一层的两条铁律，任何一条破了都会造成"看起来能跑、实际写错地方"的事故：

1. **default 行为一字不变**：`Agent()` 不传 workspace 时，沙箱根 / 任务目录 /
   记忆目录 / 命令 cwd 必须与改造前完全一致（190 例基线回归之外再钉一遍，
   因为路径错了不会报错，只会静默写歪）。
2. **自定义空间全面切换**：一个 Agent 实例的沙箱根、文件工具基准、任务目录、
   记忆目录、收件箱、团队目录、工作流目录、钩子越界判定、子智能体提示词里的
   "工作目录"必须**全部**指向该空间的选定目录/元数据目录 —— 漏掉任何一个，
   就会出现"会话在 A 空间、任务板写去 B 空间"这类跨空间串台。
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
from agent_full_v2 import Agent  # noqa: E402
from project_registry import WorkspaceRegistry  # noqa: E402
from tools import ToolRegistry  # noqa: E402


class _WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reg = WorkspaceRegistry(
            index_path=self.root / "projects.json", projects_root=self.root
        )
        self.real = self.root / "real-project"
        self.real.mkdir(parents=True, exist_ok=True)


class DefaultWorkspaceUnchangedTests(_WorkspaceFixture):
    """不传 workspace = default 空间 = 改造前行为。"""

    @classmethod
    def setUpClass(cls):
        cls.agent = Agent(silent=True)

    def test_workspace_is_default(self):
        self.assertEqual(self.agent.workspace.id, "default")
        self.assertTrue(self.agent.workspace.is_default)

    def test_file_tools_root_is_legacy_workdir(self):
        self.assertEqual(self.agent.tools.workdir, paths.WORKDIR)
        self.assertEqual(self.agent.system_prompt.workdir, paths.WORKDIR)
        self.assertEqual(self.agent.hook_system.workdir, paths.WORKDIR)

    def test_bash_keeps_process_cwd(self):
        # 历史行为：default 空间的 bash 跑在进程 cwd（bash_cwd=None → os.getcwd()）
        self.assertIsNone(self.agent.tools.bash_cwd)

    def test_runtime_dirs_are_default_space(self):
        self.assertEqual(self.agent.tools.task_manager.task_dir, paths.TASKS_DIR)
        self.assertEqual(self.agent.tools.memory.memory_dir, paths.MEMORY_DIR)
        self.assertEqual(self.agent.teammate_manager.dir, paths.TEAM_DIR)

    def test_subagent_prompt_mentions_legacy_workdir(self):
        self.assertIn(str(paths.WORKDIR), self.agent.subagent_runner.DEFAULT_SYSTEM_PROMPT)


class CustomWorkspaceWiringTests(_WorkspaceFixture):
    """自定义空间：Agent 的全部路径必须切到该空间。"""

    def setUp(self):
        super().setUp()
        self.info = self.reg.create(str(self.real))
        self.ws = self.reg.paths(self.info.id)
        self.agent = Agent(silent=True, workspace=self.ws)

    def test_workspace_adopted(self):
        self.assertEqual(self.agent.workspace.id, self.info.id)
        self.assertFalse(self.agent.workspace.is_default)

    def test_sandbox_root_is_selected_dir(self):
        self.assertEqual(self.agent.workspace.workdir, self.real.resolve())
        self.assertEqual(self.agent.tools.workdir, self.real.resolve())
        self.assertEqual(self.agent.system_prompt.workdir, self.real.resolve())
        self.assertEqual(self.agent.hook_system.workdir, self.real.resolve())

    def test_bash_runs_in_selected_dir(self):
        self.assertEqual(self.agent.tools.bash_cwd, self.real.resolve())

    def test_runtime_dirs_are_space_scoped(self):
        meta = self.root / self.info.id
        self.assertEqual(self.agent.tools.task_manager.task_dir, meta / ".tasks")
        self.assertEqual(self.agent.tools.memory.memory_dir, meta / ".memory")
        self.assertEqual(self.agent.teammate_manager.dir, meta / ".team")
        self.assertEqual(self.agent.workspace.chat_history_dir, meta / ".chathistory")

    def test_subagent_prompt_mentions_selected_dir(self):
        # 子智能体提示词不能再说"工作目录是 default 的沙盒"
        prompt = self.agent.subagent_runner.DEFAULT_SYSTEM_PROMPT
        self.assertIn(str(self.real.resolve()), prompt)
        self.assertNotIn(str(paths.WORKDIR), prompt)


class SafePathUsesInstanceWorkdirTests(unittest.TestCase):
    """`safe_path` 的越界判定基准必须是本实例的 workdir（多工作空间的关键一环）。

    注意 `base` 必须是**已 resolve** 的路径（本测试用 `Path(tmp).resolve()`）：
    macOS 的 `/var` 是指向 `/private/var` 的软链，未 resolve 的 base 会让
    `is_relative_to` 误判越界。生产口径天然满足 —— 注册表建空间时已
    `Path(...).resolve()`，worktree 目录也在 `~/.aigent` 下无软链。
    """

    def _registry(self, workdir: Path) -> ToolRegistry:
        reg = ToolRegistry.__new__(ToolRegistry)  # 跳过依赖注入，只要路径字段
        reg.workdir = workdir
        return reg

    @staticmethod
    def _tmpdir() -> tuple:
        tmp = tempfile.TemporaryDirectory()
        return tmp, Path(tmp.name).resolve()

    def test_relative_path_resolves_under_instance_workdir(self):
        tmp, root = self._tmpdir()
        with tmp:
            reg = self._registry(root)
            self.assertEqual(reg.safe_path("a/b.txt"), (root / "a/b.txt").resolve())

    def test_escape_rejected(self):
        tmp, root = self._tmpdir()
        with tmp:
            reg = self._registry(root)
            with self.assertRaises(ValueError):
                reg.safe_path("../../etc/passwd")

    def test_explicit_base_overrides_instance_workdir(self):
        tmp, root = self._tmpdir()
        with tmp:
            other = root / "worktree"
            other.mkdir()
            reg = self._registry(root)
            # worktree / 子智能体 scoped 场景仍走显式 base
            self.assertEqual(reg.safe_path("x.md", base=other), (other / "x.md").resolve())


if __name__ == "__main__":
    unittest.main()
