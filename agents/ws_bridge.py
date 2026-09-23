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
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Optional

import websockets
from openai import OpenAI

from agent_full_v2 import Agent
from attachments import (
    build_user_content,
    gc_drafts,
    gc_orphan_session_dirs,
    harvest_attachments,
    is_tool_images_message,
    migrate_to_session,
    remove_session_attachments,
    stage as stage_attachments,
)
from config import load as load_config
from config import CONFIG_FILE
import sandbox as sandbox_mod
from interaction import status_of_result
from llm_config import (
    caps_allow_image, fetch_remote_models, get_config, get_model_by_id,
    load_llm_config, resolve_model_window, save_config,
)
from logger import get_logger, install_excepthooks
from paths import (
    CHAT_HISTORY_DIR,
    DEFAULT_PROJECT_ID,
    WorkspacePaths,
    default_scratch_paths,
    workspace_paths,
)
# 引用（@-mention，2026-09-21）：与附件**完全独立**的一条通道 —— 不复制、不存储，
# 只把工作空间内的路径清单发给模型。协议与设计见 docs/frontend/13。
from refs import (
    attach_ref_blocks,
    harvest_refs,
    list_workspace as list_ref_workspace,
    normalize_refs,
    read_workspace_file,
    ref_title_hint,
)
# 右栏「变更」面板（2026-09-23）：git 取数放在 Python 侧，唯一理由是**口径唯一** ——
# "当前工作空间根"只由 paths.WorkspacePaths 定义，让 Electron 再推一遍必然分叉。
from git_changes import diff_file as git_changes_diff
from git_changes import status as git_changes_status
from permission import PermissionStore, VALID_MODES, builtin_snapshot
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


def _workspace_for_session(project_id: str, meta: dict | None) -> WorkspacePaths:
    """会话级沙箱根解析（work_root 快照，2026-09-20）。

    meta 记了 work_root（桌面端新建的会话）→ 以**快照**为准：「会话建成即锁
    空间」的姊妹规则，空间目录后续变化 / default 沙箱策略调整都不影响已有
    会话的相对路径落点，bash 与文件工具也随快照同根。
    没记（存量会话 / CLI）→ 按空间现值：default = 遗留 WORKDIR + 进程 cwd
    bash（零迁移），自定义空间 = 选定目录。
    """
    ws = _workspace_of(project_id)
    wr = (meta or {}).get("work_root")
    if not wr:
        return ws
    p = Path(wr)
    return dc_replace(ws, workdir=p, bash_cwd=p)


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


# ── 引用（@-mention，2026-09-21，桌面端「引用文件或文件夹」）────────────
# 与附件是**两条独立的通道**（设计见 docs/frontend/13）：
#   附件 = 复制副本到工作空间**之外** → 沙箱拒绝 → 只能预注入全文；
#   引用 = 指向工作空间**之内**的真实路径 → 模型能 run_read → 只给路径与类型。
# 桥层职责：① `refs_list` 命令（一次拉回完整扁平列表，按键在前端本地过滤）；
#           ② `chat` 携带 refs 时规范化路径并挂中性引用块；
#           ③ 回放时 harvest 出引用列表。
#
# ⚠️ 位置说明：以下辅助函数刻意放在 `_text_of` 定义**之前**。从该定义到
# `handle` 入口之间的源码会被 tests/test_subagent_sidecar.py 与
# test_system_injection_contract.py 切片 exec（裸 dict 命名空间，切片内的模块级
# 引用要逐个预置）。放在切片外，零维护成本。
# 注意：本节注释里**不能出现** `_text_of` 的完整定义字面量（`def` + 名字 + 括号），
# 否则 `src.index(...)` 会先命中注释，切片从一个注释中间开始 → 语法错误。

# default 空间下 @ 不可用的原因。**这是常规状态而不是错误**：default 的沙箱根是
# 临时草稿目录（~/.aigent/projects/default/scratch），里面没有用户的项目文件可引用。
# 前端据此在候选面板里显示一行原因，而不是弹 toast 打扰。
DEFAULT_REF_DISABLED_REASON = (
    "默认工作空间是临时草稿目录，没有可引用的项目文件；请先切换到自定义工作空间"
)


def _refs_disabled(project_id: str, reason: str) -> dict:
    """`refs` 信封的「不可用」形态（与成功形态同一套字段，前端不必分支解析）。"""
    return {
        "project_id": project_id,
        "workdir": "",
        "items": [],
        "truncated": False,
        "total_seen": 0,
        "skipped": 0,
        "disabled": True,
        "reason": reason,
    }


async def _refs_payload(project_id: str, session_id: str = "") -> dict:
    """组装 `refs` 信封：定位沙箱根 → 扁平列目录（BFS + 忽略清单 + 上限）。

    沙箱根必须与**工具看到的根**一致（有会话时按会话 `work_root` 快照，否则按空间
    现值）—— 否则会出现"列表里选得到、模型却读不到"的脱节，而"可读"正是引用功能
    成立的前提。
    """
    ws_paths = None
    if session_id:
        meta = None
        try:
            meta = await asyncio.to_thread(
                _manager_for_session(session_id).load_meta, session_id)
        except Exception as exc:  # noqa: BLE001 - 读不到 meta 就按空间现值兜底
            log.warning("refs_list 读取会话元数据失败 session=%s: %s: %s",
                        session_id, type(exc).__name__, exc)
        try:
            ws_paths = _workspace_for_session(project_id, meta)
        except Exception as exc:  # noqa: BLE001
            log.warning("refs_list 解析会话沙箱根失败 session=%s: %s: %s",
                        session_id, type(exc).__name__, exc)
    else:
        try:
            ws_paths = _workspace_of(project_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("refs_list 解析工作空间失败 project=%s: %s: %s",
                        project_id, type(exc).__name__, exc)

    if ws_paths is None:
        return _refs_disabled(project_id, "该工作空间的目录当前不可用（已被移动或删除）")
    if ws_paths.id == DEFAULT_PROJECT_ID:
        # 刻意在**遍历之前**返回：scratch 是草稿区，扫它既没意义也白费时间
        return _refs_disabled(project_id, DEFAULT_REF_DISABLED_REASON)
    if not _project_ready(project_id):
        return _refs_disabled(project_id, "该工作空间的目录当前不可用（已被移动或删除）")

    # 阻塞 IO 必须离开事件循环：带 node_modules 的工作空间遍历是百毫秒量级
    listing = await asyncio.to_thread(list_ref_workspace, ws_paths.workdir)
    out = dict(listing)
    out["project_id"] = project_id
    out["disabled"] = False
    return out


def _ask_snapshot_lines() -> list[str]:
    """所有在途 `ask_user` 提问的 ask_request 信封列表（2026-09-21）。

    与 `_status_snapshot_lines` 同构，供"连接建立重放"与 `status_query` 共用：
    用户关窗 / 刷新 / 断线重连时，turn 可能仍**阻塞在"等用户作答"**上。
    不重放的话，前端面板永远不会再出现，而后端还在等 —— 用户看到的就是
    "卡住不动"，且没有任何办法恢复（除了点停止）。

    ⚠️ 本函数必须定义在 `_text_of` **之前**：从 `_text_of` 到 `handle` 之间的
    代码会被两个守卫测试（test_subagent_sidecar / test_system_injection_contract）
    抽出来 exec 到裸命名空间，那片区域只能放 def、不能有模块级可执行语句，
    且 def 的注解在 def 处即求值 —— 在切片内新增模块级函数是最常见的踩雷方式。
    """
    if registry is None:
        return []
    lines = []
    for rt in registry.all_runtimes():
        for payload in rt.pending_interactions():
            lines.append(_envelope("ask_request", payload))
    return lines


def _approval_snapshot_lines() -> list[str]:
    """所有在途权限审批的 approval_request 信封列表（2026-09-22 权限管控）。

    与 `_ask_snapshot_lines` 完全同构（断线重连 / 刷新时审批卡片必须重现，
    否则后端阻塞等作答、前端却永远看不到卡片 —— 表现为"卡住不动"）；
    前端按 request_id 幂等恢复（E3）。同样必须定义在 `_text_of` **之前**
    （下方到 handle 之间的切片区域只允许 def，见上方守卫测试说明）。
    """
    if registry is None:
        return []
    lines = []
    for rt in registry.all_runtimes():
        for payload in rt.pending_approvals():
            lines.append(_envelope("approval_request", payload))
    return lines


# ── 权限设置页（docs/frontend/18，2026-09-22）────────────────────────────
# 判定策略本身在 agents/permission.py；桥层只做「读配置 / 写配置 / 推给在途会话」。

def _permission_store() -> PermissionStore:
    """设置页专用 store 实例。

    **不要**复用某个 Agent 的 `permission_gate.store` —— 那是 per-Agent 实例，而设置页
    可能在任何 agent 构造之前就被打开。`PermissionStore` 是无状态门面（读写各持锁 +
    mtime 检查热加载），多个实例指向同一文件自然一致（17 篇 §5.4-E11）。
    """
    return PermissionStore()


def _save_sandbox_enabled(enabled: bool) -> None:
    """沙盒开关落盘 config.json 并**直接覆写 os.environ**（热生效，无需重启）。

    config.load() 走 setdefault（不覆盖已有值），重跑也不会生效 —— 必须就地
    覆写 env（同 llm_config.py 的热切换先例）。sandbox.py 每次使用时读 env，
    所以覆写后下一条 bash 命令立即按新开关执行。
    """
    data = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    data["SANDBOX_ENABLED"] = "1" if enabled else "0"
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.environ["SANDBOX_ENABLED"] = "1" if enabled else "0"
    log.info("沙盒开关已保存并生效: %s", enabled)


def _refresh_permission_dirs() -> int:
    """设置页保存后：把新的全局额外目录推给所有**已构造**的在途会话 Agent。

    只有 `additional_dirs` 需要这一步 —— 其余键在 `evaluate()` 里每轮 `store.load()`
    靠 mtime 热加载天然即时生效，而额外目录进的是 gate 的**缓存集合** `_extra_dirs`。
    未构造 agent 的会话不用管：首次构造时 `restore_from_meta` 自然读到新值。
    返回刷新的会话数（仅用于日志）；任何异常都不得影响保存回执。
    """
    try:
        runtimes = registry.all_runtimes() if registry is not None else []
    except Exception as exc:  # noqa: BLE001 - 刷新失败绝不能影响保存回执
        log.warning("额外目录刷新：取运行时列表失败 %s", exc)
        return 0
    n = 0
    for rt in runtimes:
        agent = getattr(rt, "agent", None)
        if agent is None:
            continue
        try:
            agent.permission_gate.refresh_extra_dirs()
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("额外目录刷新失败 session_%s: %s", rt.sid, exc)
    return n


# ── 右侧面板（docs/frontend/19，2026-09-23）──────────────────────────────
# 三个数据命令（file_read / git_status / git_diff）共用同一套归属解析与降级信封。
# ⚠️ 本节必须留在 `_text_of` **之前** —— 下方到 handle 之间的切片区域只允许 def
# （守卫见本节末尾说明与 tests/test_*_slice_guard）。

DEFAULT_RPANEL_DISABLED_REASON = (
    "默认工作空间是临时草稿目录，没有可浏览的项目文件；请先切换到自定义工作空间"
)


async def _resolve_rpanel_target(payload: dict) -> tuple[str, object | None, str, str]:
    """右栏命令的统一归属解析 → `(project_id, ws_paths|None, reason, session_id)`。

    与 `_refs_payload` 同口径（**这是刻意的**）：有会话时按会话 `work_root` 快照
    解析沙箱根，否则按空间现值。文件树、文件预览、git status 三者必须看到**同一个
    根** —— 否则会出现"树里列出来的文件点开说不在工作空间内"这种自相矛盾。

    `ws_paths is None` 时 `reason` 是给人看的原因，调用方据此回降级信封而不是报错：
    "空间目录被移动/这是草稿区"都不是协议错误。
    """
    sid = str(payload.get("session_id") or "")
    if sid:
        pid = _SID_PROJECT.get(sid) or _project_of_session(sid)
    else:
        pid = str(payload.get("project_id") or "") or _active_project()

    meta = None
    if sid:
        try:
            meta = await asyncio.to_thread(_manager_for_session(sid).load_meta, sid)
        except Exception as exc:  # noqa: BLE001 - 读不到 meta 就按空间现值兜底
            log.warning("右栏命令读取会话元数据失败 session=%s: %s: %s",
                        sid, type(exc).__name__, exc)

    try:
        ws_paths = _workspace_for_session(pid, meta) if sid else _workspace_of(pid)
    except Exception as exc:  # noqa: BLE001
        log.warning("右栏命令解析工作空间失败 project=%s: %s: %s",
                    pid, type(exc).__name__, exc)
        return pid, None, "该工作空间的目录当前不可用（已被移动或删除）", sid

    if ws_paths.id == DEFAULT_PROJECT_ID:
        return pid, None, DEFAULT_RPANEL_DISABLED_REASON, sid
    if not _project_ready(pid):
        return pid, None, "该工作空间的目录当前不可用（已被移动或删除）", sid
    return pid, ws_paths, "", sid


def file_content_disabled(raw_path, *, project_id: str = "",
                          session_id: str = "", reason: str) -> dict:
    """`file_content` 的「读不到」形态。

    与成功形态**同一套字段**（照 `_refs_disabled` 的取舍）：前端不必为失败分支
    单独写一套解析，只要看 `reason` 非空即可切到错误态。
    """
    return {
        "project_id": project_id,
        "session_id": session_id,
        "path": str(raw_path or ""),
        "name": "",
        "size": 0,
        "mtime": 0.0,
        "encoding": "",
        "binary": False,
        "too_large": False,
        "truncated": False,
        "lines": 0,
        "text": "",
        "reason": reason,
    }


def git_status_disabled(*, project_id: str = "", session_id: str = "",
                        reason: str) -> dict:
    """`git_status` 的「不可用」形态（非仓库 / 空间不可用）。"""
    return {
        "project_id": project_id,
        "session_id": session_id,
        "available": False,
        "reason": reason,
        "root": "",
        "branch": "",
        "ahead": 0,
        "behind": 0,
        "files": [],
        "truncated": False,
    }


def git_diff_disabled(raw_path, *, project_id: str = "", session_id: str = "",
                      staged: bool = False, reason: str) -> dict:
    """`git_diff` 的「不可用」形态。"""
    return {
        "project_id": project_id,
        "session_id": session_id,
        "path": str(raw_path or ""),
        "staged": bool(staged),
        "available": False,
        "reason": reason,
        "diff": "",
        "chars": 0,
        "too_large": False,
        "binary": False,
        "untracked": False,
    }


def _text_of(content) -> str:
    """历史消息 content 兼容转换：str 直接返回，list（多模态 blocks）拼接 text。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


# ── 附件（2026-09-20，桌面端「添加文件或图片」）────────────────────────
# 协议与存储布局见 docs/frontend/12-附件与文件输入.md。桥层职责：
#   ① attachment_stage 命令：把本地路径登记成草稿附件（复制 + 解析）；
#   ② chat 携带 attachments 时：草稿归位到会话目录 + 组多模态 content；
#   ③ 回放时 harvest 出附件列表；④ 会话清空/删除时级联回收。

def _model_supports_image(model_id: str | None) -> bool:
    """该模型是否声明支持图片输入。

    三态（与前端 `modelSupportsImage` 同口径）：
    - 明确声明 input 含 image → True；
    - 明确声明了 input 列表但不含 image → False（拦下并提示切模型）；
    - 未知模型 / 元数据缺失 / 读取异常 → True。

    最后一条是刻意的：本地目录可能没收录用户新加的模型，**不能因为"我们不知道"
    就阻止使用**。宁可让 provider 回一个真实的错误，也不要本地误拦。

    三态规则本身在 `llm_config.caps_allow_image`（纯函数，与引擎共用一个口径）；
    这里保留 `get_model_by_id` 的模块级调用点 —— 测试在该名字上打桩。
    """
    if not model_id:
        return True
    try:
        model = get_model_by_id(model_id)
    except Exception as exc:  # noqa: BLE001 - 能力查询失败不阻断对话
        log.warning("读取模型能力失败（按支持图片处理）: %s: %s",
                    type(exc).__name__, exc)
        return True
    if not isinstance(model, dict):
        return True
    return caps_allow_image(model.get("capabilities"))


def _attachment_title_hint(payload: dict) -> str:
    """首条消息没有正文只有附件时的标题素材：`[附件] 文件名`。

    没有它，纯附件的首轮会得到一个空标题（`_default_session_title("")` → None）。
    """
    for att in (payload.get("attachments") or []):
        if isinstance(att, dict) and str(att.get("name") or "").strip():
            return f"[附件] {att['name']}"
    return ""


def _effective_model_id(sm: SessionManager, sid: str, payload: dict) -> str | None:
    """会话当前生效的模型 id（图片能力校验用）。

    优先级：会话元数据（既有会话，以及新建时刚落盘的绑定）> 本轮 payload
    > 全局默认。读元数据失败不阻断（返回 None → 能力校验按"未知=放过"处理）。
    """
    try:
        meta = sm.load_meta(sid) or {}
        if meta.get("model_id"):
            return str(meta["model_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("读取会话模型失败 session=%s: %s: %s", sid, type(exc).__name__, exc)
    return payload.get("model_id") or os.environ.get("OPENAI_MODEL_ID") or None


def _all_workspaces() -> list[WorkspacePaths]:
    """所有工作空间的路径束（附件 GC 要跨空间扫 —— 每个空间一份 `.attachments/`）。"""
    out: list[WorkspacePaths] = []
    try:
        out.append(workspace_paths(DEFAULT_PROJECT_ID))
    except Exception as exc:  # noqa: BLE001
        log.warning("default 空间路径解析失败: %s", exc)
    try:
        for info in get_registry().list_infos():
            if info.id == DEFAULT_PROJECT_ID:
                continue
            try:
                out.append(get_registry().paths(info.id))
            except Exception as exc:  # noqa: BLE001 - 单个空间坏掉不影响其它
                log.warning("空间 %s 路径解析失败: %s", info.id, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("列出工作空间失败（附件 GC 只扫 default）: %s", exc)
    return out


def _startup_attachment_gc() -> None:
    """启动清理：超期草稿 + 孤儿会话附件目录（跨全部工作空间）。"""
    for ws in _all_workspaces():
        try:
            gc_drafts(ws)
            gc_orphan_session_dirs(ws)
        except Exception as exc:  # noqa: BLE001 - 清理失败绝不拦启动
            log.warning("附件清理失败 project=%s: %s: %s", ws.id, type(exc).__name__, exc)


# 节流状态。**刻意不加锁、不引 threading**：这里只是"别每次登记都全盘扫一遍"的
# 尽力而为节流，两个并发登记同时触发一次 GC 完全无害（gc_* 全是
# `rmtree(ignore_errors=True)` + 按文件存在性判定，重复执行幂等）。
# 另外这段代码落在 tests 里被 exec 的源码切片范围内（`_text_of` → `handle`），
# 模块级语句会在 exec 时直接执行 —— 引入 threading/time 之外的模块级调用会让
# 那两个守卫测试加载失败（2026-09-20 踩过一次）。
_ATTACH_GC_LAST = 0.0
_ATTACH_GC_INTERVAL_SECONDS = 600


def _gc_attachments_throttled(ws: WorkspacePaths) -> None:
    global _ATTACH_GC_LAST
    now = time.monotonic()
    if now - _ATTACH_GC_LAST < _ATTACH_GC_INTERVAL_SECONDS:
        return
    _ATTACH_GC_LAST = now
    gc_drafts(ws)
    gc_orphan_session_dirs(ws)


async def _drop_session_attachments(sid: str) -> None:
    """删除某会话的附件目录（「清空会话」/「永久删除会话」调用）。

    空间按会话归属解析 —— 与其它按 sid 的操作同一条口径。
    清理失败只记日志：回收是附带动作，**绝不能反过来打断会话删除主流程**。
    """
    try:
        ws = _workspace_of(_project_of_session(sid))
        await asyncio.to_thread(remove_session_attachments, ws, sid)
    except Exception as exc:  # noqa: BLE001
        log.warning("删除会话附件失败 session=%s: %s: %s", sid, type(exc).__name__, exc)


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
    # 工具结果索引（2026-09-21）：`role=tool` 行整体不上屏（已聚合进 assistant
    # 工具条），但 `ask_user` 的**答案**要按 tool_call_id 配回发起它的那条
    # assistant 消息（渲染成只读小结块）。索引是廉价的，且只被 ask_user 用到。
    tool_contents: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            tool_contents[m["tool_call_id"]] = _text_of(m.get("content"))
    # 审批结算索引（2026-09-22 权限管控）：tool 行旁挂的 approval 按 tool_call_id
    # 配回 assistant 工具条（回放渲染「已拒绝/超时/停止」徽标，与实时路径的
    # approval_resolved 旁挂同一形状）；允许执行的工具行不落该字段。
    tool_approvals: dict[str, dict] = {}
    for m in messages:
        if (m.get("role") == "tool" and m.get("tool_call_id")
                and isinstance(m.get("approval"), dict)):
            tool_approvals[m["tool_call_id"]] = m["approval"]
    for m in messages:
        role = m.get("role")
        if role == "user":
            # 工具读图（run_read 读到图片/页图，2026-09-21）：承载图片的那条是
            # **合成**消息（由 agent_loop 追加、带 `_tool_images` marker），不是
            # 用户说的话。不跳过的话，前端每次回放都会多出一个内容为
            # "[以下是 run_read 读取的图片…]"的假用户气泡。图片本身已在工具条
            # （run_read 调用）里可见，不必在这里重复表达。
            if is_tool_images_message(m):
                continue
            content = _text_of(m.get("content"))
            if content.startswith("<system-reminder>"):
                continue
            ui_msg = {"role": "user", "content": content}
            # 附件（2026-09-20）：jsonl 存的是中性引用块，`_text_of` 只取文本块，
            # 这里额外 harvest 出附件元数据挂到 UI 消息上 —— 前端据此渲染
            # 缩略图 / 文件 chip。**不改变 content 的取值口径**（无附件的消息
            # 与改造前完全一致，连字段都不多一个）。
            attachments = harvest_attachments(m.get("content"))
            if attachments:
                ui_msg["attachments"] = attachments
            # 引用（2026-09-21）：同上，从 content 里 harvest 出引用列表供气泡渲染。
            # **只在确有引用时才加字段** —— 无引用的消息连字段都不多一个，
            # 与改造前的回放形状逐字节一致。
            refs = harvest_refs(m.get("content"))
            if refs:
                ui_msg["refs"] = refs
            # 消息记录时间（jsonl created_at，秒级 ISO 本地时间；老行缺省）
            if m.get("created_at"):
                ui_msg["created_at"] = m["created_at"]
            ui.append(ui_msg)
        elif role == "assistant":
            tool_calls = []
            tc_ids: list[str] = []
            ask_users: list[dict] = []
            for tc in (m.get("tool_calls") or []):
                tc_ids.append(tc.get("id", "") if isinstance(tc, dict) else "")
                fn = (tc.get("function") or {})
                if fn.get("name", "") == "ask_user":
                    # 结构化提问**不进普通工具条**：由消息下的只读小结块承载。
                    # 问题 = 工具参数（questions），答案 = 配对上的 tool 行 content。
                    # 实时路径（ask_request / ask_resolved 事件）与回放路径展示的是
                    # **同一份 result_text**，所以前端不做任何解析、只原样展示 ——
                    # 这也消除了"实时/回放文案不一致"的风险。
                    # `result` 为空串表示该提问未完成（进程被杀等）。
                    # `status` 由 interaction.status_of_result 反推（jsonl 不存 outcome）
                    # —— 让前端只搬运、不猜文案，徽标文案的唯一真相留在 interaction 模块。
                    ask_users.append({
                        "tool_call_id": tc_ids[-1],
                        "args": fn.get("arguments", ""),
                        "result": tool_contents.get(tc_ids[-1], ""),
                        "status": status_of_result(tool_contents.get(tc_ids[-1], "")),
                    })
                    continue
                item = {
                    "name": fn.get("name", ""),
                    "args": fn.get("arguments", ""),
                }
                # 审批结算徽标（回放路径）：tool 行旁挂的 approval 按 id 配回该
                # 工具条（HistoryToolCall.approval，与实时 approval_resolved 旁挂
                # 的形状一致）；无字段 = 正常执行，按普通工具条渲染。
                ap = tool_approvals.get(tc_ids[-1])
                if ap:
                    item["approval"] = ap
                tool_calls.append(item)
            ui_msg = {
                "role": "assistant",
                "content": _text_of(m.get("content")),
                "thinking": m.get("reasoning_content") or "",
                "_tc_ids": tc_ids,
                "toolCalls": tool_calls,
            }
            if ask_users:
                # 只在确有 ask_user 时加字段 —— 无提问的 assistant 消息连字段都
                # 不多一个，与改造前的回放形状逐字节一致。
                ui_msg["askUsers"] = ask_users
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
    # 在途提问重放（2026-09-21）：turn 可能正阻塞在"等用户作答"上，新连接
    # （含断线重连）必须能把提问面板重建出来，否则用户再也看不到那张卡。
    ask_replay = _ask_snapshot_lines()
    if ask_replay:
        log.info("WS 在途提问重放: %d 条", len(ask_replay))
    for line in ask_replay:
        line_q.put_nowait(line)
    # 在途权限审批重放（2026-09-22 权限管控）：turn 可能正阻塞在"等用户审批"上，
    # 新连接（含断线重连）必须把审批卡片重建出来，否则只能干等到超时自结算。
    approval_replay = _approval_snapshot_lines()
    if approval_replay:
        log.info("WS 在途审批重放: %d 条", len(approval_replay))
    for line in approval_replay:
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
                    # 会话**真实产生**才刷新该空间 last_opened_at（侧边栏空间
                    # 排序的唯一刷新点，2026-09-22）：新建任务下拉切换 / 切会话
                    # 对齐活动空间都只走 project_open（set_active），不动排序。
                    await asyncio.to_thread(get_registry().touch_opened, pid)
                    # 沙箱根在**新建时**解析一次并固化进元数据（work_root 快照，
                    # 「会话建成即锁空间」的姊妹规则）：
                    # - default → ~/.aigent/projects/default/scratch（草稿区，
                    #   文件工具与 bash 同根；不再用仓库内 WorkSpace/task1）；
                    # - 自定义空间 → 选定的真实目录。
                    ws_session = (
                        default_scratch_paths()
                        if pid == DEFAULT_PROJECT_ID
                        else _workspace_of(pid)
                    )
                    await asyncio.to_thread(
                        sm.set_session_work_root, new_sid, str(ws_session.workdir)
                    )
                    log.info("新会话创建: session_%s (project=%s, work_root=%s, model=%s)",
                             sid, pid, ws_session.workdir,
                             payload.get("model_id") or "global-default")
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
                    # 纯附件消息（正文为空）用 `[附件] 文件名` 兜底、纯引用消息用
                    # `[引用] 文件名` 兜底，否则首轮无标题。
                    default_title = _default_session_title(
                        text or _attachment_title_hint(payload)
                        or ref_title_hint(payload.get("refs")))
                    if default_title:
                        await asyncio.to_thread(
                            sm.set_auto_title, new_sid, default_title, "trunc"
                        )
                    await reply_sessions()
                else:
                    # 会话建成即锁空间（2026-09-20）：已有会话的归属只认缓存与
                    # 磁盘探测；请求里的 project_id 仅对「新建」生效 —— 带了不一致
                    # 的值直接忽略并记日志。后端是锁的最终守卫，不能只靠前端把
                    # 下拉框藏起来。
                    pid = _SID_PROJECT.get(sid) or _project_of_session(sid)
                    if want_pid and want_pid != pid:
                        log.warning(
                            "chat 忽略 project_id=%s：会话 %s 已归属 %s"
                            "（会话建成即锁空间，归属不可变）", want_pid, sid, pid)
                    _SID_PROJECT[sid] = pid
                    ws_session = None  # 下方按 meta 的 work_root 快照解析
                # 按该会话**所属空间**取管理器（不能用 default 的实例，否则会
                # 读写错空间的同名文件 / 报"会话不存在"）
                sm = _ensure_session_manager(pid)
                # 沙箱根按会话快照解析：运行时首次构造时固化在 SessionRuntime 上
                # （get_or_create 对已存在的运行时忽略新值），热路径不重复读 meta。
                rt = registry.get(sid)
                if rt is None:
                    if ws_session is None:
                        meta = await asyncio.to_thread(sm.load_meta, sid)
                        ws_session = _workspace_for_session(pid, meta)
                    rt = registry.get_or_create(sid, workspace=ws_session)
                # ── 在途提问期间"直接发消息" = 放弃选择题，自由文本原样回填 ──
                # （2026-09-21）必须放在 `rt.busy` 守卫**之前**：提问期间 turn 正
                # 阻塞等待，busy=True，落到下面只会回一句"正在执行，请先停止"——
                # 而用户的真实意图恰恰是作答。回填成功后**不另起 turn**（原 turn
                # 拿到答案后在同一回合继续），所以这里 continue。
                # 纯附件 / 纯引用消息（text 为空）不拦截：落到 busy 守卫被拒，
                # 符合"作答必须打字"的直觉。
                if (rt.has_pending_interaction()
                        and isinstance(text, str) and text.strip()):
                    if await asyncio.to_thread(rt.resolve_ask_free_text, text):
                        log.info("chat 转作 ask_user 自由作答: session_%s", sid)
                        continue
                if rt.busy:
                    # 同会话并发 turn 拒绝：避免两线程同时写同一会话 jsonl
                    await safe_send(ws, _envelope("error", {
                        "msg": f"该会话 (session_{sid}) 正在执行，请先用停止按钮结束后再发送",
                    }))
                    continue
                # 沙箱根兜底：运行中运行时已固化自己的路径束（会话建成即锁空间），
                # 附件等按会话解析的目录都必须用它 —— 不能用模块级常量。
                if ws_session is None:
                    ws_session = getattr(rt, "workspace", None) or _workspace_for_session(
                        pid, await asyncio.to_thread(sm.load_meta, sid))
                # ── 附件归位（2026-09-20）────────────────────────────────
                # 前端只传 att_id 列表；正文与附件分字段（`text` 恒为 str —— 标题
                # 生成 / 首轮判定 / 日志切片全依赖这个前提）。草稿区 → 会话目录的
                # 迁移在**派发 turn 之前**完成，供本轮的发送边界展开读取。
                raw_atts = payload.get("attachments") or []
                attachment_records: list[dict] = []
                if raw_atts:
                    # 图片能力校验放在**迁移之前**：否则草稿已搬到会话目录却没人
                    # 引用，只能等 GC 回收，白占一份磁盘。
                    if (any(str(a.get("kind") or "") == "image"
                            for a in raw_atts if isinstance(a, dict))
                            and not _model_supports_image(
                                _effective_model_id(sm, sid, payload))):
                        await safe_send(ws, _envelope("error", {
                            "msg": "当前会话绑定的模型不支持图片输入，"
                                   "请切换到带「图片」能力的模型，或移除图片附件后重发",
                        }))
                        continue
                    try:
                        attachment_records = await asyncio.to_thread(
                            migrate_to_session, ws_session, raw_atts, sid)
                    except Exception as exc:  # noqa: BLE001 - 归位失败不阻断对话
                        log.error("附件归位失败（本轮按无附件继续）session=%s: %s: %s",
                                  sid, type(exc).__name__, exc, exc_info=True)
                        attachment_records = []
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
                # 消息 content：**无附件无引用时是纯字符串**（与改造前逐字节一致），
                # 否则才变成 [文本块 + 附件引用块 + 路径引用块...] 的多模态数组。
                # 两种块都只记元数据，真正的文件内容由发送边界（_model_messages）展开。
                user_query = build_user_content(text, attachment_records)
                # ── 引用挂载（2026-09-21）──────────────────────────────────
                # 与附件是两条独立通道：这里只挂「路径 + 类型」的中性块，
                # **不复制文件、不读内容**。规范化以磁盘为准（前端字段可伪造，
                # 伪造不出磁盘），越界/非法条目就地丢弃但不阻断发送。
                # 无 refs 时 attach_ref_blocks 原样返回同一个对象，上面那条
                # "逐字节一致"的保证因此不受影响。
                raw_refs = payload.get("refs") or []
                ref_records: list[dict] = []
                if raw_refs:
                    ref_records = normalize_refs(ws_session.workdir, raw_refs)
                    dropped = len(raw_refs) - len(ref_records)
                    if dropped > 0:
                        log.warning(
                            "chat 引用被丢弃 %d 条（越界/非法/重复）session=%s",
                            dropped, sid)
                    user_query = attach_ref_blocks(user_query, ref_records)
                # 标题素材：正文优先；纯附件用 `[附件] 文件名`、纯引用用 `[引用] 文件名`
                title_src = (text if text.strip()
                             else _attachment_title_hint(payload)
                             or ref_title_hint(payload.get("refs")))
                # 后台线程跑 turn；事件循环继续处理其它命令（切换 / 其它会话 / stop）
                log.info("chat 派发: session_%s text=%r attachments=%d refs=%d",
                         sid, text[:80], len(attachment_records), len(ref_records))
                turn_task = asyncio.create_task(
                    rt.start_turn(user_query, reasoning_effort=reasoning_effort,
                                  max_context=max_context)
                )
                if first_turn:
                    # 第一轮 run_turn 执行完之后，再调用一次大模型总结生成标题（≤20 字）
                    asyncio.create_task(
                        _finalize_title_after_turn(turn_task, sid, title_src, sm))

            elif kind == "attachment_stage":
                # 附件登记（2026-09-20）：前端（原生对话框 / 拖拽 / 剪贴板）拿到
                # 本地绝对路径后交到这里 —— **只传路径不传字节**。后端是与前端同机
                # 的进程，直接读盘即可；同时也避开了 websockets 默认 1 MiB 帧上限
                # （base64 内联图片必然超限）。
                paths = payload.get("paths") or []
                want_pid = str(payload.get("project_id") or "") or _active_project()
                if not _project_ready(want_pid):
                    await safe_send(ws, _envelope("error", {
                        "msg": "该工作空间的目录当前不可用（已被移动或删除），无法添加附件",
                    }))
                    continue
                if not paths:
                    await safe_send(ws, _envelope("attachments_staged", {
                        "items": [], "failed": [], "project_id": want_pid,
                    }))
                    continue
                try:
                    result = await asyncio.to_thread(
                        stage_attachments, _workspace_of(want_pid), paths)
                except Exception as exc:  # noqa: BLE001 - 桥层兜底，绝不打死连接
                    log.error("附件登记失败: %s: %s", type(exc).__name__, exc,
                              exc_info=True)
                    await safe_send(ws, _envelope("error", {"msg": f"附件登记失败：{exc}"}))
                else:
                    # 顺手做一次节流 GC：长期开着的窗口也能回收超期草稿/孤儿目录
                    try:
                        await asyncio.to_thread(
                            _gc_attachments_throttled, _workspace_of(want_pid))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("附件 GC 跳过: %s: %s", type(exc).__name__, exc)
                    await safe_send(ws, _envelope("attachments_staged", {
                        "items": result["items"],
                        "failed": result["failed"],
                        "project_id": want_pid,
                    }))

            elif kind == "refs_list":
                # 引用候选列表（2026-09-21）：前端输入 `@` 时**拉一次完整扁平列表**，
                # 之后按键在本地过滤（零延迟，取舍见 docs/frontend/13）。
                # 归属按 sid 优先（与其它按 sid 的操作同口径），否则用 payload 的
                # project_id，再缺省用当前活动空间。
                sid_req = str(payload.get("session_id") or "")
                if sid_req:
                    pid = _SID_PROJECT.get(sid_req) or _project_of_session(sid_req)
                else:
                    pid = str(payload.get("project_id") or "") or _active_project()
                try:
                    refs_payload = await _refs_payload(pid, sid_req)
                except Exception as exc:  # noqa: BLE001 - 桥层兜底，绝不打死连接
                    log.error("refs_list 失败: %s: %s", type(exc).__name__, exc,
                              exc_info=True)
                    await safe_send(ws, _envelope("error", {
                        "msg": f"读取工作空间文件失败：{exc}"}))
                    continue
                await safe_send(ws, _envelope("refs", refs_payload))

            elif kind == "file_read":
                # 右栏「文件」预览（2026-09-23，docs/frontend/19）：读工作空间内
                # 单个文件。点对点回执 `file_content`。
                # 越界 / 不存在 / 二进制 / 过大 / 超长 都从 `read_workspace_file`
                # 以**正常结果**返回（reason 或标志位），只有真异常才回 error 信封。
                raw_path = str(payload.get("path") or "")
                pid, ws_paths, reason, sid = await _resolve_rpanel_target(payload)
                if ws_paths is None:
                    await safe_send(ws, _envelope("file_content", file_content_disabled(
                        raw_path, project_id=pid, session_id=sid, reason=reason)))
                    continue
                try:
                    content = await asyncio.to_thread(
                        read_workspace_file, ws_paths.workdir, raw_path)
                except Exception as exc:  # noqa: BLE001 - 桥层兜底，绝不打死连接
                    log.error("file_read 失败: %s: %s", type(exc).__name__, exc,
                              exc_info=True)
                    await safe_send(ws, _envelope("error", {
                        "msg": f"读取文件失败：{exc}"}))
                    continue
                content["project_id"] = pid
                content["session_id"] = sid
                await safe_send(ws, _envelope("file_content", content))

            elif kind == "git_status":
                # 右栏「变更」面板的文件清单（2026-09-23）。非 git 仓库是常态而
                # 不是错误 → `available:false` + 人话 reason，前端渲染平级空态。
                pid, ws_paths, reason, sid = await _resolve_rpanel_target(payload)
                if ws_paths is None:
                    await safe_send(ws, _envelope("git_status", git_status_disabled(
                        project_id=pid, session_id=sid, reason=reason)))
                    continue
                try:
                    info = await asyncio.to_thread(
                        git_changes_status, ws_paths.workdir)
                except Exception as exc:  # noqa: BLE001
                    log.error("git_status 失败: %s: %s", type(exc).__name__, exc,
                              exc_info=True)
                    await safe_send(ws, _envelope("error", {
                        "msg": f"读取 git 状态失败：{exc}"}))
                    continue
                info["project_id"] = pid
                info["session_id"] = sid
                await safe_send(ws, _envelope("git_status", info))

            elif kind == "git_diff":
                # 单个文件的 diff（**只做单文件**：整仓 diff 会卡住十几秒）。
                # `path` 是**仓库相对路径**（`git_status` 回执的产出口径）。
                raw_path = str(payload.get("path") or "")
                staged = bool(payload.get("staged"))
                pid, ws_paths, reason, sid = await _resolve_rpanel_target(payload)
                if ws_paths is None:
                    await safe_send(ws, _envelope("git_diff", git_diff_disabled(
                        raw_path, project_id=pid, session_id=sid, staged=staged,
                        reason=reason)))
                    continue
                try:
                    info = await asyncio.to_thread(
                        git_changes_diff, ws_paths.workdir, raw_path, staged=staged)
                except Exception as exc:  # noqa: BLE001
                    log.error("git_diff 失败: %s: %s", type(exc).__name__, exc,
                              exc_info=True)
                    await safe_send(ws, _envelope("error", {
                        "msg": f"读取 diff 失败：{exc}"}))
                    continue
                info["project_id"] = pid
                info["session_id"] = sid
                await safe_send(ws, _envelope("git_diff", info))

            elif kind == "stop":
                # 仅停止当前显示会话正在执行的那一轮，其它会话不受影响
                sid = str(payload.get("session_id") or "")
                log.info("停止请求: session_%s", sid)
                rt = registry.get(sid)
                if rt is not None:
                    # request_stop 内部会先 cancel_all("stopped") 结算在途提问：
                    # 否则正阻塞在 ask_user 里的工作线程要等用户再点一次才醒。
                    rt.request_stop()

            elif kind == "ask_answer":
                # 提交选择题答案（2026-09-21）。**fire-and-forget，无回包**：
                # 结果由广播的 ask_resolved 事件驱动，前端据此清面板 + 落只读小结。
                # 刻意不走 request()/点对点回包 —— 主进程的 pending 表按 kind
                # FIFO 配对且无 id，同 kind 并发会串台、还会被广播信封误消费。
                # 幂等：迟到/重复提交由 broker 丢弃（resolve 返回 False）。
                sid = str(payload.get("session_id") or "")
                rid = str(payload.get("request_id") or "")
                rt = registry.get(sid) if registry is not None else None
                if rt is not None and sid and rid:
                    ok = await asyncio.to_thread(
                        rt.resolve_ask, rid, payload.get("answers") or [])
                    log.info("ask_answer: session_%s rid=%s -> %s", sid, rid,
                             "已结算" if ok else "丢弃（迟到/重复）")
                else:
                    log.warning("ask_answer 丢弃：未知会话或空 rid (sid=%s rid=%s)",
                                sid, rid)

            elif kind == "ask_cancel":
                # 用户点「取消」：按"未作答、请自行选默认方案继续"回填，
                # 同样 fire-and-forget（回执走 ask_resolved 广播）。
                sid = str(payload.get("session_id") or "")
                rid = str(payload.get("request_id") or "")
                rt = registry.get(sid) if registry is not None else None
                if rt is not None and sid and rid:
                    ok = await asyncio.to_thread(rt.cancel_ask, rid)
                    log.info("ask_cancel: session_%s rid=%s -> %s", sid, rid,
                             "已结算" if ok else "丢弃（迟到/重复）")
                else:
                    log.warning("ask_cancel 丢弃：未知会话或空 rid (sid=%s rid=%s)",
                                sid, rid)

            elif kind == "approval_answer":
                # 提交权限审批决定（2026-09-22 权限管控，docs/frontend/17）。
                # **fire-and-forget，无回包**：结果由广播的 approval_resolved
                # 事件驱动，前端据此清卡片 + 工具行落审批徽标。幂等：迟到/重复
                # 提交由 broker 丢弃（resolve 返回 False）。
                sid = str(payload.get("session_id") or "")
                rid = str(payload.get("request_id") or "")
                rt = registry.get(sid) if registry is not None else None
                if rt is not None and sid and rid:
                    ok = await asyncio.to_thread(
                        rt.resolve_approval, rid, payload.get("decision"))
                    log.info("approval_answer: session_%s rid=%s -> %s", sid, rid,
                             "已结算" if ok else "丢弃（迟到/重复）")
                else:
                    log.warning("approval_answer 丢弃：未知会话或空 rid (sid=%s rid=%s)",
                                sid, rid)

            elif kind == "session_permission":
                # 会话权限档位切换（两档：default / full_access，docs/frontend/17）。
                # - 会话在跑（rt + agent 已构造）：走 Agent.persist_permission_mode
                #   —— gate 内存即时生效 + 会话 meta + 空间索引「最后更改值」三写，
                #   之后本空间**新建会话**默认继承该值（用户需求 #4）；
                # - 会话未建/未跑：直写会话 meta + 空间索引 —— 会话创建时
                #   _restore_permission_state 会读到这份 meta 恢复档位。
                # 决定广播 permission_changed（source=user）：所有窗口同步切盾牌 chip。
                sid = str(payload.get("session_id") or "")
                mode = str(payload.get("mode") or "")
                if not sid or mode not in VALID_MODES:
                    log.warning("session_permission 丢弃：sid=%r mode=%r", sid, mode)
                    continue
                pid = _SID_PROJECT.get(sid) or _project_of_session(sid)
                _SID_PROJECT[sid] = pid
                rt = registry.get(sid) if registry is not None else None
                try:
                    if rt is not None and rt.agent is not None:
                        await asyncio.to_thread(
                            rt.agent.persist_permission_mode, mode)
                    else:
                        sm = _ensure_session_manager(pid)
                        await asyncio.to_thread(sm.set_session_permission, sid, mode)
                        await asyncio.to_thread(
                            get_registry().set_permission_mode, pid, mode)
                except Exception as e:  # 写失败：内存未动、广播不发，前端保持原档位
                    log.error("session_permission 失败: session_%s mode=%s: %s: %s",
                              sid, mode, type(e).__name__, e)
                    continue
                log.info("session_permission: session_%s(%s) -> %s", sid, pid, mode)
                hub.broadcast("permission_changed", {
                    "session_id": sid, "mode": mode, "source": "user"})

            elif kind == "project_permission":
                # 新建任务（无会话）态切换**目标工作空间**的权限档位
                # （docs/frontend/17 §5.2）：只写 projects.json 的「最后更改值」，
                # 作为该空间**新会话**的默认档位；已有会话不受影响（各自的
                # meta 已折叠）。成功后广播 projects 刷新 —— 新建任务态的
                # chip 选中态由 projects 广播驱动（没有会话号，不带
                # permission_changed）。
                pid = str(payload.get("project_id") or "")
                mode = str(payload.get("mode") or "")
                if not pid or mode not in VALID_MODES:
                    log.warning("project_permission 丢弃：pid=%r mode=%r", pid, mode)
                    continue
                try:
                    await asyncio.to_thread(
                        get_registry().set_permission_mode, pid, mode)
                except Exception as e:  # 未知空间/非法值：不广播，前端保持原档位
                    log.error("project_permission 失败: project=%s mode=%s: %s: %s",
                              pid, mode, type(e).__name__, e)
                    continue
                log.info("project_permission: %s -> %s", pid, mode)
                await reply_projects()

            elif kind == "status_query":
                # 前端主动拉取运行状态（渲染进程刷新/HMR 不重建 WS 连接，
                # 连接建立时的重放覆盖不到该场景）。回包走本连接的 writer
                # 队列，与其它事件同管道保序；无运行会话时回空（前端自然复位）。
                lines = _status_snapshot_lines()
                ask_lines = _ask_snapshot_lines()
                approval_lines = _approval_snapshot_lines()
                log.info("status_query: %d 个运行中会话, %d 条在途提问, %d 条在途审批",
                         len(lines), len(ask_lines), len(approval_lines))
                for line in lines:
                    line_q.put_nowait(line)
                # 在途提问一并重放：渲染进程刷新后提问面板要能重建
                for line in ask_lines:
                    line_q.put_nowait(line)
                # 在途审批一并重放：渲染进程刷新后审批卡片要能重建
                for line in approval_lines:
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
                        # 权限档位（2026-09-22）：前端据此恢复盾牌 chip 的选中态
                        "permission_mode": meta.get("permission_mode") or "default",
                        # 右侧面板状态（2026-09-23，docs/frontend/19）
                        "right_panel": meta.get("right_panel"),
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
                        # 权限档位（2026-09-22）：前端据此恢复盾牌 chip 的选中态
                        "permission_mode": meta.get("permission_mode") or "default",
                        # 右侧面板状态（2026-09-23，docs/frontend/19）
                        "right_panel": meta.get("right_panel"),
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

            elif kind == "session_ui":
                # 右侧面板状态落盘（2026-09-23，docs/frontend/19）。沿用 unread /
                # permission_mode 的载体（会话元数据）→ 切会话时随 session_history
                # 回传恢复，删会话时随 meta 一起消失，零新增清理逻辑。
                # **fire-and-forget，无回包**（同 ask_answer）：主进程的 pending 表
                # 按 kind FIFO 配对且无 id，同 kind 并发会串台；前端做 400ms 防抖
                # 合并上报，丢一两条只影响下一次恢复的精确度。
                # **不 reply_sessions()**：`list_sessions()` 的白名单刻意不含该字段，
                # 重播列表既无意义又会让每次点标签都多推一遍全部会话。
                sid = str(payload.get("session_id") or "")
                if not sid:
                    continue
                sm = _manager_for_session(sid)
                if not sm.get_session_file(sid).exists():
                    continue
                try:
                    await asyncio.to_thread(
                        sm.set_session_ui, sid, payload.get("ui"))
                except FileNotFoundError:
                    continue
                except Exception as exc:  # noqa: BLE001 - 落盘失败不影响会话可用性
                    log.error("session_ui 写入失败 session_%s: %s: %s",
                              sid, type(exc).__name__, exc, exc_info=True)

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
                # 附件随清空一并回收（归档/还原**不动**附件 —— 那是软删除语义）
                await _drop_session_attachments(sid)
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
                        # 附件目录级联删除（**只能信这个出口**：归档不动附件，
                        # 所以附件必须与"永久删除"同生共死，否则永久累积）
                        await _drop_session_attachments(sid)
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

            elif kind == "permission_config_get":
                # 设置页读：归一化配置 + 内置清单（只读展示）+ 文件位置/是否存在。
                # 内置清单由后端下发 —— 前端硬编码会造第二出处（见 permission.builtin_snapshot）。
                store = _permission_store()
                cfg = await asyncio.to_thread(store.load)
                await safe_send(ws, _envelope("permission_config", {
                    "config": cfg,
                    "builtin": builtin_snapshot(),
                    "path": str(store.path),
                    "exists": store.path.exists(),
                }))

            elif kind == "permission_config_save":
                # 整份覆盖式保存（不做字段级 patch）。只回执给发起窗口、**不广播** ——
                # 广播会把另一个窗口正在编辑的未保存 draft 冲掉（对比 permission_changed
                # 必须广播：盾牌 chip 常驻输入区，多窗口不一致是视觉事故）。
                raw = payload.get("config")
                store = _permission_store()
                if not isinstance(raw, dict):
                    await safe_send(ws, _envelope("error", {"msg": "权限配置格式非法"}))
                else:
                    try:
                        normalized, warnings = await asyncio.to_thread(
                            store.save_reporting, raw
                        )
                    except OSError as exc:
                        log.error("权限配置落盘失败: %s", exc)
                        await safe_send(ws, _envelope("permission_config", {
                            "config": store.load(),
                            "applied": False,
                            "warnings": [],
                            "msg": f"保存失败：{exc}",
                        }))
                    else:
                        refreshed = _refresh_permission_dirs()
                        log.info("权限配置已保存（额外目录刷新 %d 个在途会话）", refreshed)
                        await safe_send(ws, _envelope("permission_config", {
                            "config": normalized,
                            "applied": True,
                            "warnings": warnings,
                            "msg": "权限配置已保存并生效",
                        }))

            elif kind == "sandbox_config_get":
                # 沙盒设置页读：平台/后端状态 + 开关 + 两个模板文件内容。
                # 模板不存在（首次）先补默认，保证编辑器永远有内容可展示。
                status = await asyncio.to_thread(sandbox_mod.backend_status)
                seatbelt = await asyncio.to_thread(sandbox_mod.read_template, "seatbelt")
                bwrap = await asyncio.to_thread(sandbox_mod.read_template, "bwrap")
                await safe_send(ws, _envelope("sandbox_config", {
                    "platform": status["platform"],
                    "backend": status["backend"],
                    "backend_available": status["backend_available"],
                    "sandbox_enabled": sandbox_mod.sandbox_enabled(),
                    "seatbelt_profile": seatbelt,
                    "bwrap_args": bwrap,
                    "seatbelt_path": str(sandbox_mod.SEATBELT_FILE),
                    "bwrap_path": str(sandbox_mod.BWRAP_FILE),
                }))

            elif kind == "sandbox_config_save":
                # 字段部分更新：sandbox_enabled（开关热生效）/
                # seatbelt_profile、bwrap_args（模板覆写，缺占位符拒存）/
                # reset（恢复默认模板）。回执为权威源，前端以回执重绘。
                errors: list[str] = []
                enabled = payload.get("sandbox_enabled")
                if isinstance(enabled, bool):
                    await asyncio.to_thread(_save_sandbox_enabled, enabled)
                for field, tpl_kind in (("seatbelt_profile", "seatbelt"),
                                        ("bwrap_args", "bwrap")):
                    if field in payload:
                        content = payload.get(field)
                        if not isinstance(content, str):
                            errors.append(f"{field} 必须是字符串")
                            continue
                        try:
                            await asyncio.to_thread(
                                sandbox_mod.save_template, tpl_kind, content)
                        except ValueError as exc:
                            errors.append(str(exc))
                reset = payload.get("reset")
                if reset in ("seatbelt", "bwrap"):
                    await asyncio.to_thread(sandbox_mod.reset_template, reset)
                if errors:
                    await safe_send(ws, _envelope("error", {"msg": "；".join(errors)}))
                status = await asyncio.to_thread(sandbox_mod.backend_status)
                seatbelt = await asyncio.to_thread(sandbox_mod.read_template, "seatbelt")
                bwrap = await asyncio.to_thread(sandbox_mod.read_template, "bwrap")
                await safe_send(ws, _envelope("sandbox_config", {
                    "platform": status["platform"],
                    "backend": status["backend"],
                    "backend_available": status["backend_available"],
                    "sandbox_enabled": sandbox_mod.sandbox_enabled(),
                    "seatbelt_profile": seatbelt,
                    "bwrap_args": bwrap,
                    "seatbelt_path": str(sandbox_mod.SEATBELT_FILE),
                    "bwrap_path": str(sandbox_mod.BWRAP_FILE),
                    "applied": not errors,
                    "errors": errors,
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
    # 附件清理（启动一次，跨全部工作空间）：超期草稿 + 孤儿会话目录。
    # 放进线程执行：目录可能很多，不能拖慢 WS 首连接就绪。
    try:
        await asyncio.to_thread(_startup_attachment_gc)
    except Exception as exc:  # noqa: BLE001 - 清理失败绝不拦启动
        log.warning("附件启动清理失败: %s: %s", type(exc).__name__, exc)
    async with websockets.serve(handle, "127.0.0.1", PORT):
        log.info("ws_bridge 后端启动: WS server listening on 127.0.0.1:%d "
                 "(pid=%s, model=%s)", PORT, os.getpid(),
                 os.environ.get("OPENAI_MODEL_ID", "(none)"))
        await asyncio.Future()


if __name__ == "__main__":
    install_excepthooks()
    threading.Thread(target=_watch_parent, daemon=True).start()
    asyncio.run(main())