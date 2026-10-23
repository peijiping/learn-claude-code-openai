#!/usr/bin/env python3
"""
interaction.py - 交互提问 broker（模型向用户提问，阻塞等待作答）

背景与定位（2026-09-21）
------------------------
`ask_user` 工具把「模型请求用户选择」实现成一次**普通的 tool call**（范式 A）：

    模型产出 tool_call(ask_user) → 工具 handler 阻塞等待 → 用户在前端作答
    → 答案作为 tool_result 回填 → 模型在**同一个回合内**据此继续。

为什么可以阻塞：`SessionRuntime.start_turn()` 用 `asyncio.to_thread()` 把
`run_turn` 派发到工作线程，**阻塞该线程不会卡住 asyncio 事件循环**。这是本模块
能成立的前提。

为什么不用"模型输出特殊 JSON 正文块 + 客户端解析 + 再发一条用户消息"：
那条路要自己重新实现 schema 校验、流式聚合、回放配对与压缩兼容；而工具调用
这条通道**已经全部具备**（provider 侧 schema 强校验 / tool_call 流式事件 /
tool_call_id 配对 / jsonl 落盘 / 回放配对的 `_tc_ids`）。Claude Code、Codex、
Trae、Cline 用的都是这条。

线程模型（关键，违反即死锁）
--------------------------
- 工作线程：`ask()` 阻塞在 `pending.done.wait(_POLL_SECONDS)` 循环上。
  **等待期绝不持有 `self._lock`** —— 否则事件循环线程的 `resolve()` 永远拿不到锁。
- 事件循环线程：`resolve() / cancel() / cancel_all()` 在锁内改状态、**锁外**唤醒
  （`done.set()`）与投递事件。`_emit()` 也在锁外，避免与事件循环重入。
- 唤醒分两路：主路 = `done.set()`（由 resolve / cancel / cancel_all 触发）；
  兜底 = 每 0.2s 检查一次 `stop_event`，防止某条没走 `cancel_all` 的停止路径漏网。

绝不抛异常（工具层契约）
-------------------------
工具层"永远返回东西、绝不抛异常"。本模块所有公开方法保证只返回
`str / bool / int / list`，异常一律降级为 `Error: ...` 文本。

**不做 CLI `input()` 兜底**：silent / cron / 后台线程场景可能永远无人应答，
阻塞式 `input()` 会把线程挂死（与 `hooks.permission_hook` 在 silent 下直接拒绝
是同一条教训）。无 broker 时由调用方返回明确的 Error 文本。

依赖：仅标准库 + `logger`。**不 import 任何引擎模块**（会成环）。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from logger import get_logger

log = get_logger("interaction")

# ── 规范化上限（与工具 schema 的 minItems/maxItems 一致）────────────────
MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4
HEADER_MAX_CHARS = 12
LABEL_MAX_CHARS = 60
DEFAULT_CUSTOM_LABEL = "其他"

# 等待循环的轮询切片：仅用于兜底检查 stop_event；正常唤醒靠 done.set()
_POLL_SECONDS = 0.2

# ── 结算结果（也是 ask_resolved.status 的取值）─────────────────────────
OUTCOME_ANSWERED = "answered"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_STOPPED = "stopped"

# ── 回填给模型的固定文案（前端只读小结块原样展示同一份文本）───────────
CANCELLED_TEXT = (
    "用户取消了本次提问（未作答）。\n"
    "不要重复追问同一个问题；请基于现有信息自行选择最合理的默认方案继续推进，"
    "并在回复中说明你采用了哪个默认值。"
)
STOPPED_TEXT = (
    "本轮已被用户停止，提问未获回答。\n"
    "请立即停止当前工作，不要再调用任何工具，简短说明你停在哪里即可。"
)
BUSY_TEXT = "Error: 当前已有待作答的提问，请等待用户作答后再发起新的提问。"


def _clip(value, limit: int) -> str:
    s = str(value if value is not None else "").strip()
    return s if len(s) <= limit else s[:limit]


def normalize_questions(raw) -> tuple[list | None, str]:
    """规范化模型给出的 questions 参数。

    返回 `(questions, "")` 或 `(None, 原因)`。**模型会乱填**，这里必须严防：
    题数、选项数、必填字段、label 唯一性、批内 id 唯一性都在此收敛，
    保证后续所有环节（广播 payload / 前端渲染 / 答案对齐）拿到的是干净结构。
    """
    if not isinstance(raw, list) or not raw:
        return None, "questions 必须是非空数组"
    if len(raw) > MAX_QUESTIONS:
        return None, f"最多 {MAX_QUESTIONS} 个问题（收到 {len(raw)} 个）"

    out: list[dict] = []
    used_ids: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, f"第 {idx + 1} 个问题不是对象"
        question = _clip(item.get("question"), 500)
        if not question:
            return None, f"第 {idx + 1} 个问题缺少 question"

        qid = _clip(item.get("id"), 64) or f"q{idx + 1}"
        if qid in used_ids:
            qid = f"{qid}_{idx + 1}"
        used_ids.add(qid)

        header = _clip(item.get("header"), HEADER_MAX_CHARS) or question[:HEADER_MAX_CHARS]

        options_raw = item.get("options")
        if not isinstance(options_raw, list) or len(options_raw) < MIN_OPTIONS:
            return None, (
                f"第 {idx + 1} 题的 options 至少要 {MIN_OPTIONS} 个"
            )
        if len(options_raw) > MAX_OPTIONS:
            return None, f"第 {idx + 1} 题的 options 最多 {MAX_OPTIONS} 个"

        options: list[dict] = []
        seen_labels: set[str] = set()
        for opt in options_raw:
            if not isinstance(opt, dict):
                return None, f"第 {idx + 1} 题的选项必须是对象"
            label = _clip(opt.get("label"), LABEL_MAX_CHARS)
            if not label:
                return None, f"第 {idx + 1} 题存在空 label 的选项"
            if label in seen_labels:
                return None, f"第 {idx + 1} 题的选项 label 重复：{label}"
            seen_labels.add(label)
            desc = _clip(opt.get("description"), 300)
            options.append({"label": label, "description": desc})

        out.append({
            "id": qid,
            "header": header,
            "question": question,
            "multi_select": bool(item.get("multi_select")),
            # allow_custom 显式建模（不靠 label == "其他" 这类隐式约定）
            "allow_custom": bool(item.get("allow_custom", True)),
            "custom_label": _clip(item.get("custom_label"), 20) or DEFAULT_CUSTOM_LABEL,
            "options": options,
        })
    return out, ""


def normalize_answers(questions: list, raw) -> list:
    """把前端回传的 answers 对齐到 questions。

    - 只保留命中 `options[].label` 的选择（前端/模型都可能塞脏值）；
    - 单选只取首个；多选去重保序；
    - 自定义文本单独字段（不混进 selected）。
    """
    by_qid: dict[str, dict] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("question_id"):
                by_qid[str(item["question_id"])] = item

    out: list[dict] = []
    for q in questions:
        item = by_qid.get(q["id"]) or {}
        allowed = {o["label"] for o in q["options"]}
        selected = item.get("selected") or []
        if isinstance(selected, str):
            selected = [selected]
        picked: list[str] = []
        for s in selected if isinstance(selected, list) else []:
            s = str(s)
            if s in allowed and s not in picked:
                picked.append(s)
        if not q["multi_select"] and picked:
            picked = picked[:1]
        out.append({
            "question_id": q["id"],
            "selected": picked,
            "custom_text": _clip(item.get("custom_text"), 2000),
        })
    return out


def format_answered(questions: list, answers: list) -> str:
    """已作答 → 回填给模型的 tool_result 文本（也是前端只读小结展示的同一份）。"""
    by_qid = {a["question_id"]: a for a in answers}
    lines = ["用户已完成选择："]
    for q in questions:
        a = by_qid.get(q["id"]) or {"selected": [], "custom_text": ""}
        title = q["question"] + ("（可多选）" if q["multi_select"] else "")
        # 选项用「、」连接；自定义文本用「；」单独分层 —— 否则整句拉平后
        # 分不清哪部分是用户自己写的（例：Markdown、PDF；其他：还想要 EPUB）
        body = "、".join(a["selected"])
        if a["custom_text"]:
            custom = f"{q['custom_label']}：{a['custom_text']}"
            body = f"{body}；{custom}" if body else custom
        lines.append(f"- [{q['header']}] {title} → {body or '（未选择）'}")
    return "\n".join(lines)


def format_free_text(text) -> str:
    """用户在输入框直接发消息 → 视为放弃选择题，自由文本原样回填。"""
    return "用户没有选择选项，而是直接回复了：\n" + str(text or "").strip()


def status_of_result(result_text) -> str:
    """从 `result_text` 反推结局 —— **仅供回放路径使用**。

    jsonl 里只存 tool_result 文本（不存 outcome），而前端只读小结块需要一个
    徽标文案。这个映射必须只有一处：认识这些文案的只有本模块（常量都在这里），
    前端/桥层都只搬运结果，不去 `startswith` 猜文案 —— 否则改一句常量文案，
    回放徽标就会静默错位。

    返回 `answered / cancelled / stopped`；空串（进程被杀，tool_result 未落盘）
    返回 `incomplete`，由前端渲染「未完成」。
    """
    text = str(result_text or "").strip()
    if not text:
        return "incomplete"
    if text.startswith("用户已完成选择"):
        return OUTCOME_ANSWERED
    if text.startswith("用户取消了本次提问"):
        return OUTCOME_CANCELLED
    if text.startswith("本轮已被用户停止"):
        return OUTCOME_STOPPED
    # 自由文本作答（"用户没有选择选项，而是直接回复了：…"）也算已作答
    return OUTCOME_ANSWERED


@dataclass
class InteractionPending:
    """一次在途提问。`done` 是跨线程唯一唤醒原语。"""

    request_id: str
    session_id: str
    tool_call_id: str
    questions: list
    created_at: float
    done: threading.Event = field(default_factory=threading.Event)
    outcome: str = ""          # "" = 仍在途；否则 answered / cancelled / stopped
    answers: list = field(default_factory=list)
    result_text: str = ""      # 回填给模型的 tool_result 原文
    announced: bool = False    # ask_request 是否已广播（见 _announced_locked）

    def request_payload(self) -> dict:
        """ask_request 事件载荷（只含可序列化字段）。"""
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "tool_call_id": self.tool_call_id,
            "questions": self.questions,
            "created_at": self.created_at,
        }

    def resolved_payload(self) -> dict:
        """ask_resolved 事件载荷。result_text 与回填给模型的文本逐字节相同。"""
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "tool_call_id": self.tool_call_id,
            "status": self.outcome,
            "answers": self.answers,
            "result_text": self.result_text,
        }


class InteractionBroker:
    """单会话的交互提问 broker。

    一个 `SessionRuntime` 持有一个实例（天然带 session_id 与 deliver），
    由 runtime 在 `build_agent()` 里注入给该会话 Agent 的 `ToolRegistry`。

    **同一会话同一时刻至多一个在途提问**（工具永远串行且独占执行，
    见 agent_full_v2 的分桶），因此 `pending` 表实际长度恒为 0 或 1；
    仍用 dict 是为了按 request_id 精确配对与幂等丢弃迟到答案。
    """

    def __init__(self, session_id: str, deliver=None):
        self._sid = session_id
        self._deliver = deliver
        self._lock = threading.Lock()
        self._pending: dict[str, InteractionPending] = {}
        self._closed = False

    # ══════════════════════════════════════════════════════════
    #  工具入口（在 turn 工作线程里阻塞）
    # ══════════════════════════════════════════════════════════

    def ask(self, questions, tool_call_id: str = "", stop_event=None) -> str:
        """注册一次提问 → 广播 → 阻塞等答案 → 返回 tool_result 文本。

        永不抛异常（工具层契约）。所有退出路径都保证返回字符串，从而保证
        `agent_loop` 一定会为这个 tool_call 回填一条 tool 消息 ——
        否则 jsonl 里出现孤儿 tool_call，下次 `load_session_history` 的自愈
        会原子重写会话文件。
        """
        try:
            norm, err = normalize_questions(questions)
            if norm is None:
                return f"Error: ask_user 参数非法：{err}。请修正后重试。"

            with self._lock:  # ← 只在注册期持锁
                if self._closed:
                    return STOPPED_TEXT
                if self._pending:
                    return BUSY_TEXT
                pend = InteractionPending(
                    request_id="ask_" + uuid.uuid4().hex[:10],
                    session_id=self._sid,
                    tool_call_id=str(tool_call_id or ""),
                    questions=norm,
                    created_at=time.time(),
                )
                self._pending[pend.request_id] = pend

            log.info("session_%s ask_user 发起提问 %s（%d 题，tool_call=%s）",
                     self._sid, pend.request_id, len(norm), pend.tool_call_id or "-")
            self._emit("ask_request", pend.request_payload())
            # ⚠️ 广播之后才把请求标记为"对外可见"（见 _announced_locked）。
            # 否则存在这样的窗口：pending 已注册但 ask_request 还没发出去，
            # 此刻若有外部路径（ask_answer / chat 自由作答）结算了它，
            # 前端会**先收到 ask_resolved 再收到 ask_request** ——
            # 表现为"提问面板还没出现就被判定已解决"，且只读小结块缺问题。
            with self._lock:
                pend.announced = True

            wait = pend.done.wait  # 局部绑定，循环里省一次属性查找
            while True:            # ← 等待期绝不持锁（否则 resolve 死锁）
                if wait(_POLL_SECONDS):
                    break
                # 兜底：某条停止路径没走 cancel_all 时，也在 0.2s 内收束
                if stop_event is not None and stop_event.is_set():
                    self._settle(pend, OUTCOME_STOPPED, STOPPED_TEXT, [])
                    break

            log.info("session_%s ask_user 结束 %s（%s）",
                     self._sid, pend.request_id, pend.outcome or "?")
            return pend.result_text or ""
        except Exception as e:  # noqa: BLE001 - 工具层绝不向上抛
            log.error("ask_user 执行异常: %s: %s", type(e).__name__, e, exc_info=True)
            return f"Error: ask_user 执行失败: {type(e).__name__}: {e}"

    # ══════════════════════════════════════════════════════════
    #  结算（事件循环线程调用；幂等）
    # ══════════════════════════════════════════════════════════

    def resolve(self, request_id: str, answers) -> bool:
        """结构化作答。迟到/重复提交返回 False（无副作用）。"""
        pend = self._peek(request_id)
        if pend is None:
            log.warning("session_%s ask_answer 丢弃：请求不存在或已结算（%s）",
                        self._sid, request_id)
            return False
        norm = normalize_answers(pend.questions, answers)
        return self._settle(pend, OUTCOME_ANSWERED,
                            format_answered(pend.questions, norm), norm)

    def resolve_free_text(self, request_id: str, text) -> bool:
        """用户直接在输入框发消息 = 放弃选择题，自由文本原样回填。

        **不另起 turn**：原 turn 的 `ask()` 拿到答案后在同一回合继续。
        """
        pend = self._peek(request_id)
        if pend is None:
            return False
        body = str(text or "").strip()
        if not body:
            return False
        return self._settle(pend, OUTCOME_ANSWERED, format_free_text(body), [])

    def resolve_free_text_any(self, text) -> bool:
        """按会话取"第一个在途提问"并自由作答（供 chat 分支拦截用，避免 TOCTOU）。"""
        if not str(text or "").strip():
            return False
        with self._lock:
            pend = next(iter(self._announced_locked()), None)
        if pend is None:
            return False
        return self.resolve_free_text(pend.request_id, text)

    def cancel(self, request_id: str, reason: str = OUTCOME_CANCELLED) -> bool:
        """取消单次提问（用户点「取消」）。"""
        pend = self._peek(request_id)
        if pend is None:
            return False
        outcome, text = self._cancellation(reason)
        return self._settle(pend, outcome, text, [])

    def cancel_all(self, reason: str = OUTCOME_STOPPED) -> int:
        """取消本会话所有在途提问（停止按钮 / 会话删除）；返回结算条数。"""
        with self._lock:
            pends = list(self._pending.values())
        if not pends:
            return 0
        outcome, text = self._cancellation(reason)
        n = 0
        for pend in pends:
            if self._settle(pend, outcome, text, []):
                n += 1
        log.info("session_%s cancel_all(%s)：结算 %d 条在途提问", self._sid, reason, n)
        return n

    def close(self) -> None:
        """会话被销毁时收尾：标记关闭并解锁所有阻塞线程。"""
        with self._lock:
            self._closed = True
        self.cancel_all(OUTCOME_CANCELLED)

    # ══════════════════════════════════════════════════════════
    #  查询（重放 / chat 拦截）
    # ══════════════════════════════════════════════════════════

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._announced_locked())

    def first_pending_id(self) -> str:
        with self._lock:
            pend = next(iter(self._announced_locked()), None)
        return pend.request_id if pend else ""

    def pending_payloads(self) -> list[dict]:
        """所有**已广播**的在途提问的 ask_request 载荷（新连接重放 / status_query 用）。"""
        with self._lock:
            pends = self._announced_locked()
        return [p.request_payload() for p in pends]

    # ══════════════════════════════════════════════════════════
    #  内部
    # ══════════════════════════════════════════════════════════

    def _announced_locked(self) -> list[InteractionPending]:
        """已广播、因而可被**外部路径**结算的在途提问（调用方必须持锁）。

        不变量：`ask_request` 一定先于任何外部触发的 `ask_resolved` 到达前端。
        `resolve()` / `resolve_free_text*()` 都经 `_peek()` 走这道闸，
        所以"还没广播就被结算"在外部不可达 —— 否则前端会先收到 ask_resolved，
        表现为"面板还没出现就被判已解决"。

        注意：`cancel_all()` **不**走这道闸 —— 停止/删除会话要能结算任何状态，
        哪怕广播还没发出去（多发给一个前端不认识的 request_id 是无害的）。
        """
        return [p for p in self._pending.values() if p.announced]

    def _peek(self, request_id: str) -> InteractionPending | None:
        with self._lock:
            pend = self._pending.get(request_id)
            if pend is None or pend.outcome or not pend.announced:
                return None
            return pend

    @staticmethod
    def _cancellation(reason: str) -> tuple[str, str]:
        if reason == OUTCOME_STOPPED:
            return OUTCOME_STOPPED, STOPPED_TEXT
        return OUTCOME_CANCELLED, CANCELLED_TEXT

    def _settle(self, pend: InteractionPending, outcome: str,
                result_text: str, answers: list) -> bool:
        """唯一的结算出口：锁内改状态 + 移出 pending，锁外投递与唤醒。"""
        with self._lock:
            if pend.outcome:      # 已被其它路径结算 → 幂等丢弃
                return False
            pend.outcome = outcome
            pend.result_text = result_text
            pend.answers = list(answers or [])
            self._pending.pop(pend.request_id, None)
        self._emit("ask_resolved", pend.resolved_payload())
        pend.done.set()           # ← 必须在锁外
        return True

    def _emit(self, kind: str, payload: dict) -> None:
        """投递事件；任何异常只记日志，绝不影响 ask 的返回与 turn 收尾。"""
        if self._deliver is None:
            return
        try:
            self._deliver(kind, payload)
        except Exception as e:  # noqa: BLE001
            log.error("session_%s %s 事件投递失败: %s: %s",
                      self._sid, kind, type(e).__name__, e, exc_info=True)
