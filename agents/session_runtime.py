"""
session_runtime.py - 每会话一个并发运行时的管理器（桌面端并发/后台会话接缝）。

把「一个会话对应一个独立 Agent 实例 + 会话级事件路由 + 协作式停止」封装为
SessionRuntime，由 SessionRuntimeRegistry 按会话号维护。仅作为 ws_bridge 之上
的薄封装，不自行持有 WS 连接或会话号分配策略。

设计要点：
- 每个在跑的会话各自一个 SessionRuntime，各自持有独立 Agent 实例，事件通过
  各自的 WSSink 打上 session_num 后投递，前端据此路由到对应消息缓冲；
- run_turn 在 to_thread 工作线程里执行，EventLoop 不被阻塞 → 一个会话在后台跑时，
  其它会话的 chat / session_switch / stop 命令仍能被事件循环接收处理；
- 停止为协作式：request_stop() 置位一个会话专属的 stop_evt，并同步调
  Agent.request_stop()；agent_loop 在迭代边界 / 流式 chunk 间检查并干净收尾，
  不影响其它会话。

会话号确定时机（由 ws_bridge 保证确定性）：
- 已有会话（前端携 num）：start_turn 时按 num build_agent() → switch_session 加载历史；
- 全新会话（前端 fresh / 无 num）：由 ws_bridge 在事件循环内同步 create_new_session()
  先领号并写入初始 system 消息，避免并发线程 race 到同一编号。
"""
import asyncio
import json
import threading
from typing import Awaitable, Callable, Dict, Optional

from agent_full_v2 import Agent
from llm_config import (
    ENV_LLM_LOCK, apply_model_to_env, restore_llm_env, snapshot_llm_env,
)
from streaming_client import WSSink

# deliver(kind, payload) -> 把信封投递回事件循环队列（线程安全，由 ws_bridge 提供）
Deliver = Callable[[str, dict], None]
# 会话结束时刷新会话列表的协程（由 ws_bridge 提供，依赖当前 ws 连接）
ReplySessions = Callable[[], Awaitable[None]]
# 读取某会话元数据（供模型绑定；由 ws_bridge 注入）
LoadMeta = Callable[[int], Optional[dict]]


def _bind_agent_env(load_meta: LoadMeta, num: int, agent: Agent,
                    rebuild: bool) -> tuple[Agent, str | None]:
    """在全局锁内把会话记录模型换绑到 env，构造/重载 Agent 后再恢复全局 env。

    rebuild=False 时为首次构造前的「准备 env」；rebuild=True 时按会话模型
    就地 reload_llm_bindings()。
    """
    meta = load_meta(num) or {}
    model_id = meta.get("model_id") or None
    with ENV_LLM_LOCK:
        snap = snapshot_llm_env()
        try:
            apply_model_to_env(model_id)
            if rebuild:
                agent.reload_llm_bindings()
        finally:
            restore_llm_env(snap)
    return agent, model_id


class SessionRuntime:
    def __init__(self, num: int, deliver: Deliver, reply_sessions: ReplySessions,
                 load_meta: LoadMeta):
        self.num = num
        self.agent: Optional[Agent] = None
        self.busy = False  # 本会话当前是否有一个 turn 在跑（拒绝同会话并发）
        self.stop_evt = threading.Event()
        self._pending_overrides: tuple = (None, None)  # (reasoning_effort, max_context)
        self._bg_watch_task: Optional[asyncio.Task] = None  # 后台任务完成守望
        self._deliver = deliver
        self._reply_sessions = reply_sessions
        self._load_meta: LoadMeta = load_meta
        self._bound_model: str | None = None  # 当前 agent 实际绑定的会话模型 id

    # ── 事件路由：把会话内事件打上 session_num ──────────────────
    def _bind_sink(self, agent: Agent) -> None:
        def send_func(line: str):
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                ev = {"type": "unknown", "text": line}
            ev["session_num"] = self.num
            self._deliver("event", ev)
        agent.stream_sink = WSSink(send_func=send_func)
        # 子智能体在 Agent.__init__ 阶段捕获了当时的 sinks（PrintSink），
        # 此处必须同步重绑，否则子智能体（含后台子任务）的 tool_call 事件
        # 只打印到后端 stdout，永远到不了前端 UI。
        agent.subagent_runner.sinks = [agent.stream_sink]

    def build_agent(self) -> Agent:
        """按需构造本会话的 Agent，并按其元数据记录的模型独立绑定（在工作线程里调用）。

        首次构造：在锁内换绑会话模型 env 后 Agent(silent=True)；
        之后：若会话记录的模型与已绑定的不同，则原地 reload_llm_bindings()。
        """
        if self.agent is None:
            agent, model_id = _bind_agent_env(
                self._load_meta, self.num, Agent(silent=True), rebuild=False
            )
            self.agent = agent
            self._bound_model = model_id
            self._bind_sink(self.agent)
            self.agent.switch_session(self.num)
            return self.agent
        model_id = (self._load_meta(self.num) or {}).get("model_id") or None
        if model_id != self._bound_model:
            _bind_agent_env(self._load_meta, self.num, self.agent, rebuild=True)
            self._bound_model = model_id
        return self.agent

    def request_stop(self) -> None:
        """请求停止本会话当前 turn（线程安全）；不影响其它会话。"""
        self.stop_evt.set()
        if self.agent is not None:
            self.agent.request_stop()

    async def start_turn(self, text: str,
                         reasoning_effort: Optional[str] = None,
                         max_context: Optional[str] = None) -> None:
        """派发一轮对话：后台线程跑 run_turn，事件循环保持可读。

        先发 running，结束后发 done/stopped 并刷新会话列表；
        若 turn 结束时仍有后台任务（如后台子智能体）在跑，改发 background
        并守望其完成后再回 done——让前端在整个执行期间都能看到执行状态。
        """
        # 新 turn 开始：取消上一轮遗留的后台守望（避免旧守望把运行中的 turn 误报 done）
        self._pending_overrides = (reasoning_effort, max_context)
        if self._bg_watch_task is not None:
            self._bg_watch_task.cancel()
            self._bg_watch_task = None
        self.busy = True
        self._deliver("session_status", {"num": self.num, "status": "running"})
        stopped = False
        try:
            await asyncio.to_thread(self._run_turn_worker, text)
            stopped = self.stop_evt.is_set()
        except Exception:
            # run 线程内任何未捕获异常都不应压垮事件循环：
            # 状态按 done 回，让前端侧边栏复位；真实错误已由 agent 内部处理。
            stopped = False
        finally:
            self.busy = False
            self.stop_evt.clear()
            self._deliver("session_status", {
                "num": self.num,
                "status": "stopped" if stopped else "done",
            })
            # 本轮结束：推送该会话最新的上下文统计（供前端圆圈指示器刷新）
            try:
                if self.agent is not None and self.agent.session_manager is not None:
                    self._deliver("context_stats", {
                        "num": self.num,
                        **self.agent.session_manager.context_stats_dict(
                            self.agent.history_messages
                        ),
                    })
            except Exception:
                pass  # 统计失败不阻断本轮收尾
            await self._reply_sessions()
            # turn 已结束但后台任务仍在跑：进入 background 态并守望完成
            if self.agent is not None and self.agent.background_manager.has_running():
                self._deliver("session_status", {
                    "num": self.num, "status": "background",
                })
                self._bg_watch_task = asyncio.create_task(self._watch_background())

    async def _watch_background(self) -> None:
        """守望后台任务直到全部完成（事件循环内轮询），完成后回 done。"""
        try:
            while self.agent is not None and self.agent.background_manager.has_running():
                await asyncio.sleep(1.0)
            self._deliver("session_status", {"num": self.num, "status": "done"})
            await self._reply_sessions()
        except asyncio.CancelledError:
            pass  # 新 turn 已开始，状态由 start_turn 接管

    def _run_turn_worker(self, text: str) -> None:
        agent = self.build_agent()
        reasoning_effort, max_context = self._pending_overrides
        agent.set_request_overrides(
            reasoning_effort=reasoning_effort, max_context=max_context
        )
        # 会话级上下文覆盖同步到压缩器阈值（None 恢复全局默认）
        try:
            if agent.session_manager is not None:
                agent.session_manager.set_max_context(max_context)
        except Exception:
            pass
        agent.run_turn(text)


class SessionRuntimeRegistry:
    """会话号 → SessionRuntime 的映射；共享 delivery 与会话列表刷新回调。"""

    def __init__(self, deliver: Deliver, reply_sessions: ReplySessions,
                 load_meta: LoadMeta):
        self._deliver = deliver
        self._reply_sessions = reply_sessions
        self._load_meta = load_meta
        self._sessions: Dict[int, SessionRuntime] = {}

    def get(self, num: int) -> Optional[SessionRuntime]:
        return self._sessions.get(num)

    def get_or_create(self, num: int) -> SessionRuntime:
        rt = self._sessions.get(num)
        if rt is None:
            rt = SessionRuntime(num, self._deliver, self._reply_sessions, self._load_meta)
            self._sessions[num] = rt
        return rt

    def is_busy(self, num: int) -> bool:
        rt = self._sessions.get(num)
        return bool(rt and rt.busy)

    def remove(self, num: int) -> None:
        """会话被删除/回收后移除其运行时（运行中会被上层拒绝后才到达这里）。"""
        self._sessions.pop(num, None)

    def reload_llm_bindings(self) -> None:
        """模型配置热切换后重绑所有已构造的运行时会话 Agent。

        每个会话按其各自元数据记录的模型重绑（无记录则落到新全局 env），
        维护每会话独立绑定语义；不会把所有会话统一串到同一个新全局模型。
        """
        for rt in self._sessions.values():
            if rt.agent is not None:
                rt.agent, rt._bound_model = _bind_agent_env(
                    self._load_meta, rt.num, rt.agent, rebuild=True
                )