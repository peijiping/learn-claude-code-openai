"""
ws_bridge.py - 桌面端桥层（新增，不修改 agent_full_v2.py）
把 Agent 变成 WS service：命令进、事件出（走 WSSink 的 JSON 行协议）。

协议见 docs/frontend/03-前后端通信协议.md。Electron 主进程拉起本脚本，
连 ws://127.0.0.1:<AGENT_WS_PORT>（默认 8765）。

与后端铁律一致：只新增这一个薄层，agent_full_v2.py 及以下零改动。
"""
import asyncio
import json
import os
import re
import threading
import time
from typing import Optional

import websockets
from openai import OpenAI

from agent_full_v2 import Agent
from config import load as load_config
from llm_config import fetch_remote_models, get_config, load_llm_config, save_config
from logger import get_logger, install_excepthooks
from paths import CHAT_HISTORY_DIR
from session_manage import SessionManager
from session_runtime import SessionRuntimeRegistry
from subagent_store import SubagentStore

# 启动即自举配置（Electron spawn 的 cwd 为仓库根，config.py 按 cwd 解析项目级配置）
load_config()
# 存在 llmconfig.json 则加载大模型配置映射进 env（文件缺失时不影响启动）
load_llm_config()

# 统一日志（~/.aigent/logs/agent_日期.log）
log = get_logger("ws_bridge")

PORT = int(os.environ.get("AGENT_WS_PORT", "8765"))

# 全局 Agent 仅用于：大模型配置热切换（reload_llm_bindings）、会话标题生成、
# 以及 goal/tasks/skills 等查询；"跑对话"不再走它——并发会话各自持有一个
# 独立 Agent（见 session_runtime.SessionRuntime），事件按 session_num 路由。
# 惰性会话：启动不建会话（避免每次打开窗口都多一个空 jsonl），
# 会话号由 ws_bridge 在事件循环内确定性分配。
agent = Agent(silent=True)


def _envelope(kind: str, payload: dict) -> str:
    return json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False)


# ── 连接无关的广播层 ──────────────────────────────────────────────
# 会话事件与会话列表广播到所有活跃连接：连接断开/重连不影响运行中的会话。
# （历史 bug：registry 与 deliver 绑死在单个连接上，前端断线重连后，
#   仍在执行的后台子智能体事件持续流向已被弃用的旧连接 → 前端永久停滞。）

class ConnectionHub:
    """活跃 WS 连接注册表 + 事件广播。

    - register/unregister 由 handle() 在连接建立/退出时调用（事件循环内）；
    - broadcast 把信封放入每个活跃连接的发送队列（事件循环内执行；
      工作线程经 deliver → call_soon_threadsafe 调度进来）；
    - 每个连接各自的 writer 协程负责 flush，单个连接发送异常只注销自己，
      不影响其它连接与运行中的会话。
    """

    def __init__(self):
        self._conns: list = []  # [(ws, line_q), ...]

    def register(self, ws) -> asyncio.Queue:
        line_q: asyncio.Queue = asyncio.Queue()
        self._conns.append((ws, line_q))
        return line_q

    def unregister(self, ws) -> None:
        self._conns = [(w, q) for (w, q) in self._conns if w is not ws]

    def broadcast(self, kind: str, payload: dict) -> None:
        line = _envelope(kind, payload)
        for _ws, q in list(self._conns):
            q.put_nowait(line)


hub = ConnectionHub()
# 事件循环句柄：deliver 从任意工作线程（run_turn / 后台子智能体 / 标题线程）
# 调度广播回事件循环；main() 启动时捕获。
_loop: Optional[asyncio.AbstractEventLoop] = None
# 全局唯一会话运行时注册表（所有连接共享；main() 里构建）
registry: Optional["SessionRuntimeRegistry"] = None


def deliver(kind: str, payload: dict) -> None:
    """线程安全的事件投递入口：广播到所有活跃连接。"""
    if _loop is not None:
        _loop.call_soon_threadsafe(hub.broadcast, kind, payload)


async def safe_send(ws, line: str) -> None:
    """请求-响应型回包：直接发给请求连接；异常只记日志不上抛。"""
    try:
        await ws.send(line)
    except Exception as e:
        log.warning("回包发送失败: %s: %s", type(e).__name__, e)


async def reply_sessions() -> None:
    """会话列表广播到所有活跃连接（全局 UI 状态，与连接解耦）。"""
    sm = await asyncio.to_thread(_ensure_session_manager)
    items = await asyncio.to_thread(sm.list_sessions)
    sessions = [_session_meta(i) for i in items]
    hub.broadcast("sessions", {"sessions": sessions})


def _session_meta(item: dict) -> dict:
    """session_manager.list_sessions() 的条目已是元数据 dict，直接透传。"""
    return dict(item)


# ── 会话标题生成（独立 daemon 线程，先于主对话请求发出） ──────────────

TITLE_SYSTEM_PROMPT = (
    "你是会话标题生成器。根据用户的首条消息生成一个不超过16个字的简短标题，"
    "概括用户意图。直接输出标题文本：不要引号、不要句号、不要任何解释。"
)

# 标题请求专用短超时：独立小客户端，不与主对话共用连接池/超时/重试策略。
# 若服务端串行排队，标题请求也要在 TITLE_TIMEOUT 秒内出结果或降级兜底，
# 绝不悬挂到主 turn 结束（主客户端 timeout=1200s + 3 次重试，绝不复用）。
TITLE_TIMEOUT = 30
# max_tokens 必须给足：推理模型（如 deepseek-v4-flash）的思考过程也计入
# completion 预算，预算太小会被 reasoning_tokens 吃光导致 content 为空/
# 只挤出单字。1000 对"思考 + 16 字标题"足够，成本可忽略。
TITLE_MAX_TOKENS = 1000

# 同一会话的标题线程去重（clear 后重发首条消息等场景），防止并发重复写索引
_title_threads_lock = threading.Lock()
_title_inflight: set = set()


def _generate_session_title(first_user_text: str) -> Optional[str]:
    """用独立短超时客户端发一次极小的非流式请求生成标题；任何异常返回 None（调用方降级）。"""
    text = (first_user_text or "").strip()
    if not text:
        return None
    try:
        # 独立客户端：只复用主客户端的鉴权/地址配置，连接池、超时、重试全部独立
        client = OpenAI(
            api_key=agent.llm_client.api_key,
            base_url=str(agent.llm_client.base_url),
            timeout=TITLE_TIMEOUT,
            max_retries=0,
        )
        resp = client.chat.completions.create(
            model=agent.model,
            messages=[
                {"role": "system", "content": TITLE_SYSTEM_PROMPT},
                {"role": "user", "content": text[:2000]},
            ],
            max_tokens=TITLE_MAX_TOKENS,
            temperature=0.3,
        )
        title = (resp.choices[0].message.content or "").strip().strip('"“”').strip()
        title = re.sub(r"\s+", " ", title)
        title = title.rstrip("。，,．.！!？?；;：:、")
        # 合法性校验：单字/空串拒绝（如推理模型预算被吃光只挤出"写"），走降级兜底
        if len(title) < 2:
            return None
        return title[:24]
    except Exception:
        return None


def _fallback_title(first_user_text: str) -> Optional[str]:
    """标题请求失败/不合法时的兜底：按标点切分取首个语义片段。

    例："帮我写一个简单的python程序，越简单越好…" → "帮我写一个简单的python程序"，
    而不是盲目截断 20 字（可能在词中间断开或只剩半句话）。
    """
    text = re.sub(r"\s+", " ", (first_user_text or "").strip())
    if not text:
        return None
    first_clause = re.split(r"[，,。．.！!？?；;：:、\n]", text, maxsplit=1)[0].strip()
    if len(first_clause) < 4:  # 首个标点出现太早，整句兜底
        first_clause = text
    return first_clause[:16] or None


def _title_worker(loop, session_num: int, first_user_text: str) -> None:
    """标题线程主体：生成 → 写索引 → 回发会话列表。任何异常静默吞掉，绝不影响主对话。"""
    try:
        title = _generate_session_title(first_user_text)
        source = "auto"
        if not title:
            title = _fallback_title(first_user_text)
            source = "trunc"
        if title:
            sm = _ensure_session_manager()
            sm.set_auto_title(session_num, title, source)
            # 从工作线程安全地把"刷新会话列表"调度回事件循环（广播到所有活跃连接）
            asyncio.run_coroutine_threadsafe(reply_sessions(), loop)
    except Exception:
        pass
    finally:
        with _title_threads_lock:
            _title_inflight.discard(session_num)


def _start_title_thread(session_num: int, first_user_text: str) -> None:
    """收到首条消息立即启动独立 daemon 标题线程。

    必须在 run_turn 线程提交之前调用：标题请求先于主对话请求到达服务端，
    即使服务端串行排队（本地模型/单并发代理），标题也能最先被处理。
    线程完全独立于会话的 run_turn/agent_loop 生命周期，turn 中途完成即回发。
    """
    loop = asyncio.get_running_loop()
    with _title_threads_lock:
        if session_num in _title_inflight:
            return  # 同会话已有标题线程在跑，跳过
        _title_inflight.add(session_num)
    threading.Thread(
        target=_title_worker,
        args=(loop, session_num, first_user_text),
        name=f"session-title-{session_num}",
        daemon=True,
    ).start()


def _has_real_user_turn(messages: list) -> bool:
    """历史中是否已有真实用户消息（排除 <system-reminder> 系统注入）。"""
    for m in messages:
        if m.get("role") != "user":
            continue
        if _text_of(m.get("content")).startswith("<system-reminder>"):
            continue
        return True
    return False


def _ensure_session_manager():
    """惰性会话下 session_manager 可能为 None（尚未 init/switch），
    列会话等只读操作前先兜底构建（构建后 init_session 也会复用）。

    注入 SubagentStore：子智能体执行过程写到 `session_N.subagents.jsonl`，
    主会话文件只保留标准消息（并在首次加载时把历史遗留的 in-file 行迁出）。
    """
    if agent.session_manager is None:
        agent.session_manager = SessionManager(
            CHAT_HISTORY_DIR, agent.system_prompt.build_system_prompt(),
            session_prefix=agent.session_prefix,
            subagent_store=SubagentStore(CHAT_HISTORY_DIR),
        )
    return agent.session_manager


def _text_of(content) -> str:
    """历史消息 content 兼容转换：str 直接返回，list（多模态 blocks）拼接 text。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


def _status_snapshot_lines() -> list[str]:
    """所有仍在运行（running/background）会话的 session_status 信封列表。
    连接建立重放与 status_query 命令共用，保证两处行为一致。"""
    lines = []
    for rt in registry.all_runtimes():
        status = rt.current_status()
        if status is not None:
            lines.append(_envelope(
                "session_status", {"num": rt.num, "status": status}))
    return lines


def _attach_subagent(ui: list[dict], rec: dict) -> None:
    """把一条子智能体记录挂到「发起它的那条 assistant 消息」下（唯一锚点规则）。

    优先按 `tool_call_id`（= 发起 sub_agent 的主工具调用 id）定位；找不到时
    回退到最近一条 assistant 消息（compaction 后原归属若被摘要替代，会降级
    挂到摘要消息，属可接受行为）；再找不到则跳过。

    实时挂载（前端 subCallAnchors）与回放挂载共用同一规则，保证切换会话
    前后卡片位置不跳变。
    """
    tcid = rec.get("tool_call_id", "") or ""
    target = None
    if tcid:
        for ui_msg in reversed(ui):
            if (ui_msg.get("role") == "assistant"
                    and tcid in (ui_msg.get("_tc_ids") or [])):
                target = ui_msg
                break
    if target is None:
        for ui_msg in reversed(ui):
            if ui_msg.get("role") == "assistant":
                target = ui_msg
                break
    if target is None:
        return
    target.setdefault("subagents", []).append({
        "id": rec.get("subagent_id", ""),
        "name": rec.get("name", ""),
        "thinking": rec.get("thinking", ""),
        "toolCalls": rec.get("toolCalls", []),
        "status": rec.get("status", "done"),
        "durationMs": rec.get("duration_ms"),
        "error": rec.get("error", ""),
    })


def _history_to_ui(messages: list, subagent_records: list | None = None) -> list[dict]:
    """session 历史 → 前端可渲染消息列表。

    - 跳过 system / tool 消息（前者无展示价值，后者已聚合进 assistant 工具条）
    - 跳过系统注入的 user 消息（<system-reminder> 开头的 todo reminder 等）
    - assistant 保留 reasoning_content → thinking、tool_calls → 工具条
    - 子智能体执行过程：主源为**旁路记录**（`session_N.subagents.jsonl`，
      经 subagent_records 传入）；messages 里若仍残留 `role=subagent` 行
      （尚未迁移的旧数据）一并挂载，按 subagent_id 去重、旁路记录优先。
    """
    ui: list[dict] = []
    legacy_rows: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            content = _text_of(m.get("content"))
            if content.startswith("<system-reminder>"):
                continue
            ui.append({"role": "user", "content": content})
        elif role == "assistant":
            tool_calls = []
            tc_ids: list[str] = []
            for tc in (m.get("tool_calls") or []):
                tc_ids.append(tc.get("id", "") if isinstance(tc, dict) else "")
                tool_calls.append({
                    "name": (tc.get("function") or {}).get("name", ""),
                    "args": (tc.get("function") or {}).get("arguments", ""),
                })
            ui.append({
                "role": "assistant",
                "content": _text_of(m.get("content")),
                "thinking": m.get("reasoning_content") or "",
                "_tc_ids": tc_ids,
                "toolCalls": tool_calls,
            })
        elif role == "subagent":
            # 旧数据残留的 in-file 记录行（正常已由迁移搬到旁路文件）
            legacy_rows.append(m)

    records: list[dict] = list(subagent_records or [])
    seen = {r.get("subagent_id") for r in records}
    for row in legacy_rows:
        if row.get("subagent_id") not in seen:
            records.append(row)
            seen.add(row.get("subagent_id"))
    for rec in records:
        _attach_subagent(ui, rec)
    return ui


async def handle(ws):
    line_q = hub.register(ws)
    log.info("WS 连接建立: %s (active=%d)", ws.remote_address, len(hub._conns))
    # 状态重放：新连接（含断线重连）立即得知仍在运行的会话，
    # 前端据此恢复运行指示（转圈/后台脉冲点）。
    # 渲染进程刷新（HMR/Cmd+R）不重建此连接，那种场景由前端主动发
    # status_query 命令拉取（走同一快照函数）。
    replay = _status_snapshot_lines()
    if replay:
        log.info("WS 状态重放: %d 个运行中会话", len(replay))
    for line in replay:
        line_q.put_nowait(line)

    async def writer():
        # 每连接一个 writer：发送异常只注销本连接并退出，
        # 绝不静默死亡（历史 bug：异常杀死事件管道后 TCP/心跳仍正常，
        # 表现为"连接活着但事件断流"，且无任何日志可查）。
        while True:
            line = await line_q.get()
            try:
                await ws.send(line)
            except Exception as e:
                log.error("WS writer 异常退出 (%s): %s: %s",
                          ws.remote_address, type(e).__name__, e)
                hub.unregister(ws)
                return

    writer_task = asyncio.create_task(writer())
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await safe_send(ws, _envelope("error", {"msg": f"bad json: {raw}"}))
                continue
            kind = msg.get("kind")
            payload = msg.get("payload") or {}

            if kind == "chat":
                # 并发会话：每个会话由注册表里的 SessionRuntime 独立跑 run_turn，
                # 事件按 session_num 路由到前端对应缓冲。本循环不做 await run_turn，
                # 派发后立即继续读命令 → 任意会话可后台执行、切换不断流。
                sm = _ensure_session_manager()
                num = payload.get("num")
                text = payload.get("text", "")
                # 全新会话（前端无激活会话 / 未带 num）：事件循环内确定性领号 +
                # 写入初始 system 消息（create_new_session 读文件取 max，必须在这个
                # 单线程事件循环里执行，避免并发线程 race 到同一编号）。
                if num is None:
                    new_num, new_file = sm.create_new_session()
                    for m in sm._build_initial_messages():
                        sm.append_message_to_session(new_file, m)
                    num = new_num
                    log.info("新会话创建: session_%d (model=%s)",
                             num, payload.get("model_id") or "global-default")
                    await safe_send(ws, _envelope("session", {"num": num, "message_count": 0}))
                    # 新建会话首批：把前端选择的模型持久化进该会话元数据。
                    #（参数覆盖由前端在收到 session 信封后按 UI 形状 map 写入，此处只记模型；
                    #  chat 透传的 overrides 是已换算的单轮 resolved 形状，不宜直接落元数据。）
                    await asyncio.to_thread(
                        sm.set_session_model, new_num,
                        model_id=payload.get("model_id"),
                    )
                    await reply_sessions()
                rt = registry.get_or_create(num)
                if rt.busy:
                    # 同会话并发 turn 拒绝：避免两线程同时写同一会话 jsonl
                    await safe_send(ws, _envelope("error", {
                        "msg": f"该会话 (session_{num}) 正在执行，请先用停止按钮结束后再发送",
                    }))
                    continue
                # 标题：全新会话，或该会话此前从无真实 user 消息（旧会话首轮）
                history = await asyncio.to_thread(
                    sm.load_session_history, sm.get_session_file(num)
                )
                if not _has_real_user_turn(history):
                    _start_title_thread(num, text)
                # 会话级请求覆盖：思考强度 / 更大上下文（本轮生效，内存态，不写配置）。
                # 前端下拉悬浮面板改动后随 chat 命令带上来。
                ov = payload.get("overrides") or {}
                reasoning_effort = ov.get("thinking_strength") or None
                max_context_raw = ov.get("max_context") or None
                # 前端发送的是叠加态（standard/extended 二选一），这里已由前端换算成
                # 具体窗口字符串；若前端仅传开关位则回落到 None（走全局）。跳过空串。
                max_context = str(max_context_raw) if max_context_raw else None
                # 后台线程跑 turn；事件循环继续处理其它命令（切换 / 其它会话 / stop）
                log.info("chat 派发: session_%s text=%r", num, text[:80])
                asyncio.create_task(
                    rt.start_turn(text, reasoning_effort=reasoning_effort,
                                  max_context=max_context)
                )

            elif kind == "stop":
                # 仅停止当前显示会话正在执行的那一轮，其它会话不受影响
                num = int(payload.get("num", 0))
                log.info("停止请求: session_%d", num)
                rt = registry.get(num)
                if rt is not None:
                    rt.request_stop()

            elif kind == "status_query":
                # 前端主动拉取运行状态（渲染进程刷新/HMR 不重建 WS 连接，
                # 连接建立时的重放覆盖不到该场景）。回包走本连接的 writer
                # 队列，与其它事件同管道保序；无运行会话时回空（前端自然复位）。
                lines = _status_snapshot_lines()
                log.info("status_query: %d 个运行中会话", len(lines))
                for line in lines:
                    line_q.put_nowait(line)

            elif kind == "session_switch":
                sm = _ensure_session_manager()
                num = int(payload.get("num", 0))
                log.info("会话切换请求: session_%d", num)
                # 运行中的会话不读磁盘回放：turn 在途时 jsonl 可能处于
                # "assistant(tool_calls) 已落盘、tool 响应未落盘" 的中间态，
                # load_session_history 的孤儿清理会把它当坏数据重写文件，
                # 截断在途消息。前端对运行中会话本就以实时缓冲为准
                # （session_history 的 hasLive 守卫），此处回放空消息即可。
                if registry.is_busy(num):
                    # 消息回放跳过（以实时缓冲为准），但模型与参数仍按元数据恢复，
                    # 保证切到运行中会话时其参数覆盖也能正确加载。
                    meta = (await asyncio.to_thread(sm.load_meta, num)) or {}
                    await safe_send(ws, _envelope("session_history", {
                        "num": num, "messages": [],
                        "model_id": meta.get("model_id"),
                        "overrides": meta.get("overrides") or {},
                    }))
                    await reply_sessions()
                    continue
                try:
                    _, sess_file, history = await asyncio.to_thread(sm.switch_session, num)
                except FileNotFoundError:
                    await safe_send(ws, _envelope("error", {"msg": f"session {num} not found"}))
                else:
                    # 切换只是"按号读取该会话历史回放"（并刷新列表），
                    # 不改变任何运行中会话的执行状态 → 切换不断流。
                    # 顺带读取该会话记录的模型与参数，供前端按元数据恢复选中。
                    meta = (await asyncio.to_thread(sm.load_meta, num)) or {}
                    # 子智能体执行过程来自旁路文件（与主 jsonl 物理隔离），
                    # 按 tool_call_id 挂到发起它的 assistant 消息下（与实时一致）
                    records = await asyncio.to_thread(
                        sm.load_subagent_records, sess_file)
                    await safe_send(ws, _envelope("session_history", {
                        "num": num,
                        "messages": _history_to_ui(history, records),
                        "model_id": meta.get("model_id"),
                        "overrides": meta.get("overrides") or {},
                    }))
                    # 切换会话后推送该会话的上下文统计（供前端圆圈指示器按会话展示）
                    try:
                        stats = await asyncio.to_thread(sm.context_stats_dict, history)
                        await safe_send(ws, _envelope("context_stats", {"num": num, **stats}))
                    except Exception:
                        pass
                    await reply_sessions()

            elif kind == "session_model":
                # 记录会话最后选择的模型 + 参数到会话元数据（会话级独立绑定）；
                # 无 num（新建任务预设态）由 chat 首条统一持久化，此处仅处理已建会话。
                sm = _ensure_session_manager()
                num = int(payload.get("num", 0) or 0)
                if num <= 0:
                    continue
                if not sm.get_session_file(num).exists():
                    await safe_send(ws, _envelope("error", {"msg": f"session {num} not found"}))
                    continue
                model_id = payload.get("model_id")
                overrides = payload.get("overrides") or None
                await asyncio.to_thread(
                    sm.set_session_model, num,
                    model_id=model_id if model_id is not None else None,
                    overrides=overrides if isinstance(overrides, dict) else None,
                )
                await safe_send(ws, _envelope("session_model", {
                    "num": num, "model_id": model_id, "overrides": overrides,
                }))
                await reply_sessions()

            elif kind == "session_clear":
                sm = _ensure_session_manager()
                num = int(payload.get("num", 0) or 0)
                if num <= 0:
                    await safe_send(ws, _envelope("error", {"msg": "当前无激活会话"}))
                    continue
                if registry.is_busy(num):
                    await safe_send(ws, _envelope("error", {
                        "msg": f"该会话 (session_{num}) 正在执行，暂不能清空",
                    }))
                    continue
                if not sm.get_session_file(num).exists():
                    await safe_send(ws, _envelope("error", {"msg": f"session {num} not found"}))
                    continue
                await asyncio.to_thread(sm.clear_session, sm.get_session_file(num))
                log.info("会话清空: session_%d", num)
                await safe_send(ws, _envelope("session", {"num": num, "message_count": 0}))
                await reply_sessions()

            elif kind == "sessions_list":
                await reply_sessions()

            elif kind == "session_rename":
                sm = _ensure_session_manager()
                try:
                    await asyncio.to_thread(
                        sm.rename_session,
                        int(payload.get("num", 0)), str(payload.get("title", "")),
                    )
                except FileNotFoundError:
                    await safe_send(ws, _envelope("error", {"msg": f"session {payload.get('num')} not found"}))
                except ValueError as exc:
                    await safe_send(ws, _envelope("error", {"msg": f"重命名失败：{exc}"}))
                else:
                    log.info("会话重命名: session_%s -> %r",
                             payload.get("num"), payload.get("title"))
                    await reply_sessions()

            elif kind == "session_trash":
                sm = _ensure_session_manager()
                num = int(payload.get("num", 0))
                # 运行中的会话禁止进回收站（后台还在写文件 / 流式输出）
                if registry.is_busy(num):
                    await safe_send(ws, _envelope("error", {
                        "msg": f"该会话 (session_{num}) 正在执行，请先停止后再删除",
                    }))
                    continue
                try:
                    await asyncio.to_thread(sm.trash_session, num)
                except (FileNotFoundError, ValueError):
                    await safe_send(ws, _envelope("error", {"msg": f"session {num} not found"}))
                else:
                    log.info("会话进回收站: session_%d", num)
                    registry.remove(num)
                    await reply_sessions()

            elif kind == "session_restore":
                sm = _ensure_session_manager()
                try:
                    await asyncio.to_thread(sm.restore_session, int(payload.get("num", 0)))
                except (FileNotFoundError, ValueError):
                    await safe_send(ws, _envelope("error", {"msg": f"session {payload.get('num')} not found"}))
                else:
                    log.info("会话从回收站还原: session_%s", payload.get("num"))
                    await reply_sessions()

            elif kind == "session_delete":
                # 批量永久删除：逐条执行，单条失败不断整批；
                # 运行中的会话拒绝删除（后台还在写文件）
                sm = _ensure_session_manager()
                nums = payload.get("nums") or []
                deleted, failed = [], []
                for n in nums:
                    try:
                        num = int(n)
                    except (TypeError, ValueError):
                        continue
                    if registry.is_busy(num):
                        failed.append(num)
                        continue
                    try:
                        ok = await asyncio.to_thread(sm.delete_session_permanent, num)
                    except (TypeError, ValueError):
                        continue
                    if ok:
                        deleted.append(num)
                        registry.remove(num)
                    else:
                        failed.append(num)
                # 结果回发后不再全量广播 sessions：前端以 deleted[] 本地增量移除，
                # 避免删除完成后重建整个会话列表（逐个重数 message_count）造成的刷新延迟。
                log.info("会话批量永久删除: deleted=%s failed=%s", deleted, failed)
                await safe_send(ws, _envelope("session_delete_result", {
                    "deleted": deleted, "failed": failed,
                }))

            elif kind == "trash_list":
                sm = _ensure_session_manager()
                items = await asyncio.to_thread(sm.list_sessions, "trashed")
                await safe_send(ws, _envelope("sessions_trashed", {
                    "sessions": [_session_meta(i) for i in items],
                }))

            elif kind == "goal_status":
                text = await asyncio.to_thread(agent.goal_status)
                await safe_send(ws, _envelope("goal_status", {"text": text}))

            elif kind == "tasks":
                # 惰性会话下可能尚未绑定 todo manager，无激活会话时给占位文本
                if agent.session_num is None:
                    await safe_send(ws, _envelope("tasks", {"text": "(当前会话暂无待办)"}))
                    continue
                text = await asyncio.to_thread(agent.show_tasks)
                if not text.strip():
                    text = "(当前会话暂无待办)"
                await safe_send(ws, _envelope("tasks", {"text": text}))

            elif kind == "skills":
                text = await asyncio.to_thread(agent.skills.list_skills)
                await safe_send(ws, _envelope("skills", {"text": text}))

            elif kind == "llm_config_get":
                await safe_send(ws, _envelope("llm_config", {"config": get_config()}))

            elif kind == "llm_config_save":
                config = payload.get("config") or {}
                old_primary = os.environ.get("OPENAI_MODEL_ID", "")
                try:
                    saved = await asyncio.to_thread(save_config, config)
                    # 重新映射进 env 并就地热切换 LLM 绑定，立即生效（无需重启）
                    await asyncio.to_thread(load_llm_config)
                    result = await asyncio.to_thread(agent.reload_llm_bindings)
                except ValueError as exc:
                    log.warning("模型配置保存失败: %s", exc)
                    await safe_send(ws, _envelope("error", {"msg": f"保存失败：{exc}"}))
                else:
                    # 同步所有已构造的运行时会话 Agent 的新绑定（并发后台会话也立即生效）
                    await asyncio.to_thread(registry.reload_llm_bindings)
                    ok = result.get("applied", False)
                    if ok:
                        log.info("模型配置切换生效: %s -> %s", old_primary or "(none)",
                                 result.get("primary"))
                    else:
                        log.warning("模型配置保存但未生效: %s", result.get("reason", ""))
                    await safe_send(ws, _envelope("llm_config", {
                        "config": get_config(),
                        "applied": ok,
                        "msg": (f"模型配置已生效（{result.get('primary')}）"
                                if ok else result.get("reason", "未生效")),
                    }))

            elif kind == "llm_models_fetch":
                # 「刷新」按钮：调 GET {base_url}/models 拉取该连接可用的模型 id 列表。
                # api_key 留空时回退用已保存连接（connection_id）的密钥。
                try:
                    res = await asyncio.to_thread(
                        fetch_remote_models,
                        str(payload.get("base_url") or ""),
                        str(payload.get("api_key") or ""),
                        str(payload.get("connection_id") or ""),
                        str(payload.get("models_path") or "/models"),
                    )
                except Exception as exc:  # noqa: BLE001 - 统一回前端展示
                    res = {"ok": False, "models": [], "error": f"{type(exc).__name__}: {exc}"}
                await safe_send(ws, _envelope("llm_models", {
                    "ok": bool(res.get("ok")),
                    "models": res.get("models", []),
                    "base_url": res.get("base_url", ""),
                    "error": res.get("error", ""),
                }))

            else:
                await safe_send(ws, _envelope("error", {"msg": f"unknown kind: {kind}"}))
    finally:
        hub.unregister(ws)
        writer_task.cancel()
        code = getattr(ws, "close_code", None)
        log.info("WS 连接断开: %s (code=%s, active=%d)",
                 ws.remote_address, code, len(hub._conns))


def _watch_parent():
    """孤儿看护：父进程（Electron）意外死亡（如被强杀，来不及走 before-quit 清理）
    时本进程会被收养到 ppid=1，此时自动退出，避免残留进程占住 WS 端口
    导致下次 dev 启动 bind 失败（errno 48）。"""
    while True:
        time.sleep(1)
        if os.getppid() == 1:
            log.warning("父进程(Electron)已退出，ws_bridge 随退避免残留占用端口")
            os._exit(0)


async def main():
    global _loop, registry
    _loop = asyncio.get_running_loop()
    # 全局唯一会话运行时注册表：所有连接共享。连接可断可换，
    # 运行中的会话事件始终经 hub 广播到当前活跃连接（见 ConnectionHub）。
    def _load_meta(num: int):
        return _ensure_session_manager().load_meta(num)
    registry = SessionRuntimeRegistry(deliver, reply_sessions, _load_meta)
    async with websockets.serve(handle, "127.0.0.1", PORT):
        log.info("ws_bridge 后端启动: WS server listening on 127.0.0.1:%d "
                 "(pid=%s, model=%s)", PORT, os.getpid(),
                 os.environ.get("OPENAI_MODEL_ID", "(none)"))
        await asyncio.Future()


if __name__ == "__main__":
    install_excepthooks()
    threading.Thread(target=_watch_parent, daemon=True).start()
    asyncio.run(main())