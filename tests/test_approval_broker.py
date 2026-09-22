#!/usr/bin/env python3
"""权限审批 broker（`agents/approval.py`）守护测试 —— 2026-09-22。

与 test_interaction_broker 同一双线程模型：真起一个线程跑 `broker.request()`
（模拟 turn 工作线程的 PreToolUse 阻塞），主线程负责结算（模拟 asyncio 事件
循环线程），不 mock 内部函数，断言：

- 事件流：`approval_request` → `approval_resolved`，载荷字段与 status 正确；
- 三选一映射：allow_once / allow_session / deny → 返回值对齐 gate 的
  APPROVE_* 契约（审批决定不进模型上下文，只有返回值回填）；
- 超时自结算（approval 特有）：亚秒级 deadline 到点自动判 timeout，
  给出确定性结局并广播 approval_resolved(status=timeout)；
- 兜底唤醒：只置 `stop_event`（不走 cancel_all）也能在轮询切片内收束；
- 幂等：迟到 / 重复 / 非法 decision / 未知 request_id 一律 False、不二次唤醒；
- **等待期不持锁**：request 阻塞期间主线程 `resolve` 立即生效
  （把 `done.wait()` 写进锁内的话这里会死锁 —— 本模块最要命的回归）；
- 撞车 fail-closed：同会话第二个并发审批立即拒绝，不排队；
- 重放：`pending_payloads()` 只含**已广播**的请求（announced 闸门）；
- 载荷收敛：超长 args 字符串值截断（防大信封）；
- `deliver` 抛异常不影响审批返回（钩子层契约：绝不向上抛）。

另含审批元数据的**落盘 → 加载回放链路**（session_manage tool 行旁挂 approval，
前端回放渲染徽标的数据源）与模型边界白名单投影（approval 不进模型上下文）。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from agent_full_v2 import MODEL_MSG_FIELDS  # noqa: E402
from session_manage import SessionManager  # noqa: E402
from approval import (  # noqa: E402
    OUTCOME_ALLOWED_ONCE,
    OUTCOME_ALLOWED_SESSION,
    OUTCOME_DENIED,
    OUTCOME_STOPPED,
    OUTCOME_TIMEOUT,
    ApprovalBroker,
)
from permission import (  # noqa: E402
    APPROVE_ALLOW_ONCE,
    APPROVE_ALLOW_SESSION,
    APPROVE_DENY,
    VALID_DECISIONS,
)

SID = "FAKESESSION01"

# 截断上限与后缀（与 approval._ARG_VALUE_MAX_CHARS 对齐；保持同步即回归点）
ARG_MAX = 600
ARG_SUFFIX = "…（截断）"


def _request_kwargs(**overrides) -> dict:
    """一次审批请求的标准参数（允许用例覆盖个别键）。"""
    kw = dict(
        tool_call_id="toolu_fake_1",
        tool_name="bash",
        args={"command": "rm -rf build"},
        trigger="dangerous_pattern",
        reason="危险命令模式：rm ",
        session_scope_hint="将允许匹配「rm 」的命令，直到会话结束",
        mode="default",
        timeout_seconds=30.0,
    )
    kw.update(overrides)
    return kw


class _Requester:
    """在独立线程里跑 `broker.request()`，主线程负责结算（复刻生产双线程模型）。

    `ready` 判据用 `broker.has_pending()`（announced 闸门）：pending 在广播
    approval_request **之前**注册，只等注册会出现"结算抢在广播之前"的竞态；
    has_pending 只认已广播的请求，广播真发出来了才开始结算。
    """

    def __init__(self, broker: ApprovalBroker, kwargs: dict, stop_event=None):
        self.broker = broker
        self.kwargs = dict(kwargs)
        if stop_event is not None:
            self.kwargs["stop_event"] = stop_event
        self.result = None
        self.exc = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.result = self.broker.request(**self.kwargs)
        except BaseException as e:  # noqa: BLE001 - 就是要抓住"抛了"
            self.exc = e

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 3.0
        while time.time() < deadline and not self.broker.has_pending():
            time.sleep(0.005)
        return self

    def __exit__(self, *exc_info):
        self.thread.join(timeout=5.0)
        return False

    @property
    def finished(self) -> bool:
        return not self.thread.is_alive()


class _BrokerTestCase(unittest.TestCase):
    def setUp(self):
        self.events: list[tuple[str, dict]] = []

        def deliver(kind, payload):
            self.events.append((kind, payload))

        self.broker = ApprovalBroker(SID, deliver)

    # ── 事件流辅助 ────────────────────────────────────────────
    def events_of(self, kind: str) -> list[dict]:
        return [p for (k, p) in self.events if k == kind]

    def request_id(self) -> str:
        self.assertTrue(self.events_of("approval_request"),
                        "approval_request 应已广播")
        return self.events_of("approval_request")[0]["request_id"]

    def requester(self, **kwargs) -> _Requester:
        return _Requester(self.broker, _request_kwargs(**kwargs))

    def settle_and_join(self, requester: _Requester, decision: str):
        """主线程结算 → 断言等待期不持锁（1 秒内必须醒，否则就是死锁回归）。"""
        self.assertTrue(self.broker.resolve(self.request_id(), decision))
        t0 = time.time()
        while time.time() - t0 < 1.0 and not requester.finished:
            time.sleep(0.005)
        self.assertTrue(requester.finished,
                         "resolve 后 1 秒内未唤醒 —— 等待期持锁的死锁回归")


# ═══════════════════════════════════════════════════════════════════
#  事件流与三选一映射
# ═══════════════════════════════════════════════════════════════════

class TestApprovalFlow(_BrokerTestCase):
    def _assert_request_payload(self, payload: dict):
        self.assertEqual(payload["session_id"], SID)
        self.assertTrue(payload["request_id"].startswith("apr_"))
        self.assertEqual(payload["tool_call_id"], "toolu_fake_1")
        self.assertEqual(payload["tool_name"], "bash")
        self.assertEqual(payload["trigger"], "dangerous_pattern")
        self.assertEqual(payload["mode"], "default")
        self.assertEqual(payload["timeout_seconds"], 30.0)
        self.assertIn("reason", payload)
        self.assertIn("session_scope_hint", payload)
        self.assertIn("created_at", payload)
        self.assertEqual(payload["args"], {"command": "rm -rf build"})

    def test_allow_once_flow(self):
        with self.requester() as rq:
            self.settle_and_join(rq, APPROVE_ALLOW_ONCE)
        self.assertIsNone(rq.exc)
        self.assertEqual(rq.result, APPROVE_ALLOW_ONCE)
        # 事件流：request → resolved，顺序与字段
        self.assertEqual([k for (k, _) in self.events],
                         ["approval_request", "approval_resolved"])
        self._assert_request_payload(self.events_of("approval_request")[0])
        resolved = self.events_of("approval_resolved")[0]
        self.assertEqual(resolved["status"], OUTCOME_ALLOWED_ONCE)
        self.assertEqual(resolved["decision"], "allow_once")
        self.assertEqual(resolved["session_id"], SID)
        self.assertEqual(resolved["tool_call_id"], "toolu_fake_1")
        self.assertIn("at", resolved)
        # 结算后重放视图必须为空
        self.assertEqual(self.broker.pending_payloads(), [])

    def test_allow_session_flow(self):
        with self.requester() as rq:
            self.settle_and_join(rq, APPROVE_ALLOW_SESSION)
        self.assertEqual(rq.result, APPROVE_ALLOW_SESSION)
        self.assertEqual(self.events_of("approval_resolved")[0]["status"],
                         OUTCOME_ALLOWED_SESSION)

    def test_deny_flow(self):
        with self.requester() as rq:
            self.settle_and_join(rq, APPROVE_DENY)
        self.assertEqual(rq.result, APPROVE_DENY)
        self.assertEqual(self.events_of("approval_resolved")[0]["status"],
                         OUTCOME_DENIED)

    def test_pending_payloads_replay(self):
        """在途期间重放视图返回已广播的请求载荷（新连接重放用）。"""
        with self.requester() as rq:
            pend = self.broker.pending_payloads()
            self.assertEqual(len(pend), 1)
            self.assertEqual(pend[0]["request_id"], self.request_id())
            self.settle_and_join(rq, APPROVE_DENY)
        self.assertEqual(self.broker.pending_payloads(), [])


# ═══════════════════════════════════════════════════════════════════
#  幂等与校验（迟到 / 重复 / 非法 / 未知）
# ═══════════════════════════════════════════════════════════════════

class TestApprovalIdempotency(_BrokerTestCase):
    def test_late_or_duplicate_resolve(self):
        with self.requester() as rq:
            rid = self.request_id()
            self.assertTrue(self.broker.resolve(rid, APPROVE_DENY))
            t0 = time.time()
            while time.time() - t0 < 1.0 and not rq.finished:
                time.sleep(0.005)
            # 重复提交：返回 False、不二次广播、不影响已定的结局
            self.assertFalse(self.broker.resolve(rid, APPROVE_ALLOW_ONCE))
        self.assertEqual(rq.result, APPROVE_DENY)
        self.assertEqual(len(self.events_of("approval_resolved")), 1)

    def test_invalid_decision_rejected_then_cancel_wakes(self):
        """非法 decision 丢弃且不结算；随后 cancel_all 能唤醒阻塞线程。"""
        with self.requester() as rq:
            self.assertFalse(self.broker.resolve(self.request_id(), "maybe"))
            self.assertFalse(rq.finished, "非法 decision 不应结算在途请求")
            n = self.broker.cancel_all(OUTCOME_STOPPED)
            self.assertEqual(n, 1)
            t0 = time.time()
            while time.time() - t0 < 1.0 and not rq.finished:
                time.sleep(0.005)
        self.assertEqual(rq.result, "stopped")
        self.assertEqual(self.events_of("approval_resolved")[0]["status"],
                         OUTCOME_STOPPED)

    def test_resolve_unknown_request_id(self):
        self.assertFalse(self.broker.resolve("apr_nonexistent", APPROVE_DENY))
        self.assertEqual(self.events, [])

    def test_cancel_all_is_idempotent(self):
        with self.requester() as rq:
            self.assertEqual(self.broker.cancel_all(OUTCOME_STOPPED), 1)
            t0 = time.time()
            while time.time() - t0 < 1.0 and not rq.finished:
                time.sleep(0.005)
            self.assertEqual(self.broker.cancel_all(OUTCOME_STOPPED), 0)
        self.assertEqual(rq.result, "stopped")

    def test_valid_decisions_are_exactly_three(self):
        """契约校验：前端三选一的 decision 取值（防止随意扩表）。"""
        self.assertEqual(set(VALID_DECISIONS),
                         {"allow_once", "allow_session", "deny"})


# ═══════════════════════════════════════════════════════════════════
#  超时自结算 / 停止兜底 / 关闭
# ═══════════════════════════════════════════════════════════════════

class TestApprovalTimeoutAndStop(_BrokerTestCase):
    def test_timeout_self_settles(self):
        """超时自结算：deadline 到点自动判 timeout（approval 特有兜底）。"""
        with self.requester(timeout_seconds=0.4) as rq:
            t0 = time.time()
            while time.time() - t0 < 2.0 and not rq.finished:
                time.sleep(0.01)
        self.assertTrue(rq.finished, "超时未自结算")
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(rq.result, "timeout")
        resolved = self.events_of("approval_resolved")[0]
        self.assertEqual(resolved["status"], OUTCOME_TIMEOUT)
        self.assertEqual(resolved["decision"], "")  # 超时无用户决定

    def test_zero_timeout_falls_back_to_default(self):
        """脏值（<=0）退回 DEFAULT_TIMEOUT_SECONDS，而不是立即超时。"""
        with self.requester(timeout_seconds=0) as rq:
            t0 = time.time()
            time.sleep(0.5)
            self.assertFalse(rq.finished, "timeout=0 应退回默认 300s 而非立即超时")
            self.settle_and_join(rq, APPROVE_DENY)
        self.assertLess(time.time() - t0, 2.0)

    def test_stop_event_wakes_within_poll_slice(self):
        """兜底一：不走 cancel_all、只置 stop_event，0.2s 轮询切片内收束。"""
        stop_evt = threading.Event()
        with self.requester(stop_event=stop_evt) as rq:
            stop_evt.set()
            t0 = time.time()
            while time.time() - t0 < 1.0 and not rq.finished:
                time.sleep(0.005)
        self.assertTrue(rq.finished, "stop_event 未在轮询切片内唤醒")
        self.assertEqual(rq.result, "stopped")
        self.assertEqual(self.events_of("approval_resolved")[0]["status"],
                         OUTCOME_STOPPED)

    def test_close_settles_pending_and_rejects_new(self):
        """close()：解锁所有在途 + 后续新审批直接 stopped（不再广播）。"""
        with self.requester() as rq:
            self.broker.close()
            t0 = time.time()
            while time.time() - t0 < 1.0 and not rq.finished:
                time.sleep(0.005)
            self.assertEqual(rq.result, "stopped")
        n_events = len(self.events)
        # 关闭后的新请求：立即 stopped，且不再广播 approval_request
        result = self.broker.request(**_request_kwargs(tool_call_id="toolu_2"))
        self.assertEqual(result, "stopped")
        self.assertEqual(len(self.events), n_events)


# ═══════════════════════════════════════════════════════════════════
#  撞车 / 载荷收敛 / 投递容错
# ═══════════════════════════════════════════════════════════════════

class TestApprovalEdgeCases(_BrokerTestCase):
    def test_concurrent_request_fail_closed(self):
        """同会话撞车（理论不可达，防御性回归）：第二个并发审批立即拒绝。"""
        with self.requester() as rq:
            second = self.broker.request(
                **_request_kwargs(tool_call_id="toolu_conflict"))
            self.assertEqual(second, APPROVE_DENY)  # fail-closed 拒绝，不排队
            self.settle_and_join(rq, APPROVE_DENY)

    def test_args_clipped_in_broadcast(self):
        """超长 args 字符串截断：防 run_write 携带整份文件内容撑爆广播。"""
        long_args = {"content": "x" * (ARG_MAX + 400)}
        with _Requester(self.broker,
                        _request_kwargs(args=long_args, tool_name="run_write")) as rq:
            self.settle_and_join(rq, APPROVE_DENY)
        payload = self.events_of("approval_request")[0]
        clipped = payload["args"]["content"]
        self.assertTrue(clipped.endswith(ARG_SUFFIX))
        self.assertLessEqual(len(clipped), ARG_MAX + len(ARG_SUFFIX))

    def test_deliver_exception_does_not_break_request(self):
        """deliver 抛异常：审批链路仍完整（钩子层契约：绝不向上抛）。"""
        broken = ApprovalBroker(SID, lambda kind, payload: (_ for _ in ()).throw(
            RuntimeError("boom")))
        with _Requester(broken, _request_kwargs()) as rq:
            rid = broken.pending_payloads()[0]["request_id"]
            self.assertTrue(broken.resolve(rid, APPROVE_ALLOW_SESSION))
            t0 = time.time()
            while time.time() - t0 < 1.0 and not rq.finished:
                time.sleep(0.005)
        self.assertEqual(rq.result, APPROVE_ALLOW_SESSION)

    def test_request_with_non_dict_args(self):
        """脏参数（非 dict）不炸：按空载荷广播，审批链路照常。"""
        with _Requester(self.broker, _request_kwargs(args=None)) as rq:
            payload = self.broker.pending_payloads()[0]
            self.assertEqual(payload["args"], {})
            self.settle_and_join(rq, APPROVE_DENY)


# ═══════════════════════════════════════════════════════════════════
#  审批元数据的落盘 → 加载回放链路（范式 C 的另一半保证）
# ═══════════════════════════════════════════════════════════════════

class TestApprovalHistoryRoundtrip(unittest.TestCase):
    """审批决定的落盘与回放：jsonl tool 行旁挂 approval（前端徽标数据源），
    且 MODEL_MSG_FIELDS 白名单投影把它挡在模型上下文之外（读取不设限、
    发送白名单 —— 与 usage/model_info 同一契约）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.chat_dir = Path(self._td.name)
        # SessionManager → ContextCompact → LLMClient 构造时校验密钥 env
        # （只构造客户端对象、不发任何请求）：测试环境无真实凭据，
        # 塞假值通过构造校验，addCleanup 恢复原值，不污染进程内其他测试。
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
            old = os.environ.get(key)
            os.environ[key] = "test-dummy"
            if old is None:
                self.addCleanup(os.environ.pop, key, None)
            else:
                self.addCleanup(os.environ.__setitem__, key, old)
        self.sm = SessionManager(self.chat_dir, "system-prompt-for-test")

    def _write_rows(self, session: Path, rows: list[dict]):
        session.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    def test_tool_row_approval_roundtrip(self):
        session = self.chat_dir / "session_test.jsonl"
        self._write_rows(session, [
            self.sm._message_to_json_row({
                "role": "assistant", "content": "", "reasoning_content": "",
                "tool_calls": [{"id": "toolu_1", "type": "function",
                                "function": {"name": "bash",
                                             "arguments": json.dumps(
                                                 {"command": "rm x"})}}],
            }),
            self.sm._message_to_json_row({
                "role": "tool",
                "content": "Error: Permission denied by user",
                "tool_call_id": "toolu_1",
                "approval": {"decision": "denied",
                             "trigger": "dangerous_pattern",
                             "pattern": "rm ", "mode": "default",
                             "at": "2026-09-22T11:00:00"},
            }),
        ])
        msgs = self.sm.load_session_history(session)
        tool_rows = [m for m in msgs if m.get("role") == "tool"]
        self.assertEqual(len(tool_rows), 1)
        # 读取保留：回放徽标的数据源
        self.assertEqual(tool_rows[0]["approval"]["decision"], "denied")
        # 模型边界：白名单投影后 approval 必须消失（不进模型上下文）
        projected = {k: tool_rows[0][k] for k in MODEL_MSG_FIELDS
                     if k in tool_rows[0]}
        self.assertNotIn("approval", projected)
        self.assertEqual(projected["tool_call_id"], "toolu_1")

    def test_allowed_tool_row_writes_no_approval_field(self):
        # 允许执行的工具行不写 approval（无字段 = 正常流，前端按普通工具条渲染）
        row = self.sm._message_to_json_row({
            "role": "tool", "content": "ok", "tool_call_id": "toolu_2",
        })
        self.assertNotIn("approval", row)


if __name__ == "__main__":
    unittest.main()
