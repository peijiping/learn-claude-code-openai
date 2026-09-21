"""停止机制离线回归测试（2026-09-21 新增）。

背景：前端会话列表在 background 态（turn 已结束、后台子智能体仍在跑）显示
"执行中"，但发送按钮不显示"停止"，用户想停停不了。改造后：

  1. BackgroundManager.request_stop_all() —— 运行中的后台任务立即标 stopped
     并置各自 stop_event；has_running() 变 False；worker 迟到完成不得把
     stopped"复活"成 completed（否则守望会把被放弃的结果再续轮注入）。
  2. SubAgent.spawn_subagent(stop_event=...) —— 事件已置位时在迭代边界收束为
     aborted transcript（前端显示「已中断」徽标），不发任何 LLM 调用。

全部离线：不调 LLM、不触碰真实会话目录。
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

from background_manager import BackgroundManager  # noqa: E402
from subagent import SubAgent  # noqa: E402


def _wait_until(pred, timeout: float = 5.0) -> bool:
    """轮询等待条件成立（测试用，替代裸 sleep）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class TestRequestStopAll(unittest.TestCase):
    """BackgroundManager.request_stop_all 的状态机语义。"""

    def setUp(self):
        self.bm = BackgroundManager()

    def test_stop_all_marks_running_stopped(self):
        release = threading.Event()

        def executor():
            release.wait(5)
            return "late"

        bg_id = self.bm.start_background_task(
            "sub_agent", {"prompt": "任务"}, "call_1", executor)

        self.assertTrue(self.bm.has_running())
        stopped = self.bm.request_stop_all()
        self.assertEqual(stopped, [bg_id])
        self.assertFalse(self.bm.has_running())
        self.assertFalse(self.bm.has_completed_pending())
        with self.bm.background_lock:
            t = self.bm.background_tasks[bg_id]
            self.assertEqual(t["status"], "stopped")
            self.assertTrue(t["stop_event"].is_set())
        release.set()

    def test_worker_does_not_resurrect_stopped(self):
        """迟到完成不得把 stopped 复活成 completed（关键回归点）。"""
        release = threading.Event()
        before = set(threading.enumerate())

        def executor():
            release.wait(5)
            return "late result"

        self.bm.start_background_task(
            "sub_agent", {"prompt": "任务"}, "call_1", executor)
        self.bm.request_stop_all()
        with self.bm.background_lock:
            bg_id = list(self.bm.background_tasks)[0]
            self.assertEqual(self.bm.background_tasks[bg_id]["status"], "stopped")
        # 精确找到 worker 线程，等它真正跑完（含收尾的状态写入尝试）
        workers = set(threading.enumerate()) - before
        release.set()
        for t in workers:
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "worker 线程未在 5s 内结束")
        with self.bm.background_lock:
            self.assertEqual(self.bm.background_tasks[bg_id]["status"], "stopped")
            self.assertNotIn(bg_id, self.bm.background_results)
        self.assertFalse(self.bm.has_completed_pending())

    def test_normal_completion_unaffected(self):
        """不停止时：completed → collect → notified 原语义保持。"""
        bg_id = self.bm.start_background_task(
            "bash", {"command": "echo ok"}, "call_1", lambda: "ok")
        self.assertTrue(_wait_until(
            lambda: self.bm.background_tasks[bg_id]["status"] == "completed"))
        self.assertTrue(self.bm.has_completed_pending())
        notes = self.bm.collect_background_results()
        self.assertEqual(len(notes), 1)
        self.assertIn("ok", notes[0])
        self.assertEqual(self.bm.background_tasks[bg_id]["status"], "notified")
        self.assertEqual(self.bm.collect_background_results(), [])

    def test_stop_all_only_touches_running(self):
        """已 completed/notified 的任务不受 stop_all 影响。"""
        bg_done = self.bm.start_background_task(
            "bash", {"command": "echo ok"}, "call_1", lambda: "ok")
        self.assertTrue(_wait_until(
            lambda: self.bm.background_tasks[bg_done]["status"] == "completed"))
        release = threading.Event()
        bg_run = self.bm.start_background_task(
            "sub_agent", {"prompt": "任务"}, "call_2",
            lambda: release.wait(5) or "late")
        stopped = self.bm.request_stop_all()
        self.assertEqual(stopped, [bg_run])
        self.assertEqual(self.bm.background_tasks[bg_done]["status"], "completed")
        release.set()


class TestSubagentStopEvent(unittest.TestCase):
    """spawn_subagent 的协作式停止。"""

    def _make_stub(self) -> SubAgent:
        sub = SubAgent.__new__(SubAgent)
        sub.base_tools = []
        sub.tool_handlers = {}
        sub.tool_registry = None
        sub.sinks = []
        sub.sub_llm_client = None  # 预置停止信号时不应触达
        sub.model = "stub"
        sub.MAX_ITERATIONS = 100
        return sub

    def test_preset_stop_event_returns_aborted(self):
        ev = threading.Event()
        ev.set()
        summary, transcript = self._make_stub().spawn_subagent(
            "测试任务", tool_call_id="call_1", stop_event=ev)
        self.assertEqual(summary, "已停止")
        self.assertEqual(transcript["status"], "aborted")
        self.assertEqual(transcript["text"], "已停止")
        self.assertEqual(transcript["tool_call_id"], "call_1")
        self.assertEqual(transcript["error"], "")
        self.assertIsInstance(transcript["duration_ms"], int)

    def test_no_stop_event_default_params_unaffected(self):
        """不传 stop_event 时参数默认不改变既有签名行为（占位校验）。"""
        import inspect
        sig = inspect.signature(SubAgent.spawn_subagent)
        self.assertIsNone(sig.parameters["stop_event"].default)


if __name__ == "__main__":
    unittest.main()
