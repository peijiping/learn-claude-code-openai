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
import agent_full_v2  # noqa: E402
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


class _StubTaskManager:
    """只提供 `_sync_task_board` 需要的 scope。

    快照内容由测试 patch `agent_full_v2.current_board` 提供 ——
    这样测试不会去读用户真实的 `.tasks/` 目录。
    """

    scope = "session_test"


class _StubTools:
    def __init__(self, todo_manager, task_manager=None):
        self._todo_manager = todo_manager
        self.task_manager = task_manager if task_manager is not None else _StubTaskManager()

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
    agent.session_id = "1"
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

    def test_todo_reminder_is_gone(self):
        """防回退：todo reminder 已于 2026-09-16 下线，不应再存在该入口。

        它有三个硬伤：只读 TodoManager 对 task 零感知、只在 init/switch 触发
        （同会话中断后续轮根本不注入）、注入粒度过粗。
        替代实现是 _sync_task_board()，其注入契约由 test_task_board 系列守护。
        """
        agent = _make_agent()
        self.assertFalse(hasattr(agent, "_inject_todo_reminder"))

    # ── 任务板注入（2026-09-16 新增，承接原 todo reminder 的职责）────

    @staticmethod
    def _board(group_id="g_test", derived="pending"):
        def row(i, subject, status, ds):
            return {"id": f"t{i}", "subject": subject, "status": status,
                    "derived_status": ds, "owner": None, "parentId": None,
                    "depth": 0, "orderIndex": i, "blockedBy": [], "result": "",
                    "started_at": None, "updated_at": 0.0,
                    "child_total": 0, "child_completed": 0}
        return {
            "group_id": group_id, "revision": 1, "status": "running",
            "counts": {"total": 2, "completed": 1, "in_progress": 0,
                       "pending": 1, "blocked": 0},
            "tasks": [row(1, "已完成项", "completed", "completed"),
                      row(2, "剩下的活", "pending", derived)],
        }

    def _patch_board(self, board):
        """替换快照来源，避免测试读用户真实的 .tasks/ 目录。

        签名需与调用口径一致：`_sync_task_board` 传 `(scope, tasks_dir)`
        （tasks_dir = 本会话所属工作空间的 .tasks，多工作空间改造后新增）。
        """
        orig = agent_full_v2.current_board
        agent_full_v2.current_board = lambda scope, tasks_dir=None: board
        self.addCleanup(lambda: setattr(agent_full_v2, "current_board", orig))

    def test_task_board_reminder_is_wrapped(self):
        """必须 <system-reminder> 包裹，否则会以用户气泡的形式漏到前端。"""
        self._patch_board(self._board())
        agent = _make_agent()
        agent._sync_task_board()

        self.assertEqual(len(agent.history_messages), 1)
        content = agent.history_messages[0]["content"]
        self.assertTrue(content.startswith(SYSTEM_REMINDER_PREFIX))
        self.assertIn('<task_board group="g_test">', content)
        self.assertIn("剩下的活", content)
        self.assertNotIn("已完成项", content, "已完成的项不该出现在提醒里")

    def test_task_board_reminder_dedups_by_group(self):
        """同组二次调用不再注入 —— "同会话中断后继续"不刷屏的保证。"""
        self._patch_board(self._board())
        agent = _make_agent()
        agent._sync_task_board()
        agent._sync_task_board()

        self.assertEqual(len(agent.history_messages), 1, "同组只注入一次")

    def test_task_board_injects_again_for_new_group(self):
        """换组要重新注入，否则新一轮的活模型看不到。"""
        self._patch_board(self._board())
        agent = _make_agent()
        agent._sync_task_board()
        self._patch_board(self._board(group_id="g_next"))
        agent._sync_task_board()

        self.assertEqual(len(agent.history_messages), 2)

    def test_no_task_board_reminder_without_unfinished_group(self):
        """没有未完成组时既不注入、也不清理旧标记。"""
        self._patch_board(None)
        agent = _make_agent()
        agent._sync_task_board()

        self.assertEqual(agent.history_messages, [])

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
        """端到端：全部注入消息都不应在 UI 回放里出现为 user 气泡。"""
        src = (AGENTS_DIR / "ws_bridge.py").read_text(encoding="utf-8")
        seg = src[src.index("def _text_of("):src.index("async def handle(ws):")]
        # 片段内已出现类型标注（Optional[...] 等）；exec 命名空间为裸 dict，
        # 注解在 def 处即求值会 NameError，故预置整个 typing 命名空间。
        # （2026-09-16 修复，与 test_subagent_sidecar 同因）
        #
        # 切片内的其它模块级引用也必须显式预置（ws_bridge 演进时在此补名）：
        # SessionManager / WorkspacePaths 是附件辅助函数的参数注解；
        # harvest_attachments 由 `_history_to_ui` 实际调用。
        # （2026-09-20 修复：附件功能上线，与 test_subagent_sidecar 再次同因）
        # harvest_refs 同上 —— 引用（@-mention）功能回放用（2026-09-21）。
        # is_tool_images_message 同因 —— 工具读图（view_image）的合成消息判据，
        # `_history_to_ui` 用它跳过 agent_loop 追加的那条假 user 消息（2026-09-21）。
        import typing
        from attachments import harvest_attachments, is_tool_images_message
        from paths import WorkspacePaths
        from refs import harvest_refs
        from session_manage import SessionManager
        ns: dict = {n: getattr(typing, n) for n in dir(typing) if not n.startswith("_")}
        ns.update({
            "SessionManager": SessionManager,
            "WorkspacePaths": WorkspacePaths,
            "harvest_attachments": harvest_attachments,
            "harvest_refs": harvest_refs,
            "is_tool_images_message": is_tool_images_message,
        })
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
        self._patch_board(self._board())
        agent._sync_task_board()

        messages = [
            {"role": "user", "content": "真实用户提问"},
            {"role": "user", "content": notif},
            *agent.history_messages,
        ]
        ui = history_to_ui(messages)

        self.assertEqual([m["content"] for m in ui], ["真实用户提问"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
