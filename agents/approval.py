#!/usr/bin/env python3
"""
approval.py - 权限审批 broker（范式 C：审批决定不进模型上下文）

背景与定位（2026-09-22）
------------------------
与 `interaction.py` 的 `ask_user`（范式 A：答案作为 tool_result 进模型上下文，
模型在同一回合继续）相对，审批走**范式 C**：

    PreToolUse 判定 ask → broker 广播 approval_request → 前端弹卡片
    → 用户三选一 → broker 返回**结局**（allow_once / allow_session /
    deny / timeout / stopped）→ 只有结局文案回填 tool_result，
    「谁点的、点了什么、为什么」这些 UI 元数据走 tool 行旁挂 `approval`
    字段落盘（`load_session_history` 三字段投影 → 天然不进模型上下文）。

为什么可以阻塞：与 ask_user 完全同一条线程模型 —— `SessionRuntime.start_turn()`
用 `asyncio.to_thread()` 把 `run_turn` 派发到工作线程，`permission_hook` 在
PreToolUse（turn 工作线程）里同步调用 `gate.check_tool_call` → 本模块阻塞，
**不会卡住 asyncio 事件循环**。

线程模型（逐条克隆 interaction.py，违反即死锁）
------------------------------------------------
- 工作线程：`request()` 阻塞在 `pending.done.wait(_POLL_SECONDS)` 循环上。
  **等待期绝不持有 `self._lock`** —— 否则事件循环线程的 `resolve()` 拿不到锁。
- 事件循环线程：`resolve() / cancel_all()` 在锁内改状态、**锁外**唤醒
  （`done.set()`）与投递事件。`_emit()` 也在锁外。
- 唤醒三路：主路 = `done.set()`（resolve / cancel_all 触发）；
  兜底一 = 每 0.2s 检查 `stop_event`（漏网的停止路径）；
  兜底二 = **超时自结算**（approval 特有）：deadline 到点自动判 timeout，
  给出确定性结局 —— 审批挂起期间 turn 无法推进，用户离开时不能永远挂着。

绝不抛异常（钩子层契约）
-------------------------
`PermissionGate.check_tool_call` 不在 try 内调用本模块，因此 `request()`
任何异常都必须就地消化并降级为 `deny`（fail-closed：宁可多拒，不可放行）。

独占性
------
同会话同一时刻至多一个在途审批（工具串行执行，`permission_hook` 在
PreToolUse 顺序触发）。万一撞车（理论不可达）：fail-closed 直接拒绝本次，
不排队 —— 排队会让两条审批卡片叠在消息流里且语义混乱。

依赖方向：`permission → 本模块零依赖`（broker 由 SessionRuntime 注入 gate）；
本模块 import permission 的**纯常量**（APPROVE_* / VALID_DECISIONS），无环。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from logger import get_logger
from permission import (
    APPROVE_ALLOW_SESSION,
    APPROVE_ALLOW_ONCE,
    APPROVE_DENY,
    DEFAULT_TIMEOUT_SECONDS,
    VALID_DECISIONS,
)

log = get_logger("approval")

# 等待循环的轮询切片：兜底检查 stop_event / 超时 deadline；正常唤醒靠 done.set()
_POLL_SECONDS = 0.2

# ── 结局常量（也是 approval_resolved.status 的取值，文档 §4.2）─────────
OUTCOME_ALLOWED_ONCE = "allowed_once"
OUTCOME_ALLOWED_SESSION = "allowed_session"
OUTCOME_DENIED = "denied"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_STOPPED = "stopped"

# 用户决定（approval_answer.decision）→ 结局（approval_resolved.status）
_DECISION_TO_OUTCOME = {
    APPROVE_ALLOW_ONCE: OUTCOME_ALLOWED_ONCE,
    APPROVE_ALLOW_SESSION: OUTCOME_ALLOWED_SESSION,
    APPROVE_DENY: OUTCOME_DENIED,
}

# args 载荷里单个字符串值的截断上限：run_write 可能携带整份文件内容，
# 审批卡片只需要 command / path 级别的可读信息，防大信封撑爆广播。
_ARG_VALUE_MAX_CHARS = 600


def _clip_args(args) -> dict:
    """把工具参数收敛成可广播的展示载荷：字符串超长截断，其余原样。"""
    if not isinstance(args, dict):
        return {}
    out: dict = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > _ARG_VALUE_MAX_CHARS:
            out[key] = value[:_ARG_VALUE_MAX_CHARS] + "…（截断）"
        else:
            out[key] = value
    return out


@dataclass
class ApprovalPending:
    """一次在途审批。`done` 是跨线程唯一唤醒原语。"""

    request_id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    args: dict
    trigger: str          # 触发类型（dangerous_pattern / outside_dir / mcp_destructive …）
    reason: str           # 人类可读的触发原因（卡片正文）
    session_scope_hint: str  # 「会话内允许」按钮的预览副文案
    mode: str             # 当时会话模式（default / full_access）
    timeout_seconds: float
    created_at: float
    done: threading.Event = field(default_factory=threading.Event)
    outcome: str = ""     # "" = 仍在途；否则 OUTCOME_* 之一
    decision: str = ""    # 用户决定原文（allow_once / allow_session / deny；超时/停止为空）
    announced: bool = False  # approval_request 是否已广播（见 _announced_locked）

    def request_payload(self) -> dict:
        """approval_request 事件载荷（只含可序列化字段，args 已截断）。"""
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "args": _clip_args(self.args),
            "trigger": self.trigger,
            "reason": self.reason,
            "session_scope_hint": self.session_scope_hint,
            "mode": self.mode,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
        }

    def resolved_payload(self) -> dict:
        """approval_resolved 事件载荷（status 见 OUTCOME_* 常量）。"""
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "tool_call_id": self.tool_call_id,
            "status": self.outcome,
            "decision": self.decision,
            "at": time.time(),
        }


class ApprovalBroker:
    """单会话的权限审批 broker。

    一个 `SessionRuntime` 持有一个实例（天然带 session_id 与 deliver），
    由 runtime 在 `build_agent()` 里注入给该会话 Agent 的
    `permission_gate`（`gate.attach_broker(broker)`）。

    **同一会话同一时刻至多一个在途审批**（PreToolUse 串行触发），
    pending 表用 dict 只是为了按 request_id 精确配对与幂等丢弃迟到作答。
    """

    def __init__(self, session_id: str, deliver=None):
        self._sid = session_id
        self._deliver = deliver
        self._lock = threading.Lock()
        self._pending: dict[str, ApprovalPending] = {}
        self._closed = False

    # ══════════════════════════════════════════════════════════
    #  钩子入口（在 turn 工作线程里阻塞；由 PermissionGate 调用）
    # ══════════════════════════════════════════════════════════

    def request(self, *, tool_call_id: str, tool_name: str, args: dict,
                trigger: str, reason: str, session_scope_hint: str,
                mode: str, timeout_seconds, stop_event=None) -> str:
        """注册一次审批 → 广播 → 阻塞等作答 → 返回结局。

        返回值契约（对齐 permission.py 的 APPROVE_* 常量）：
            "allow_once"    → 本次放行（gate 不记任何账）
            "allow_session" → 本次放行 + gate 记会话内允许
            "deny" / "timeout" / "stopped" → 阻断（gate 记 approval 字段并回填文案）

        永不抛异常：任何异常降级为 deny（fail-closed）。
        """
        try:
            # 超时收敛：调用方（store）已钳 60–3600，这里只防脏值；
            # 不设下限钳制 —— 单测需要亚秒级超时。
            try:
                timeout = float(timeout_seconds)
                if timeout <= 0:
                    timeout = float(DEFAULT_TIMEOUT_SECONDS)
            except (TypeError, ValueError):
                timeout = float(DEFAULT_TIMEOUT_SECONDS)

            with self._lock:  # ← 只在注册期持锁
                if self._closed:
                    return OUTCOME_STOPPED
                if self._pending:
                    # 理论不可达（PreToolUse 串行）；万一撞车 fail-closed 拒绝。
                    # 返回值须对齐 APPROVE_* 契约（"deny"），不能返回裸结局词
                    # "denied"（那是 approval_resolved.status 的词族）。
                    log.warning("session_%s 审批撞车（已有在途请求），本次自动拒绝",
                                self._sid)
                    return APPROVE_DENY
                pend = ApprovalPending(
                    request_id="apr_" + uuid.uuid4().hex[:10],
                    session_id=self._sid,
                    tool_call_id=str(tool_call_id or ""),
                    tool_name=str(tool_name or ""),
                    args=args if isinstance(args, dict) else {},
                    trigger=str(trigger or ""),
                    reason=str(reason or ""),
                    session_scope_hint=str(session_scope_hint or ""),
                    mode=str(mode or ""),
                    timeout_seconds=timeout,
                    created_at=time.time(),
                )
                self._pending[pend.request_id] = pend

            log.info("session_%s 发起审批 %s（%s: %s，timeout=%ss，tool_call=%s）",
                     self._sid, pend.request_id, tool_name, trigger, timeout,
                     pend.tool_call_id or "-")
            self._emit("approval_request", pend.request_payload())
            # ⚠️ 广播之后才标记"对外可见"（announced 闸门，克隆 ask 的教训）：
            # 否则前端会先收到 approval_resolved 再收到 approval_request，
            # 表现为"卡片还没出现就被判定已解决"。
            with self._lock:
                pend.announced = True

            deadline = time.monotonic() + timeout
            wait = pend.done.wait  # 局部绑定，循环里省一次属性查找
            while True:            # ← 等待期绝不持锁（否则 resolve 死锁）
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 超时自结算（approval 特有的兜底二）：确定性结局
                    self._settle(pend, OUTCOME_TIMEOUT, "")
                    break
                if wait(min(_POLL_SECONDS, remaining)):
                    break
                # 兜底一：漏网的停止路径（没走 cancel_all 的）在 0.2s 内收束
                if stop_event is not None and stop_event.is_set():
                    self._settle(pend, OUTCOME_STOPPED, "")
                    break

            log.info("session_%s 审批结束 %s（%s）",
                     self._sid, pend.request_id, pend.outcome or "?")
            # 结局 → 返回值（对齐 gate 的契约常量）
            if pend.outcome == OUTCOME_ALLOWED_ONCE:
                return APPROVE_ALLOW_ONCE
            if pend.outcome == OUTCOME_ALLOWED_SESSION:
                return APPROVE_ALLOW_SESSION
            if pend.outcome == OUTCOME_TIMEOUT:
                return "timeout"
            if pend.outcome == OUTCOME_STOPPED:
                return "stopped"
            return APPROVE_DENY
        except Exception as e:  # noqa: BLE001 - 钩子层绝不向上抛
            log.error("审批执行异常: %s: %s", type(e).__name__, e, exc_info=True)
            return APPROVE_DENY

    # ══════════════════════════════════════════════════════════
    #  结算（事件循环线程调用；幂等）
    # ══════════════════════════════════════════════════════════

    def resolve(self, request_id: str, decision: str) -> bool:
        """前端三选一作答（approval_answer）。迟到/重复/非法返回 False（无副作用）。"""
        decision = str(decision or "").strip()
        if decision not in VALID_DECISIONS:
            log.warning("session_%s approval_answer 丢弃：非法 decision（%s）",
                        self._sid, decision)
            return False
        pend = self._peek(request_id)
        if pend is None:
            log.warning("session_%s approval_answer 丢弃：请求不存在或已结算（%s）",
                        self._sid, request_id)
            return False
        outcome = _DECISION_TO_OUTCOME[decision]
        return self._settle(pend, outcome, decision)

    def cancel_all(self, reason: str = OUTCOME_STOPPED) -> int:
        """停止按钮 / 会话销毁：结算本会话所有在途审批；返回结算条数。"""
        with self._lock:
            pends = list(self._pending.values())
        n = 0
        for pend in pends:
            if self._settle(pend, OUTCOME_STOPPED, ""):
                n += 1
        if n:
            log.info("session_%s cancel_all(%s)：结算 %d 条在途审批",
                     self._sid, reason, n)
        return n

    def close(self) -> None:
        """会话被销毁时收尾：标记关闭并解锁所有阻塞线程。"""
        with self._lock:
            self._closed = True
        self.cancel_all(OUTCOME_STOPPED)

    # ══════════════════════════════════════════════════════════
    #  查询（重连重放 / status_query）
    # ══════════════════════════════════════════════════════════

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._announced_locked())

    def pending_payloads(self) -> list[dict]:
        """所有**已广播**的在途审批的 approval_request 载荷（新连接重放用）。"""
        with self._lock:
            pends = self._announced_locked()
        return [p.request_payload() for p in pends]

    # ══════════════════════════════════════════════════════════
    #  内部
    # ══════════════════════════════════════════════════════════

    def _announced_locked(self) -> list[ApprovalPending]:
        """已广播、因而可被**外部路径**结算的在途审批（调用方必须持锁）。

        不变量：`approval_request` 一定先于任何外部触发的 `approval_resolved`
        到达前端。`resolve()` 经 `_peek()` 走这道闸，"还没广播就被作答"在外部
        不可达。`cancel_all()` **不**走这道闸 —— 停止/删除要能结算任何状态
        （多发给一个前端不认识的 request_id 是无害的）。
        """
        return [p for p in self._pending.values() if p.announced]

    def _peek(self, request_id: str) -> ApprovalPending | None:
        with self._lock:
            pend = self._pending.get(request_id)
            if pend is None or pend.outcome or not pend.announced:
                return None
            return pend

    def _settle(self, pend: ApprovalPending, outcome: str, decision: str) -> bool:
        """唯一的结算出口：锁内改状态 + 移出 pending，锁外投递与唤醒。"""
        with self._lock:
            if pend.outcome:      # 已被其它路径结算 → 幂等丢弃
                return False
            pend.outcome = outcome
            pend.decision = str(decision or "")
            self._pending.pop(pend.request_id, None)
        self._emit("approval_resolved", pend.resolved_payload())
        pend.done.set()           # ← 必须在锁外
        return True

    def _emit(self, kind: str, payload: dict) -> None:
        """投递事件；任何异常只记日志，绝不影响审批的返回与 turn 收尾。"""
        if self._deliver is None:
            return
        try:
            self._deliver(kind, payload)
        except Exception as e:  # noqa: BLE001
            log.error("session_%s %s 事件投递失败: %s: %s",
                      self._sid, kind, type(e).__name__, e, exc_info=True)
