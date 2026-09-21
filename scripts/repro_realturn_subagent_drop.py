#!/usr/bin/env python3
"""复现：真实模型 + 真实 SessionRuntime 链路下，sub_agent 派发后 turn 是否停止推进。

与 scripts/repro_bg_subagent_events.py 的区别：**LLM 用真实模型**（不桩），
只把 SubAgent.spawn_subagent 换成"睡 3 秒后返回一段摘要"的桩，避免真读 PDF。

记录：
  - 每次 LLM 调用的 finish_reason / content / reasoning 长度 / 聚合出的 tool_calls
  - 原始 chunk 里 tool_calls delta 的形状（判断是模型没发还是聚合丢了）
  - 全部 deliver 事件与 session_status 时间线
  - 会话落盘的最终消息序列

用法（仓库根目录）：
    .venv/bin/python scripts/repro_realturn_subagent_drop.py [--source <原会话jsonl>]
"""
import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

from config import load as load_config          # noqa: E402
load_config()
from llm_config import load_llm_config           # noqa: E402
load_llm_config()

import paths                                     # noqa: E402
from subagent_store import SubagentStore         # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="repro_realturn_"))
# 让所有模块都指向临时会话目录（不碰用户的 ~/.aigent/projects/default/.chathistory）
paths.CHAT_HISTORY_DIR = TMP

import agent_full_v2                             # noqa: E402
import session_runtime                           # noqa: E402
import subagent as subagent_mod                  # noqa: E402

agent_full_v2.CHAT_HISTORY_DIR = TMP
session_runtime.CHAT_HISTORY_DIR = TMP
subagent_mod.CHAT_HISTORY_DIR = getattr(subagent_mod, "CHAT_HISTORY_DIR", TMP)

T0 = time.monotonic()
TIMELINE: list[tuple[float, str, str]] = []
CALLS: list[dict] = []


def ts() -> str:
    return f"{time.monotonic() - T0:7.3f}s"


def deliver(kind: str, payload: dict) -> None:
    if kind == "event":
        ev = payload
        desc = ev.get("type", "?")
        bits = []
        if ev.get("subagent_id"):
            bits.append(f"sub={ev['subagent_id']}")
        if ev.get("tool_name"):
            bits.append(f"name={ev['tool_name']}")
        if desc in ("thinking_delta", "content_delta", "tool_call_delta"):
            bits.append(f"+{len(ev.get('text') or ev.get('args') or '')}ch")
        TIMELINE.append((time.monotonic() - T0, kind, f"{desc} {' '.join(bits)}"))
    else:
        TIMELINE.append((time.monotonic() - T0, kind,
                         json.dumps(payload, ensure_ascii=False)[:160]))


# ── 原始 chunk 间谍 LLM 代理 ────────────────────────────────────
class SpyCompletions:
    def __init__(self, inner, call_idx: int):
        self.inner = inner
        self.call_idx = call_idx
        self.raw = []

    def create(self, **kwargs):
        stream = self.inner.create(**kwargs)
        self.raw = []

        def gen():
            for chunk in stream:
                self.raw.append(chunk)
                yield chunk
        return gen()


class SpyChat:
    def __init__(self, inner, idx):
        # inner 是 SDK 的 Chat 命名空间，真正的 create 资源在 inner.completions
        self.completions = SpyCompletions(inner.completions, idx)


class SpyLLM:
    """把真实 LLM 客户端包一层，只拦截 `chat.completions.create` 记录原始 chunk。"""

    def __init__(self, inner, idx):
        self._inner = inner
        self.chat = SpyChat(inner.chat, idx)


_orig_streamed_create = agent_full_v2.streamed_create


def recording_streamed_create(llm, sinks=None, should_stop=None, **kwargs):
    idx = len(CALLS) + 1
    spy = SpyLLM(llm, idx)
    t_start = time.monotonic()
    print(f"[{ts()}] [main-llm] 第 {idx} 次调用开始 "
          f"(messages={len(kwargs.get('messages') or [])}, "
          f"max_tokens={kwargs.get('max_tokens')}, model={kwargs.get('model')})")
    message, finish_reason, usage = _orig_streamed_create(
        spy, sinks=sinks, should_stop=should_stop, **kwargs)
    tc_deltas = []
    for i, c in enumerate(spy.chat.completions.raw):
        for ch in (getattr(c, "choices", None) or []):
            d = getattr(ch, "delta", None)
            tcs = getattr(d, "tool_calls", None) if d is not None else None
            for tc in (tcs or []):
                tc_deltas.append({
                    "chunk": i,
                    "index": getattr(tc, "index", "<MISSING>"),
                    "id": getattr(tc, "id", None),
                    "name": getattr(getattr(tc, "function", None), "name", None),
                })
    rec = {
        "call": idx,
        "ms": round((time.monotonic() - t_start) * 1000),
        "finish_reason": finish_reason,
        "usage": usage,
        "content_len": len(message.content or ""),
        "reasoning_len": len(message.reasoning_content or ""),
        "aggregated_tools": [tc.function.name for tc in (message.tool_calls or [])],
        "raw_chunks": len(spy.chat.completions.raw),
        "raw_tc_delta_count": len(tc_deltas),
        "raw_tc_first": tc_deltas[:3],
        "content": (message.content or "")[:300],
        "reasoning_tail": (message.reasoning_content or "")[-160:],
    }
    CALLS.append(rec)
    print(f"[{ts()}] [main-llm] 第 {idx} 次调用完成 finish={finish_reason!r} "
          f"content={rec['content_len']} reasoning={rec['reasoning_len']} "
          f"tools={rec['aggregated_tools']} raw_tc_delta={rec['raw_tc_delta_count']}")
    return message, finish_reason, usage


def build_subagent_stub(agent):
    """把子智能体的重活（真读 PDF）换成 3 秒桩，其余链路保持真实。"""
    real_factory = agent.subagent_runner.__class__

    def stub_spawn_subagent(prompt, system_prompt=None, allowed_tools=None,
                            workdir=None, tool_call_id=""):
        sid = "sub_stub0001"
        started = time.time()
        agent.subagent_runner._emit_sub_agent(
            "sub_agent_start", sid, prompt[:120], tool_id=tool_call_id)
        for i in range(3):
            time.sleep(1.0)
            agent.subagent_runner._emit_sub_agent(
                "thinking_delta", sid, text=f"子智能体思考 {i}；")
            agent.subagent_runner._emit_sub_agent(
                "tool_exec_start", sid, tool_id=f"stub_t{i}", tool_name="run_read_pdf")
            time.sleep(0.01)
            agent.subagent_runner._emit_sub_agent(
                "tool_exec_end", sid, tool_id=f"stub_t{i}", tool_name="run_read_pdf")
        transcript = {
            "subagent_id": sid, "tool_call_id": tool_call_id,
            "name": (prompt[:80] + "…") if len(prompt) > 80 else prompt,
            "status": "done", "prompt": prompt, "thinking": "桩思考",
            "text": "桩摘要：6 个 PDF 均已读取。",
            "toolCalls": [{"tool_id": f"stub_t{i}", "name": "run_read_pdf",
                           "args": '{"path":"x.pdf"}', "status": "done"} for i in range(3)],
            "error": "", "started_at": "2026-09-14T10:00:00",
            "duration_ms": int((time.time() - started) * 1000),
        }
        agent.subagent_runner._emit_sub_agent(
            "sub_agent_end", sid, tool_id=tool_call_id)
        return "桩摘要：6 个 PDF 均已读取。", transcript

    agent.subagent_runner.spawn_subagent = stub_spawn_subagent
    return real_factory


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source",
                    default="/Users/peijiping/.aigent/projects/default/.chathistory/session_4.jsonl")
    args = ap.parse_args()

    # 用真实会话历史做上下文（同名 session_1.jsonl，切到临时目录）
    shutil.copy(args.source, TMP / "session_1.jsonl")
    # 去掉最后那条"声称要派子智能体"的空 tool_calls assistant 消息，回到派发前状态
    lines = [ln for ln in (TMP / "session_1.jsonl").read_text(
        encoding="utf-8").splitlines() if ln.strip()]
    (TMP / "session_1.jsonl").write_text(
        "\n".join(lines[:-1]) + "\n", encoding="utf-8")

    agent_full_v2.streamed_create = recording_streamed_create

    async def reply_sessions():
        pass

    rt = session_runtime.SessionRuntime(1, deliver, reply_sessions, lambda n: {})
    T0_local = time.monotonic()
    globals()["T0"] = T0_local
    print(f"[{ts()}] === 构建 agent ===")
    agent = rt.build_agent()
    build_subagent_stub(agent)
    print(f"[{ts()}] === start_turn 开始 ===")
    await rt.start_turn("看看我的pdf文件内容都有哪些，用子智能体看")
    if rt._bg_watch_task is not None:
        print(f"[{ts()}] === 等待 bg watch ===")
        try:
            await rt._bg_watch_task
        except Exception as e:  # noqa: BLE001
            print(f"[{ts()}] bg watch 异常: {type(e).__name__}: {e}")
    print(f"[{ts()}] === 结束 ===")

    print("\n════════ LLM 调用明细 ════════")
    for r in CALLS:
        print(json.dumps(r, ensure_ascii=False, indent=1))

    print("\n════════ 事件时间线 ════════")
    for t, kind, desc in TIMELINE:
        print(f"  {t:7.3f}s  [{kind:14s}] {desc}")

    print("\n════════ 会话落盘（最终） ════════")
    for i, ln in enumerate((TMP / "session_1.jsonl").read_text(
            encoding="utf-8").splitlines(), 1):
        m = json.loads(ln)
        tcs = m.get("tool_calls")
        print(f"  L{i} role={m.get('role')} "
              f"content_len={len(m.get('content') or '')} "
              f"tool_calls={[t['function']['name'] for t in tcs] if tcs else None} "
              f"content={json.dumps((m.get('content') or '')[:80], ensure_ascii=False)}")
    sidecar = TMP / "session_1.subagents.jsonl"
    print(f"\n[旁路文件] {sidecar.name} 存在={sidecar.exists()} "
          f"行数={len(sidecar.read_text(encoding='utf-8').splitlines()) if sidecar.exists() else 0}")
    print(f"[临时目录] {TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
