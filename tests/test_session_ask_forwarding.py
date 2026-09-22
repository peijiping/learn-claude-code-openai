#!/usr/bin/env python3
"""SessionRuntime / SessionRuntimeRegistry 对 ask_user 的转发守护测试 —— 2026-09-21。

`ask_user` 的阻塞线程由 broker 持有，但**解锁它的两条生产路径**都在
SessionRuntime 上，漏一条就会挂死线程：

1. `request_stop()` 必须 `cancel_all("stopped")` —— 否则用户点"停止"时那个
   工作线程会一直卡在 `done.wait()`，停止按钮形同失效；
   而且这一步必须在 `if self.agent is not None` **之外**（agent 尚未构造时也可能
   有在途提问）。
2. `SessionRuntimeRegistry.remove()` 必须 `close()` —— 否则删除会话时线程悬空。

另测转发面的透传与幂等语义（`resolve_ask` / `resolve_ask_free_text` /
`cancel_ask` / `pending_interactions`）。

入口：`.venv/bin/python -m unittest discover -s tests`
"""

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from session_runtime import SessionRuntime, SessionRuntimeRegistry  # noqa: E402

SID = "FAKESESSION01"

QUESTIONS = [{
    "id": "color", "header": "主题色", "question": "你希望主题色是？",
    "options": [{"label": "蓝色"}, {"label": "绿色"}],
}]


class _AskThread:
    """在独立线程里跑 `broker.ask()`（复刻 turn 工作线程的阻塞语义）。"""

    def __init__(self, rt: SessionRuntime, questions=None):
        self.rt = rt
        self.questions = questions or QUESTIONS
        self.result = None
        self.exc = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            # 直接驱动 broker：本测试对象是 runtime 的**转发**，不是工具接线
            # （工具层接线另见 test_ask_user_bucketing / test_ask_user_schema_contract）
            self.result = self.rt._interaction.ask(self.questions, tool_call_id="toolu_1")
        except BaseException as e:  # noqa: BLE001
            self.exc = e

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 3.0
        while time.time() < deadline and not self.rt.has_pending_interaction():
            time.sleep(0.005)
        return self

    def __exit__(self, *exc_info):
        self.thread.join(timeout=5.0)
        return False

    def wait_finished(self, timeout=3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and self.thread.is_alive():
            time.sleep(0.02)
        return not self.thread.is_alive()


def _runtime(deliver=None) -> SessionRuntime:
    return SessionRuntime(
        SID,
        deliver or (lambda kind, payload: None),
        reply_sessions=None,
        load_meta=lambda sid: {},
    )


class TestRuntimeForwarding(unittest.TestCase):
    def setUp(self):
        self.events: list[tuple[str, dict]] = []
        self.rt = _runtime(lambda kind, payload: self.events.append((kind, payload)))

    def rid(self) -> str:
        return self.rt.pending_interactions()[0]["request_id"]

    def test_pending_snapshot_shape(self):
        with _AskThread(self.rt):
            payloads = self.rt.pending_interactions()
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["session_id"], SID)
            self.assertEqual(payloads[0]["tool_call_id"], "toolu_1")
            self.assertTrue(self.rt.has_pending_interaction())
            self.assertTrue(self.rt.resolve_ask(self.rid(), [
                {"question_id": "color", "selected": ["绿色"]}
            ]))
        self.assertFalse(self.rt.has_pending_interaction())

    def test_resolve_ask_wakes_worker(self):
        with _AskThread(self.rt) as a:
            self.assertTrue(self.rt.resolve_ask(self.rid(), [
                {"question_id": "color", "selected": ["蓝色"]}
            ]))
        self.assertTrue(a.wait_finished())
        self.assertIn("→ 蓝色", a.result)
        self.assertEqual([k for k, _ in self.events], ["ask_request", "ask_resolved"])

    def test_resolve_ask_is_idempotent(self):
        with _AskThread(self.rt) as a:
            rid = self.rid()
            self.assertTrue(self.rt.resolve_ask(rid, []))
            self.assertFalse(self.rt.resolve_ask(rid, []))
        self.assertEqual([k for k, _ in self.events].count("ask_resolved"), 1)

    def test_resolve_free_text(self):
        with _AskThread(self.rt) as a:
            self.assertTrue(self.rt.resolve_ask_free_text("我改主意了"))
        self.assertTrue(a.wait_finished())
        self.assertIn("我改主意了", a.result)

    def test_resolve_free_text_without_pending_returns_false(self):
        """无在途提问时返回 False —— ws_bridge 据此回落正常 chat 流程。"""
        self.assertFalse(self.rt.resolve_ask_free_text("普通消息"))
        self.assertEqual(self.events, [])

    def test_cancel_ask(self):
        with _AskThread(self.rt) as a:
            self.assertTrue(self.rt.cancel_ask(self.rid()))
        self.assertTrue(a.wait_finished())
        self.assertIn("取消了本次提问", a.result)

    def test_cancel_ask_unknown_returns_false(self):
        self.assertFalse(self.rt.cancel_ask("ask_nope"))


class TestStopUnblocksAsk(unittest.TestCase):
    """停止按钮必须能打断阻塞中的提问（否则那个工作线程永远卡住）。"""

    def test_request_stop_cancels_pending_without_agent(self):
        rt = _runtime()
        self.assertIsNone(rt.agent)  # 关键：agent 未构造时也必须生效
        with _AskThread(rt) as a:
            rt.request_stop()
            self.assertTrue(a.wait_finished(), "request_stop 未解锁阻塞中的 ask 线程")
        self.assertEqual(a.exc, None)
        self.assertIn("本轮已被用户停止", a.result)
        self.assertTrue(rt.stop_evt.is_set())
        self.assertFalse(rt.has_pending_interaction())

    def test_start_turn_clears_stop_but_ask_still_works(self):
        """新一轮开始时 stop_evt 被清空；之后发起的提问仍能正常等待作答。"""
        rt = _runtime()
        rt.stop_evt.set()
        rt.stop_evt.clear()          # 复刻 start_turn 入口的重置
        with _AskThread(rt) as a:
            self.assertTrue(rt.resolve_ask(rt.pending_interactions()[0]["request_id"], []))
        self.assertTrue(a.wait_finished())
        self.assertNotIn("停止", a.result)


class TestRegistryRemoveCloses(unittest.TestCase):
    """删除会话必须解锁在途提问线程。"""

    def setUp(self):
        self.events: list[tuple[str, dict]] = []
        self.reg = SessionRuntimeRegistry(
            deliver=lambda kind, payload: self.events.append((kind, payload)),
            reply_sessions=None,
            load_meta=lambda sid: {},
        )

    def test_remove_unblocks_pending_ask(self):
        rt = self.reg.get_or_create(SID)
        with _AskThread(rt) as a:
            self.reg.remove(SID)
            self.assertTrue(a.wait_finished(), "registry.remove 未解锁阻塞中的 ask 线程")
        self.assertIsNone(rt.agent)
        self.assertIsNone(self.reg.get(SID))
        self.assertFalse(rt.has_pending_interaction())

    def test_remove_unknown_sid_is_noop(self):
        self.reg.remove("NOPE")  # 不抛异常

    def test_get_or_create_keeps_workspace_and_broker(self):
        rt = self.reg.get_or_create(SID)
        self.assertIs(self.reg.get_or_create(SID), rt)  # 已存在则复用（不漂移）
        self.assertFalse(rt.has_pending_interaction())


if __name__ == "__main__":
    unittest.main(verbosity=2)
