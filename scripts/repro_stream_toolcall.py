#!/usr/bin/env python3
"""复现：主对话里 sub_agent 工具调用在流式聚合阶段"凭空消失"。

用法（仓库根目录）：
    .venv/bin/python scripts/repro_stream_toolcall.py <session_jsonl> [--cut N]

行为：读取会话 jsonl，截掉最后一条 assistant 消息（可疑的"空 tool_calls"响应），
把剩余消息原样喂给 streamed_create（与 agent_loop 完全同参），打印：
  - finish_reason
  - 聚合出的 content / reasoning 长度
  - 聚合出的 tool_calls（名称 + 参数）
并旁路记录每个原始 chunk 的 delta 形状（tool_calls 的 index/id/name/arguments），
用于判断"是模型没发工具调用"还是"聚合器把工具调用丢了"。
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

from config import load as load_config          # noqa: E402
load_config()
from llm_config import load_llm_config           # noqa: E402
load_llm_config()

from llm_manage import LLMClient                 # noqa: E402
from streaming_client import consume_stream       # noqa: E402
from tools import ToolRegistry                    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", help="会话 jsonl 路径")
    ap.add_argument("--cut", type=int, default=1,
                    help="从尾部截掉的消息条数（默认 1：去掉最后的 assistant）")
    ap.add_argument("--no-thinking", action="store_true")
    args = ap.parse_args()

    msgs = [json.loads(ln) for ln in
            Path(args.session).read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.cut:
        msgs = msgs[:-args.cut]
    print(f"[repro] 输入消息 {len(msgs)} 条，最后一条 role={msgs[-1].get('role')}")
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            print("[repro]   历史含 tool_calls:",
                  [tc["function"]["name"] for tc in m["tool_calls"]])

    reg = ToolRegistry()
    tools = reg.build_agent_tools(team_mode=False)
    print(f"[repro] 工具集 {len(tools)} 个，含 sub_agent="
          f"{any(t['function']['name'] == 'sub_agent' for t in tools)}")

    kwargs = dict(
        model=os.environ.get("OPENAI_MODEL_ID", ""),
        messages=msgs,
        tools=tools,
        tool_choice="auto",
        parallel_tool_calls=True,
        max_tokens=8000,
        temperature=0.5,
    )
    if not args.no_thinking:
        kwargs["reasoning_effort"] = "high"
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

    raw_chunks = []
    resp = LLMClient().llm.chat.completions.create(
        stream=True, stream_options={"include_usage": True}, **kwargs)

    class Spy:
        def __init__(self):
            self.inner = []

        def __iter__(self):
            for chunk in resp:
                raw_chunks.append(chunk)
                yield chunk

    message, finish_reason, usage = consume_stream(Spy(), sinks=[], should_stop=None)

    print(f"\n[repro] finish_reason = {finish_reason!r}  usage={usage}")
    print(f"[repro] content 长度 = {len(message.content or '')}")
    print(f"[repro] reasoning 长度 = {len(message.reasoning_content or '')}")
    if message.tool_calls:
        for tc in message.tool_calls:
            print(f"[repro] ✅ tool_call: {tc.function.name} args={tc.function.arguments[:300]}")
    else:
        print("[repro] ❌ 聚合结果没有任何 tool_call")
        print(f"[repro] content = {(message.content or '')[:400]!r}")

    # 原始 chunk 里 tool_calls 的真实形状
    tc_chunks = []
    for i, c in enumerate(raw_chunks):
        for ch in (getattr(c, "choices", None) or []):
            d = getattr(ch, "delta", None)
            tcs = getattr(d, "tool_calls", None) if d is not None else None
            if tcs:
                for tc in tcs:
                    tc_chunks.append({
                        "chunk": i,
                        "index": getattr(tc, "index", "<missing>"),
                        "id": getattr(tc, "id", None),
                        "name": getattr(getattr(tc, "function", None), "name", None),
                        "args": getattr(getattr(tc, "function", None), "arguments", None),
                    })
    print(f"\n[repro] 原始 chunk 总数 = {len(raw_chunks)}，含 tool_calls 的 delta = {len(tc_chunks)}")
    for row in tc_chunks[:40]:
        print("   ", row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
