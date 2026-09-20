"""
session_runtime.py - 每会话一个并发运行时的管理器（桌面端并发/后台会话接缝）。

把「一个会话对应一个独立 Agent 实例 + 会话级事件路由 + 协作式停止」封装为
SessionRuntime，由 SessionRuntimeRegistry 按会话号维护。仅作为 ws_bridge 之上
的薄封装，不自行持有 WS 连接或会话号分配策略。

设计要点：
- 每个在跑的会话各自一个 SessionRuntime，各自持有独立 Agent 实例，事件通过
  各自的 WSSink 打上 session_id 后投递，前端据此路由到对应消息缓冲；
- run_turn 在 to_thread 工作线程里执行，EventLoop 不被阻塞 → 一个会话在后台跑时，
  其它会话的 chat / session_switch / stop 命令仍能被事件循环接收处理；
- 停止为协作式：request_stop() 置位一个会话专属的 stop_evt，并同步调
  Agent.request_stop()；agent_loop 在迭代边界 / 流式 chunk 间检查并干净收尾，
  不影响其它会话。

会话 id 确定时机（由 ws_bridge 保证确定性）：
- 已有会话（前端携 session_id）：start_turn 时按 id build_agent() → switch_session 加载历史；
- 全新会话（前端 fresh / 无 session_id）：由 ws_bridge 在事件循环内同步 create_new_session()
  先生成短 id 并写入初始 system 消息，避免并发线程 race 到同一会话。
"""
import asyncio
import json
import threading
import time
from typing import Awaitable, Callable, Dict, Optional

from agent_full_v2 import Agent
from llm_config import (
    ENV_LLM_LOCK, apply_model_to_env, resolve_model_window, restore_llm_env,
    snapshot_llm_env,
)
from logger import get_logger
from paths import CHAT_HISTORY_DIR
from session_manage import SessionManager
from streaming_client import WSSink
from subagent_store import SubagentStore

# 统一日志（~/.aigent/logs/agent_日期.log）
log = get_logger("runtime")

# 后台任务完成后的自动续轮上限：正常场景（派后台子智能体 → 自动总结）只续一轮；
# 上限防御"续轮又派后台 → 再续轮"的极端连环派发，避免后台守望无限循环。
MAX_BG_FOLLOWUPS = 10

# 续轮**异常**后的额外重试次数（2026-09-18 事故新增）：续轮本身抛异常时，
# 把本批已消费的后台结果回滚后最多再试 1 次，仍失败才收尾回 done。
# 不设这个上限会变成"异常 → 回滚 → 再异常 → 再回滚"的死循环。
MAX_BG_FOLLOWUP_RETRIES = 1

# deliver(kind, payload) -> 把信封投递回事件循环队列（线程安全，由 ws_bridge 提供）
Deliver = Callable[[str, dict], None]
# 会话结束时刷新会话列表的协程（由 ws_bridge 提供，依赖当前 ws 连接）
ReplySessions = Callable[[], Awaitable[None]]
# 读取某会话元数据（供模型绑定；由 ws_bridge 注入）
LoadMeta = Callable[[str], Optional[dict]]


def _bind_agent_env(load_meta: LoadMeta, sid: str, agent_or_factory,
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
    meta = load_meta(sid) or {}
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
    def __init__(self, sid: str, deliver: Deliver, reply_sessions: ReplySessions,
                 load_meta: LoadMeta, workspace=None):
        self.sid = sid
        self.agent: Optional[Agent] = None
        # 本会话所属工作空间的路径束（多工作空间，2026-09-18）。
        # None = default 空间（老调用方 / 复现脚本不传；行为与改造前一致，
        # 模块级 CHAT_HISTORY_DIR 仍作兜底，见 _bind_subagent_store）。
        self.workspace = workspace
        self.busy = False  # 本会话当前是否有一个 turn 在跑（拒绝同会话并发）
        self.stop_evt = threading.Event()
        self._pending_overrides: tuple = (None, None)  # (reasoning_effort, max_context)
        self._bg_watch_task: Optional[asyncio.Task] = None  # 后台任务完成守望
        self._deliver = deliver
        self._reply_sessions = reply_sessions
        self._load_meta: LoadMeta = load_meta
        self._bound_model: str | None = None  # 当前 agent 实际绑定的会话模型 id
        self._turn_started: float = 0.0  # 当前 turn 开始时间（耗时统计用）
        # 本轮 LLM 响应画像（仅在事件流过时累计，纯排障用，不改变任何行为）
        self._response_diag = self._new_response_diag()

    # ── 每个 LLM 响应的画像（排障用）───────────────────────────────
    @staticmethod
    def _new_response_diag() -> dict:
        return {"content": 0, "thinking": 0, "tools": []}

    def _note_response_event(self, ev: dict) -> None:
        """按事件流累计"主智能体本次 LLM 响应"的画像，并在 turn_end 打一行日志。

        为什么要有这行日志（2026-09-14 事故复盘）：模型偶尔会产出
        「正文承诺派发子智能体、但一个 tool_call 都没有」的**正常 stop** 响应，
        此时 agent_loop 判定"模型想停"直接收尾 —— 前端只看到一句
        "我派一个子智能体去读"然后就没有下文（用户描述为"直接中断、不往下执行"）。
        而当时的日志里只有 "turn 结束"，无法区分「模型没发工具调用」与
        「工具调用发了却丢了」，排查全靠反推 token 数。补上这行后一眼可判。

        只统计主智能体事件（带 subagent_id 的是子智能体内部事件，另有打点）。
        """
        t = ev.get("type")
        if ev.get("subagent_id"):
            return
        if t == "content_delta":
            self._response_diag["content"] += len(ev.get("text") or "")
        elif t == "thinking_delta":
            self._response_diag["thinking"] += len(ev.get("text") or "")
        elif t == "tool_call":
            self._response_diag["tools"].append(ev.get("tool_name") or "?")
        elif t == "turn_end":
            diag = self._response_diag
            finish = ev.get("finish_reason") or ""
            log.info("session_%s LLM 响应: finish=%s 工具调用=%s content=%d字 thinking=%d字 usage=%s",
                     self.sid, finish or "(空)", diag["tools"] or "无",
                     diag["content"], diag["thinking"], ev.get("usage") or {})
            if finish == "tool_calls" and not diag["tools"]:
                # 协议自相矛盾：声明"有工具调用"却一个都没聚合出来。
                log.error("session_%s 协议异常: finish_reason=tool_calls 但未收到任何 tool_call "
                          "事件（本轮工具调用丢失，表现为「说要干活却没有下文」）", self.sid)
            self._response_diag = self._new_response_diag()

    # ── 事件路由：把会话内事件打上 session_id ───────────────────
    def _bind_sink(self, agent: Agent) -> None:
        def send_func(line: str):
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                ev = {"type": "unknown", "text": line}
            ev["session_id"] = self.sid
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
            self._note_response_event(ev)
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

        目录取 `agent.workspace`（该会话所属工作空间的元数据目录），
        **不能**再用模块级 `CHAT_HISTORY_DIR`（那只代表 default 空间）。
        """
        chat_dir = self.workspace.chat_history_dir if self.workspace else CHAT_HISTORY_DIR
        store = SubagentStore(chat_dir)
        if agent.session_manager is None:
            agent.session_manager = SessionManager(
                chat_dir, agent.system_prompt.build_system_prompt(),
                session_prefix=agent.session_prefix, subagent_store=store,
                project_id=agent.workspace.id,
                tasks_dir=agent.workspace.tasks_dir,
            )
        else:
            agent.session_manager.subagent_store = store

    def _bind_task_board(self, agent: Agent) -> None:
        """把任务板快照接到本会话的前端通道（桌面端任务面板的实时数据源）。

        接线选在 TaskManager 层、而不是工具 handler 层：
        子智能体与队友复用**同一个** TaskManager 实例（`tools.handlers` 的闭包
        指向同一对象），所以"子智能体认领/完成任务 → 面板实时更新"不需要任何
        额外接线就自动成立。放到 handler 层就得为每个调用方各接一遍。

        `self._deliver` 是线程安全的（投递到事件循环队列），因此后台子智能体在
        daemon 线程里改任务也能安全推送。

        推送的是**整份快照**（幂等替换，不是增量）：面板 ≤20 行、体积可忽略，
        换来的是前端 store 无需增量合并状态机 —— 断线重连 / 会话切换 / 回放
        三条路径天然幂等。多线程乱序则由快照里的 revision 兜住。
        """
        def emit(payload: dict) -> None:
            try:
                self._deliver("task_board", {"session_id": self.sid, **payload})
            except Exception as e:
                # 推送失败绝不能影响任务本身（任务已落盘）
                log.error("session_%s 任务快照投递失败: %s: %s",
                          self.sid, type(e).__name__, e)

        agent.tools.task_manager.set_emitter(emit)

    def _push_status(self, status: str) -> None:
        """会话执行状态的唯一出口：关键节点打日志 + 广播到前端。
        running=turn 执行中；background=turn 结束但后台任务仍在跑；
        done/stopped=全部结束。排查"前端执行状态断了"先看这串日志。"""
        log.info("session_%s status -> %s", self.sid, status)
        self._deliver("session_status", {"session_id": self.sid, "status": status})

    def build_agent(self) -> Agent:
        """按需构造本会话的 Agent，并按其元数据记录的模型独立绑定（在工作线程里调用）。

        首次构造：在锁内换绑会话模型 env 后用工厂构造 Agent（首轮即按会话绑定模型）；
        之后：若会话记录的模型与已绑定的不同，则原地 reload_llm_bindings()。
        """
        if self.agent is None:
            agent, model_id = _bind_agent_env(
                self._load_meta, self.sid,
                # 每会话的 Agent 携带**该会话所属工作空间**的路径束：会话历史 /
                # 任务 / 记忆 / 沙箱根都随空间走（多工作空间改造的接线点）。
                lambda: Agent(silent=True, workspace=self.workspace), rebuild=False
            )
            self.agent = agent
            self._bound_model = model_id
            self._bind_sink(self.agent)
            # 必须在 switch_session 之前绑 store（后者会惰性构造 SessionManager）
            self._bind_subagent_store(self.agent)
            self.agent.switch_session(self.sid)
            # 任务板推送必须在 switch_session 之后：set_scope 在 switch_session 里完成，
            # 而 TaskManager 的 scope 决定快照读哪个会话的文件。
            self._bind_task_board(self.agent)
            log.info("session_%s agent 构建完成 (model=%s)",
                     self.sid, model_id or "global-default")
            return self.agent
        model_id = (self._load_meta(self.sid) or {}).get("model_id") or None
        if model_id != self._bound_model:
            log.info("session_%s 会话模型重绑: %s -> %s",
                     self.sid, self._bound_model or "global-default",
                     model_id or "global-default")
            _bind_agent_env(self._load_meta, self.sid, self.agent, rebuild=True)
            self._bound_model = model_id
        return self.agent

    def request_stop(self) -> None:
        """请求停止本会话当前 turn（线程安全）；不影响其它会话。"""
        self.stop_evt.set()
        if self.agent is not None:
            self.agent.request_stop()

    def record_model_switch(self, model_id: str | None) -> None:
        """把一次用户侧模型切换记录到本会话运行中的 agent（turn 收尾净变化展示）。

        仅当本会话确实在执行的 agent 存在时转发；空闲期（agent 未构造或
        turn 未运行）由 Agent.record_model_switch 内部忽略。
        """
        if self.agent is not None and model_id is not None:
            self.agent.record_model_switch(model_id)

    def current_status(self) -> Optional[str]:
        """新连接状态重放用（ws_bridge.handle）：
        running=turn 执行中；background=turn 已结束但后台任务仍在跑；None=空闲。"""
        if self.busy:
            return "running"
        if self.agent is not None and self.agent.background_manager.has_running():
            return "background"
        return None

    async def start_turn(self, text: str | list,
                         reasoning_effort: Optional[str] = None,
                         max_context: Optional[str] = None) -> None:
        """派发一轮对话：后台线程跑 run_turn，事件循环保持可读。

        `text` 为**用户消息的 content**：无附件时是纯字符串（与改造前一致），
        带附件时是 `[文本块 + 附件引用块...]` 的多模态数组（由 ws_bridge 组装）。
        本层只做透传，不解释内容 —— 展开成语义线格式发生在 Agent 的发送边界。

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
            log.info("session_%s bg watch cancelled (new turn)", self.sid)
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
            log.error("session_%s turn worker 异常: %s: %s",
                      self.sid, type(e).__name__, e)
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
            log.info("session_%s turn 结束 (%.1fs, %s, bg_running=%s)",
                     self.sid, elapsed, "stopped" if stopped else "done", bg_running)
            # 本轮结束：推送该会话最新的上下文统计（供前端圆圈指示器刷新）
            try:
                if self.agent is not None and self.agent.session_manager is not None:
                    self._deliver("context_stats", {
                        "session_id": self.sid,
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
        log.info("session_%s bg watch start", self.sid)
        followups = 0
        followup_failures = 0
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
                log.info("session_%s bg followup turn #%d", self.sid, followups)
                self.busy = True
                self._push_status("running")
                # 续轮前抓快照：本轮即将被消费的那批后台结果 id。
                # 续轮异常时按这批 id 精确回滚（见下方失败分支）。
                pending_ids = self.agent.background_manager.snapshot_completed_ids()
                failed = False
                try:
                    await asyncio.to_thread(self._run_followup_worker)
                except Exception as e:
                    failed = True
                    # exc_info=True：事故取证时"只有类型+消息、没有堆栈"会让定位
                    # 变慢（2026-09-18 事故复盘）；堆栈必须落盘。
                    log.error("session_%s bg followup 异常: %s: %s",
                              self.sid, type(e).__name__, e, exc_info=True)
                finally:
                    self.busy = False
                if failed:
                    # 关键：续轮在 agent_loop 起点已把本批结果标记为 notified
                    # （已消费）。若直接回循环顶，has_completed_pending() 恒为
                    # False → 会话被静默判 done：用户看到"会话结束了，但最终
                    # 总结没出来、任务板停在半路"（2026-09-18 事故现象）。
                    # 故把本批结果退回 pending，让守望再给一次机会；上限
                    # MAX_BG_FOLLOWUP_RETRIES 防死循环，仍失败才收尾。
                    restored = self.agent.background_manager.restore_completed(pending_ids)
                    followup_failures += 1
                    log.warning(
                        "session_%s bg followup 失败，回滚 %d/%d 条后台结果待重试"
                        "（第 %d 次失败，上限 %d）",
                        self.sid, restored, len(pending_ids), followup_failures,
                        MAX_BG_FOLLOWUP_RETRIES)
                    if restored == 0 or followup_failures > MAX_BG_FOLLOWUP_RETRIES:
                        break
                    continue
                # 续轮结束后：若用户在这期间点了停止，保留停止信号并退出循环；
                # 否则清掉信号进入下一轮判断（避免把停止误当正常信号吞掉）
                if self.stop_evt.is_set():
                    break
                self.stop_evt.clear()
            total = turn_elapsed + (time.monotonic() - started)
            log.info("session_%s bg watch done (%.1fs total)", self.sid, total)
            self._push_status("done")
            await self._reply_sessions()
        except asyncio.CancelledError:
            log.info("session_%s bg watch cancelled", self.sid)
            raise  # 新 turn 已开始，状态由 start_turn 接管

    def _resolve_turn_context(self, max_context: str | None) -> str | None:
        """本轮统计/压缩窗口：显式覆盖优先；缺省回落所选模型的标准窗口。

        后端兜底（修 1M 统计 bug）：此前 None 会回落全局 env MAX_CONTEXT_TOKENS
        （如 1M），与所选模型真实窗口（如 128k）不符。模型元数据也缺失时
        才维持 None（走全局默认）。
        """
        if max_context:
            return max_context
        if self._bound_model:
            return resolve_model_window(self._bound_model, extended=False)
        return None

    def _run_followup_worker(self) -> None:
        """后台完成后的自动续轮 worker（非用户输入，复用会话模型与参数覆盖）。"""
        agent = self.build_agent()
        reasoning_effort, max_context = self._pending_overrides
        agent.set_request_overrides(
            reasoning_effort=reasoning_effort, max_context=max_context
        )
        agent.set_turn_model_snapshot(self._bound_model)
        # 会话级上下文覆盖同步到压缩器阈值（缺省回落模型标准窗口）
        try:
            if agent.session_manager is not None:
                agent.session_manager.set_max_context(
                    self._resolve_turn_context(max_context))
        except Exception:
            pass
        agent.run_background_followup()

    def _run_turn_worker(self, text: str | list) -> None:
        agent = self.build_agent()
        reasoning_effort, max_context = self._pending_overrides
        agent.set_request_overrides(
            reasoning_effort=reasoning_effort, max_context=max_context
        )
        agent.set_turn_model_snapshot(self._bound_model)
        # 会话级上下文覆盖同步到压缩器阈值：显式覆盖优先，缺省回落所选模型的
        # 标准窗口（不再让 None 回落全局 env，避免统计/压缩用错窗口）
        try:
            if agent.session_manager is not None:
                agent.session_manager.set_max_context(
                    self._resolve_turn_context(max_context))
        except Exception:
            pass
        agent.run_turn(text)


class SessionRuntimeRegistry:
    """会话 id → SessionRuntime 的映射；共享 delivery 与会话列表刷新回调。"""

    def __init__(self, deliver: Deliver, reply_sessions: ReplySessions,
                 load_meta: LoadMeta):
        self._deliver = deliver
        self._reply_sessions = reply_sessions
        self._load_meta = load_meta
        self._sessions: Dict[str, SessionRuntime] = {}

    def get(self, sid: str) -> Optional[SessionRuntime]:
        return self._sessions.get(sid)

    def all_runtimes(self) -> list["SessionRuntime"]:
        """所有已注册的会话运行时（新连接状态重放用）。"""
        return list(self._sessions.values())

    def get_or_create(self, sid: str, workspace=None) -> SessionRuntime:
        """取（或创建）某会话的运行时。

        `workspace`：该会话所属工作空间的路径束。首次创建时传入并固化在本运行时上；
        已存在时**忽略**新值（一个会话的工作空间不可漂移 —— 否则同一个 sid 的
        事件前半段写 A 空间、后半段写 B 空间）。多工作空间下调用方必须先解析出
        该 sid 的归属再调用。
        """
        rt = self._sessions.get(sid)
        if rt is None:
            rt = SessionRuntime(sid, self._deliver, self._reply_sessions,
                                self._load_meta, workspace=workspace)
            self._sessions[sid] = rt
        return rt

    def is_busy(self, sid: str) -> bool:
        rt = self._sessions.get(sid)
        return bool(rt and rt.busy)

    def is_active(self, sid: str) -> bool:
        """会话是否有活动：turn 执行中，**或** turn 已结束但后台任务仍在跑。

        与 is_busy 的关键区别：后台子智能体执行期间 turn 早已结束（busy=False），
        但后台线程仍在写会话旁路文件、仍会追加主 jsonl 的 tool 结果/通知。
        只看 busy 会把这个窗口当成"空闲"→ 前端切会话时触发磁盘回放、
        load_session_history 的自愈可能**原子重写正在被写入的会话文件**，
        表现为"子智能体一跑，前后端状态就错位/丢消息，过一会儿又对上了"。
        （ws_bridge 里那段"运行中不回放"的注释说的就是这个 hazard，但守卫
        只覆盖了 busy，漏了 background —— 2026-09-14 补齐。）
        """
        rt = self._sessions.get(sid)
        if rt is None:
            return False
        if rt.busy:
            return True
        return rt.agent is not None and rt.agent.background_manager.has_running()

    def remove(self, sid: str) -> None:
        """会话被删除/回收后移除其运行时（运行中会被上层拒绝后才到达这里）。"""
        self._sessions.pop(sid, None)

    def reload_llm_bindings(self) -> None:
        """模型配置热切换后重绑所有已构造的运行时会话 Agent。

        每个会话按其各自元数据记录的模型重绑（无记录则落到新全局 env），
        维护每会话独立绑定语义；不会把所有会话统一串到同一个新全局模型。
        """
        for rt in self._sessions.values():
            if rt.agent is None:
                continue
            old_bound = rt._bound_model
            rt.agent, rt._bound_model = _bind_agent_env(
                self._load_meta, rt.sid, rt.agent, rebuild=True
            )
            # 绑定模型确实变化 → 记为一次用户侧模型切换（重绑可能来自全局/会话
            # 模型变更，主循环后续迭代即用新模型）。不再限定 busy：turn 执行中
            # 计入本轮，空闲期计入 pending（下一轮并入），两种都要有提示
            #（修复 2026-09-16：空闲切换无提示）。
            if old_bound != rt._bound_model:
                rt.record_model_switch(rt._bound_model)