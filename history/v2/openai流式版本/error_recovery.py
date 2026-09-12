#!/usr/bin/env python3
"""
error_recovery.py - 错误恢复模块

把 S11 的两段错误处理代码封装成一个状态机类，集中管理 LLM 调用过程中的
所有异常路径：

- 429 限流 / 503 服务端过载 → 内层指数退避重试（透明重试）
- 连续 503 → 切换到 FALLBACK_MODEL
- max_tokens 截断 → 两阶段恢复（先升级额度到 64K，再走续写 prompt）
- prompt 超出上下文窗口 → 复用 ContextCompact 压缩后重试
- 不可恢复错误（auth 失败、参数错、未知异常等）→ 写错误消息并退出

使用方式：
    from error_recovery import ErrorRecovery, RecoveryAction

    recovery = ErrorRecovery(primary_model=MODEL, fallback_model=FALLBACK_MODEL)
    while True:
        try:
            resp = recovery.with_retry(lambda: llm.chat.completions.create(
                model=recovery.current_model,
                max_tokens=recovery.current_max_tokens,
                ...
            ))
        except Exception as e:
            if recovery.handle_exception(e, messages, session_manager, session_file) \
                    == RecoveryAction.ABORT:
                return
            continue

        if resp.choices[0].finish_reason == "length":
            if recovery.handle_truncation(resp.choices[0].message, messages,
                                          session_manager, session_file) \
                    == RecoveryAction.ABORT:
                return
            continue
"""

import os
import random
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from session_manage import SessionManager


# 路径之外的可调参数一律从 .env 内联读取；未设置时回退到下方默认值。
# 新增/修改时同步更新 .env 与 .env.example，详见 AGENTS.md。

# max_tokens 升级上限（截断时第一次升级到的额度）
ESCALATED_MAX_TOKENS = int(os.environ.get("ESCALATED_MAX_TOKENS") or 64000)
# 默认 max_tokens（首轮请求的输出上限）
DEFAULT_MAX_TOKENS = int(os.environ.get("DEFAULT_MAX_TOKENS") or 8000)
# max_tokens 截断后的最大续写次数
MAX_RECOVERY_RETRIES = int(os.environ.get("MAX_RECOVERY_RETRIES") or 3)
# 内层 with_retry 对 429/503 的最大重试次数
MAX_RETRIES = int(os.environ.get("MAX_RETRIES") or 10)
# 指数退避的初始延迟（毫秒）；实际等待 = base * 2^attempt + 随机抖动
BASE_DELAY_MS = int(os.environ.get("BASE_DELAY_MS") or 500)
# 退避上限（毫秒），防止指数爆炸
BACKOFF_CAP_MS = int(os.environ.get("BACKOFF_CAP_MS") or 32000)
# 连续 503 错误次数上限；达到后切换到 FALLBACK_MODEL
MAX_CONSECUTIVE_503 = int(os.environ.get("MAX_CONSECUTIVE_503") or 3)
# 续写提示：模型输出被 max_tokens 截断后，追加到 messages 的 user 消息
# 要求：直接接着写，不要道歉、不要复述上文、从中途断掉的地方继续
CONTINUATION_PROMPT = os.environ.get(
    "CONTINUATION_PROMPT"
) or "输出已达 token 上限，直接续写 —— 不要道歉，不要复述，从中途中断处接着往下写。"


class RecoveryAction(Enum):
    """错误恢复控制器返回给主循环的动作指令。"""
    CONTINUE = "continue"   # 回到主循环顶部，用修改后的参数再试
    ABORT = "abort"         # 已写完错误消息，退出当前轮


class RecoveryState:
    """跨循环迭代跟踪错误恢复进度（控制器内部状态）。"""

    def __init__(self, primary_model: str):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_503 = 0  # deepseek 的服务器过载错误码为 503
        self.has_attempted_reactive_compact = False
        self.current_model = primary_model


class ErrorRecovery:
    """LLM 错误恢复状态机。

    职责：
    - 内层 with_retry：处理 429/503 等临时错误，透明重试或切备用模型
    - 外层 handle_truncation：处理 max_tokens 截断（finish_reason == "length"）
    - 外层 handle_exception：处理 with_retry 主动 raise 的非临时错误

    所有"是否继续循环"的判断通过 ``RecoveryAction`` 返回给主循环，
    避免主循环需要知道恢复细节。
    """

    def __init__(
        self,
        primary_model: str,
        fallback_model: Optional[str] = None,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        escalated_max_tokens: int = ESCALATED_MAX_TOKENS,
        max_recovery_retries: int = MAX_RECOVERY_RETRIES,
        max_retries: int = MAX_RETRIES,
        base_delay_ms: int = BASE_DELAY_MS,
        backoff_cap_ms: int = BACKOFF_CAP_MS,
        max_consecutive_503: int = MAX_CONSECUTIVE_503,
        continuation_prompt: str = CONTINUATION_PROMPT,
    ):
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self.default_max_tokens = default_max_tokens
        self.escalated_max_tokens = escalated_max_tokens
        self.max_recovery_retries = max_recovery_retries
        self.max_retries = max_retries
        self.base_delay_ms = base_delay_ms
        self.backoff_cap_ms = backoff_cap_ms
        self.max_consecutive_503 = max_consecutive_503
        self.continuation_prompt = continuation_prompt

        self.state = RecoveryState(primary_model)
        # 当前 max_tokens，escalation 时会被覆盖为 escalated_max_tokens
        self.max_tokens = default_max_tokens

    # ── 供主循环读取的当前参数 ──
    @property
    def current_model(self) -> str:
        return self.state.current_model

    @property
    def current_max_tokens(self) -> int:
        return self.max_tokens

    # ── 内层工具方法 ──
    @staticmethod
    def _retry_delay(attempt: int, base_delay_ms: int,
                     backoff_cap_ms: int,
                     retry_after: Optional[float] = None) -> float:
        """Exponential backoff with jitter. Retry-After takes priority."""
        if retry_after:
            return retry_after
        base = min(base_delay_ms * (2 ** attempt), backoff_cap_ms) / 1000
        jitter = random.uniform(0, base * 0.25)
        return base + jitter

    @staticmethod
    def _is_prompt_too_long_error(e: Exception) -> bool:
        """Check whether an API error indicates prompt/context too long."""
        msg = str(e).lower()
        return (("prompt" in msg and "long" in msg)
                or "prompt_is_too_long" in msg
                or "context_length_exceeded" in msg
                or "max_context_window" in msg)

    # ── 内层异常处理：仅"透明重试"，不修改入参 ──
    def with_retry(self, fn: Callable):
        """
        【内层异常处理】—— 只负责"透明重试"。

        职责范围：仅处理临时性错误（429 限流 / 503 服务过载），
        通过指数退避 + 抖动等待后，**用相同的入参**重新调用 fn。

        不能处理的错误（比如 prompt_too_long、auth 失败等）：
        会立即 raise 出去，交给外层 handle_exception 决定怎么恢复。
        """
        for attempt in range(self.max_retries):
            try:
                result = fn()
                return result
            except Exception as e:
                name = type(e).__name__
                msg = str(e).lower()

                # ── 分支 1：429 限流 ──
                # 现象：请求太快，API 拒绝；处理：等一会儿再试，**不修改任何入参**
                if "ratelimit" in name.lower() or "429" in msg:
                    delay = self._retry_delay(
                        attempt, self.base_delay_ms, self.backoff_cap_ms
                    )
                    print(f"  \033[33m[429 rate limit] retry {attempt+1}/{self.max_retries},"
                          f" wait {delay:.1f}s\033[0m")
                    time.sleep(delay)
                    continue                                 # 回到 for 顶部，再试一次

                # ── 分支 2：503 服务端过载 ──
                # 现象：上游模型服务器忙不过来；处理：退避等待，
                # 如果连续多次 503 且配置了 fallback_model，则切换到备用模型
                if "overloaded" in name.lower() or "503" in msg:
                    self.state.consecutive_503 += 1
                    # 连续 max_consecutive_503 次 503，触发"换模型"策略
                    if self.state.consecutive_503 >= self.max_consecutive_503:
                        if self.fallback_model:
                            # 切换到备用模型，并把计数器清零（在新模型上重新计数）
                            self.state.current_model = self.fallback_model
                            self.state.consecutive_503 = 0
                            print(f"  \033[31m[503 x{self.max_consecutive_503}]"
                                  f" switching to {self.fallback_model}\033[0m")
                        else:
                            # 没配备用模型，只能继续重试主模型
                            self.state.consecutive_503 = 0
                            print(f"  \033[31m[503 x{self.max_consecutive_503}]"
                                  f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m")
                    delay = self._retry_delay(
                        attempt, self.base_delay_ms, self.backoff_cap_ms
                    )
                    print(f"  \033[33m[503 overloaded] retry {attempt+1}/{self.max_retries},"
                          f" wait {delay:.1f}s\033[0m")
                    time.sleep(delay)
                    continue

                # ── 既不是 429 也不是 503：属于"无法靠重试解决"的错误 ──
                # 例如：prompt_too_long、context 超限、auth 失败、参数错误等。
                # 这里的 raise 是"我不处理，往上传"的信号，
                # 外层 handle_exception 会接住并按错误类型做 compact / 放弃等策略。
                raise

        # 全部重试用完（429/503 始终没恢复），抛错给外层
        raise RuntimeError(f"Max retries ({self.max_retries}) exceeded")

    # ── 外层处理：max_tokens 截断（finish_reason == "length"） ──
    def handle_truncation(
        self,
        response_msg,
        history_messages: list,
        session_manager: SessionManager,
        session_file: Path,
    ) -> RecoveryAction:
        """
        处理 max_tokens 截断。返回 CONTINUE 或 ABORT。

        两阶段：
        1. 第一次截断：把 max_tokens 升级到 escalated_max_tokens，重发同样的请求
        2. 第二次及之后：把已截断内容追加到 messages，注入续写 prompt
        3. 超过 max_recovery_retries：放弃
        """
        # ── 第一阶段：扩大额度（default → escalated）──
        # 第一次遇到截断时不要把这段"断头输出"塞进 messages，
        # 因为它很可能是半句话/半个 JSON，没啥用；直接把上限拉大重试更划算
        if not self.state.has_escalated:
            self.max_tokens = self.escalated_max_tokens
            self.state.has_escalated = True
            print(f"  \033[33m[max_tokens] escalating"
                  f" {self.default_max_tokens} -> {self.escalated_max_tokens}\033[0m")
            return RecoveryAction.CONTINUE              # 回到 while 顶部，用更大额度重发

        # ── 第二阶段：escalated 还不够，进入"续写"模式 ──
        # 此时输出截断是发生在"快要说完"的位置，把这部分内容留着
        # 然后塞一条 user 提示让模型接着写
        tmp_msg = {"role": "assistant", "content": response_msg.content}
        history_messages.append(tmp_msg)
        session_manager.append_message_to_session(session_file, tmp_msg)

        if self.state.recovery_count < self.max_recovery_retries:
            # 增加续写提示
            tmp_msg = {"role": "user", "content": self.continuation_prompt}
            history_messages.append(tmp_msg)
            session_manager.append_message_to_session(session_file, tmp_msg)
            self.state.recovery_count += 1
            print(f"  \033[33m[max_tokens] continuation"
                  f" {self.state.recovery_count}/{self.max_recovery_retries}\033[0m")
            return RecoveryAction.CONTINUE              # 回到 while 顶部，让模型续写

        # 续写 max_recovery_retries 次还是截断 → 放弃，告诉用户
        print("  \033[31m[max_tokens] recovery limit reached\033[0m")
        return RecoveryAction.ABORT

    # ── 外层处理：with_retry raise 出来的非临时错误 ──
    def handle_exception(
        self,
        e: Exception,
        history_messages: list,
        session_manager: SessionManager,
        session_file: Path,
    ) -> RecoveryAction:
        """
        处理 with_retry 主动 raise 出来的"非临时错误"。

        策略是"修改输入后再试"或"放弃"，不是简单的重试。
        """
        # ── Path 2：prompt 超出模型上下文窗口 ──
        # 现象：API 报错说 prompt 太长；处理：压缩 messages 后再试一次
        if self._is_prompt_too_long_error(e):
            if not self.state.has_attempted_reactive_compact:
                # 第一次触发：复用主循环同款四层压缩管线（含 L4 LLM 摘要 +
                # 自动同步会话文件），不重复造轮子。紧急态 used_percent 必超 100%，
                # 95% 阈值门控会通过；且必然 > SUMMARY_TRIGGER_RATIO(0.80)，
                # L4 摘要会真正跑。
                session_manager.maybe_compact_context(history_messages, session_file)
                # 标记"已经试过 compact"，避免下次又触发 compact 陷入死循环
                self.state.has_attempted_reactive_compact = True
                return RecoveryAction.CONTINUE          # 回到 while 顶部，用更短的 messages 再试
            # 第二次还超长 → 没救了，向用户报告错误
            print("  \033[31m[unrecoverable] still too long after compact\033[0m")
            error_msg_dict = {
                "role": "assistant",
                "content": [
                    {"type": "text",
                     "text": "[Error] Context too large, cannot continue."}
                ],
            }.model_dump()
            # 加入大模型回复到历史消息中
            history_messages.append(error_msg_dict)
            session_manager.append_message_to_session(session_file, error_msg_dict)
            return RecoveryAction.ABORT

        # ── 兜底：其他所有错误（auth 失败、参数错、未知异常等）──
        # 既不能重试也不能 compact，直接退出当前轮
        name = type(e).__name__
        print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
        error_msg_dict = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}
            ],
        }.model_dump()
        # 加入大模型回复到历史消息中
        history_messages.append(error_msg_dict)
        session_manager.append_message_to_session(session_file, error_msg_dict)
        return RecoveryAction.ABORT
