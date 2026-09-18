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
from llm_config import (
    fetch_remote_models, get_config, load_llm_config, resolve_model_window,
    save_config,
)
from logger import get_logger, install_excepthooks
from paths import CHAT_HISTORY_DIR, DEFAULT_PROJECT_ID, WorkspacePaths
from project_registry import WorkspaceError, get_registry
from session_manage import SessionManager, set_session_id_guard
from session_runtime import SessionRuntimeRegistry
from subagent_store import SubagentStore
from task_manager import current_board

# 启动即自举配置（Electron spawn 的 cwd 为仓库根，config.py 按 cwd 解析项目级配置）
load_config()
# 存在 llmconfig.json 则加载大模型配置映射进 env（文件缺失时不影响启动）
load_llm_config()
# 工作空间索引自举：projects.json 缺失/损坏时重建为只含 default 的索引
# （老用户升级路径：只有 default 一个空间，行为与升级前完全一致）。
try:
    get_registry().ensure()
except Exception as _e:  # noqa: BLE001 - 索引坏了也不能拦启动（get_registry 内部已自愈）
    print(f"[projects] projects.json 自举失败：{_e}")

# 统一日志（~/.aigent/logs/agent_日期.log）
log = get_logger("ws_bridge")

# 联调调试钩子：Electron 主进程在 launch.json 里通过 PYTHON_DEBUG_PORT 把这个
# 变量随 spawn 透传给本进程（PythonManager 复制 process.env），据此决定是否
# 起 debugpy，不设环境变量时零开销，完全不影响正常 `npm run dev`。
# PYTHON_DEBUG_WAIT=1 时后端会一直等到调试器 attach 才继续，保证可从启动点断点。
DEBUG_PORT = os.environ.get("PYTHON_DEBUG_PORT")
if DEBUG_PORT:
    try:
        import debugpy
        debugpy.listen(("127.0.0.1", int(DEBUG_PORT)))
        if os.environ.get("PYTHON_DEBUG_WAIT") == "1":
            debugpy.wait_for_client()
        log.info("联调钩子: debugpy 监听 127.0.0.1:%s", DEBUG_PORT)
    except Exception as e:  # debugpy 缺失等，仅警告不阻断启动
        log.warning("联调钩子: debugpy 未就绪, 本次不联调: %s", e)

PORT = int(os.environ.get("AGENT_WS_PORT", "8765"))

# 全局 Agent 仅用于：大模型配置热切换（reload_llm_bindings）、会话标题生成、
# 以及 goal/tasks/skills 等查询；"跑对话"不再走它——并发会话各自持有一个
# 独立 Agent（见 session_runtime.SessionRuntime），事件按 session_id 路由。
# 惰性会话：启动不建会话（避免每次打开窗口都多一个空 jsonl），
# 会话 id 由 ws_bridge 在事件循环内确定性分配（随机短 id + 查重）。
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
# 事件循环句柄：deliver 从任意工作线程（run_turn / 后台子智能体）
# 调度广播回事件循环；main() 启动时捕获。
_loop: Optional[asyncio.AbstractEventLoop] = None
# 全局唯一会话运行时注册表（所有连接共享；main() 里构建）
registry: Optional["SessionRuntimeRegistry"] = None


# 会话列表刷新去重：usage_stats（携带 turn）与标题精炼等可能同时触发 reply_sessions，
# 同一事件循环里只排一次队（全量重建列表，幂等）。
_sessions_refresh_pending = False

# ── 工作空间路由缓存（多工作空间，2026-09-18）──────────────────────────
# 一个后端进程承载全部工作空间：会话 → 空间 → 路径束 / SessionManager。
_MANAGER_CACHE: dict[str, SessionManager] = {}  # project_id → SessionManager
_SID_PROJECT: dict[str, str] = {}               # session_id → project_id


async def _refresh_sessions_once() -> None:
    global _sessions_refresh_pending
    _sessions_refresh_pending = False
    await reply_sessions()


def deliver(kind: str, payload: dict) -> None:
    """线程安全的事件投递入口：广播到所有活跃连接。"""
    if _loop is not None:
        _loop.call_soon_threadsafe(hub.broadcast, kind, payload)
        # 轮级 usage 定稿（每轮一次）后同步刷新会话列表：add_usage_totals 刚把
        # 本轮用量写进会话元数据，悬停信息卡（SessionTooltip）读 sessions 载荷的
        # usage_totals，若不刷新会停留在创建/上次刷新时的旧快照（显示 —）。
        # 迟到子智能体补发的 usage_stats（仅 session、无 turn）不触发。
        if (
            kind == "event"
            and isinstance(payload, dict)
            and payload.get("type") == "usage_stats"
            and isinstance(payload.get("usage"), dict)
            and bool(payload["usage"].get("turn"))
        ):
            global _sessions_refresh_pending
            if not _sessions_refresh_pending:
                _sessions_refresh_pending = True
                asyncio.run_coroutine_threadsafe(_refresh_sessions_once(), _loop)


async def safe_send(ws, line: str) -> None:
    """请求-响应型回包：直接发给请求连接；异常只记日志不上抛。"""
    try:
        await ws.send(line)
    except Exception as e:
        log.warning("回包发送失败: %s: %s", type(e).__name__, e)


async def reply_sessions() -> None:
    """会话列表广播到所有活跃连接（**全部工作空间**，分组由前端按 project 完成）。

    与改造前的差别只有一处：以前只有 default 一个空间的会话；现在把所有空间的
    会话合成一份列表，每条都带 `project`（所属空间 id）。前端拿 `projects` 信封
    把 id 映射成名称，挂到对应空间节点下。

    **刻意不做全局重排**：各空间的列表自身已按「最后修改时间」倒序
    （`SessionManager.list_sessions` 的口径），前端按空间过滤时顺序天然正确；
    再来一次全局排序反而会把某个空间的顺序按别的空间的时间戳打乱。
    """
    sessions = await asyncio.to_thread(_list_all_sessions)
    hub.broadcast("sessions", {"sessions": sessions})


def _list_all_sessions() -> list[dict]:
    """遍历全部工作空间列出活跃会话（单个空间出错只跳过它，不拖垮整个列表）。"""
    out: list[dict] = []
    for info in _list_infos():
        try:
            sm = _ensure_session_manager(info.id)
            items = sm.list_sessions("active")
        except Exception as e:
            log.error("列出工作空间 %s 的会话失败: %s: %s",
                      info.id, type(e).__name__, e)
            continue
        for s in items:
            # meta 里缺 project（存量会话）时按目录归属兜底，绝不落到 default
            s["project"] = s.get("project") or info.id
            out.append(s)
    return out


def _list_all_trashed() -> list[dict]:
    """全部工作空间的回收站条目（回收站是全局视图，删除时按 sid 各自解析空间）。"""
    out: list[dict] = []
    for info in _list_infos():
        try:
            items = _ensure_session_manager(info.id).list_sessions("trashed")
        except Exception as e:
            log.error("列出工作空间 %s 的回收站失败: %s: %s",
                      info.id, type(e).__name__, e)
            continue
        for s in items:
            s["project"] = s.get("project") or info.id
            out.append(s)
    return out


def _projects_payload() -> dict:
    """工作空间列表载荷（侧边栏树 + 输入框 chip 下拉的数据源）。

    只含空间元数据与可达性；会话数由前端按 `sessions` 信封自行分组统计 ——
    避免为了一个角标在每次广播时多扫一遍全部会话文件。
    """
    reg = get_registry()
    return {
        "projects": [info.to_payload() for info in reg.list_infos()],
        "active": reg.active_id(),
    }


async def reply_projects() -> None:
    """工作空间列表广播（连接建立时重放 + 每次增删改后刷新）。"""
    payload = await asyncio.to_thread(_projects_payload)
    hub.broadcast("projects", payload)


def _session_meta(item: dict) -> dict:
    """session_manager.list_sessions() 的条目已是元数据 dict，直接透传。"""
    return dict(item)


# ── 会话标题生成（简单时序：先默认标题，首轮结束后再 LLM 精炼） ────────

TITLE_SYSTEM_PROMPT = (
    "你是会话标题生成器。根据用户的首条消息生成一个不超过20个字的简短标题，"
    "概括用户意图。直接输出标题文本：不要引号、不要句号、不要任何解释。"
)

# 标题请求专用短超时：独立小客户端，不与主对话共用连接池/超时/重试策略。
# 首轮结束后的标题总结请求也要在 TITLE_TIMEOUT 秒内出结果，超时则保留默认标题，
# 绝不悬挂到主对话流程（主客户端 timeout=1200s + 3 次重试，绝不复用）。
TITLE_TIMEOUT = 30
# max_tokens 必须给足：推理模型（如 deepseek-v4-flash）的思考过程也计入
# completion 预算，预算太小会被 reasoning_tokens 吃光导致 content 为空/
# 只挤出单字。1000 对"思考 + 20 字标题"足够，成本可忽略。
TITLE_MAX_TOKENS = 1000


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
        return title[:20]
    except Exception:
        return None


def _default_session_title(text: str) -> Optional[str]:
    """默认标题：首条用户消息前 30 字符（空白归一为单行），创建会话元数据时立即可读。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return None
    return t[:30]


async def _finalize_title_after_turn(turn_task, session_id: str,
                                     first_user_text: str, sm: SessionManager) -> None:
    """第一轮 run_turn 结束后，用大模型总结生成标题（≤20 字）并写回会话元数据。

    标题生成不再与首轮并行抢跑（旧 _start_title_thread 方案）：创建会话时已有
    "首条消息前 30 字"的默认标题可读，第一轮执行完后再调一次 LLM 精炼。
    LLM 失败/不合法则保留默认标题；任何异常都不影响主流程。

    `sm` 由调用方按**该会话所属工作空间**传入（多工作空间）：写错空间会把标题
    写进另一个空间的同名会话文件。
    """
    try:
        await turn_task
    except Exception:
        pass  # turn 异常也照常生成标题，不阻断
    try:
        title = await asyncio.to_thread(_generate_session_title, first_user_text)
        if not title:
            return  # 保留创建时的默认标题（首条消息前 30 字）
        await asyncio.to_thread(sm.set_auto_title, session_id, title, "auto")
        await reply_sessions()
    except Exception:
        log.warning("session_%s 标题生成失败，保留默认标题", session_id)


def _has_real_user_turn(messages: list) -> bool:
    """历史中是否已有真实用户消息（排除 <system-reminder> 系统注入）。"""
    for m in messages:
        if m.get("role") != "user":
            continue
        if _text_of(m.get("content")).startswith("<system-reminder>"):
            continue
        return True
    return False


async def _reply_task_board(ws, sm, sid: str) -> None:
    """补发某会话当前的任务板快照（重放用；纯读磁盘，无副作用）。

    **只发「未完成组」**（`task_manager.current_board`）—— 已结束的组不回放。
    这正是"会话切换 / 复现时仅显示正在执行的组，已经结束的不显示"的实现点：
    配合前端在收到 session_history 时先把该会话 board 置 null，切走再切回
    就不会残留上一轮那版 `done` 快照。

    与实时通道的分工：实时推 `latest_board`（**包含**最后一版 status=done，
    前端据此自动收起并显示「全部完成」），重放推 `current_board`（无未完成组
    就发 board=null）。

    注意：运行中的会话也必须补发这一封 —— 既有守卫禁止的是
    `load_session_history` 的**落盘重写**，而这里只读该会话的**一个**任务文件
    （`.tasks/<scope>.json`，2026-09-16 起一会话一文件），
    没有任何副作用；不补发的话切到"正在跑长任务"的会话会看到空面板，
    要等下一次任务状态变化才出现。
    """
    try:
        scope = f"{sm.session_prefix}{sid}"
        # tasks_dir 由该会话所属空间的 SessionManager 给出（多工作空间：模块级
        # TASKS_DIR 只代表 default，直接用它会让自定义空间的会话读到空面板）
        board = await asyncio.to_thread(current_board, scope, sm.tasks_dir)
    except Exception as e:
        log.error("读取任务板失败 session_%s: %s: %s", sid, type(e).__name__, e)
        board = None
    await safe_send(ws, _envelope("task_board", {"session_id": sid, "board": board}))


def _ensure_session_manager(project_id: str = DEFAULT_PROJECT_ID) -> SessionManager:
    """按工作空间取（并缓存）会话管理器。

    每个工作空间一份：会话历史 / 任务目录 / 子智能体旁路记录都跟随该空间的元数据
    目录。改造前只有一个挂在全局 agent 上的 SessionManager（恒指 default），多空间
    下它回答不了"这个会话属于哪个空间"。

    两个缓存都只增不删 —— 命中后 O(1)，量级 = 空间数 / 会话数，可忽略。
    """
    pid = project_id or DEFAULT_PROJECT_ID
    sm = _MANAGER_CACHE.get(pid)
    if sm is not None:
        return sm
    ws = _workspace_of(pid)
    sm = SessionManager(
        ws.chat_history_dir, agent.system_prompt.build_system_prompt(),
        session_prefix=agent.session_prefix,
        subagent_store=SubagentStore(ws.chat_history_dir),
        project_id=ws.id,
        tasks_dir=ws.tasks_dir,
    )
    _MANAGER_CACHE[pid] = sm
    return sm


def _workspace_of(project_id: str) -> WorkspacePaths:
    """工作空间 id → 路径束（沙箱根 / 元数据目录的唯一出处）。"""
    return get_registry().paths(project_id)


def _list_infos() -> list:
    """全部工作空间元数据（注册表异常时退化为空列表，只影响列表展示）。"""
    try:
        return get_registry().list_infos()
    except Exception as e:
        log.error("读取工作空间列表失败: %s: %s", type(e).__name__, e)
        return []


def _active_project() -> str:
    """当前活动工作空间（前端 chip 默认值 / 未指定 project_id 的新会话归属）。"""
    try:
        return get_registry().active_id()
    except Exception as e:
        log.error("读取活动工作空间失败，回落 default: %s: %s", type(e).__name__, e)
        return DEFAULT_PROJECT_ID


def _project_ready(project_id: str) -> bool:
    """该工作空间的真实目录是否可用（被删/改名/移动硬盘未挂载 → False）。

    default 恒 True（它没有真实目录）。不可用时拒绝新建会话/发消息 ——
    否则工具会在一片"空气目录"里跑，或把文件写到别处。
    """
    try:
        info = get_registry().get(project_id)
    except Exception:
        return False
    return bool(info and info.exists)


def _project_of_session(sid: str) -> str:
    """会话 id → 所属工作空间 id（进程内缓存 + 磁盘探测兜底）。

    会话 id **全局唯一**（`new_session_id` 的查重范围已扩到全部工作空间，见
    session_manage），所以缓存"已知归属"是安全的：一个 sid 只可能属于一个空间。

    未命中（老会话 / 管理操作带来的陌生 id）按各空间的元数据目录探测：meta 文件
    或 jsonl 命中即缓存；都不命中则回落 default（与改造前"只有 default"一致）。
    """
    pid = _SID_PROJECT.get(sid)
    if pid:
        return pid
    prefix = agent.session_prefix
    for info in _list_infos():
        try:
            base = _workspace_of(info.id).chat_history_dir
        except WorkspaceError:
            continue
        if ((base / f"{prefix}{sid}.meta.json").exists()
                or (base / f"{prefix}{sid}.jsonl").exists()):
            _SID_PROJECT[sid] = info.id
            return info.id
    return DEFAULT_PROJECT_ID


def _manager_for_session(sid: str) -> SessionManager:
    """会话 → 其**所属空间**的 SessionManager。

    ⚠️ 所有按 session_id 操作的命令（重命名/回收站/还原/删除/清空/模型绑定）
    都必须经这里取 sm —— 直接调 `_ensure_session_manager()` 会拿到 default 的
    实例，对自定义空间的会话就会"查无此会话"或误改同名的 default 文件。
    """
    return _ensure_session_manager(_project_of_session(sid))


def _busy_sessions_of_project(project_id: str) -> list[str]:
    """该工作空间里仍在执行（turn 在跑，或后台任务仍在写文件）的会话 id。

    删除工作空间的守卫：目录里还有人在写就不能删（`is_active` 而非 `is_busy`——
    后台子智能体执行期 turn 已结束但线程仍在写会话文件）。
    """
    if registry is None:
        return []
    return [rt.sid for rt in registry.all_runtimes()
            if rt.workspace is not None and rt.workspace.id == project_id
            and registry.is_active(rt.sid)]


def _sessions_of_project(project_id: str) -> list[str]:
    """该工作空间下已注册的会话运行时 id（删除后清理用）。"""
    if registry is None:
        return []
    return [rt.sid for rt in registry.all_runtimes()
            if rt.workspace is not None and rt.workspace.id == project_id]


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
    if registry is None:
        # 仅可能出现在 main() 赋值之前（测试直调 handle）；按"无运行会话"处理，
        # 不让一个未初始化的模块状态把整个连接循环打死。
        return lines
    for rt in registry.all_runtimes():
        status = rt.current_status()
        if status is not None:
            lines.append(_envelope(
                "session_status", {"session_id": rt.sid, "status": status}))
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


def _resolve_session_window(meta: dict) -> Optional[str]:
    """按会话元数据解析该会话的上下文窗口字符串（供切会话的 context_stats）。

    口径与前端 resolveOverridesPayload 一致：绑定模型 + 参数覆盖里的
    max_context_option（extended 显式选过才取扩展窗口，否则标准窗口）。
    未绑定模型/元数据缺失返回 None（沿用全局默认）。

    历史 bug：切会话直接用共享 SessionManager 的默认窗口（全局 env
    MAX_CONTEXT_TOKENS=1M），导致所有会话圆圈都显示 1M，与所选模型
    真实窗口（如 128k）不符。
    """
    model_id = meta.get("model_id") or None
    if not model_id:
        return None
    ov = ((meta.get("overrides") or {}).get(model_id)) or {}
    extended = isinstance(ov, dict) and ov.get("max_context_option") == "extended"
    return resolve_model_window(model_id, extended=extended)


def _history_to_ui(messages: list, subagent_records: list | None = None) -> list[dict]:
    """session 历史 → 前端可渲染消息列表。

    - 跳过 system / tool 消息（前者无展示价值，后者已聚合进 assistant 工具条）
    - 跳过系统注入的 user 消息（<system-reminder> 开头的 memory/env/task_board 注入等）
    - assistant 保留 reasoning_content → thinking、tool_calls → 工具条
    - 子智能体执行过程：主源为**旁路记录**（`session_<id>.subagents.jsonl`，
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
            ui_msg = {"role": "user", "content": content}
            # 消息记录时间（jsonl created_at，秒级 ISO 本地时间；老行缺省）
            if m.get("created_at"):
                ui_msg["created_at"] = m["created_at"]
            ui.append(ui_msg)
        elif role == "assistant":
            tool_calls = []
            tc_ids: list[str] = []
            for tc in (m.get("tool_calls") or []):
                tc_ids.append(tc.get("id", "") if isinstance(tc, dict) else "")
                tool_calls.append({
                    "name": (tc.get("function") or {}).get("name", ""),
                    "args": (tc.get("function") or {}).get("arguments", ""),
                })
            ui_msg = {
                "role": "assistant",
                "content": _text_of(m.get("content")),
                "thinking": m.get("reasoning_content") or "",
                "_tc_ids": tc_ids,
                "toolCalls": tool_calls,
            }
            if m.get("created_at"):
                ui_msg["created_at"] = m["created_at"]
            # 轮级 token 消耗 + 本轮模型快照 + 会话级累计快照（UI 展示元数据，
            # turn 收尾时写入末条 assistant 行的 usage / model_info / usage_session 节点）
            if m.get("usage"):
                ui_msg["usage"] = m["usage"]
            if m.get("model_info"):
                ui_msg["model_info"] = m["model_info"]
            if m.get("usage_session"):
                ui_msg["usage_session"] = m["usage_session"]
            ui.append(ui_msg)
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
    # 工作空间列表重放：新连接（含断线重连 / 渲染进程刷新）立即拿到侧边栏树与
    # chip 下拉所需的全部空间元数据，不必等前端主动拉。放在会话列表之前 ——
    # 前端要用它把 session.project（id）映射成名称、决定挂在哪个节点下。
    line_q.put_nowait(_envelope("projects", await asyncio.to_thread(_projects_payload)))
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
                # 事件按 session_id 路由到前端对应缓冲。本循环不做 await run_turn，
                # 派发后立即继续读命令 → 任意会话可后台执行、切换不断流。
                sid = payload.get("session_id")
                text = payload.get("text", "")
                # 目标工作空间：新建会话时由前端显式带上（点哪个空间的「+」就进哪个
                # 空间）；老前端 / 未带时用当前活动空间。**显式优先**，不依赖进程级
                # 活动态 —— 两个窗口并发时各发各的，不会互相串空间。
                want_pid = str(payload.get("project_id") or "") or None
                # 全新会话（前端无激活会话 / 未带 session_id）：事件循环内确定性
                # 生成短 id + 写入初始 system 消息（随机 id + 查重在这个单线程
                # 事件循环里执行，避免并发线程 race 到同一会话文件）。
                if sid is None:
                    pid = want_pid or _active_project()
                    if not _project_ready(pid):
                        await safe_send(ws, _envelope("error", {
                            "msg": "该工作空间的目录当前不可用（已被移动或删除），"
                                   "请重新选择目录后再试",
                        }))
                        continue
                    sm = _ensure_session_manager(pid)
                    new_sid, new_file = sm.create_new_session()
                    for m in sm._build_initial_messages():
                        sm.append_message_to_session(new_file, m)
                    sid = new_sid
                    # 会话归属一建立就入缓存：后续 _load_meta / 管理操作都靠它定位空间
                    _SID_PROJECT[sid] = pid
                    log.info("新会话创建: session_%s (project=%s, model=%s)",
                             sid, pid, payload.get("model_id") or "global-default")
                    # 带上 project_id：前端据此把"活动空间"对齐到新会话的归属
                    #（点空间 B 的「+」新建时，活动空间可能还停在 A）
                    await safe_send(ws, _envelope("session", {
                        "session_id": sid, "message_count": 0, "project_id": pid,
                    }))
                    # 新建会话首批：把前端选择的模型持久化进该会话元数据。
                    #（参数覆盖由前端在收到 session 信封后按 UI 形状 map 写入，此处只记模型；
                    #  chat 透传的 overrides 是已换算的单轮 resolved 形状，不宜直接落元数据。）
                    await asyncio.to_thread(
                        sm.set_session_model, new_sid,
                        model_id=payload.get("model_id"),
                    )
                    # 默认标题：创建会话元数据时即用首条消息前 30 字，列表立刻可读；
                    # 首轮结束后再由 _finalize_title_after_turn 用 LLM 总结精炼（≤20 字）。
                    default_title = _default_session_title(text)
                    if default_title:
                        await asyncio.to_thread(
                            sm.set_auto_title, new_sid, default_title, "trunc"
                        )
                    await reply_sessions()
                # 已有会话：按该会话**所属空间**取管理器（不能用 default 的实例，
                # 否则会读写错空间的同名文件 / 报"会话不存在"）
                pid = _SID_PROJECT.get(sid) or want_pid or _project_of_session(sid)
                _SID_PROJECT[sid] = pid
                sm = _ensure_session_manager(pid)
                rt = registry.get_or_create(sid, workspace=_workspace_of(pid))
                if rt.busy:
                    # 同会话并发 turn 拒绝：避免两线程同时写同一会话 jsonl
                    await safe_send(ws, _envelope("error", {
                        "msg": f"该会话 (session_{sid}) 正在执行，请先用停止按钮结束后再发送",
                    }))
                    continue
                # 首轮判定：全新会话，或该会话此前从无真实 user 消息（旧会话首轮）。
                # 标题不在首轮并行抢跑（旧 _start_title_thread 方案）：新建会话时已有
                # 默认标题（首条消息前 30 字）可读，这里只标记首轮，待 run_turn 结束后
                # 再调用一次 LLM 总结生成精炼标题（≤20 字）并写回。
                history = await asyncio.to_thread(
                    sm.load_session_history, sm.get_session_file(sid)
                )
                first_turn = not _has_real_user_turn(history)
                # 会话级请求覆盖：思考强度 / 更大上下文（本轮生效，内存态，不写配置）。
                # 前端下拉悬浮面板改动后随 chat 命令带上来。
                ov = payload.get("overrides") or {}
                reasoning_effort = ov.get("thinking_strength") or None
                max_context_raw = ov.get("max_context") or None
                # 前端发送的是叠加态（standard/extended 二选一），这里已由前端换算成
                # 具体窗口字符串；若前端仅传开关位则回落到 None（走全局）。跳过空串。
                max_context = str(max_context_raw) if max_context_raw else None
                # 后台线程跑 turn；事件循环继续处理其它命令（切换 / 其它会话 / stop）
                log.info("chat 派发: session_%s text=%r", sid, text[:80])
                turn_task = asyncio.create_task(
                    rt.start_turn(text, reasoning_effort=reasoning_effort,
                                  max_context=max_context)
                )
                if first_turn:
                    # 第一轮 run_turn 执行完之后，再调用一次大模型总结生成标题（≤20 字）
                    asyncio.create_task(
                        _finalize_title_after_turn(turn_task, sid, text, sm))

            elif kind == "stop":
                # 仅停止当前显示会话正在执行的那一轮，其它会话不受影响
                sid = str(payload.get("session_id") or "")
                log.info("停止请求: session_%s", sid)
                rt = registry.get(sid)
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
                sid = str(payload.get("session_id") or "")
                log.info("会话切换请求: session_%s", sid)
                # 切到哪个会话就切到它所属的空间：管理器（会话文件/meta）与运行时的
                # 路径束都按该会话的归属解析（多工作空间）
                pid = _SID_PROJECT.get(sid) or _project_of_session(sid)
                _SID_PROJECT[sid] = pid
                sm = _ensure_session_manager(pid)
                # 运行中（含"后台子智能体仍在跑"）的会话不读磁盘回放：turn 在途
                # 或后台 worker 在写时，jsonl 可能处于 "assistant(tool_calls) 已
                # 落盘、tool 响应未落盘" 的中间态，load_session_history 的孤儿清理
                # 会把它当坏数据重写文件，截断在途消息。前端对运行中会话本就以实时
                # 缓冲为准（session_history 的 hasLive 守卫），此处回放空消息即可。
                # 注意用 is_active 而非 is_busy：后台子智能体执行期间 turn 已结束
                # （busy=False），但后台线程仍在写文件，同样不能回放——
                # 历史 bug：只判 busy 时，"子智能体一跑就切会话"会撞上原子重写，
                # 表现为前后端会话状态错位（2026-09-14 修复）。
                if registry.is_active(sid):
                    # 消息回放跳过（以实时缓冲为准），但模型与参数仍按元数据恢复，
                    # 保证切到运行中会话时其参数覆盖也能正确加载。
                    meta = (await asyncio.to_thread(sm.load_meta, sid)) or {}
                    await safe_send(ws, _envelope("session_history", {
                        "session_id": sid, "messages": [],
                        "model_id": meta.get("model_id"),
                        "overrides": meta.get("overrides") or {},
                        "usage_totals": meta.get("usage_totals"),
                    }))
                    # 任务板照常补发：只读 .tasks/，不触碰会话文件，无重写风险
                    await _reply_task_board(ws, sm, sid)
                    await reply_sessions()
                    continue
                try:
                    _, sess_file, history = await asyncio.to_thread(sm.switch_session, sid)
                except FileNotFoundError:
                    await safe_send(ws, _envelope("error", {"msg": f"session {sid} not found"}))
                else:
                    # 切换只是"按 id 读取该会话历史回放"（并刷新列表），
                    # 不改变任何运行中会话的执行状态 → 切换不断流。
                    # 顺带读取该会话记录的模型与参数，供前端按元数据恢复选中。
                    meta = (await asyncio.to_thread(sm.load_meta, sid)) or {}
                    # 子智能体执行过程来自旁路文件（与主 jsonl 物理隔离），
                    # 按 tool_call_id 挂到发起它的 assistant 消息下（与实时一致）
                    records = await asyncio.to_thread(
                        sm.load_subagent_records, sess_file)
                    await safe_send(ws, _envelope("session_history", {
                        "session_id": sid,
                        "messages": _history_to_ui(history, records),
                        "model_id": meta.get("model_id"),
                        "overrides": meta.get("overrides") or {},
                        "usage_totals": meta.get("usage_totals"),
                    }))
                    # 任务板补发：只发未完成组 → 已结束的组切回来不显示
                    await _reply_task_board(ws, sm, sid)
                    # 切换会话后推送该会话的上下文统计（供前端圆圈指示器按会话展示）；
                    # 窗口按会话元数据（绑定模型 + 参数覆盖）解析，不用共享
                    # SessionManager 的全局默认（见 _resolve_session_window 注释）
                    try:
                        stats = await asyncio.to_thread(
                            sm.context_stats_dict, history,
                            _resolve_session_window(meta),
                        )
                        await safe_send(ws, _envelope("context_stats", {"session_id": sid, **stats}))
                    except Exception:
                        pass
                    await reply_sessions()

            elif kind == "session_set_unread":
                # 标记会话未读/已读（读/未读由前端判定：进入会话=已读，非当前查看会话
                # 完整结束=未读）。写入元数据持久化后重播会话列表，跨窗口/重启生效。
                sid = str(payload.get("session_id") or "")
                if not sid:
                    continue
                sm = _manager_for_session(sid)
                if not sm.get_session_file(sid).exists():
                    continue
                unread = bool(payload.get("unread", False))
                await asyncio.to_thread(sm.set_unread, sid, unread)
                await reply_sessions()

            elif kind == "session_model":
                # 记录会话最后选择的模型 + 参数到会话元数据（会话级独立绑定）；
                # 无 session_id（新建任务预设态）由 chat 首条统一持久化，此处仅处理已建会话。
                sid = str(payload.get("session_id") or "")
                if not sid:
                    continue
                sm = _manager_for_session(sid)
                if not sm.get_session_file(sid).exists():
                    await safe_send(ws, _envelope("error", {"msg": f"session {sid} not found"}))
                    continue
                model_id = payload.get("model_id")
                overrides = payload.get("overrides") or None
                await asyncio.to_thread(
                    sm.set_session_model, sid,
                    model_id=model_id if model_id is not None else None,
                    overrides=overrides if isinstance(overrides, dict) else None,
                )
                # 会话绑定模型被切换 → 记录到运行中 agent：turn 执行中被切换计入
                # 本轮，空闲期切换计入 pending（下一轮并入）。不再限定 busy——
                # 「切换模型后再发消息」的切换同样要展示（修复 2026-09-16）。
                if model_id:
                    rt = registry.get(sid)
                    if rt is not None:
                        await asyncio.to_thread(rt.record_model_switch, model_id)
                await safe_send(ws, _envelope("session_model", {
                    "session_id": sid, "model_id": model_id, "overrides": overrides,
                }))
                await reply_sessions()

            elif kind == "session_clear":
                sid = str(payload.get("session_id") or "")
                if not sid:
                    await safe_send(ws, _envelope("error", {"msg": "当前无激活会话"}))
                    continue
                sm = _manager_for_session(sid)
                if registry.is_active(sid):
                    await safe_send(ws, _envelope("error", {
                        "msg": f"该会话 (session_{sid}) 正在执行（或后台任务仍在跑），暂不能清空",
                    }))
                    continue
                if not sm.get_session_file(sid).exists():
                    await safe_send(ws, _envelope("error", {"msg": f"session {sid} not found"}))
                    continue
                await asyncio.to_thread(sm.clear_session, sm.get_session_file(sid))
                log.info("会话清空: session_%s", sid)
                await safe_send(ws, _envelope("session", {"session_id": sid, "message_count": 0}))
                await reply_sessions()

            elif kind == "sessions_list":
                await reply_sessions()

            # ── 工作空间（多项目，2026-09-18）───────────────────────────
            # 目录选择由 Electron 主进程弹原生选择框（dialog.showOpenDialog），
            # 后端只负责登记 + 建元数据目录 + 落索引，职责边界与其它命令一致。
            elif kind == "projects_list":
                await reply_projects()

            elif kind == "project_add":
                try:
                    info = await asyncio.to_thread(
                        get_registry().create, str(payload.get("path") or ""))
                except WorkspaceError as exc:
                    # 目录不存在/不可写/选了 ~/.aigent 内部目录 → 原样告诉用户原因
                    await safe_send(ws, _envelope("error", {"msg": str(exc)}))
                except Exception as exc:  # noqa: BLE001 - 桥层兜底，绝不打死连接
                    log.error("新增工作空间失败: %s: %s", type(exc).__name__, exc)
                    await safe_send(ws, _envelope("error", {"msg": f"新增工作空间失败：{exc}"}))
                else:
                    log.info("工作空间就绪: %s（%s）", info.id, info.path)
                    await reply_projects()
                    await reply_sessions()

            elif kind == "project_open":
                try:
                    await asyncio.to_thread(
                        get_registry().set_active, str(payload.get("project_id") or ""))
                except WorkspaceError as exc:
                    await safe_send(ws, _envelope("error", {"msg": str(exc)}))
                else:
                    await reply_projects()

            elif kind == "project_rename":
                try:
                    await asyncio.to_thread(
                        get_registry().rename,
                        str(payload.get("project_id") or ""), str(payload.get("name") or ""),
                    )
                except WorkspaceError as exc:
                    await safe_send(ws, _envelope("error", {"msg": str(exc)}))
                else:
                    await reply_projects()

            elif kind == "project_remove":
                pid = str(payload.get("project_id") or "")
                # 守卫：该空间还有会话在跑（或后台任务在跑）就拒绝 —— 删掉正在写的
                # 会话文件是不可逆事故，且用户还没机会看到"总结没出来"。
                busy = _busy_sessions_of_project(pid)
                if busy:
                    await safe_send(ws, _envelope("error", {
                        "msg": f"该工作空间还有 {len(busy)} 个会话正在执行，请先停止后再删除",
                    }))
                    continue
                try:
                    info = await asyncio.to_thread(get_registry().remove, pid)
                except WorkspaceError as exc:
                    await safe_send(ws, _envelope("error", {"msg": str(exc)}))
                except Exception as exc:  # noqa: BLE001
                    log.error("删除工作空间失败 %s: %s: %s", pid, type(exc).__name__, exc)
                    await safe_send(ws, _envelope("error", {"msg": f"删除工作空间失败：{exc}"}))
                else:
                    # 清掉该空间的缓存与 sid→project 映射：目录已不存在，留着会
                    # 让后续按 sid 的操作去建/读一个已删除的目录
                    _MANAGER_CACHE.pop(pid, None)
                    for sid_key, pid_val in [kv for kv in _SID_PROJECT.items() if kv[1] == pid]:
                        _SID_PROJECT.pop(sid_key, None)
                    for sid_key in _sessions_of_project(pid):
                        registry.remove(sid_key)
                    log.info("工作空间已删除: %s（%s）真实目录保留", info.id, info.path)
                    await reply_projects()
                    await reply_sessions()

            elif kind == "session_rename":
                tgt = str(payload.get("session_id") or "")
                sm = _manager_for_session(tgt)
                try:
                    await asyncio.to_thread(
                        sm.rename_session, tgt, str(payload.get("title", "")),
                    )
                except FileNotFoundError:
                    await safe_send(ws, _envelope("error", {"msg": f"session {tgt} not found"}))
                except ValueError as exc:
                    await safe_send(ws, _envelope("error", {"msg": f"重命名失败：{exc}"}))
                else:
                    log.info("会话重命名: session_%s -> %r", tgt, payload.get("title"))
                    await reply_sessions()

            elif kind == "session_trash":
                sid = str(payload.get("session_id") or "")
                # 运行中（含后台子智能体仍在跑）的会话禁止进回收站：
                # 后台 worker 还在往会话文件/旁路文件写，移动文件会造成写入丢失
                # 与"孤儿文件复活"。用 is_active 覆盖 background 窗口（2026-09-14）。
                if registry.is_active(sid):
                    await safe_send(ws, _envelope("error", {
                        "msg": f"该会话 (session_{sid}) 正在执行（或后台任务仍在跑），请先停止后再删除",
                    }))
                    continue
                sm = _manager_for_session(sid)
                try:
                    await asyncio.to_thread(sm.trash_session, sid)
                except (FileNotFoundError, ValueError):
                    await safe_send(ws, _envelope("error", {"msg": f"session {sid} not found"}))
                else:
                    log.info("会话进回收站: session_%s (project=%s)", sid, _project_of_session(sid))
                    registry.remove(sid)
                    await reply_sessions()

            elif kind == "session_restore":
                tgt = str(payload.get("session_id") or "")
                sm = _manager_for_session(tgt)
                try:
                    await asyncio.to_thread(sm.restore_session, tgt)
                except (FileNotFoundError, ValueError):
                    await safe_send(ws, _envelope("error", {"msg": f"session {tgt} not found"}))
                else:
                    log.info("会话从回收站还原: session_%s", tgt)
                    await reply_sessions()

            elif kind == "session_delete":
                # 批量永久删除：逐条执行，单条失败不断整批；
                # 有活动的会话拒绝删除（turn 在跑，或后台子智能体还在写文件）
                ids = payload.get("ids") or []
                deleted, failed = [], []
                for raw in ids:
                    sid = str(raw or "")
                    if not sid:
                        continue
                    if registry.is_active(sid):
                        failed.append(sid)
                        continue
                    # 每个 sid 各自解析空间（批量删除可能跨空间：回收站是全局列表）
                    sm = _manager_for_session(sid)
                    try:
                        ok = await asyncio.to_thread(sm.delete_session_permanent, sid)
                    except (TypeError, ValueError):
                        continue
                    if ok:
                        deleted.append(sid)
                        registry.remove(sid)
                    else:
                        failed.append(sid)
                # 结果回发后不再全量广播 sessions：前端以 deleted[] 本地增量移除，
                # 避免删除完成后重建整个会话列表（逐个重数 message_count）造成的刷新延迟。
                log.info("会话批量永久删除: deleted=%s failed=%s", deleted, failed)
                await safe_send(ws, _envelope("session_delete_result", {
                    "deleted": deleted, "failed": failed,
                }))

            elif kind == "trash_list":
                # 回收站是**全局**的（跨工作空间）：删除按 sid 各自解析空间，
                # 所以这里要把每个空间的 trashed 合起来返回，每条带 project。
                items = await asyncio.to_thread(_list_all_trashed)
                await safe_send(ws, _envelope("sessions_trashed", {"sessions": items}))

            elif kind == "goal_status":
                text = await asyncio.to_thread(agent.goal_status)
                await safe_send(ws, _envelope("goal_status", {"text": text}))

            elif kind == "tasks":
                # todo 已下线（2026-09-16）：本命令改为返回当前会话的 task 看板文本。
                # 无激活会话时仍给占位文本 —— 此时 task_manager 尚未 set_scope，
                # list_tasks 会退化成列出全局任务（旧全局看板兼容语义），必须拦住。
                if agent.session_id is None:
                    await safe_send(ws, _envelope("tasks", {"text": "(当前会话暂无任务)"}))
                    continue
                text = await asyncio.to_thread(agent.show_tasks)
                if not text.strip():
                    text = "(当前会话暂无任务)"
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


def _session_id_taken_globally(sid: str) -> bool:
    """该会话 id 是否已被**任意工作空间**占用（跨空间查重的唯一实现）。

    注入给 `session_manage.set_session_id_guard`：新建会话时逐空间 stat
    `session_<id>.jsonl` / `.meta.json`。id 是前端事件路由键，跨空间重号会让
    事件进错会话、切会话切错空间，且无法自愈 —— 用 N 次 stat 换掉这个风险。
    """
    prefix = agent.session_prefix
    for info in _list_infos():
        try:
            base = _workspace_of(info.id).chat_history_dir
        except WorkspaceError:
            continue
        if ((base / f"{prefix}{sid}.jsonl").exists()
                or (base / f"{prefix}{sid}.meta.json").exists()):
            return True
    return False


async def main():
    global _loop, registry
    _loop = asyncio.get_running_loop()
    # 会话 id 跨工作空间唯一性守卫（见 _session_id_taken_globally）
    set_session_id_guard(_session_id_taken_globally)
    # 全局唯一会话运行时注册表：所有连接共享。连接可断可换，
    # 运行中的会话事件始终经 hub 广播到当前活跃连接（见 ConnectionHub）。
    def _load_meta(sid: str):
        # 会话 → 所属空间的管理器（多工作空间：不能固定用 default 的那份）
        return _manager_for_session(sid).load_meta(sid)
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