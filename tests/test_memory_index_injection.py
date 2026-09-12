"""「尾部按需注入」离线回归测试（记忆索引 + 环境与上下文）。

运行方式（无需额外依赖，pytest 未安装也能跑；装了 pytest 同样可收集）::

    cd /Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main
    .venv/bin/python -m unittest discover -s tests -v

覆盖两个 L2 热段注入块（机制同构，各 tag 独立判指纹）：

| 块 | tag | 内容 | 变化来源 |
| --- | --- | --- | --- |
| 记忆索引 | `memory_index` | `read_index()` 全文 | `write_memory` / `forget_memory` |
| 环境上下文 | `env` | 日期 / 星期 / 平台 | 跨天；换机器 resume |

设计要点：
  - 幂等：sha1(内容)[:12] 作为指纹写进 `revision` 属性，随注入消息落盘；未变则零开销。
  - 自洽：指纹从**本会话历史**恢复，不存外部状态 → resume / 切会话不重复、不漏注入。
  - 兜底：标记被上下文压缩裁掉 → 扫不到 → 视为未注入 → 下一轮自动补注。
  - 隐身前端的约定：必须以 <system-reminder> 开头
    （ws_bridge._history_to_ui 会跳过以此开头的 user 消息，见 ws_bridge.py:328）。

全部离线：不调 LLM、不写任何真实会话文件。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from agent_full_v2 import Agent, ENV_TAG, MEMORY_INDEX_TAG  # noqa: E402

REVISION_RE = re.compile(rf'<{MEMORY_INDEX_TAG} revision="([0-9a-f]{{12}})">')


class _StubMemory:
    """替身 MemoryStore：只提供 read_index()。"""

    def __init__(self, text: str = ""):
        self.text = text

    def read_index(self) -> str:
        return self.text


class _StubSessionManager:
    """替身 SessionManager：记录落盘调用，不碰文件系统。"""

    def __init__(self):
        self.appended: list = []

    def append_message_to_session(self, session_file, message):
        self.appended.append((session_file, message))


def _make_agent(memory_text: str = "", history: list = None) -> Agent:
    """离线构造 Agent：绕开 __init__（会真的建 LLMClient / 连 MCP）。"""
    agent = Agent.__new__(Agent)
    agent.history_messages = list(history) if history else []
    agent.memory = _StubMemory(memory_text)
    agent.session_file = Path("/tmp/_unittest_never_written.jsonl")
    agent.session_manager = _StubSessionManager()
    agent.session_prefix = "session_"
    agent.session_num = 1
    return agent


class MemoryIndexInjectionTests(unittest.TestCase):

    def test_first_injection_appends_one_reminder(self):
        agent = _make_agent("- [a](a.md) — 描述A")
        agent._sync_memory_index()

        self.assertEqual(len(agent.history_messages), 1)
        self.assertEqual(len(agent.session_manager.appended), 1)
        msg = agent.history_messages[0]
        self.assertEqual(msg["role"], "user")
        self.assertIn("描述A", msg["content"])
        # 落盘的必须与留在历史里的同一条
        self.assertIs(agent.session_manager.appended[0][1], msg)

    def test_injection_wrapped_in_system_reminder(self):
        """必须用 <system-reminder> 包裹，否则会变成前端聊天气泡。"""
        agent = _make_agent("- [a](a.md) — 描述A")
        agent._sync_memory_index()
        content = agent.history_messages[0]["content"]

        self.assertTrue(content.startswith("<system-reminder>"))
        self.assertTrue(content.rstrip().endswith("</system-reminder>"))
        self.assertIn(f"<{MEMORY_INDEX_TAG} revision=", content)

    def test_idempotent_when_index_unchanged(self):
        agent = _make_agent("- [a](a.md) — 描述A")
        agent._sync_memory_index()
        for _ in range(3):
            agent._sync_memory_index()

        self.assertEqual(len(agent.history_messages), 1)
        self.assertEqual(len(agent.session_manager.appended), 1)

    def test_reinject_only_on_change(self):
        agent = _make_agent("- [a](a.md) — 描述A")
        agent._sync_memory_index()
        first_rev = REVISION_RE.search(agent.history_messages[0]["content"]).group(1)

        agent.memory.text = "- [a](a.md) — 描述A\n- [b](b.md) — 描述B"
        agent._sync_memory_index()

        self.assertEqual(len(agent.history_messages), 2)
        second_rev = REVISION_RE.search(agent.history_messages[1]["content"]).group(1)
        self.assertNotEqual(first_rev, second_rev)
        self.assertIn("描述B", agent.history_messages[1]["content"])

    def test_revision_recovered_from_history_after_resume(self):
        """模拟 resume：新实例 + 已有注入消息 → 不重复注入。"""
        original = _make_agent("- [a](a.md) — 描述A")
        original._sync_memory_index()
        resumed = _make_agent(
            "- [a](a.md) — 描述A", history=original.history_messages
        )

        resumed._sync_memory_index()

        self.assertEqual(len(resumed.session_manager.appended), 0)
        self.assertEqual(len(resumed.history_messages), 1)

    def test_reinject_when_marker_dropped_by_compaction(self):
        """标记被上下文压缩裁掉后必须自动补注（无需额外逻辑）。"""
        agent = _make_agent("- [a](a.md) — 描述A")
        agent._sync_memory_index()
        self.assertEqual(len(agent.history_messages), 1)

        # 模拟 snip_compact 把中间消息替换成占位符
        agent.history_messages = [{"role": "user", "content": "[snipped 3 messages]"}]
        agent._sync_memory_index()

        self.assertEqual(len(agent.history_messages), 2)
        self.assertIn("描述A", agent.history_messages[-1]["content"])

    def test_empty_index_uses_placeholder(self):
        agent = _make_agent("")
        agent._sync_memory_index()
        self.assertIn("（暂无记忆）", agent.history_messages[0]["content"])

    def test_revision_is_12_hex_chars(self):
        agent = _make_agent("- [a](a.md) — 描述A")
        agent._sync_memory_index()
        match = REVISION_RE.search(agent.history_messages[0]["content"])
        self.assertIsNotNone(match, "注入消息必须带 revision 指纹")

    def test_non_string_content_does_not_crash(self):
        """历史里存在多模态 content 列表时不崩。"""
        agent = _make_agent(
            "- [a](a.md) — 描述A",
            history=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        )
        agent._sync_memory_index()
        self.assertEqual(len(agent.history_messages), 2)


class EnvironmentInjectionTests(unittest.TestCase):
    """环境与上下文（日期 / 星期 / 平台）—— 现场取值，变化才注入。"""

    def _agent_with_env(self, snapshot):
        """构造 Agent 并固定环境快照（避免依赖真实日期/平台）。"""
        agent = _make_agent()
        agent._environment_snapshot = staticmethod(lambda: list(snapshot))
        return agent

    def test_first_injection_contains_date_and_platform(self):
        agent = self._agent_with_env(
            [("当前日期", "2026-09-11"), ("星期", "周五"), ("运行平台", "darwin")]
        )
        agent._sync_environment()

        self.assertEqual(len(agent.history_messages), 1)
        content = agent.history_messages[0]["content"]
        self.assertTrue(content.startswith("<system-reminder>"))
        self.assertIn(f"<{ENV_TAG} revision=", content)
        self.assertIn("2026-09-11", content)
        self.assertIn("周五", content)
        self.assertIn("darwin", content)
        self.assertIs(agent.session_manager.appended[0][1], agent.history_messages[0])

    def test_idempotent_when_environment_unchanged(self):
        agent = self._agent_with_env([("当前日期", "2026-09-11")])
        agent._sync_environment()
        for _ in range(3):
            agent._sync_environment()
        self.assertEqual(len(agent.history_messages), 1)

    def test_reinject_when_date_changes(self):
        """跨天：日期变了才补一条。"""
        agent = self._agent_with_env([("当前日期", "2026-09-11")])
        agent._sync_environment()

        agent._environment_snapshot = staticmethod(
            lambda: [("当前日期", "2026-09-12")]
        )
        agent._sync_environment()

        self.assertEqual(len(agent.history_messages), 2)
        self.assertIn("2026-09-12", agent.history_messages[1]["content"])

    def test_reinject_when_platform_changes(self):
        """换机器 resume：平台变了才补一条。"""
        agent = self._agent_with_env([("运行平台", "darwin")])
        agent._sync_environment()

        agent._environment_snapshot = staticmethod(lambda: [("运行平台", "linux")])
        agent._sync_environment()

        self.assertEqual(len(agent.history_messages), 2)
        self.assertIn("linux", agent.history_messages[1]["content"])

    def test_revision_recovered_from_history_after_resume(self):
        snapshot = [("当前日期", "2026-09-11"), ("运行平台", "darwin")]
        original = self._agent_with_env(snapshot)
        original._sync_environment()

        resumed = self._agent_with_env(snapshot)
        resumed.history_messages = list(original.history_messages)
        resumed._sync_environment()

        self.assertEqual(len(resumed.session_manager.appended), 0)

    def test_snapshot_is_live_not_frozen(self):
        """实时取值：同一实例连续两次采集都反映当前系统状态。"""
        first = Agent._environment_snapshot()
        second = Agent._environment_snapshot()
        self.assertEqual(first, second)          # 同一天内稳定 → 指纹不变
        labels = [label for label, _ in first]
        self.assertEqual(labels, ["当前日期", "星期", "运行平台"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
