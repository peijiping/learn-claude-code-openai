#!/usr/bin/env python3
"""离线复现：后台子智能体执行期间，事件是否持续流到 deliver（前端）。

走真实生产链路：
  SessionRuntime.start_turn → to_thread(run_turn) → agent_loop
    → sub_agent(run_in_background=true) → BackgroundManager 守护线程
    → SubAgent.spawn_subagent（子 LLM 桩：流式 thinking + 工具调用，带延时）
    → 全部事件经 WSSink(send_func=SessionRuntime._bind_sink.send_func) → deliver

记录 (相对时刻, kind, 事件) 时间线，验证后台窗口内事件是否断流。
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

import agent_full_v2  # noqa: E402
import subagent as subagent_mod  # noqa: E402
from background_manager import BackgroundManager  # noqa: E402
from hooks import HookSystem  # noqa: E402
from session_manage import SessionManager  # noqa: E402
from session_runtime import SessionRuntime  # noqa: E402
from subagent_store import SubagentStore  # noqa: E402
from streaming_client import consume_stream  # noqa: E402

T0 = time.monotonic()
TIMELINE: list[tuple[float, str, str]] = []


def ts() -> str:
    return f"{time.monotonic() - T0:7.3f}s"


def deliver(kind: str, payload: dict) -> None:
    """生产 deliver 的替身：ws_bridge.deliver → call_soon_threadsafe(hub.broadcast)。
    这里直接记录（时序本身就是被测对象）；广播层此前已单独验证过是纯转发。"""
    if kind == "event":
        ev = payload
        desc = ev.get("type", "?")
        extra = []
        if ev.get("subagent_id"):
            extra.append(f"sub={ev['subagent_id']}")
        if ev.get("tool_id"):
            extra.append(f"tid={ev['tool_id'][:18]}")
        if ev.get("tool_name"):
            extra.append(f"name={ev['tool_name']}")
        if desc in ("thinking_delta", "content_delta", "tool_call_delta"):
            extra.append(f"+{len(ev.get('text') or ev.get('args') or '')}ch")
        TIMELINE.append((time.monotonic() - T0, kind, payload,
                         f"{desc} {' '.join(extra)}"))
    else:
        TIMELINE.append((time.monotonic() - T0, kind, payload,
                         json_s(payload)[:110]))


def json_s(o) -> str:
    import json
    return json.dumps(o, ensure_ascii=False)


# ── 伪 LLM 流式响应 ─────────────────────────────────────────────
def chunk(delta=None, finish=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(**(delta or {})), finish_reason=finish)],
        usage=usage)


def stream_of(chunks):
    return iter(chunks)


class MainLLMScript:
    """主智能体 LLM 脚本：第 1 次调用派 sub_agent(后台)，第 2 次输出占位正文后结束。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, llm, sinks=None, should_stop=None, **kwargs):
        self.calls += 1
        n = self.calls
        print(f"[{ts()}] [main-llm] 第 {n} 次调用开始")
        if n == 1:
            chunks = [
                chunk(delta={"reasoning_content": "主智能体思考：需要派子智能体读pdf"}),
                chunk(delta={"content": "我派一个后台子智能体去读。"}),
                chunk(delta={"tool_calls": [SimpleNamespace(
                    index=0, id="call_BG1", type="function",
                    function=SimpleNamespace(name="sub_agent", arguments=''))]}),
                chunk(delta={"tool_calls": [SimpleNamespace(
                    index=0, id="call_BG1", type="function",
                    function=SimpleNamespace(name=None, arguments='{"prompt":"读6个pdf","run_in_background":true}'))]}),
            ]
            msg, fr, usage = consume_stream(stream_of(chunks), sinks, should_stop=should_stop)
            print(f"[{ts()}] [main-llm] 第 {n} 次调用完成 (finish={fr})")
            return msg, fr, usage
        # 第 2 次调用：占位回复（此刻 bg 还在跑）
        print(f"[{ts()}] [main-llm] 第 {n} 次调用：输出占位正文（模拟主智能体先收尾）")
        chunks = [
            chunk(delta={"content": "已把读取任务放到后台，请稍等。"}),
            chunk(delta={}, finish="stop", usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=10, total_tokens=110)),
        ]
        msg, fr, usage = consume_stream(stream_of(chunks), sinks, should_stop=should_stop)
        print(f"[{ts()}] [main-llm] 第 {n} 次调用完成 (finish={fr})")
        return msg, fr, usage


class SubLLMScript:
    """子智能体 LLM 脚本：慢速流式 thinking（模拟 2.5s 思考）→ 2 个工具调用 → 收尾。

    每个工具调用后再来一轮小 thinking，总时长 ~3s，保证后台窗口可观测。
    """

    def __init__(self):
        self.calls = 0

    def __call__(self, llm, sinks=None, should_stop=None, **kwargs):
        self.calls += 1
        n = self.calls
        print(f"[{ts()}] [sub-llm] 子智能体第 {n} 次调用开始")
        if n == 1:
            # 慢速 thinking：20 段 × 0.1s = 2s
            for i in range(20):
                time.sleep(0.1)
                consume_stream(stream_of([chunk(delta={"reasoning_content": f"子思考{i} "})]), sinks)
            chunks = [
                chunk(delta={"tool_calls": [SimpleNamespace(
                    index=0, id="call_S1", type="function",
                    function=SimpleNamespace(name="run_read_pdf", arguments='{"path":"a.pdf","max_pages":3}'))]}),
                chunk(delta={"tool_calls": [SimpleNamespace(
                    index=1, id="call_S2", type="function",
                    function=SimpleNamespace(name="run_read_pdf", arguments='{"path":"b.pdf","max_pages":3}'))]}),
            ]
            msg, fr, usage = consume_stream(stream_of(chunks), sinks)
            print(f"[{ts()}] [sub-llm] 第 {n} 次调用完成 (finish={fr})")
            return msg, fr, usage
        # 收尾
        chunks = [
            chunk(delta={"reasoning_content": "子智能体汇总思考 "}),
            chunk(delta={"content": "### 结果\n两个 pdf 都读完了"}),
            chunk(delta={}, finish="stop"),
        ]
        msg, fr, usage = consume_stream(stream_of(chunks), sinks)
        print(f"[{ts()}] [sub-llm] 第 {n} 次调用完成 (finish={fr})")
        return msg, fr, usage


def fake_read_pdf(**kwargs):
    time.sleep(0.15)  # 模拟工具耗时
    return "pdf 内容若干字" * 30


def build_offline_agent(tmp: Path):
    """Agent.__new__ 离线桩（照 tests/test_subagent_sidecar.py 的模式），只填 agent_loop 需要的字段。"""
    agent = agent_full_v2.Agent.__new__(agent_full_v2.Agent)
    agent.session_prefix = "session_"
    agent.session_num = 1
    agent.silent = True
    agent.total_tokens = 0
    agent.max_agent_iterations = 10
    agent._stop_evt = __import__("threading").Event()
    agent.model = "offline-stub"
    agent.llm_client = None
    agent._request_overrides = {"reasoning_effort": None, "max_context": None}
    agent.team_mode = False
    agent.background_manager = BackgroundManager()

    # 会话：真实 SessionManager（纯磁盘），预写 system + user
    sm = SessionManager(tmp, "sys", subagent_store=SubagentStore(tmp))
    agent.session_manager = sm
    f = tmp / "session_1.jsonl"
    f.write_text("".join(__import__("json").dumps(m, ensure_ascii=False) + "\n" for m in [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "看看我的pdf内容都有哪些？用子智能体看"},
    ]), encoding="utf-8")
    agent.session_file = f
    agent.history_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "看看我的pdf内容都有哪些？用子智能体看"},
    ]

    # agent_loop 依赖的离线替身
    agent._sync_memory_index = lambda: None
    agent._sync_environment = lambda: None
    agent._sync_project_rules = lambda: None
    agent._print = lambda *a, **k: None
    agent._advanced_llm_kwargs = lambda: {}
    agent.goal_controller = SimpleNamespace(
        begin_query=lambda: None,
        evaluate_after_turn=lambda *a, **k: SimpleNamespace(action="allow", reason=""))

    hooks = HookSystem(silent=True)
    hooks.register_default_hooks()
    agent.hook_system = hooks

    from error_recovery import ErrorRecovery
    agent.recovery = ErrorRecovery(primary_model="offline-stub", fallback_model="")

    class StubTools:
        def build_agent_tools(self, team_mode=False):
            return []
        def get_todo_manager(self):
            return SimpleNamespace(has_open_items=lambda: False)
    agent.tools = StubTools()

    # 子智能体：真实 SubAgent，LLM 走慢速脚本桩；工具 handle 里给假 read_pdf
    sub = subagent_mod.SubAgent.__new__(subagent_mod.SubAgent)
    sub.base_tools = []
    sub.tool_handlers = {"run_read_pdf": fake_read_pdf}
    sub.tool_registry = None
    sub.hook_system = hooks
    sub.sub_llm_client = None
    sub.model = "offline-stub"
    sub.sinks = []  # _bind_sink 会重绑为 [WSSink]
    agent.subagent_runner = sub

    agent._subagent_transcripts = {}
    agent._subagent_persisted = set()
    import threading
    agent._subagent_lock = threading.Lock()
    return agent


async def main():
    global T0
    tmpctx = tempfile.TemporaryDirectory()
    tmp = Path(tmpctx.name)
    agent = build_offline_agent(tmp)

    # 主 LLM 脚本桩（agent_full_v2 命名空间里的 streamed_create）
    orig_main = agent_full_v2.streamed_create
    orig_sub = subagent_mod.streamed_create
    agent_full_v2.streamed_create = MainLLMScript()
    subagent_mod.streamed_create = SubLLMScript()

    async def reply_sessions():
        pass

    rt = SessionRuntime(1, deliver, reply_sessions, lambda n: {})
    # 关键：走生产 bind —— send_func 含 begin_subagent 落盘 + deliver
    rt.agent = agent
    rt._bound_model = None
    rt._bind_sink(agent)

    T0 = time.monotonic()
    print(f"[{ts()}] === start_turn 开始 ===")
    await rt.start_turn("看看我的pdf内容都有哪些？用子智能体看")
    # start_turn 返回时 turn 已结束；若进入 background 态，等待 bg watch 收尾
    if rt._bg_watch_task is not None:
        print(f"[{ts()}] === 等待 bg watch 收尾 ===")
        try:
            await rt._bg_watch_task
        except Exception as e:
            print(f"[{ts()}] bg watch 异常: {type(e).__name__}: {e}")
    print(f"[{ts()}] === 全部结束 ===")

    agent_full_v2.streamed_create = orig_main
    subagent_mod.streamed_create = orig_sub
    tmpctx.cleanup()

    # ── 输出时间线 ──
    print("\n════════ 事件时间线（相对 start_turn）════════")
    for t, kind, _pl, desc in TIMELINE:
        print(f"  {t:7.3f}s  [{kind:14s}] {desc}")

    # 导出原始 envelope 序列（供前端 store 回放测试用）
    export = [{"t": round(t, 4), "kind": kind, "payload": pl}
              for t, kind, pl, _d in TIMELINE]
    out = Path(__file__).parent / "repro_timeline.json"
    out.write_text(json.dumps(export, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[导出] {out}")

    # ── 判定 ──
    evs = [x for x in TIMELINE if x[1] == "event"]
    kinds = [x[3].split(" ")[0] for x in evs]
    stat = [(x[0], x[3]) for x in TIMELINE if x[1] == "session_status"]
    print("\n════════ 状态机 ════════")
    for t, desc in stat:
        print(f"  {t:7.3f}s  {desc}")

    bg_start = next((t for t, d in stat if "background" in d), None)
    follow = next((t for t, d in stat if "running" in d and bg_start and t > bg_start), None)
    if bg_start and follow:
        during = [x[3] for x in evs if bg_start < x[0] < follow]
        print(f"\n后台窗口 ({bg_start:.2f}s → {follow:.2f}s) 内事件数: {len(during)}")
        for d in during:
            print(f"    {d}")
        n_start = sum(1 for d in during if d.startswith("sub_agent_start"))
        n_end = sum(1 for d in during if d.startswith("sub_agent_end"))
        n_think = sum(1 for d in during if d.startswith("thinking_delta"))
        n_tool = sum(1 for d in during if d.startswith("tool_call"))
        print(f"  → sub_agent_start={n_start} sub_agent_end={n_end} thinking={n_think} tool={n_tool}")
        if n_think == 0 and n_tool == 0:
            print("  ❌ 后台窗口内子智能体事件断流（复现 bug）")
        else:
            print("  ✅ 后台窗口内事件持续流入")


if __name__ == "__main__":
    asyncio.run(main())
