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
import time
from typing import Awaitable, Callable, Dict, Optional

from agent_full_v2 import Agent
from llm_config import (
    ENV_LLM_LOCK, apply_model_to_env, restore_llm_env, snapshot_llm_env,
)
from paths import CHAT_HISTORY_DIR
from session_manage import SessionManager
from streaming_client import WSSink
from subagent_store import SubagentStore

# 后台任务完成后的自动续轮上限：正常场景（派后台子智能体 → 自动总结）只续一轮；
# 上限防御"续轮又派后台 → 再续轮"的极端连环派发，避免后台守望无限循环。
MAX_BG_FOLLOWUPS = 10

# deliver(kind, payload) -> 把信封投递回事件循环队列（线程安全，由 ws_bridge 提供）
Deliver = Callable[[str, dict], None]
# 会话结束时刷新会话列表的协程（由 ws_bridge 提供，依赖当前 ws 连接）
ReplySessions = Callable[[], Awaitable[None]]
# 读取某会话元数据（供模型绑定；由 ws_bridge 注入）
LoadMeta = Callable[[int], Optional[dict]]


def _bind_agent_env(load_meta: LoadMeta, num: int, agent_or_factory,
                    rebuild: bool) -> tuple[Agent, str | None]:
    """在全局锁内把会话记录模型换绑到 env，构造/重载 Agent 后再恢复全局 env。

    rebuild=False（首建）：传入 **Agent 工厂**（可调用对象），函数在锁内先换绑
    会话模型 env，再调用工厂构造 Agent —— 保证首轮起就按会话绑定模型生效。
    历史 bug：曾把已构造好的 Agent(silent=True) 作为参数传入，参数求值发生在
    函数体换绑 env 之前，导致首建永远捕获全局默认模型；首次 rebuild(True) 时
    _bound_model 又与元数据对齐，切换模型后比较失效、表现为"切换不生效"。
    rebuild=True（重载）：传入既有 Agent 实例，按会话模型就地
    reload_llm_bindings()。
    """
    meta = load_meta(num) or {}
    model_id = meta.get("model_id") or None
    with ENV_LLM_LOCK:
        snap = snapshot_llm_env()
        try:
            apply_model_to_env(model_id)
            if rebuild:
                agent_or_factory.reload_llm_bindings()
            else:
                agent_or_factory = agent_or_factory()
        finally:
            restore_llm_env(snap)
    return agent_or_factory, model_id


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
        self._turn_started: float = 0.0  # 当前 turn 开始时间（耗时统计用）

    # ── 事件路由：把会话内事件打上 session_num ──────────────────
    def _bind_sink(self, agent: Agent) -> None:
        def send_func(line: str):
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                ev = {"type": "unknown", "text": line}
            ev["session_num"] = self.num
            # 子智能体启动：立刻落一条 running 占位记录。进程被强杀（无终态）
            # 时历史里仍留痕迹，回放显示"运行中/已中断"；终态记录由 Agent 在
            # 拿到 transcript 后写入（同 subagent_id，后写覆盖先写）。
            if ev.get("type") == "sub_agent_start" and ev.get("subagent_id"):
                sm = agent.session_manager
                if sm is not None and agent.session_file is not None:
                    sm.begin_subagent(
                        agent.session_file,
                        subagent_id=ev["subagent_id"],
                        tool_call_id=ev.get("tool_id", ""),
                        name=(ev.get("text") or "子智能体")[:80],
                        prompt=ev.get("text") or "",
                    )
            self._deliver("event", ev)
        agent.stream_sink = WSSink(send_func=send_func)
        # 子智能体在 Agent.__init__ 阶段捕获了当时的 sinks（PrintSink），
        # 此处必须同步重绑，否则子智能体（含后台子任务）的 tool_call 事件
        # 只打印到后端 stdout，永远到不了前端 UI。
        agent.subagent_runner.sinks = [agent.stream_sink]

    def _bind_subagent_store(self, agent: Agent) -> None:
        """把子智能体旁路记录存储绑给本会话 Agent 的 SessionManager。

        Agent 内部惰性构造 SessionManager（init_session / switch_session 里
        `if self.session_manager is None`），所以在 switch_session 之前先把
        带 store 的实例建好即可；已存在则直接改属性。绑定后子智能体执行过程
        写到 `session_N.subagents.jsonl`，主会话文件只保留标准消息。
        """
        store = SubagentStore(CHAT_HISTORY_DIR)
        if agent.session_manager is None:
            agent.session_manager = SessionManager(
                CHAT_HISTORY_DIR, agent.system_prompt.build_system_prompt(),
                session_prefix=agent.session_prefix, subagent_store=store,
            )
        else:
            agent.session_manager.subagent_store = store

    def _push_status(self, status: str) -> None:
        """会话执行状态的唯一出口：关键节点打日志 + 广播到前端。
        running=turn 执行中；background=turn 结束但后台任务仍在跑；
        done/stopped=全部结束。排查"前端执行状态断了"先看这串日志。"""
        print(f"[session {self.num}] status -> {status}")
        self._deliver("session_status", {"num": self.num, "status": status})

    def build_agent(self) -> Agent:
        """按需构造本会话的 Agent，并按其元数据记录的模型独立绑定（在工作线程里调用）。

        首次构造：在锁内换绑会话模型 env 后用工厂构造 Agent（首轮即按会话绑定模型）；
        之后：若会话记录的模型与已绑定的不同，则原地 reload_llm_bindings()。
        """
        if self.agent is None:
            agent, model_id = _bind_agent_env(
                self._load_meta, self.num, lambda: Agent(silent=True), rebuild=False
            )
            self.agent = agent
            self._bound_model = model_id
            self._bind_sink(self.agent)
            # 必须在 switch_session 之前绑 store（后者会惰性构造 SessionManager）
            self._bind_subagent_store(self.agent)
            self.agent.switch_session(self.num)
            print(f"[session {self.num}] agent built (model={model_id or 'global-default'})")
            return self.agent
        model_id = (self._load_meta(self.num) or {}).get("model_id") or None
        if model_id != self._bound_model:
            print(f"[session {self.num}] rebinding model "
                  f"{self._bound_model or 'global-default'} -> {model_id or 'global-default'}")
            _bind_agent_env(self._load_meta, self.num, self.agent, rebuild=True)
            self._bound_model = model_id
        return self.agent

    def request_stop(self) -> None:
        """请求停止本会话当前 turn（线程安全）；不影响其它会话。"""
        self.stop_evt.set()
        if self.agent is not None:
            self.agent.request_stop()

    def current_status(self) -> Optional[str]:
        """新连接状态重放用（ws_bridge.handle）：
        running=turn 执行中；background=turn 已结束但后台任务仍在跑；None=空闲。"""
        if self.busy:
            return "running"
        if self.agent is not None and self.agent.background_manager.has_running():
            return "background"
        return None

    async def start_turn(self, text: str,
                         reasoning_effort: Optional[str] = None,
                         max_context: Optional[str] = None) -> None:
        """派发一轮对话：后台线程跑 run_turn，事件循环保持可读。

        先发 running，结束后按终态发**一条**状态：
        - stopped：用户主动停止；
        - background：turn 结束但后台任务（如后台子智能体）仍在跑，
          守望其完成后再回 done；
        - done：全部结束。
        （历史缺陷：结束路径先发 done 再发 background，两条之间隔着
          reply_sessions 的磁盘 IO，前端运行指示会"消失又出现"闪烁。）
        """
        # 新 turn 开始：取消上一轮遗留的后台守望（避免旧守望把运行中的 turn 误报 done）
        if self._bg_watch_task is not None:
            print(f"[session {self.num}] bg watch cancelled (new turn)")
            self._bg_watch_task.cancel()
            self._bg_watch_task = None
        self._pending_overrides = (reasoning_effort, max_context)
        self.busy = True
        self._turn_started = time.monotonic()
        self._push_status("running")
        stopped = False
        try:
            await asyncio.to_thread(self._run_turn_worker, text)
            stopped = self.stop_evt.is_set()
        except Exception as e:
            # run 线程内任何未捕获异常都不应压垮事件循环：
            # 状态按 done 回，让前端侧边栏复位；真实错误已由 agent 内部处理。
            print(f"[session {self.num}] turn worker crashed: "
                  f"{type(e).__name__}: {e}")
            stopped = False
        finally:
            self.busy = False
            self.stop_evt.clear()
            elapsed = time.monotonic() - self._turn_started
            bg_running = (
                self.agent is not None
                and self.agent.background_manager.has_running()
            )
            # 终态只发一条：有后台任务直接进 background，避免 done→background
            # 闪烁；无后台任务发 done/stopped 一步到位。
            self._push_status(
                "stopped" if stopped else ("background" if bg_running else "done")
            )
            print(f"[session {self.num}] turn end ({elapsed:.1f}s, "
                  f"{'stopped' if stopped else 'done'}, "
                  f"bg_running={bg_running})")
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
            if bg_running:
                self._bg_watch_task = asyncio.create_task(self._watch_background(elapsed))

    async def _watch_background(self, turn_elapsed: float = 0.0) -> None:
        """守望后台任务直到全部完成（事件循环内轮询），完成后回 done。

        若后台任务完成后尚有未消费的执行结果（如后台子智能体刚跑完、
        结果还没注入主智能体上下文），自动续一轮 turn（run_background_followup）
        让主智能体拿到 task_notification 并给出最终总结；否则主智能体会
        停在"后台任务已派发"的占位回复上，用户永远等不到最后总结。
        续轮本身也可能再派后台任务 → 循环守望；MAX_BG_FOLLOWUPS 兜底。
        """
        started = time.monotonic()
        print(f"[session {self.num}] bg watch start")
        followups = 0
        try:
            while True:
                while self.agent is not None and self.agent.background_manager.has_running():
                    await asyncio.sleep(1.0)
                # 用户中途点了停止：不再自动续轮，直接收尾
                if self.stop_evt.is_set():
                    break
                # 无待消费结果或达续轮上限：收尾回 done
                if (self.agent is None
                        or not self.agent.background_manager.has_completed_pending()
                        or followups >= MAX_BG_FOLLOWUPS):
                    break
                followups += 1
                print(f"[session {self.num}] bg followup turn #{followups}")
                self.busy = True
                self._push_status("running")
                try:
                    await asyncio.to_thread(self._run_followup_worker)
                except Exception as e:
                    print(f"[session {self.num}] bg followup crashed: "
                          f"{type(e).__name__}: {e}")
                finally:
                    self.busy = False
                # 续轮结束后：若用户在这期间点了停止，保留停止信号并退出循环；
                # 否则清掉信号进入下一轮判断（避免把停止误当正常信号吞掉）
                if self.stop_evt.is_set():
                    break
                self.stop_evt.clear()
            total = turn_elapsed + (time.monotonic() - started)
            print(f"[session {self.num}] bg watch done ({total:.1f}s total)")
            self._push_status("done")
            await self._reply_sessions()
        except asyncio.CancelledError:
            print(f"[session {self.num}] bg watch cancelled")
            raise  # 新 turn 已开始，状态由 start_turn 接管

    def _run_followup_worker(self) -> None:
        """后台完成后的自动续轮 worker（非用户输入，复用会话模型与参数覆盖）。"""
        agent = self.build_agent()
        reasoning_effort, max_context = self._pending_overrides
        agent.set_request_overrides(
            reasoning_effort=reasoning_effort, max_context=max_context
        )
        try:
            if agent.session_manager is not None:
                agent.session_manager.set_max_context(max_context)
        except Exception:
            pass
        agent.run_background_followup()

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

    def all_runtimes(self) -> list["SessionRuntime"]:
        """所有已注册的会话运行时（新连接状态重放用）。"""
        return list(self._sessions.values())

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