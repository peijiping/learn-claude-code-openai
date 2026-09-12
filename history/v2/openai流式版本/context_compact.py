#!/usr/bin/env python3
"""
context_compact.py — 上下文压缩（v2 教程 s08 对齐版）

四层压缩管线，编排顺序与 CC 源码一致：

    L3 budget → L1 snip → L2 micro → [token 超阈值?] → L4 summary

L1/L2/L3 始终每轮运行（0 API 调用），由各层内部阈值决定是否真做修改；
L4 用一次 LLM 摘要，仅在 token 仍超阈值时触发。
L4 触发时把压缩前的完整 messages 写到 .transcripts/，便于事后追溯。

文件结构（自上而下读）：
  1. 配置常量（.env 可覆盖）
  2. 数据类（ContextStats / CompactResult）
  3. ContextCompact 编排器：消息工具 + Token 工具 + L1-L4 全部封装为实例方法

公共 API：class ContextCompact 的方法；模块级只保留配置常量与数据类。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from llm_manage import LLMClient
from streaming_client import streamed_create

from paths import TRANSCRIPT_DIRNAME, TOOL_RESULTS_DIRNAME


# ── 1. 配置常量（运行时可由 .env 覆盖） ──────────────────────────────
# 路径之外的可调参数一律 os.environ.get(KEY) or default 内联读取；
# 新增/修改时同步更新 .env 与 .env.example，详见 AGENTS.md。


# 上下文窗口默认 token 上限；构造 ContextCompact 时若 .env 未配 MAX_CONTEXT_TOKENS 则回落到此值。
DEFAULT_MAX_CONTEXT_TOKENS = int(os.environ.get("DEFAULT_MAX_CONTEXT_TOKENS") or 1000000)

# L1 snip —— 消息条数裁剪
# 消息总条数超过该值时触发 snip_compact，把中间替换为单条占位 HumanMessage。
SNIP_MAX_MESSAGES = int(os.environ.get("SNIP_MAX_MESSAGES") or 50)
# snip 时保留的最前面若干条（覆盖 SystemMessage + workspace 指令注入，避免上下文漂移）。
SNIP_KEEP_HEAD = int(os.environ.get("SNIP_KEEP_HEAD") or 3)
# snip 时保留的最后面若干条；默认 = SNIP_MAX_MESSAGES - SNIP_KEEP_HEAD，等价于"头+尾"覆盖整个窗口。
SNIP_KEEP_TAIL = int(os.environ.get("SNIP_KEEP_TAIL") or SNIP_MAX_MESSAGES - SNIP_KEEP_HEAD)

# L2 micro —— 旧 tool_result 占位
# micro_compact 时除最近 N 条 ToolMessage 外，旧的会被替换为占位文本（不能直接删，否则违反 API"tool_use 必须有对应 tool_result"约束）。
KEEP_RECENT_TOOL_RESULTS = int(os.environ.get("KEEP_RECENT_TOOL_RESULTS") or 3)

# L3 persist —— 超大 tool_result 落盘
# 单条 tool_result 超过该字符数则把全文写入 .tool_results/，消息里只留"路径+预览"占位。
PERSIST_THRESHOLD = int(os.environ.get("PERSIST_THRESHOLD") or 30000)
# 最后一条 AIMessage 之后所有 ToolMessage 的总字节上限；超出时按大小优先把最大的若干条落盘。
MAX_TOOL_RESULT_BYTES = int(os.environ.get("MAX_TOOL_RESULT_BYTES") or 200000)
# 落盘后消息内嵌的原文预览长度（字符数），便于模型在不看全文时仍能拿到关键上下文。
PREVIEW_LENGTH = int(os.environ.get("PREVIEW_LENGTH") or 2000)

# 触发阈值（按"已用 token / 上下文上限"的比例）
# L1/L2/L3 无前置阈值，每轮始终运行；L1/L2/L3 跑完后若 token 仍超该比例，再走 L4 用 LLM 做整段摘要。
# 这是唯一会发 API 调用的压缩层，由 SUMMARY_TRIGGER_RATIO 单独门控。
SUMMARY_TRIGGER_RATIO = float(os.environ.get("SUMMARY_TRIGGER_RATIO") or 0.80)

# L4 summary —— 摘要时除前缀（SystemMessage + workspace 指令）外，原样保留的最近消息条数，防止刚发生的工具结果被一起压掉。
PRESERVE_RECENT_SUMMARY_MESSAGES = int(os.environ.get("PRESERVE_RECENT_SUMMARY_MESSAGES") or 10)

# L4 摘要 prompt 模板（9 段式结构化摘要）
_SUMMARY_PROMPT = """\
请将以下对话历史压缩为结构化摘要，保留后续继续任务所需的事实、决策、文件、错误和待办。

摘要必须使用以下 9 个章节标题：
1. Primary Request and Intent — 用户的请求和意图
2. Key Technical Concepts — 关键技术概念
3. Files and Code Sections — 涉及的文件和代码片段
4. Errors and fixes — 遇到的错误和修复
5. Problem Solving — 问题解决过程
6. All user messages — 所有用户消息（非工具结果）
7. Pending Tasks — 待完成的任务
8. Current Work — 当前进行的工作
9. Optional Next Step — 可选的下一步

对话历史：
{transcript}
"""


# ── 2. 数据类 ────────────────────────────────────────────────────────


@dataclass
class ContextStats:
    """上下文用量统计：用于 UI 展示与压缩决策。"""
    used_tokens: int          # 当前消息历史估算占用的 token 数（启发式估算，非精确值）
    max_tokens: int           # 上下文窗口的 token 上限（来自 .env MAX_CONTEXT_TOKENS 或默认值）
    used_percent: float       # 已用比例 0-100，用于触发压缩管线的阈值判断
    remaining_percent: float  # 剩余比例 0-100，主要给 UI 展示"还剩多少可用"
    max_label: str            # 上限的可读化文本（"200K" / "1M"），给 UI 标签用


@dataclass
class CompactResult:
    """一次压缩/剪枝操作的结果。"""
    messages: list                              # 操作后的消息列表（未触发压缩时与输入相同）
    changed: bool                               # 本次操作是否真的改动了消息（用于决定是否写 transcript / 打日志）
    operations: dict = field(default_factory=dict)  # 各压缩层的计数明细（key 见 _empty_operations: tool_results_persisted / messages_snip_compacted / ...）
    before: Optional[ContextStats] = None      # 操作前的用量；未触发压缩时和 after 相同
    after: Optional[ContextStats] = None       # 操作后的用量


# ── 3. ContextCompact 编排器 ────────────────────────────────────────


class ContextCompact:
    """四层压缩管线编排器。

    编排顺序：L3 budget → L1 snip → L2 micro → [token 超阈值?] → L4 summary。

    L1/L2/L3 始终每轮调用（0 API 调用），由各层内部阈值决定是否真正修改 messages：
      - L3 内部阈值：最后一条 AI 之后的所有 ToolMessage 字节数 > MAX_TOOL_RESULT_BYTES
      - L1 内部阈值：消息总条数 > SNIP_MAX_MESSAGES
      - L2 内部阈值：ToolMessage 总数 > KEEP_RECENT_TOOL_RESULTS 且 content > 320 字符
    仅 L4 summary 走 LLM，由 SUMMARY_TRIGGER_RATIO 门控。
    构造时可通过 max_context_tokens / summarizer / transcript_dir / tool_results_dir
    覆盖默认配置；其余阈值统一从模块级常量读取（可由 .env 覆盖）。
    """

    def __init__(
        self,
        max_context_tokens: Optional[int] = None,
        summarizer: Optional[Callable[[str], str]] = None,
        transcript_dir: Optional[Path] = None,
        tool_results_dir: Optional[Path] = None,
    ):
        self.max_context_tokens = (
            max_context_tokens
            or self.parse_max_context_tokens(os.environ.get("MAX_CONTEXT_TOKENS"), DEFAULT_MAX_CONTEXT_TOKENS)
        )
        self.summarizer = summarizer
        self.transcript_dir = Path(transcript_dir) if transcript_dir else Path.cwd() / TRANSCRIPT_DIRNAME
        self.tool_results_dir = Path(tool_results_dir) if tool_results_dir else Path.cwd() / TOOL_RESULTS_DIRNAME
        self.llm_client = LLMClient().llm

    # ── 3a. 消息工具：content 归一化、类型判断、序列化 ──────────────

    def content_to_str(self, content) -> str:
        """把 LangChain 消息的 content 归一化为 str。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    parts.append(str(block.get("text", block)))
                else:
                    parts.append(getattr(block, "text", str(block)))
            return "\n".join(parts)
        return str(content)

    def message_to_text(self, msg) -> str:
        """从 LangChain 消息 / dict / 其他对象里取出文本 content（统一为 str）。"""
        if hasattr(msg, "content"):
            return self.content_to_str(msg.content)
        if isinstance(msg, dict):
            return self.content_to_str(msg.get("content", ""))
        return str(msg)

    def is_ai_with_tool_use(self, msg) -> bool:
        return isinstance(msg, dict) and msg["role"] == "assistant" and "tool_calls"

    def is_tool_result(self, msg) -> bool:
        return isinstance(msg, dict) and msg["role"] == "tool"



    def message_to_dict(self, msg) -> dict:
        """把 LangChain 消息转成 dict 便于 jsonl 落盘。"""
        if isinstance(msg, dict) and msg["role"] == "system":
            return {"role": "system", "content": msg["content"]}
        if isinstance(msg, dict) and msg["role"] == "user":
            return {"role": "user", "content": msg["content"]}
        if isinstance(msg, dict):
            row = {"role": "assistant", "content": msg["content"]}
            if "tool_calls" in msg:
                row["tool_calls"] = msg["tool_calls"]
            if getattr(msg, "id", None):
                row["id"] = msg.id
            return row
        if isinstance(msg, dict) and msg["role"] == "tool":
            return {"role": "tool", "content": msg["content"], "tool_call_id": msg["tool_call_id"]}
        return {"role": "unknown", "content": str(msg["content"])}

    # ── 3b. Token 工具：解析、格式化、估算 ──────────────────────────

    def parse_max_context_tokens(self, value: Optional[str], default: int) -> int:
        """解析 '200K' / '1M' / '196608' 这类字符串为 token 整数。"""
        if not value:
            return default
        text = str(value).strip().upper()
        if not text:
            return default

        multiplier = 1
        if text.endswith("M"):
            multiplier, text = 1_000_000, text[:-1]
        elif text.endswith("K"):
            multiplier, text = 1_000, text[:-1]

        try:
            tokens = int(float(text) * multiplier)
        except ValueError:
            return default
        return tokens if tokens > 0 else default

    def format_token_count(self, tokens: int) -> str:
        """整百万显示 '1M'，整千显示 '200K'，其他保持原样。"""
        if tokens >= 1_000_000 and tokens % 1_000_000 == 0:
            return f"{tokens // 1_000_000}M"
        if tokens >= 1_000 and tokens % 1_000 == 0:
            return f"{tokens // 1_000}K"
        return str(tokens)

    def estimate_tokens(self, messages: list) -> int:
        """粗略估算消息列表的 token 数。
        启发式系数：中文字 1.5、英文词 1.3、其他 0.5；每条消息另加 4（role + 格式）。
        """
        total = 0
        for msg in messages:
            total += 4
            text = self.message_to_text(msg)
            chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
            english = len(re.findall(r"[a-zA-Z]+", text))
            other = max(0, len(text) - chinese - english)
            total += int(chinese * 1.5 + english * 1.3 + other * 0.5)

            if self.is_ai_with_tool_use(msg):
                total += len(json.dumps(msg.get("tool_calls", []), ensure_ascii=False)) // 4
        return total

    # ── 3c. 上下文统计 ──────────────────────────────────────────────

    def context_stats(self, messages: list) -> ContextStats:
        used = self.estimate_tokens(messages)
        used_percent = min(100.0, (used / self.max_context_tokens) * 100)
        return ContextStats(
            used_tokens=used,
            max_tokens=self.max_context_tokens,
            used_percent=used_percent,
            remaining_percent=max(0.0, 100.0 - used_percent),
            max_label=self.format_token_count(self.max_context_tokens),
        )

    def format_context_label(self, messages: list) -> str:
        s = self.context_stats(messages)
        return f"max：{s.max_label}，used：{s.used_tokens}，{int(s.remaining_percent)}%"

    # ── 3d. 边界调整：避开拆开 tool_use / tool_result ──────────────

    def _find_last_ai_index(self, messages: list) -> int:
        """返回最后一条 AIMessage 的索引；找不到返回 -1。"""
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], AIMessage):
                return i
        return -1

    def _advance_past_tool_results(self, messages: list, start: int) -> int:
        """把 start 推过紧随其后的 ToolMessage（避免把 tool_use 与 tool_result 拆开）。"""
        while start < len(messages) and self.is_tool_result(messages[start]):
            start += 1
        return start

    def _retreat_before_orphan_tool_result(self, messages: list, start: int) -> int:
        """start 是 ToolMessage 且 start-1 是 AIMessage(tool_calls) 时，回退 1 格避免孤立。"""
        if (
            0 < start < len(messages)
            and self.is_tool_result(messages[start])
            and self.is_ai_with_tool_use(messages[start - 1])
        ):
            return start - 1
        return start

    # ── 3e. L1: snip_compact —— 裁掉中间消息 ───────────────────────

    def snip_compact(self, messages: list, max_messages: int = SNIP_MAX_MESSAGES) -> list:
        """消息条数 > max_messages 时，保留头 SNIP_KEEP_HEAD + 尾 SNIP_KEEP_TAIL 条，中间用占位 HumanMessage 替代。"""
        if len(messages) <= max_messages:
            return messages

        head_end = self._advance_past_tool_results(messages, SNIP_KEEP_HEAD)
        tail_start = self._retreat_before_orphan_tool_result(messages, len(messages) - SNIP_KEEP_TAIL)
        if head_end >= tail_start:
            return messages

        placeholder = HumanMessage(content=f"[snipped {tail_start - head_end} messages from conversation middle]")
        return [*messages[:head_end], placeholder, *messages[tail_start:]]

    # ── 3f. L2: micro_compact —— 旧 tool_result 用占位文本替换 ─────

    _MICRO_PLACEHOLDER = "[Earlier tool result compacted. Re-run if needed.]"

    def micro_compact(self, messages: list, keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> int:
        """把"较旧"且长度 > 320 字符的 ToolMessage 替换为占位文本；返回被替换条数。
        不直接删除是为了保持 OpenAI/Anthropic API 约束：每条 tool_use 必须有对应 tool_result。
        """
        tool_results = [(i, m) for i, m in enumerate(messages) if self.is_tool_result(m)]
        if len(tool_results) <= keep_recent:
            return 0
        replaced = 0
        for _, msg in tool_results[:-keep_recent]:
            if len(self.content_to_str(msg.content)) > 320 and msg.content != self._MICRO_PLACEHOLDER:
                msg.content = self._MICRO_PLACEHOLDER
                replaced += 1
        return replaced

    # ── 3g. L3: tool_result_budget —— 超大工具输出落盘 ──────────────

    def persist_large_output(self, tool_use_id: str, output: str, tool_results_dir: Path) -> str:
        """把超大工具输出写到磁盘，返回"路径 + 2KB 预览"的占位文本。已存在则跳过写入。"""
        if len(output) <= PERSIST_THRESHOLD:
            return output
        tool_results_dir.mkdir(parents=True, exist_ok=True)
        path = tool_results_dir / f"{tool_use_id}.txt"
        if not path.exists():
            try:
                path.write_text(output, encoding="utf-8")
            except OSError:
                return output
        return (
            f"<persisted-output>\n"
            f"Full output: {path}\n"
            f"Preview:\n{output[:PREVIEW_LENGTH]}\n"
            f"</persisted-output>"
        )

    def tool_result_budget(
        self,
        messages: list,
        max_bytes: int = MAX_TOOL_RESULT_BYTES,
        persist_threshold: int = PERSIST_THRESHOLD,
        tool_results_dir: Optional[Path] = None,
    ) -> int:
        """监控最后一条 AIMessage 之后的所有 ToolMessage 总字节数；超大者落盘，返回落盘条数。

        LangChain 适配：s08 教程的"最后一条 user 消息里所有 tool_result"在这里对应
        "最后一条 AIMessage 之后的所有 ToolMessage"。
        """
        if tool_results_dir is None:
            tool_results_dir = self.tool_results_dir

        last_ai = self._find_last_ai_index(messages)
        if last_ai < 0:
            return 0

        tool_msgs = [m for m in messages[last_ai + 1:] if self.is_tool_result(m)]
        if not tool_msgs:
            return 0

        total = sum(len(self.content_to_str(m.content)) for m in tool_msgs)
        if total <= max_bytes:
            return 0

        # 按体积从大到小排序，优先落盘最大的
        ranked = sorted(tool_msgs, key=lambda m: len(self.content_to_str(m.content)), reverse=True)
        persisted = 0
        for msg in ranked:
            if total <= max_bytes:
                break
            content_str = self.content_to_str(msg.content)
            if len(content_str) <= persist_threshold:
                continue
            new_content = self.persist_large_output(msg.tool_call_id, content_str, tool_results_dir)
            total = total - len(content_str) + len(new_content)
            msg.content = new_content
            persisted += 1
        return persisted

    # ── 3h. L4: compact_history —— LLM 整段摘要 ─────────────────────

    def _format_message_for_summary(self, msg) -> str:
        """把单条消息格式化为 '[role]\\ncontent[\\ntool_calls/tool_call_id]' 形式。"""
        role = msg["role"] if isinstance(msg, dict) and "role" in msg else "unknown"
        extra = ""
        if self.is_ai_with_tool_use(msg):
            extra = "\ntool_calls: " + json.dumps(msg["tool_calls"], ensure_ascii=False)
        elif self.is_tool_result(msg):
            extra = f"\ntool_call_id: {msg['tool_call_id']}"
        return f"[{role}]\n{self.content_to_str(msg['content'])}{extra}"

    def _build_summary_prompt(self, messages: list) -> str:
        """构建摘要 prompt（9 段式结构化模板）。"""
        transcript = "\n\n".join(self._format_message_for_summary(m) for m in messages)
        return _SUMMARY_PROMPT.format(transcript=transcript)

    def summarize_history(
        self,
        messages: list,
        summarizer: Optional[Callable[[str], str]] = None,
    ) -> str:
        """调用 summarizer 对消息列表生成结构化摘要文本；缺省时使用实例上的 summarizer，再缺省时临时构造 LLM。"""
        prompt = self._build_summary_prompt(messages)
        chosen = summarizer or self.summarizer
        if chosen is not None:
            return chosen(prompt)
        
        # 统一流式入口：摘要调用无 tools，不上 UI（sinks=None），仅内部聚合
        msg, _finish, _usage = streamed_create(
            self.llm_client,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.5,
            reasoning_effort="high", #思考强度，DeepSeek只有 high、max 两个选项
            extra_body={"thinking":{"type":"disabled"}} #思考模式开关，值范围 disabled、enabled，默认 enabled
        )

        return self.content_to_str(msg.content) or "(empty summary)"

    def write_transcript(self, messages: list, transcript_dir: Optional[Path] = None) -> Path:
        """把当前完整历史写到 .transcripts/transcript_<timestamp>.jsonl。"""
        if transcript_dir is None:
            transcript_dir = self.transcript_dir
        transcript_dir.mkdir(parents=True, exist_ok=True)
        path = transcript_dir / f"transcript_{int(time.time())}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(self.message_to_dict(msg), ensure_ascii=False, default=str) + "\n")
        return path

    def _protected_prefix_end(self, messages: list) -> int:
        """返回受保护前缀的结束位置：仅 SystemMessage（不能进摘要）。"""
        return 1 if messages and messages[0].role == "system" else 0

    def _find_ai_with_tool_call(self, messages: list, before_index: int, tool_call_id: str) -> Optional[int]:
        """从 before_index 往前找含指定 tool_call_id 的 AIMessage；遇到 HumanMessage 停止（跨轮无意义）。"""
        for i in range(before_index - 1, -1, -1):
            msg = messages[i]
            if msg.role == "user":
                return None
            if self.is_ai_with_tool_use(msg) and any(tc.get("id") == tool_call_id for tc in msg.tool_calls):
                return i
        return None

    def _expand_recent_start_for_tool_pairs(self, messages: list, recent_start: int) -> int:
        """把保留后缀的起点向前扩展，让每个 ToolMessage 都能找到对应 tool_call（避免孤立 tool_result）。"""
        start = recent_start
        while start > 0 and self.is_tool_result(messages[start]):
            prev_ai = self._find_ai_with_tool_call(messages, start, messages[start].tool_call_id)
            if prev_ai is None or prev_ai >= start:
                break
            start = prev_ai
        return start

    def compact_history(
        self,
        messages: list,
        summarizer: Optional[Callable[[str], str]] = None,
        transcript_dir: Optional[Path] = None,
    ) -> list:
        """把中间一段 messages 压缩为单条摘要 HumanMessage。
        保留前缀：SystemMessage + workspace 指令。
        保留后缀：最后 PRESERVE_RECENT_SUMMARY_MESSAGES 条原文。
        压缩前先 write_transcript 做全量快照。
        """
        transcript_path = self.write_transcript(messages, transcript_dir)
        if len(messages) <= PRESERVE_RECENT_SUMMARY_MESSAGES + 1:
            return messages

        prefix_end = self._protected_prefix_end(messages)
        recent_start = max(prefix_end, len(messages) - PRESERVE_RECENT_SUMMARY_MESSAGES)
        recent_start = self._expand_recent_start_for_tool_pairs(messages, recent_start)
        if recent_start <= prefix_end:
            return messages

        to_summarize = messages[prefix_end:recent_start]
        if not to_summarize:
            return messages

        summary = self.summarize_history(to_summarize, summarizer=summarizer)
        print(f"[transcript saved: {transcript_path}]")
        return [
            *messages[:prefix_end],
            HumanMessage(content=f"<context_summary>\n{summary}\n</context_summary>"),
            *messages[recent_start:],
        ]

    # ── 3i. 编排：四层管线 ────────────────────────────────────────

    def _empty_operations(self) -> dict:
        """operations 字典的零值模板，session_manage 用 ops.get(key) 是否非 0 来决定是否打印。"""
        return {
            "tool_results_persisted": 0,
            "messages_snip_compacted": 0,
            "tool_results_micro_compacted": 0,
            "summary_messages_replaced": 0,
            "transcript_written": None,
        }

    def compact_if_needed(self, messages: list, force: bool = False) -> CompactResult:
        """每轮都跑 L3 → L1 → L2，由各层内部阈值决定是否真做修改；token 仍超 SUMMARY_TRIGGER_RATIO 时再走 L4。

        force=True 时跳过 L4 的阈值判断直接摘要（手动 /compact 用）。
        与 v2 教程 s08 一致：便宜的层（0 API）每轮必跑，昂贵的 L4（1 API）才按需触发。
        """
        before = self.context_stats(messages)
        operations = self._empty_operations()
        current, changed = messages, False

        # L3 budget —— 把超大 tool_result 落盘（内部阈值：总字节 > MAX_TOOL_RESULT_BYTES）
        persisted = self.tool_result_budget(current)
        if persisted:
            operations["tool_results_persisted"] = persisted
            changed = True

        # L1 snip —— 裁中间消息（内部阈值：消息数 > SNIP_MAX_MESSAGES）
        snipped = self.snip_compact(current)
        if len(snipped) != len(current):
            operations["messages_snip_compacted"] = len(current) - len(snipped)
            current, changed = snipped, True

        # L2 micro —— 旧 tool_result 占位（内部阈值：ToolMessage 数 > KEEP_RECENT 且 content > 320 字符）
        micro_count = self.micro_compact(current)
        if micro_count:
            operations["tool_results_micro_compacted"] = micro_count
            changed = True

        # L4 summary —— 仍超阈值（或 force）才用 LLM 摘要
        if force or self.context_stats(current).used_percent >= SUMMARY_TRIGGER_RATIO * 100:
            new_messages = self.compact_history(current)
            if len(new_messages) != len(current):
                operations["summary_messages_replaced"] = len(current) - len(new_messages)
                operations["transcript_written"] = str(self.transcript_dir / "transcript_*.jsonl")
                current, changed = new_messages, True

        return CompactResult(
            messages=current, changed=changed, operations=operations,
            before=before, after=self.context_stats(current),
        )
