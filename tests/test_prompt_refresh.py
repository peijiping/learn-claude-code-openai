"""方案 B（进会话刷新 system prompt）与方案 C（工作区指令变更尾部注入）离线回归测试。

运行方式（无需额外依赖，pytest 未安装也能跑；装了 pytest 同样可收集）::

    cd /Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main
    .venv/bin/python -m unittest discover -s tests -v

## 方案 B
`load_session_history()` 读回来的 `messages[0]` 是**会话创建时**那份，会覆盖掉
`build_system_prompt()` 重算的结果 ⇒ 改了 AGENTS.md / 装了新技能当前会话看不到。
修复：三个进会话入口（init / switch / new）都调 `_refresh_system_prompt()` 做替换。

## 方案 C
会话**期间**改了 AGENTS.md（用户习惯：让智能体随时改它记录项目记忆）⇒ 走尾部注入，
不重建 `messages[0]`（重建会让整段前缀含全部历史失效重算，长会话下贵得多）。

全部离线：不调 LLM、不写任何真实会话文件。
"""
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from agent_full_v2 import Agent, PROJECT_RULES_TAG  # noqa: E402


def _rev(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


class _StubBuilder:
    """替身 SystemPromptBuilder：工作区指令内容与构建结果都可随时改。"""

    def __init__(self, workspace_text: str = "", prompt: str = "SYSTEM-PROMPT"):
        self.workspace_text = workspace_text
        self.prompt = prompt

    @property
    def workspace_revision(self) -> str:
        return _rev(self.workspace_text)

    def build_system_prompt(self) -> str:
        return self.prompt

    def get_workspace_instructions(self) -> str:
        return self.workspace_text


class _StubSessionManager:
    def __init__(self, system_prompt: str = "HELD-OLD"):
        self.system_prompt = system_prompt
        self.appended: list = []

    def append_message_to_session(self, session_file, message):
        self.appended.append((session_file, message))


def _make_agent(builder, history=None, with_revision=True) -> Agent:
    """离线构造 Agent；with_revision=False 模拟"尚未进会话"。"""
    agent = Agent.__new__(Agent)
    agent.system_prompt = builder
    agent.history_messages = list(history) if history else []
    agent.session_file = Path("/tmp/_unittest_never_written.jsonl")
    agent.session_manager = _StubSessionManager()
    agent.session_prefix = "session_"
    agent.session_num = 1
    if with_revision:
        agent._prompt_workspace_revision = builder.workspace_revision
    return agent


class PromptRefreshTests(unittest.TestCase):
    """方案 B：进会话时把 messages[0] 换成最新构建结果。"""

    def test_replaces_stale_system_message(self):
        agent = _make_agent(
            _StubBuilder(prompt="NEW"),
            history=[{"role": "system", "content": "OLD"},
                     {"role": "user", "content": "你好"}],
        )
        self.assertTrue(agent._refresh_system_prompt())
        self.assertEqual(agent.history_messages[0]["content"], "NEW")
        # 其余历史不受影响
        self.assertEqual(agent.history_messages[1], {"role": "user", "content": "你好"})

    def test_noop_when_identical(self):
        agent = _make_agent(
            _StubBuilder(prompt="SAME"),
            history=[{"role": "system", "content": "SAME"}],
        )
        self.assertFalse(agent._refresh_system_prompt())
        self.assertEqual(len(agent.history_messages), 1)

    def test_updates_session_manager_held_prompt(self):
        """新建会话用的是 SessionManager 持有的那份，必须一起刷新。"""
        agent = _make_agent(
            _StubBuilder(prompt="NEW"),
            history=[{"role": "system", "content": "OLD"}],
        )
        agent._refresh_system_prompt()
        self.assertEqual(agent.session_manager.system_prompt, "NEW")

    def test_empty_history_is_safe(self):
        agent = _make_agent(_StubBuilder(prompt="NEW"))
        self.assertFalse(agent._refresh_system_prompt())
        self.assertEqual(agent.history_messages, [])

    def test_non_system_first_message_left_alone(self):
        """首条不是 system 时不乱插，避免破坏会话结构。"""
        agent = _make_agent(
            _StubBuilder(prompt="NEW"),
            history=[{"role": "user", "content": "无 system 头"}],
        )
        self.assertFalse(agent._refresh_system_prompt())
        self.assertEqual(agent.history_messages[0]["role"], "user")
        self.assertEqual(len(agent.history_messages), 1)

    def test_records_workspace_revision(self):
        agent = _make_agent(
            _StubBuilder(workspace_text="RULES-V1", prompt="NEW"),
            history=[{"role": "system", "content": "OLD"}],
            with_revision=False,
        )
        agent._refresh_system_prompt()
        self.assertEqual(agent._prompt_workspace_revision, _rev("RULES-V1"))


class ProjectRulesSyncTests(unittest.TestCase):
    """方案 C：会话期间 AGENTS.md 被改动 → 尾部注入最新全文。"""

    def test_no_injection_when_unchanged(self):
        agent = _make_agent(
            _StubBuilder(workspace_text="RULES", prompt="SYS"),
            history=[{"role": "system", "content": "SYS"}],
        )
        agent._sync_project_rules()
        self.assertEqual(len(agent.history_messages), 1)
        self.assertEqual(agent.session_manager.appended, [])

    def test_injects_on_mid_session_change(self):
        builder = _StubBuilder(workspace_text="RULES-V1", prompt="SYS")
        agent = _make_agent(builder, history=[{"role": "system", "content": "SYS"}])
        agent._sync_project_rules()          # 与 system 一致 → 不注入

        builder.workspace_text = "RULES-V2"  # 会话中途被改
        agent._sync_project_rules()

        self.assertEqual(len(agent.history_messages), 2)
        content = agent.history_messages[-1]["content"]
        self.assertIn("RULES-V2", content)
        self.assertIn("以本条为准", content)
        self.assertIn(f'<{PROJECT_RULES_TAG} revision="{_rev("RULES-V2")}">', content)

    def test_idempotent_after_injection(self):
        builder = _StubBuilder(workspace_text="V1", prompt="SYS")
        agent = _make_agent(builder, history=[{"role": "system", "content": "SYS"}])
        builder.workspace_text = "V2"
        agent._sync_project_rules()
        for _ in range(3):
            agent._sync_project_rules()
        self.assertEqual(len(agent.history_messages), 2)

    def test_reinjects_on_second_change(self):
        builder = _StubBuilder(workspace_text="V1", prompt="SYS")
        agent = _make_agent(builder, history=[{"role": "system", "content": "SYS"}])
        builder.workspace_text = "V2"
        agent._sync_project_rules()
        builder.workspace_text = "V3"
        agent._sync_project_rules()
        self.assertEqual(len(agent.history_messages), 3)
        self.assertIn("V3", agent.history_messages[-1]["content"])

    def test_injects_notice_when_workspace_file_removed(self):
        builder = _StubBuilder(workspace_text="V1", prompt="SYS")
        agent = _make_agent(builder, history=[{"role": "system", "content": "SYS"}])
        builder.workspace_text = ""
        agent._sync_project_rules()
        self.assertEqual(len(agent.history_messages), 2)
        self.assertIn("已被移除", agent.history_messages[-1]["content"])

    def test_wrapped_in_system_reminder(self):
        builder = _StubBuilder(workspace_text="V1", prompt="SYS")
        agent = _make_agent(builder, history=[{"role": "system", "content": "SYS"}])
        builder.workspace_text = "V2"
        agent._sync_project_rules()
        self.assertTrue(
            agent.history_messages[-1]["content"].startswith("<system-reminder>")
        )

    def test_persisted_to_session_file(self):
        builder = _StubBuilder(workspace_text="V1", prompt="SYS")
        agent = _make_agent(builder, history=[{"role": "system", "content": "SYS"}])
        builder.workspace_text = "V2"
        agent._sync_project_rules()
        self.assertEqual(len(agent.session_manager.appended), 1)
        self.assertIs(agent.session_manager.appended[0][1], agent.history_messages[-1])

    def test_session_entry_then_sync_is_noop(self):
        """B 与 C 的交接：进会话刷新后，C 不应把同一份内容再注入一遍。"""
        agent = _make_agent(
            _StubBuilder(workspace_text="RULES", prompt="SYS"),
            history=[{"role": "system", "content": "OLD"}],
            with_revision=False,
        )
        agent._refresh_system_prompt()   # 进会话
        agent._sync_project_rules()      # 同一轮 turn 起点
        self.assertEqual(len(agent.history_messages), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
