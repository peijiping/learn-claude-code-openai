"""系统注入消息的「包裹约定」守卫测试。

运行方式（无需额外依赖，pytest 未安装也能跑；装了 pytest 同样可收集）::

    cd /Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main
    .venv/bin/python -m unittest discover -s tests -v

## 被守护的约定

`ws_bridge._history_to_ui()` 判定"这条 user 消息是给模型看的系统注入、不该出现在
前端聊天界面"的**唯一**依据是：

    content.startswith("<system-reminder>")

因此**任何**往 `history_messages` 尾部追加的动态内容，都必须用
`<system-reminder>…</system-reminder>` 包裹，否则会以用户气泡的形式漏到 UI
（`<task_notification>` 曾因漏包裹而长期可见，见 docs/frontend/03 §2.1）。

新增同类注入时，请同步在本文件加一条断言。
"""
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from agent_full_v2 import Agent  # noqa: E402
from background_manager import BackgroundManager  # noqa: E402

SYSTEM_REMINDER_PREFIX = "<system-reminder>"


class _StubBuilder:
    """替身 SystemPromptBuilder（只需支撑工作区指令变更的注入路径）。"""

    def __init__(self, workspace_text: str = "", prompt: str = "SYS"):
        self.workspace_text = workspace_text
        self.prompt = prompt

    @property
    def workspace_revision(self) -> str:
        return hashlib.sha1(self.workspace_text.encode("utf-8")).hexdigest()[:12]

    def build_system_prompt(self) -> str:
        return self.prompt

    def get_workspace_instructions(self) -> str:
        return self.workspace_text


class _StubSessionManager:
    def __init__(self):
        self.appended: list = []

    def append_message_to_session(self, session_file, message):
        self.appended.append(message)


class _StubTodoManager:
    def has_open_items(self) -> bool:
        return True

    def render(self) -> str:
        return "- [ ] 待办样例"


class _StubTools:
    def __init__(self, todo_manager):
        self._todo_manager = todo_manager

    def get_todo_manager(self):
        return self._todo_manager


class _StubMemory:
    def __init__(self, text=""):
        self.text = text

    def read_index(self) -> str:
        return self.text


def _make_agent() -> Agent:
    """离线构造 Agent，绕开 __init__（会真的建 LLMClient / 连 MCP）。"""
    agent = Agent.__new__(Agent)
    agent.history_messages = []
    agent.tools = _StubTools(_StubTodoManager())
    agent.memory = _StubMemory("- [a](a.md) — 描述A")
    agent.system_prompt = _StubBuilder()
    agent._prompt_workspace_revision = agent.system_prompt.workspace_revision
    agent.session_file = Path("/tmp/_unittest_never_written.jsonl")
    agent.session_manager = _StubSessionManager()
    agent.session_prefix = "session_"
    agent.session_num = 1
    return agent


class SystemInjectionContractTests(unittest.TestCase):
    """所有尾部注入都必须以 <system-reminder> 开头（前端过滤的唯一依据）。"""

    def test_task_notification_is_wrapped(self):
        bm = BackgroundManager()
        bm.background_tasks["bg_1"] = {"status": "completed", "command": "ls"}
        bm.background_results["bg_1"] = "目录内容"

        notifications = bm.collect_background_results()

        self.assertEqual(len(notifications), 1)
        self.assertTrue(notifications[0].startswith(SYSTEM_REMINDER_PREFIX))
        self.assertIn("<task_notification>", notifications[0])
        self.assertIn("目录内容", notifications[0])

    def test_task_notification_result_count_unchanged(self):
        """包裹不能改变消费语义：状态仍转 notified、数据仍保留。"""
        bm = BackgroundManager()
        bm.background_tasks["bg_1"] = {"status": "completed", "command": "ls"}
        bm.background_results["bg_1"] = "输出"

        bm.collect_background_results()
        self.assertEqual(bm.background_tasks["bg_1"]["status"], "notified")
        self.assertIn("bg_1", bm.background_results)
        # 再收集一次不重复
        self.assertEqual(bm.collect_background_results(), [])

    def test_todo_reminder_is_wrapped(self):
        agent = _make_agent()
        agent._inject_todo_reminder()

        self.assertEqual(len(agent.history_messages), 1)
        self.assertTrue(
            agent.history_messages[0]["content"].startswith(SYSTEM_REMINDER_PREFIX)
        )

    def test_memory_index_is_wrapped(self):
        agent = _make_agent()
        agent._sync_memory_index()

        self.assertEqual(len(agent.history_messages), 1)
        self.assertTrue(
            agent.history_messages[0]["content"].startswith(SYSTEM_REMINDER_PREFIX)
        )

    def test_environment_is_wrapped(self):
        agent = _make_agent()
        agent._sync_environment()

        self.assertEqual(len(agent.history_messages), 1)
        self.assertTrue(
            agent.history_messages[0]["content"].startswith(SYSTEM_REMINDER_PREFIX)
        )

    def test_project_rules_is_wrapped(self):
        agent = _make_agent()
        agent.system_prompt.workspace_text = "CHANGED-RULES"  # 模拟会话中途改动
        agent._sync_project_rules()

        self.assertEqual(len(agent.history_messages), 1)
        self.assertTrue(
            agent.history_messages[0]["content"].startswith(SYSTEM_REMINDER_PREFIX)
        )

    def test_injected_messages_are_filtered_by_history_to_ui(self):
        """端到端：三条注入消息都不应在 UI 回放里出现为 user 气泡。"""
        src = (AGENTS_DIR / "ws_bridge.py").read_text(encoding="utf-8")
        seg = src[src.index("def _text_of("):src.index("async def handle(ws):")]
        ns: dict = {}
        exec(compile(seg, "ws_bridge_hist", "exec"), ns)  # noqa: S102 - 测试内自用
        history_to_ui = ns["_history_to_ui"]

        bm = BackgroundManager()
        bm.background_tasks["bg_1"] = {"status": "completed", "command": "ls"}
        bm.background_results["bg_1"] = "输出"
        notif = bm.collect_background_results()[0]

        agent = _make_agent()
        agent._sync_memory_index()
        agent._sync_environment()
        agent.system_prompt.workspace_text = "CHANGED-RULES"
        agent._sync_project_rules()
        agent._inject_todo_reminder()

        messages = [
            {"role": "user", "content": "真实用户提问"},
            {"role": "user", "content": notif},
            *agent.history_messages,
        ]
        ui = history_to_ui(messages)

        self.assertEqual([m["content"] for m in ui], ["真实用户提问"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
