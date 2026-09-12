#!/usr/bin/env python3
"""
streaming_client.py - 流式输出统一模块（P0 ~ P4）

把六个 LLM 调用点从 stream=False 的同步调用统一到流式通道：

- 事件模型：StreamEvent（thinking_delta / content_delta / tool_call_start /
            tool_call_delta / tool_call / turn_end）
- sink 抽象：EventSink（消费事件）；PrintSink（CLI 增量打印，含预测式工具
            调用显示）；FilterSink（按事件类型转发，subagent 把工具事件转给
            UI）；WSSink（桌面端接缝：把事件序列化成 JSON 线协议推给前端）
- 聚合器：consume_stream 在分派事件的同时聚合出完整消息，返回与
          OpenAI `response.choices[0].message` 接口兼容的对象，
          下游（历史追加 / 截断恢复 / goal 评估）无需改动。
- 统一入口：streamed_create(llm, sinks, **kwargs) 等价 create() 但走流式。

P3 预测式工具调用：工具名一出现（首个 name delta）就发 tool_call_start，
参数增量随流发 tool_call_delta，聚合完再发一个带完整参数的 tool_call；
PrintSink 据此在参数还没拼完时就先把工具行显示出来（对齐主流智能体观感）。

P4 接缝：桌面前端只需把 WSSink 的 send 换成 websocket 发送函数，
后端其余代码零改动。
"""

import json
from types import SimpleNamespace
from typing import Callable, Iterator, List, Optional


# ── 事件模型 ──────────────────────────────────────────────────

class StreamEvent:
    """一次流式增量事件。type 取值：
    thinking_delta / content_delta / tool_call_start / tool_call_delta /
    tool_call / turn_end
    """
    def __init__(self, type, text="", tool_id="", tool_name="", args="",
                 finish_reason="", usage=None):
        self.type = type
        self.text = text
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.args = args
        self.finish_reason = finish_reason
        self.usage = usage or {}

    def to_dict(self) -> dict:
        """P4 线协议：把事件完整序列化成 dict（前端可直接渲染）。"""
        return {
            "type": self.type,
            "text": self.text,
            "tool_id": self.tool_id,
            "tool_name": self.tool_name,
            "args": self.args,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
        }

    def to_json(self) -> str:
        """P4 线协议：序列化成一行 JSON（JSON 行协议，前端按行解析）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)


class EventSink:
    """sink 协议：只负责消费事件，不关心事件从哪来。"""
    def emit(self, ev: StreamEvent):
        raise NotImplementedError


class PrintSink(EventSink):
    """CLI 增量打印：thinking 灰色细体，content 正常输出。
    P3 预测式工具调用：工具名一出现就把 `⚙ name(` 打出来，参数增量随流续写，
    聚合完成（tool_call 事件）时闭合 `)`。silent=True（cron）时静默。
    """
    def __init__(self, silent: bool = False):
        self.silent = silent
        self._mode = None  # None | "thinking" | "content" | "tool"

    # ── 块切换辅助：把当前打开的块干净收尾 ──
    def _leave_block(self):
        if self._mode == "thinking":
            print("\n[/thinking]\033[0m\n", end="", flush=True)
        elif self._mode == "content":
            print(flush=True)
        elif self._mode == "tool":
            print(")\033[0m", flush=True)
        self._mode = None

    def emit(self, ev: StreamEvent):
        if self.silent:
            return
        t = ev.type
        if t == "thinking_delta":
            if self._mode is None:
                print("\033[2;90m[thinking]\n", end="", flush=True)
                self._mode = "thinking"
            print(ev.text, end="", flush=True)
        elif t == "content_delta":
            if self._mode != "content":
                self._leave_block()
                self._mode = "content"
            print(ev.text, end="", flush=True)
        elif t == "tool_call_start":
            # 预测式显示：参数还没拼完就把工具行先画出来
            if self._mode != "tool":
                self._leave_block()
                self._mode = "tool"
            print(f"\033[2;93m  ⚙ {ev.tool_name}(", end="", flush=True)
            if ev.args:  # 罕见：start 事件已携带缓冲的参数前缀
                print(ev.args, end="", flush=True)
        elif t == "tool_call_delta":
            if self._mode == "tool":
                print(ev.args, end="", flush=True)
        elif t == "tool_call":
            # 聚合完成：预测行已打开就闭合；否则兜底打印完整行
            if self._mode == "tool":
                print(")\033[0m", flush=True)
                self._mode = None
            else:
                self._leave_block()
                print(f"\033[2;93m  ⚙ {ev.tool_name}({ev.args})\033[0m", flush=True)
        elif t == "turn_end":
            self._leave_block()


class FilterSink(EventSink):
    """只把指定事件类型转发给内层 sinks（subagent 把工具类事件转给 UI）。"""
    def __init__(self, sinks: List[EventSink], types):
        self.sinks = sinks
        self.types = set(types)

    def emit(self, ev: StreamEvent):
        if ev.type in self.types:
            for s in self.sinks:
                s.emit(ev)


class WSSink(EventSink):
    """P4 桌面端接缝：把 StreamEvent 序列化成 JSON 行协议推给前端。

    send_func: 实际的发送函数（如 websocket.send）。留空时打印兜底，
    便于无前端时单测 / 调试。前端按行解析 JSON 还原事件流即可，
    后端其余代码零改动（只需把 Agent 的 stream_sink 换成 WSSink）。
    """
    def __init__(self, send_func: Optional[Callable[[str], None]] = None):
        self.send_func = send_func or (lambda line: print(f"[WSSink] {line}"))

    def emit(self, ev: StreamEvent):
        self.send_func(ev.to_json())


# ── 聚合结果对象（接口兼容 OpenAI message） ─────────────────────

class StreamedToolCall:
    """流式聚合出的工具调用，接口兼容 OpenAI tool_call（含 model_dump）。"""
    def __init__(self, id: str, name: str, arguments: str):
        self.id = id
        self.type = "function"
        self.function = SimpleNamespace(name=name, arguments=arguments)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.function.name,
                         "arguments": self.function.arguments},
        }


class StreamedMessage:
    """流式聚合出的 assistant 消息，接口兼容 response.choices[0].message。"""
    def __init__(self, content, reasoning_content, tool_calls):
        self.role = "assistant"
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls

    def model_dump(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "reasoning_content": self.reasoning_content,
            "tool_calls": [tc.model_dump() for tc in self.tool_calls]
            if self.tool_calls else None,
        }


# ── 流式消费与统一入口 ─────────────────────────────────────────

def consume_stream(response, sinks: Optional[List[EventSink]] = None):
    """迭代流式响应：把增量事件分发给 sinks，同时聚合出完整消息。

    返回 (message, finish_reason, usage)：
    - message: StreamedMessage（.content / .reasoning_content / .tool_calls / model_dump）
    - finish_reason: str，如 "stop" / "length" / "tool_calls"
    - usage: dict（include_usage 拿到时才有值，否则空 dict）
    """
    sinks = sinks or []
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: dict = {}  # index -> {id, name, args}
    finish_reason = ""
    usage: dict = {}

    for chunk in response:
        # include_usage 时 usage 在最后的尾包上（choices 为空）
        u = getattr(chunk, "usage", None)
        if u is not None:
            usage = {
                "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                "total_tokens": getattr(u, "total_tokens", 0) or 0,
            }
        if not chunk.choices:
            continue

        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        delta = choice.delta

        # DeepSeek 的 thinking 增量（reasoning_content）
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            reasoning_parts.append(rc)
            ev = StreamEvent(type="thinking_delta", text=rc)
            for s in sinks:
                s.emit(ev)

        c = getattr(delta, "content", None)
        if c:
            content_parts.append(c)
            ev = StreamEvent(type="content_delta", text=c)
            for s in sinks:
                s.emit(ev)

        # 工具调用增量：同一 index 多次出现，逐段拼接 id/name/arguments。
        # 后续 delta 可能省略 id（首个携带 id），缺失字段用 getattr 兜底。
        # P3 预测式上行：name 首次出现发 tool_call_start（把此前缓冲的 args 一并带上），
        # 之后每个 arguments 片段发 tool_call_delta，让 UI 在参数拼完前先渲染工具行。
        tcs = getattr(delta, "tool_calls", None)
        if tcs:
            for tc in tcs:
                idx = tc.index
                if idx not in tool_calls:
                    tool_calls[idx] = {"id": "", "name": "", "args": "", "started": False}
                if getattr(tc, "id", None):
                    tool_calls[idx]["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        tool_calls[idx]["name"] = fn.name
                        if not tool_calls[idx]["started"]:
                            tool_calls[idx]["started"] = True
                            ev = StreamEvent(
                                type="tool_call_start",
                                tool_id=tool_calls[idx]["id"],
                                tool_name=fn.name,
                                args=tool_calls[idx]["args"],
                            )
                            for s in sinks:
                                s.emit(ev)
                    if getattr(fn, "arguments", None):
                        tool_calls[idx]["args"] += fn.arguments
                        # start 已发过才单独上行 delta；否则参数并入 start 事件
                        if tool_calls[idx]["started"]:
                            ev = StreamEvent(
                                type="tool_call_delta",
                                tool_id=tool_calls[idx]["id"],
                                tool_name=tool_calls[idx]["name"],
                                args=fn.arguments,
                            )
                            for s in sinks:
                                s.emit(ev)

    tool_call_objs = [
        StreamedToolCall(t["id"], t["name"], t["args"])
        for t in (tool_calls[i] for i in sorted(tool_calls))
    ]
    message = StreamedMessage(
        content="".join(content_parts) or None,
        reasoning_content="".join(reasoning_parts) or None,
        tool_calls=tool_call_objs or None,
    )

    # 工具调用与回合结束事件：对 UI 延迟不敏感，聚合后统一派发
    for tc in tool_call_objs:
        ev = StreamEvent(type="tool_call", tool_id=tc.id,
                         tool_name=tc.function.name, args=tc.function.arguments)
        for s in sinks:
            s.emit(ev)
    ev = StreamEvent(type="turn_end", finish_reason=finish_reason, usage=usage)
    for s in sinks:
        s.emit(ev)

    return message, finish_reason, usage


def streamed_create(llm, sinks: Optional[List[EventSink]] = None, **kwargs):
    """统一流式入口：等价 `llm.chat.completions.create(...)` 但走流式。

    - llm: OpenAI SDK 实例（LLMClient().llm 或子智能体的客户端）
    - sinks: 增量事件消费者（None 表示仅内部聚合，不上任何 UI）
    - kwargs: create() 的原生参数（model/messages/tools/max_tokens/...）
    返回 (message, finish_reason, usage)。
    """
    response = llm.chat.completions.create(
        stream=True,
        stream_options={"include_usage": True},
        **kwargs,
    )
    return consume_stream(response, sinks)
