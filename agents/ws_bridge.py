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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
from execution_mode import (
    MODE_GOAL,
    MODE_NORMAL,
    MODE_PLAN,
    VALID_EXECUTION_MODES,
    plan_content_payload,
    read_plan_file,
)
from goal import GoalError, MAX_GOAL_LENGTH
from interaction import status_of_result
from llm_config import (
    caps_allow_image, fetch_remote_models, get_config, get_model_by_id,
    load_llm_config, resolve_model_window, save_config,
)
from logger import get_logger, install_excepthooks
from paths import (
    CHAT_HISTORY_DIR,
    DEFAULT_PROJECT_ID,
    MCP_PKGS_DIR,
    PLUGIN_MARKETS,
    SKILL_MARKETS,
    WorkspacePaths,
    default_scratch_paths,
    plan_display_path,
    plan_file_for_session,
    plan_relpath,
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
    resolve_within,
)
# 右栏「变更」面板（2026-09-23）：git 取数放在 Python 侧，唯一理由是**口径唯一** ——
# "当前工作空间根"只由 paths.WorkspacePaths 定义，让 Electron 再推一遍必然分叉。
from git_changes import diff_file as git_changes_diff
from git_changes import status as git_changes_status
# MCP 服务管理（2026-09-30）：设置页「MCP」面板（docs/frontend/23）。
# 桥层只做「读配置 / 写配置 / 一次性试连 / 转发市场」四件事；客户端、热重载、
# 破坏性门控全部复用既有 mcp_manager，不在这里重造。
# `_interpolate_value` 带下划线但是**故意的**：试连必须与运行时走同一套 ${VAR}
# 展开，否则会出现「测试通过、保存后连不上」这种最难查的偏差。
# 本地包安装（2026-10-07）：设置页「MCP → 本地包」（docs/frontend/23 §本地安装）。
#
# 与市场条目的分歧：市场只**翻译**成一条 `npx -y pkg@ver`，包由 npm 在首次连接时
# 隐式拉取；本地安装是真的把包装进 `~/.aigent/mcp/pkgs/<包@版本>/`，再把条目的
# `command` 指向包内 bin 的绝对路径。**下载与写配置刻意分成两步**：
#   · `mcp_pkg_install` 只下载 + 校验（与 `mcp_market_resolve` 的"纯翻译不落盘"同构），
#   · 写条目仍走既有的 `mcp_server_upsert`（复用热重载与校验，零重复逻辑）。
# 这样"装坏了"不会留下一条指向不存在文件的配置。
from mcp_installer import InstallError as McpInstallError
from mcp_installer import install as install_mcp_package
from mcp_installer import install_fail as install_mcp_fail
from mcp_installer import list_packages as list_mcp_packages
from mcp_installer import plan as plan_mcp_package
from mcp_installer import plan_fail as plan_mcp_fail
from mcp_installer import remove as remove_mcp_package
from mcp_installer import verify as verify_mcp_package
from mcp_manager import MCPServerSession
from mcp_manager import _interpolate_value as interpolate_mcp_config
from mcp_market import resolve as resolve_market_item
from mcp_market import search as search_market
from mcp_store import McpStore
# 技能 / 插件管理（2026-09-30）：设置页「技能」「插件」两个菜单（docs/frontend/24、25）。
# 与 MCP 侧完全同构：桥层只做「读配置 / 写配置 / 转发市场」；解析、抓取、落盘的
# 规则全部在 store / market 两个模块里，这里一行业务规则都不放。
#
# ⚠️ 注意 `skill_*` 与既有的 `skills` 命令**不是一回事**：`skills` 是给模型的
#    "技能清单文本"查询（保留原样，见下文 handle 里的分支），`skill_*` 是设置页的
#    管理面。两者命名刻意区分，别合并。
from plugin_market import DEFAULT_MARKET_ID as PLUGIN_DEFAULT_MARKET
from plugin_market import fetch_files as fetch_plugin_files
from plugin_market import list_markets as list_plugin_markets
from plugin_market import plan_fail as plugin_plan_fail
from plugin_market import remove_market as remove_plugin_market
from plugin_market import resolve as resolve_plugin_item
from plugin_market import search as search_plugin_market
from plugin_market import upsert_market as upsert_plugin_market
from plugin_store import WIRED_COMPONENTS as WIRED_PLUGIN_COMPONENTS
from plugin_store import PluginStore, list_files
from skill_market import DEFAULT_MARKET_ID as SKILL_DEFAULT_MARKET
from skill_market import fetch_files as fetch_skill_files
from skill_market import list_markets as list_skill_markets
from skill_market import plan_fail as skill_plan_fail
from skill_market import remove_market as remove_skill_market
from skill_market import resolve as resolve_skill_item
from skill_market import search as search_skill_market
from skill_market import upsert_market as upsert_skill_market
from skill_store import (
    MANIFEST_NAME as SKILL_MANIFEST_NAME,
    SkillStore,
    build_skill_md,
    load_sources_strict,
    read_skill_text,
)
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

# 计划批准后的续跑指令（`plan_approve` 在会话空闲时用它起一轮，2026-09-25）。
# 与其它"系统代发"不同，这里**刻意**是一条真实 user 消息：它要落进历史，模型
# 据此知道"计划已批准、可以写操作了"（撤销 plan 提醒另由
# `agent_full_v2._sync_execution_mode` 的 `plan-exited` 注入负责，二者互补）。
PLAN_APPROVED_RESUME_TEXT = (
    "[计划已批准] 用户批准了你的计划文书，现在开始按计划执行。"
    "可以正常使用 run_write / run_edit / bash 等写操作。"
)

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

    **失败语义（2026-09-24 加固）**：任何异常都抛给调用方（转成回执 errors），
    但绝不"兜底成 {} 再回写" —— config.json 是用户可能手改过的文件，读不出来就
    拒绝写，否则会把其余键**整份抹掉**（数据灾难）。env 只在落盘成功后才覆写，
    避免"内存里关着、磁盘上开着"的假一致。
    """
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(
                f"{CONFIG_FILE} 无法解析，已拒绝覆写以免抹掉其它配置：{e}"
            ) from e
        if not isinstance(data, dict):
            raise ValueError(f"{CONFIG_FILE} 顶层不是 JSON 对象，已拒绝覆写")
    data["SANDBOX_ENABLED"] = "1" if enabled else "0"
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.environ["SANDBOX_ENABLED"] = "1" if enabled else "0"
    log.info("沙盒开关已保存并生效: %s", enabled)


def _sandbox_payload_sync(applied: bool | None = None,
                          errors: list[str] | None = None) -> dict:
    """沙盒设置页回执载荷（get/save 共用，**逐字段兜底**）。

    为什么逐字段 try：`handle()` 的命令分发链**没有兜底 try/except**，这里抛出去会
    直接掀掉整条 WS 连接（表现为前端掉线重连）。所以任何一项探测失败都降级成中性值
    并记日志，回执照发（前端不至于永远停在"读取沙盒设置…"）。

    本函数会**阻塞**（含后端探测的 subprocess 试跑），只允许经
    `_sandbox_payload()` 在线程里调用，别在事件循环里直接调。
    """
    collected: list[str] = list(errors or [])
    try:
        status = sandbox_mod.backend_status()
    except Exception as exc:  # noqa: BLE001
        log.error("沙盒后端状态探测失败: %s: %s", type(exc).__name__, exc)
        status = {"platform": sys.platform, "backend": None,
                  "backend_available": False, "reason": "error"}
        collected.append(f"后端探测失败：{exc}")
    try:
        enabled = sandbox_mod.sandbox_enabled()
    except Exception as exc:  # noqa: BLE001
        log.error("沙盒开关读取失败: %s: %s", type(exc).__name__, exc)
        enabled = False
    templates: dict[str, str] = {}
    for field, tpl_kind in (("seatbelt_profile", "seatbelt"), ("bwrap_args", "bwrap")):
        try:
            templates[field] = sandbox_mod.read_template(tpl_kind)
        except Exception as exc:  # noqa: BLE001
            log.error("沙盒模板读取失败 %s: %s: %s", tpl_kind, type(exc).__name__, exc)
            templates[field] = ""
            collected.append(f"{tpl_kind} 模板读取失败：{exc}")
    payload = {
        "platform": status.get("platform") or sys.platform,
        "backend": status.get("backend"),
        "backend_available": bool(status.get("backend_available")),
        "reason": status.get("reason"),
        "sandbox_enabled": enabled,
        **templates,
        "seatbelt_path": str(sandbox_mod.SEATBELT_FILE),
        "bwrap_path": str(sandbox_mod.BWRAP_FILE),
    }
    if applied is not None:
        payload["applied"] = applied
        payload["errors"] = collected
    return payload


async def _sandbox_payload(applied: bool | None = None,
                           errors: list[str] | None = None) -> dict:
    """`_sandbox_payload_sync` 的异步外壳（探测含 subprocess 试跑，必须下线程）。"""
    return await asyncio.to_thread(_sandbox_payload_sync, applied, errors)


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


# ── MCP 服务管理（docs/frontend/23，2026-09-30）──────────────────────────
# 桥层职责：① 读写 ~/.aigent/mcp/mcp_servers.json（写入由 mcp_store 负责）；
#           ② 把各 runtime 的实时连接状态叠加到原始条目上（**UI 数据源是原始文件，
#              不是 mcp_manager.load_config 的过滤结果** —— 后者会把 enable:0 的
#              条目藏起来，于是"被禁用的条目永远看不见、也就无法重新启用"）；
#           ③ 一次性试连（不落盘、不登记）；④ 转发官方 registry 市场。
#
# ⚠️ 本节刻意留在 `_text_of` 定义**之前**（与上方权限/沙盒辅助函数同理）：
# 从 `_text_of` 到 `handle` 之间的源码会被 tests 切片 exec，落在切片外零维护成本。

def _mcp_store() -> McpStore:
    """设置页专用 store 实例。

    与 `_permission_store()` 同理：**不要**复用某个 Agent 持有的东西 —— MCPManager
    是 per-Agent 实例（agent_full_v2.py:423），而设置页可能在任何 Agent 构造之前
    就被打开。McpStore 无状态门面，多实例指向同一对文件自然一致。
    """
    return McpStore()


def _mcp_managers() -> list:
    """所有**可观测**的 mcp_manager：全局 Agent + 每个已构造的会话 runtime。

    为什么必须带上全局 Agent（`agent`）：它在 `Agent.__init__` 里就
    `connect_all()` 了（agent_full_v2.py:423-430）。只看 `registry.all_runtimes()`
    会把"用户刚在设置页启用一条、还没开会话"显示成**未连接** —— 而真实连接其实
    已经建立，用户会以为没生效。按 `id()` 去重，防止将来全局 Agent 进了 registry
    被算两次。
    """
    out: list = []
    seen: set[int] = set()

    def _add(holder) -> None:
        mgr = getattr(holder, "mcp_manager", None)
        if mgr is not None and id(mgr) not in seen:
            seen.add(id(mgr))
            out.append(mgr)

    try:
        _add(agent)
    except Exception as exc:  # noqa: BLE001 - 构造失败不该掀掉状态汇总
        log.warning("MCP 状态汇总：全局 Agent 不可用 %s: %s", type(exc).__name__, exc)
    if registry is None:
        return out
    try:
        runtimes = registry.all_runtimes()
    except Exception as exc:  # noqa: BLE001 - 取列表失败不该掀掉整条命令
        log.warning("MCP 状态汇总：取运行时列表失败 %s: %s", type(exc).__name__, exc)
        return out
    for rt in runtimes:
        _add(getattr(rt, "agent", None))
    return out


def _mcp_runtime_snapshot() -> tuple[dict, int]:
    """汇总各 runtime 的连接状态 → `({name: {connected, tools, error?}}, 会话数)`。

    多实例（每个会话一个 Agent、各持一份 MCPManager）时取**并集**：任一 runtime
    连上就算已连接，工具清单取最长的那份；错因只在"谁都没连上"时才回传。
    """
    agg: dict[str, dict] = {}
    errors: dict[str, str] = {}
    managers = _mcp_managers()
    for mgr in managers:
        try:
            catalog = mgr.catalog()          # {name: [tool]}，未连接的是空列表
        except Exception as exc:  # noqa: BLE001 - 单个 manager 坏了不影响其余
            log.warning("MCP 目录读取失败：%s: %s", type(exc).__name__, exc)
            continue
        for name, tools in catalog.items():
            cur = agg.setdefault(name, {"connected": False, "tools": []})
            if tools:
                cur["connected"] = True
                if len(tools) > len(cur["tools"]):
                    cur["tools"] = list(tools)
                errors.pop(name, None)
                continue
            try:
                err = mgr.last_error(name)
            except Exception:  # noqa: BLE001 - 兼容没有该方法的旧 manager
                err = None
            if err and name not in errors:
                errors[name] = err
    for name, err in errors.items():
        cur = agg.setdefault(name, {"connected": False, "tools": []})
        if not cur["connected"]:
            cur["error"] = err
    return agg, len(managers)


def _mcp_config_payload_sync(applied: bool | None = None,
                            errors: list[str] | None = None,
                            warnings: list[str] | None = None,
                            msg: str = "",
                            pkg_action: dict | None = None) -> dict:
    """MCP 设置页回执载荷（get/save/remove 共用，**逐字段兜底**）。

    为什么逐字段 try：`handle()` 的命令分发链**没有兜底 try/except**，这里抛出去会
    直接掀掉整条 WS 连接。任何一项探测失败都降级成中性值，回执照发 —— 否则前端
    会永远停在"读取 MCP 配置…"（权限页与沙盒页都踩过这个"塌成加载态"）。

    `status` 五态（前端据此渲染状态点）：
      - `disabled`     条目 enable=0，不参与连接
      - `connected`    至少一个 runtime 已连上
      - `error`        有确切的失败原因（来自 mcp_manager.last_error）
      - `idle`         当前没有任何会话（MCPManager 是 per-Agent），还没机会连
      - `disconnected` 有会话、没连上、也没留下错因（例如刚写盘、下一轮才 reconcile）
    """
    store = _mcp_store()
    collected: list[str] = list(errors or [])
    try:
        servers, read_errors = store.list_servers()
        collected.extend(read_errors)
    except Exception as exc:  # noqa: BLE001 - 读配置失败不能拦回执
        log.error("MCP 配置读取失败：%s: %s", type(exc).__name__, exc)
        servers, collected = [], collected + [f"读取配置失败：{exc}"]
    try:
        snapshot, session_count = _mcp_runtime_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.error("MCP 状态汇总失败：%s: %s", type(exc).__name__, exc)
        snapshot, session_count = {}, 0
        collected.append(f"状态汇总失败：{exc}")

    for item in servers:
        state = snapshot.get(item["name"]) or {}
        if not item.get("enable"):
            item["status"] = "disabled"
        elif state.get("connected"):
            item["status"] = "connected"
        elif state.get("error"):
            item["status"] = "error"
        elif session_count == 0:
            item["status"] = "idle"
        else:
            item["status"] = "disconnected"
        item["tools"] = list(state.get("tools") or [])
        item["tool_count"] = len(item["tools"])
        item["last_error"] = state.get("error")

    # 本地已安装包（docs/frontend/23 §本地安装）。**放在同一份回执里**而不是另开
    # 一条命令：设置页打开时两边都要显示，分两次往返会出现"条目列表已刷新、本地包
    # 列表还是旧的"这种中间态。扫目录很便宜（通常个位数条目）。
    try:
        packages = list_mcp_packages()
    except Exception as exc:  # noqa: BLE001 - 扫描失败不能拦回执
        log.error("MCP 本地包列表读取失败：%s: %s", type(exc).__name__, exc)
        packages = []
        collected.append(f"本地包列表读取失败：{exc}")
    # 反向引用：哪些条目正在用这个包（卸载前要提醒用户，否则删完条目就起不来了）
    used: dict[str, list[str]] = {}
    for s in servers:
        pkg = s.get("pkg") if isinstance(s.get("pkg"), dict) else None
        if pkg and pkg.get("slug"):
            used.setdefault(str(pkg["slug"]), []).append(s["name"])
    for p in packages:
        p["referenced_by"] = used.get(p["slug"], [])

    payload: dict = {
        "path": str(store.path),
        "sources_path": str(store.sources_path),
        "exists": store.path.exists(),
        "servers": servers,
        "packages": packages,
        "pkgs_dir": str(MCP_PKGS_DIR),
        "sessions": session_count,
        "summary": {
            "total": len(servers),
            "enabled": sum(1 for s in servers if s.get("enable")),
            "connected": sum(1 for s in servers if s.get("status") == "connected"),
            "packages": len(packages),
        },
    }
    if applied is not None:
        payload["applied"] = applied
        payload["warnings"] = list(warnings or [])
    if collected:
        payload["errors"] = collected
    if msg:
        payload["msg"] = msg
    # 本地包动作（install / remove / verify）的结果挂在这一个字段上 —— 让回执保持
    # **一条**（`mcp_config` 是"整份替换"语义，前端只认这一种形状），而不是为每个
    # 动作新开一个信封再各写一遍 store 替换逻辑。
    if pkg_action is not None:
        payload["pkg_action"] = pkg_action
    return payload


async def _mcp_config_payload(applied: bool | None = None,
                             errors: list[str] | None = None,
                             warnings: list[str] | None = None,
                             msg: str = "",
                             pkg_action: dict | None = None) -> dict:
    """`_mcp_config_payload_sync` 的异步外壳（读文件 + 汇总状态都要下线程）。"""
    return await asyncio.to_thread(
        _mcp_config_payload_sync, applied, errors, warnings, msg, pkg_action)


def _reload_one_manager(mgr) -> bool:
    """单个 manager 热重载；返回是否成功（异常已被吞掉记日志）。"""
    try:
        mgr.maybe_reload()
        return True
    except Exception as exc:  # noqa: BLE001 - 单个 runtime 坏了不影响其余
        log.warning("MCP 热重载失败：%s: %s", type(exc).__name__, exc)
        return False


def _reload_mcp_all_runtimes() -> int:
    """配置落盘后让所有已构造 runtime 立即 reconcile（mtime 已变 → 精确增删改）。

    复用既有 `maybe_reload()`，**不需要新增"重新加载"接口**。未构造的会话不用管：
    `agent_full_v2.py` 构造 Agent 时 `connect_all()` 自然读到新配置。返回触发过的
    runtime 数（仅日志用）；任何异常都不得影响保存回执。

    ⚠️ **必须并发，不能串行**（2026-10-08 修，实测「开关点了没反应」的根因之二）。
    `maybe_reload()` 内部对每条启用中的条目做 connect 握手，单条上限
    `MCP_CONNECT_TIMEOUT`（默认 15s）。串行时耗时是**各 manager 累加**：
    全局 Agent + N 个会话 runtime，2 个 manager × (cgc 连不上 15s + zotero 0.03s)
    = **30s+，正好撞上主进程 `mcpServerUpsert` 的 30s 超时** → promise 回 null →
    回执被丢弃、`mcpSaving` 复位但列表状态不更新 → 用户看到"点了没反应"。
    并发后总耗时 = 最慢的那一个（≈15s），与 manager 个数无关。

    `maybe_reload()` 逐 manager 加锁保护 `_config` 之外的状态，本身无跨 manager
    共享可变状态，故并发安全（每个 manager 各自读同一份配置文件，只读无写）。
    """
    managers = _mcp_managers()
    if not managers:
        return 0
    t0 = time.time()
    if len(managers) == 1:
        n = 1 if _reload_one_manager(managers[0]) else 0
    else:
        with ThreadPoolExecutor(max_workers=min(len(managers), 8),
                                 thread_name_prefix="mcp-reload") as pool:
            # 一次性提交全部 future 再等，避免逐个 submit+result 退化成串行
            futures = [pool.submit(_reload_one_manager, m) for m in managers]
            n = sum(1 for f in futures if f.result())
    # 这次热重载是整条 upsert 回执里最慢的一段（要真做 connect 握手），
    # 必须单独记耗时：主进程 mcpServerUpsert 的超时是 30s，一旦逼近它，
    # 前端表现就是"开关点了没反应"（promise 回 null、回执被丢弃）。
    log.info("MCP 热重载完成：%d/%d 个 runtime，耗时 %.2fs", n, len(managers),
             time.time() - t0)
    return n


# 定点重连的进程级锁。**必须跨 manager 串行化**（2026-10-08 补）。
# 试连触发的重连与对话轮的 `maybe_reload()`、与 upsert 触发的热重载可能同时落在
# 同一个 manager 上，而 `MCPManager` 自身对 `_clients` **没有任何锁**：
# 两个线程各自跑 `connect()` 时，后写的直接覆盖 `_clients[name]`，先建的那个
# session 失去引用 → **stdio 子进程泄漏**。泄漏的子进程若持独占文件锁
# （cgc 的 kuzu），后续所有连接尝试必然失败，且看不出与本次改动有关。
# 粒度取"单条目重连"而非"整个 manager"：锁内只有一次 connect 握手。
_mcp_reconnect_lock = threading.Lock()


def _mcp_reconnect_one_sync(name: str) -> dict:
    """试连成功后，让**所有**已构造 runtime 真正把这个条目连上。

    为什么**不能**复用 `_reload_mcp_all_runtimes()`（2026-10-08）：
    `maybe_reload()` 头一件事是比 `mtime`，没变就 `return ""`（mcp_manager.py:586）。
    试连刻意**不落盘** → 文件 mtime 不变 → 整个热重载是**空转**，一个连接都不会
    重发。这正是"关一下再打开开关就好了"的机制（那一次真写了盘）。
    所以这里必须自己调 `connect(name)`，点名重连，不依赖 mtime。

    三个必须守住的边界：
    1. **已连接的直接跳过** —— `connect()` 虽有 `if name in self._clients: return`
       的幂等保护，但探测本身不花时间、真正贵的是漏进去那次白跑的 15s 预算。
    2. **取并集语义** —— 任一 runtime 连上就算成功（与 `_mcp_runtime_snapshot`
       的状态口径一致）；逐个 manager 的错因只进日志，不回传（回执里的 `error`
       已经被试连结论占用了）。
    3. **绝不上抛** —— 调用点在 WS 命令分发链上，抛出去会掀掉整条连接。

    返回 `{refreshed, connected, skipped, total}`：`refreshed` 供回执区分
    "试连通过但没触发重连"（草稿 / 禁用条目）与"已同步到运行时"。
    """
    started = time.time()
    if not name:
        return {"refreshed": False, "connected": 0, "skipped": 0, "total": 0}
    try:
        managers = _mcp_managers()
    except Exception as exc:  # noqa: BLE001 - 取列表失败不该让试连结论崩掉
        log.warning("MCP 定点重连：取运行时列表失败 %s: %s", type(exc).__name__, exc)
        return {"refreshed": False, "connected": 0, "skipped": 0, "total": 0}

    connected = 0
    skipped = 0
    for mgr in managers:
        try:
            # 已在连接中：算"已同步"，不重复握手。
            if name in mgr.connected_names():
                skipped += 1
                connected += 1
                continue
        except Exception as exc:  # noqa: BLE001 - 兼容没有该方法的旧 manager
            log.warning("MCP 定点重连：读取连接表失败 %s: %s", type(exc).__name__, exc)
            continue
        try:
            with _mcp_reconnect_lock:
                result = mgr.connect(name)
            # ⚠️ 判据必须是「**登记成功**」而不是「没报错」：`connect()` 对未知条目
            # 返回的是 `Unknown server 'x'. Available: ...`，**不以 `MCP error`
            # 开头**（mcp_manager.py:493）。只判 `startswith("MCP error")` 会把
            # "条目根本没进这个 manager 的配置"误计成已连接 → `refresh_connected`
            # 虚报、前端显示「已同步到运行时（1 个运行时）」而状态其实没变。
            # 权威判据只有一个：它有没有进 `_clients`。
            if name in mgr.connected_names():
                connected += 1
            else:
                # 典型场景：cgc 这类持进程级独占锁的 stdio 服务器，别的 runtime
                # 还持着锁 → 这里必然超时失败。这是**先验事实不是 bug**
                # （多 manager 并存时只有一个能连上），状态显示 error 是诚实的。
                log.warning("MCP 定点重连未成功：%s → %s", name, result)
        except Exception as exc:  # noqa: BLE001 - 单个 manager 坏了不影响其余
            log.warning("MCP 定点重连异常：%s %s: %s", name, type(exc).__name__, exc)
    if connected:
        log.info("MCP 定点重连完成：%s → %d/%d 个 runtime 已连接（%d 个原本就在连接），"
                 "耗时 %.2fs", name, connected, len(managers), skipped,
                 time.time() - started)
    return {"refreshed": True, "connected": connected, "skipped": skipped,
            "total": len(managers)}


def _mcp_test_sync(config: dict) -> dict:
    """一次性试连：起临时 session，握手 + 列工具，然后**立刻拆掉**。

    三条硬约束（改之前先读）：
    1. **不落盘、不登记** —— 只在临时对象上试，绝不碰 `mcp_servers.json`，
       也不进任何 runtime 的 `_clients`（否则会污染模型工具池）。
    2. **必须在 `asyncio.to_thread` 里跑** —— `start()` 要等握手完成，最长
       `MCP_CONNECT_TIMEOUT`（默认 15s）；在事件循环里同步等会让所有会话的
       流式事件一起卡住。
    3. **绝不上抛** —— `handle()` 无兜底 try，抛出去会掀掉整条 WS 连接。

    注意取值顺序：`stop()` 会把 `_session`/`_tools` 重置为空，所以 `ready`、
    `tools`、`resources` **必须在 stop() 之前**读出来。
    """
    started = time.time()
    try:
        errors = _mcp_store().validate_config(config)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"校验失败：{exc}", "tools": [],
                "tool_count": 0, "resource_count": 0, "elapsed_ms": 0}
    if errors:
        return {"ok": False, "error": "；".join(errors), "tools": [],
                "tool_count": 0, "resource_count": 0, "elapsed_ms": 0}

    session = MCPServerSession("__test__", interpolate_mcp_config(dict(config)))
    summary = ""
    ok = False
    tools: list[str] = []
    resource_count = 0
    try:
        summary = session.start()
        ok = session.ready
        if ok:
            tools = list(session.tool_names)
            resource_count = len(session.resources)
    except Exception as exc:  # noqa: BLE001
        summary = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            session.stop()
        except Exception as exc:  # noqa: BLE001 - 清理失败不该盖掉试连结论
            log.warning("MCP 试连清理失败：%s: %s", type(exc).__name__, exc)
    return {
        "ok": ok,
        "error": "" if ok else (summary or "连接失败（未返回原因）"),
        "tools": tools,
        "tool_count": len(tools),
        "resource_count": resource_count,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


# ── 技能 / 插件管理（docs/frontend/24、25，2026-09-30）────────────────────
# 与 MCP 一节完全同构（见上）：UI 数据源是**磁盘扫描结果**（含被禁用项），
# 而不是 `SkillLoader.SKILL_REGISTRY` —— 后者只收启用的技能，拿它当列表数据源
# 会让"被禁用的技能永远看不见、也就无法重新启用"（同 mcp_store 的坑，23 篇 §1.4）。
#
# ⚠️ 本节同样必须留在 `_text_of` **之前**（理由见上一节的注释）。

def _skill_store() -> SkillStore:
    """设置页专用技能 store（无状态门面，多实例指向同一份磁盘自然一致）。"""
    return SkillStore()


def _plugin_store() -> PluginStore:
    return PluginStore()


def _skill_config_payload_sync(applied: bool | None = None,
                               errors: list[str] | None = None,
                               warnings: list[str] | None = None,
                               msg: str = "") -> dict:
    """技能设置页回执载荷（**逐字段兜底**）。

    ⚠️ 为什么逐字段 try：`handle()` 的命令分发链**没有兜底 try/except**，这里抛出去
    会直接掀掉整条 WS 连接。任何一项探测失败都降级成中性值、回执照发 —— 否则前端
    会永远停在"读取技能配置…"。

    「读失败 ≠ 没有技能」这条必须能区分：`skills: [] + errors` 时前端渲染的是
    "配置读取失败"，而不是"还没有任何技能"（后者会诱导用户去市场重装，反而把磁盘上
    还在的技能覆盖掉，同 23 篇 §4.2-3）。
    """
    store = _skill_store()
    plugin_store = _plugin_store()
    collected = list(errors or [])

    skills: list[dict] = []
    try:
        skills = store.scan()
    except Exception as exc:  # noqa: BLE001
        log.error("技能目录扫描失败：%s: %s", type(exc).__name__, exc)
        collected.append(f"读取技能目录失败：{exc}")
    try:
        # 旁路元数据损坏 → 启停状态全丢。必须让用户看见，不能静默当作"全部启用"。
        load_sources_strict(store.sources_path)
    except Exception as exc:  # noqa: BLE001
        collected.append(f"技能元数据文件损坏（启停状态可能不准）：{exc}")
    try:
        if store.skills_dir.exists() and not os.access(store.skills_dir, os.R_OK):
            collected.append(f"技能目录不可读：{store.skills_dir}")
    except Exception as exc:  # noqa: BLE001
        collected.append(f"技能目录探测失败：{exc}")

    try:
        markets = list_skill_markets()
    except Exception as exc:  # noqa: BLE001
        log.error("技能源列表读取失败：%s: %s", type(exc).__name__, exc)
        markets = []
        collected.append(f"读取技能源失败：{exc}")

    contributed = 0
    try:
        contributed = len(plugin_store.iter_skill_manifests())
    except Exception as exc:  # noqa: BLE001 - 插件侧坏掉不该让技能页整体失败
        log.warning("插件技能统计失败：%s: %s", type(exc).__name__, exc)

    payload: dict = {
        "dir": str(store.skills_dir),
        "sources_path": str(store.sources_path),
        # 源注册表的落点**从常量取**（不写死文件名 —— 写死就与 paths 里那份重复了，
        # 改常量时这里会静默指向另一个文件）
        "markets_path": str(SKILL_MARKETS),
        "exists": store.skills_dir.exists(),
        "skills": skills,
        "markets": markets,
        "default_market": SKILL_DEFAULT_MARKET,
        "plugin_skill_count": contributed,
        "summary": {
            "total": len(skills),
            "enabled": sum(1 for s in skills if s.get("enabled")),
            "from_market": sum(1 for s in skills if s.get("source") == "market"),
            "invalid": sum(1 for s in skills if not s.get("has_manifest")),
        },
    }
    if applied is not None:
        payload["applied"] = applied
        payload["warnings"] = list(warnings or [])
    if collected:
        payload["errors"] = collected
    if msg:
        payload["msg"] = msg
    return payload


async def _skill_config_payload(applied: bool | None = None,
                                errors: list[str] | None = None,
                                warnings: list[str] | None = None,
                                msg: str = "") -> dict:
    """`_skill_config_payload_sync` 的异步外壳（扫目录 + 读元数据都要下线程）。"""
    return await asyncio.to_thread(
        _skill_config_payload_sync, applied, errors, warnings, msg)


def _plugin_config_payload_sync(applied: bool | None = None,
                                errors: list[str] | None = None,
                                warnings: list[str] | None = None,
                                msg: str = "") -> dict:
    """插件设置页回执载荷（逐字段兜底，理由同上）。"""
    store = _plugin_store()
    collected = list(errors or [])
    plugins: list[dict] = []
    try:
        plugins = store.scan()
    except Exception as exc:  # noqa: BLE001
        log.error("插件目录扫描失败：%s: %s", type(exc).__name__, exc)
        collected.append(f"读取插件目录失败：{exc}")
    try:
        load_sources_strict(store.sources_path)
    except Exception as exc:  # noqa: BLE001
        collected.append(f"插件元数据文件损坏（启停状态可能不准）：{exc}")

    try:
        markets = list_plugin_markets()
    except Exception as exc:  # noqa: BLE001
        log.error("插件市场列表读取失败：%s: %s", type(exc).__name__, exc)
        markets = []
        collected.append(f"读取插件市场失败：{exc}")

    wired = 0
    try:
        wired = len(store.iter_skill_manifests())
    except Exception as exc:  # noqa: BLE001
        log.warning("插件技能统计失败：%s: %s", type(exc).__name__, exc)

    payload: dict = {
        "dir": str(store.plugins_dir),
        "sources_path": str(store.sources_path),
        "markets_path": str(PLUGIN_MARKETS),
        "exists": store.plugins_dir.exists(),
        "plugins": plugins,
        "markets": markets,
        "default_market": PLUGIN_DEFAULT_MARKET,
        "wired_components": list(WIRED_PLUGIN_COMPONENTS),
        "contributed_skill_count": wired,
        "summary": {
            "total": len(plugins),
            "enabled": sum(1 for p in plugins if p.get("enabled")),
            "invalid": sum(1 for p in plugins if not p.get("has_manifest")),
            "contributing": sum(1 for p in plugins
                                if p.get("enabled") and p.get("components", {}).get("skills")),
        },
    }
    if applied is not None:
        payload["applied"] = applied
        payload["warnings"] = list(warnings or [])
    if collected:
        payload["errors"] = collected
    if msg:
        payload["msg"] = msg
    return payload


async def _plugin_config_payload(applied: bool | None = None,
                                 errors: list[str] | None = None,
                                 warnings: list[str] | None = None,
                                 msg: str = "") -> dict:
    return await asyncio.to_thread(
        _plugin_config_payload_sync, applied, errors, warnings, msg)


def _reload_skills_all_runtimes() -> int:
    """技能/插件变更后，重建所有**已构造** runtime 的 system prompt。

    为什么必须做这一步：`Agent._refresh_system_prompt()` 只在**会话入口**（init /
    switch / new）被调用，不是每轮都跑 —— 也就是说"装了新技能，当前会话看不到"
    是个真实存在过的坑（agent_full_v2.py:1930 的注释就是为它写的）。刚在设置页装完
    技能，用户切回对话就期望它生效，所以这里主动触发一次重建。

    代价是**整段前缀缓存失效一次**（同该方法的 docstring 所述）—— 但这只在技能列表
    真的变过时才发生，而"改了技能表"本来就必须让模型看到新内容。未构造的会话不用管：
    首次构造时自然读到新状态。

    全程 try/except：刷新失败绝不能影响保存回执（同 `_reload_mcp_all_runtimes`）。
    返回触发重建的 runtime 数（仅日志用）。
    """
    holders: list = []
    try:
        holders.append(agent)
    except Exception as exc:  # noqa: BLE001
        log.warning("技能热刷新：全局 Agent 不可用 %s: %s", type(exc).__name__, exc)
    if registry is not None:
        try:
            holders.extend(getattr(rt, "agent", None) for rt in registry.all_runtimes())
        except Exception as exc:  # noqa: BLE001
            log.warning("技能热刷新：取运行时列表失败 %s: %s", type(exc).__name__, exc)
    n = 0
    seen: set[int] = set()
    for holder in holders:
        if holder is None or id(holder) in seen:
            continue
        seen.add(id(holder))
        refresh = getattr(holder, "_refresh_system_prompt", None)
        if not callable(refresh):
            continue
        try:
            refresh()
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("技能热刷新失败：%s: %s", type(exc).__name__, exc)
    return n


def _skill_read_sync(name: str) -> dict:
    """读单个技能的 SKILL.md 全文（设置页「查看正文」，**不落盘**）。"""
    store = _skill_store()
    item = store.get(name)
    if item is None:
        return {"name": name, "text": "", "error": f"没有名为「{name}」的技能"}
    return {
        "name": item["name"],
        "text": read_skill_text(item["manifest"]),
        "path": item["manifest"],
        "dir": item["path"],
        "description": item["description"],
        "files": item["files"],
        "error": "",
    }


def _plugin_read_sync(name: str) -> dict:
    """读单个插件的 plugin.json 原文 + 文件清单（设置页「查看详情」）。"""
    store = _plugin_store()
    item = store.get(name)
    if item is None:
        return {"name": name, "plugin_json": "", "files": [], "error":
                f"没有名为「{name}」的插件"}
    text = read_skill_text(item["manifest_path"])
    return {
        "name": item["name"],
        "plugin_json": text,
        "path": item["path"],
        "manifest_path": item["manifest_path"],
        "components": item["components"],
        "files": list_files(item["path"]),
        "error": "",
    }


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


# ── 计划文书（任务执行模式，docs/frontend/22）────────────────────────────
# 2026-09-29 起文书落在**工作空间内**：`<工作空间根>/.aiagent/plan/<模型命名>.md`
# （名字由模型给、目录由后端强制，口径唯一在 `paths.plan_filename` /
# `paths.plan_dir_for`）。因此：
#   · `plan_path` 对外是**相对工作空间**的路径（右栏标签 / `file_read` 的口径），
#     由 meta 的 `plan_name` 拼出；
#   · `plan_read` 按 meta 的记录解析（不再由 sid 推路径），并用 `resolve_within`
#     兜一道 —— meta 是可手改的，绝不允许它把读取引到工作空间之外；
#   · 存量会话（meta 无 `plan_name`）回退旧的元数据目录口径，照样能读。

def _exec_mode_fields(sm, sid: str, meta: dict) -> dict:
    """`session_history` 的执行模式字段（**两处构造点共用**，避免口径分叉）。

    `plan_path` 的判据在 `paths.plan_display_path`（**三个出口共用同一个函数**：
    本处 / `Agent.execution_state` / `SessionManager.list_sessions`）—— 新口径给
    相对工作空间的 `.aiagent/plan/<name>.md`，存量会话回退旧的元数据目录绝对路径。
    前端据此重建计划卡片外壳与右栏标签，正文仍经 `plan_read` 拉取。

    `goal_round` / `goal_started_at`（2026-09-30 目标可见化）：常驻目标条要在
    切会话后显示正确的「第 N 轮」与已运行时长。语义与 `goal_condition` 一样是
    **投影**（真相恒为 `GoalController.active`），active 为空时 meta 里是 None。
    """
    meta = meta if isinstance(meta, dict) else {}
    return {
        "execution_mode": meta.get("execution_mode") or "normal",
        "plan_status": meta.get("plan_status"),
        "plan_path": plan_display_path(
            meta.get("plan_status"), meta.get("plan_name"),
            str(sm.plan_file_for(sid))),
        "goal_condition": meta.get("goal_condition"),
        "goal_round": meta.get("goal_round"),
        "goal_started_at": meta.get("goal_started_at"),
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


def _goal_condition_from_message(raw: str, text: str, payload: dict) -> str:
    """目标模式的**首条指令即目标**（2026-09-30，docs/frontend/22 §2.6）。

    点「目标模式」不再弹框问条件：前端只做**武装**（本地态），条件取「下一条指令
    的正文」，随同一条 `chat` 送到（`exec_condition`）。正文为空（纯附件 / 纯引用）
    时用 `[附件] 文件名` / `[引用] 文件名` 兜底 —— 与默认标题同一口径，**绝不产生
    空条件**（`GoalController.set_goal` 对空条件抛 `GoalError`）。

    `raw` 是前端**显式**给的条件（保留给旧客户端 / 未来"手动改目标"入口），非空时
    优先于消息正文。返回空串 = 本次没有可用条件，调用方应静默忽略整个目标预选
    （武装态绝不阻断发送）。
    """
    cond = (raw or "").strip()
    if cond:
        return cond
    return ((text or "").strip()
            or _attachment_title_hint(payload)
            or ref_title_hint(payload.get("refs")) or "")


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
            # 目标模式标记（2026-09-30，docs/frontend/22 §6.6）：落盘的 user 行可能
            # 旁挂 `goal` 元数据，两种 kind 走两条路：
            #   - `check`（每轮 Stop 裁决结果）与 `set`（`[Goal set]`）：**都不是
            #     用户说的话** → 转成 `role:"goal_check"` 的展示卡，不再产出用户
            #     气泡（否则回放里会冒出 `[Goal set] Condition: …` 这种怪消息）；
            #   - `instruction`（被设为目标的那条指令）：就是用户原话 → 照常渲染成
            #     气泡，只额外透传 `goal`，前端据此挂「已设为执行目标」徽标。
            # 分流必须放在 `<system-reminder>` 前缀判断**之前** —— 判据按 kind 走，
            # 不依赖正文内容（check 记录的 content 是空串，本就不命中前缀）。
            goal_marker = m.get("goal") if isinstance(m.get("goal"), dict) else None
            if goal_marker and goal_marker.get("kind") in ("check", "set"):
                card = {"role": "goal_check", "goal": goal_marker}
                if m.get("created_at"):
                    card["created_at"] = m["created_at"]
                ui.append(card)
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
            # 目标指令徽标的数据源（2026-09-30）：**只在确有标记时才加字段** ——
            # 无 goal 的消息连字段都不多一个，与改造前的回放形状逐字节一致。
            if goal_marker:
                ui_msg["goal"] = goal_marker
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
                # 本条消息会不会**当场建出**新会话（下方预选执行模式有两条落地路径，
                # 二者必须互斥：fresh 走"建会话时写 meta"，已有会话走"派发前生效"）。
                fresh_session = sid is None
                # 「本条消息就是被设为执行目标的那条指令」的条件文本（2026-09-30
                # 目标可见化，docs/frontend/22 §6.6）。两条命中路径（fresh 预选 /
                # 已有会话武装）各自填，随 `start_turn` 透传给 `run_turn`，由后者
                # 给落盘的 user 行打 `goal` 标记 —— 前端据此在这条消息上渲染
                # 「已设为执行目标」徽标，切会话回放同样可见。
                # why 在这里算：**只有桥层**同时知道"本条是首条消息"与"要不要进
                # goal 模式"（条件口径 `_goal_condition_from_message` 也在这层），
                # agent 侧没有任何信息可反查（正文与条件在有附件时并不相等）。
                goal_instruction: str | None = None
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
                    # ── 预选执行模式随首条消息落地（2026-09-27，docs/frontend/22 §2.5）──
                    # "执行方式"两项在**无会话**时也可选（前端记为 `pendingExecMode`
                    # 草稿），随首条 chat 一起送到这里。**必须在这一刻写 meta**：
                    #   · 迟一步（前端等 `session` 信封回来再发 session_exec_mode）会撞
                    #     `rt.busy` —— 那时 turn 已经派发，goal 被评审 P1-5 的守卫拒掉；
                    #   · 写 meta 即够：Agent 在 `build_agent()` 里 `switch_session`
                    #     → `_restore_execution_state` 读回，**本轮开始前就已生效**。
                    # 非法值（未知 mode / goal 缺条件 / 条件超长）**静默忽略** ——
                    # 草稿态绝不阻断发送；用户可从胶囊没亮、或随后的 sessions 广播看出。
                    pending_exec = str(payload.get("exec_mode") or "")
                    # goal 的条件 = 本条消息（「首条指令即目标」，2026-09-30）：前端
                    # 点「目标模式」不再弹框问条件，只武装本地态，条件随首条 chat 到达。
                    pending_cond = _goal_condition_from_message(
                        str(payload.get("exec_condition") or ""), text, payload)
                    if pending_exec in (MODE_PLAN, MODE_GOAL):
                        if pending_exec == MODE_GOAL and (
                                not pending_cond or len(pending_cond) > MAX_GOAL_LENGTH):
                            log.warning(
                                "chat 预选执行模式被忽略（goal 条件非法）session_%s len=%d",
                                sid, len(pending_cond))
                        else:
                            try:
                                await asyncio.to_thread(
                                    sm.set_session_execution, new_sid,
                                    execution_mode=pending_exec,
                                    plan_status=None,
                                    goal_condition=(pending_cond
                                                    if pending_exec == MODE_GOAL else None),
                                )
                                # goal 还要**显式告知模型**目标是什么：预选路径没有
                                # 走 `Agent.set_execution_mode`，也就没有它内部的
                                # `_append_goal_set_message`；而 `_restore_execution_state`
                                # 只重建控制器、不补这条消息（否则切会话时会重复注入）。
                                # 在这里补一次即等价（消息形状逐字对齐 `_append_goal_set_message`，
                                # 与"用户中途点胶囊设目标"产生的历史一模一样）。
                                # plan 侧不用补：`_sync_execution_mode()` 会在 switch_session
                                # 时按指纹注入 <system-reminder>（幂等，不会重复）。
                                if pending_exec == MODE_GOAL:
                                    await asyncio.to_thread(
                                        sm.append_message_to_session, new_file, {
                                            "role": "user",
                                            "content": (
                                                "[Goal set]\n"
                                                f"Condition: {pending_cond}\n"
                                                "Work toward this condition; the session "
                                                "will be evaluated when you stop."
                                            ),
                                            # 展示标记（2026-09-30）：回放时渲染成
                                            # 「目标设定」卡片，而不是一条怪气泡。
                                            "goal": {"kind": "set",
                                                     "condition": pending_cond},
                                        })
                                    # 首条消息即目标指令 → 打徽标（见上面的声明）
                                    goal_instruction = pending_cond
                                log.info("新会话预选执行模式: session_%s -> %s",
                                         sid, pending_exec)
                            except Exception as exc:  # noqa: BLE001 - 预选失败不阻断发送
                                log.error(
                                    "预选执行模式落盘失败 session_%s mode=%s: %s: %s",
                                    sid, pending_exec, type(exc).__name__, exc)
                    elif pending_exec:
                        log.warning("chat 忽略未知的 exec_mode=%r session_%s",
                                    pending_exec, sid)
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
                # ── 已有会话的「目标模式：首条指令即目标」（2026-09-30，docs/frontend/22 §2.6）──
                # 点「目标模式」不再弹框问条件：前端只**武装**（本地态），条件在这一刻
                # 才成立（= 本条消息的正文），所以落地只能在**这里** —— 位置是硬约束，
                # 三条路径都验过：
                #   · `session_exec_mode` 命令：那时还没有条件 → `GoalError` 必拒；
                #   · turn 派发之后再设：`rt.busy=True` → 撞评审 P1-5 的 busy 守卫
                #     （`GoalController` 无锁，工作线程正在读同一对象）；
                #   · 这里：busy 守卫已过、turn 未派发 → 与"用户中途点胶囊设目标"
                #     同一时序、同一落盘路径。
                # fresh 会话不走这里（`fresh_session`）：它已在上面的建会话分支落过
                # meta + `[Goal set]`，再走一遍会重复注入。
                armed_exec = str(payload.get("exec_mode") or "")
                if armed_exec == MODE_GOAL and not fresh_session:
                    goal_cond = _goal_condition_from_message(
                        str(payload.get("exec_condition") or ""), text, payload)
                    if not goal_cond or len(goal_cond) > MAX_GOAL_LENGTH:
                        # 武装态绝不阻断发送（同 fresh 分支口径）：条件非法就按普通消息发，
                        # 前端胶囊随后的 `sessions` 广播自行纠正。
                        log.warning("chat 目标模式被忽略（条件非法）session_%s len=%d",
                                    sid, len(goal_cond))
                    elif rt.agent is not None:
                        # ① Agent 已构造（本会话至少跑过一轮）：薄委托给既有切换入口 ——
                        # 跨模式清场（plan → goal）、`[Goal set]` 消息、落盘与
                        # `execution_mode_changed` 广播都在里面，桥层不重复做。
                        try:
                            err = await asyncio.to_thread(
                                rt.agent.set_execution_mode, MODE_GOAL, goal_cond)
                        except GoalError as exc:
                            err = str(exc)
                        except Exception as exc:  # noqa: BLE001 - 绝不打死连接
                            err = f"{type(exc).__name__}: {exc}"
                        if err:
                            log.warning("chat 目标模式被拒 session_%s: %s", sid, err)
                        else:
                            # 本条消息即目标指令 → 打徽标（Agent 侧补的 `[Goal set]`
                            # 消息自带 `goal` 标记，这里只管本条 user 行）
                            goal_instruction = goal_cond
                            log.info("chat 目标模式生效（首条指令即目标）session_%s", sid)
                    else:
                        # ② Agent 尚未构造（选中但从未发过消息 / 重启后第一次发）：
                        # 只写 meta —— 紧随其后的 `start_turn` → `build_agent()` →
                        # `switch_session` → `_restore_execution_state` 读回，首轮即生效。
                        # `[Goal set]` 必须手工补一次：restore 路径只重建控制器、**不**补
                        # 这条消息（否则切会话会重复注入）；形状与
                        # `Agent._append_goal_set_message` 逐字对齐。
                        try:
                            await asyncio.to_thread(
                                sm.set_session_execution, sid,
                                execution_mode=MODE_GOAL, plan_status=None,
                                plan_name=None, goal_condition=goal_cond,
                                # 目标运行期指标初值（2026-09-30）：此刻控制器还没
                                # 建（Agent 未构造），轮次恒 0、起点就是现在。
                                # 不写的话常驻目标条在首次重连时会没有轮次可显示。
                                goal_round=0, goal_started_at=time.time())
                            await asyncio.to_thread(
                                sm.append_message_to_session,
                                sm.get_session_file(sid), {
                                    "role": "user",
                                    "content": (
                                        "[Goal set]\n"
                                        f"Condition: {goal_cond}\n"
                                        "Work toward this condition; the session "
                                        "will be evaluated when you stop."
                                    ),
                                    "goal": {"kind": "set", "condition": goal_cond},
                                })
                        except Exception as exc:  # noqa: BLE001 - 失败不阻断发送
                            log.error("chat 目标模式落盘失败 session_%s: %s: %s",
                                      sid, type(exc).__name__, exc)
                        else:
                            goal_instruction = goal_cond   # 本条消息即目标指令
                            # 未跑会话无 Agent 推信封 → 自行组装与 `execution_state`
                            # 同形状的广播（多窗口一致，同 session_exec_mode 口径）
                            hub.broadcast("execution_mode_changed", {
                                "session_id": sid, "mode": MODE_GOAL,
                                "plan_status": None, "plan_path": None,
                                "goal_condition": goal_cond,
                                "goal_round": 0,
                                "goal_started_at": time.time(),
                            })
                            log.info("chat 目标模式已落 meta（agent 未构造）session_%s", sid)
                elif armed_exec:
                    log.warning("chat 忽略未知的 exec_mode=%r session_%s", armed_exec, sid)
                # 后台线程跑 turn；事件循环继续处理其它命令（切换 / 其它会话 / stop）
                log.info("chat 派发: session_%s text=%r attachments=%d refs=%d",
                         sid, text[:80], len(attachment_records), len(ref_records))
                turn_task = asyncio.create_task(
                    rt.start_turn(user_query, reasoning_effort=reasoning_effort,
                                  max_context=max_context,
                                  goal_instruction=goal_instruction)
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

            elif kind == "session_exec_mode":
                # 任务执行模式切换（2026-09-25，docs/frontend/22）。与权限档位**正交**：
                # 权限回答"能不能做 / 要不要审批"，执行模式回答"以什么方式做"。状态独立、
                # UI 入口独立、判定链**不合并**（hooks 里 plan 守卫与 permission 各判一次）。
                #
                # 双路径（评审 P1-7 —— **禁止**照抄 `switchPermission` 的 `if (!sid) return`
                # 静默丢弃）：
                #   ① 会话在跑（rt + agent 已构造）：走 `Agent.set_execution_mode` 薄委托
                #      —— 内存即时生效 + 落 meta + 推 `execution_mode_changed`；
                #   ② 会话已建但未构造 Agent（选中但未发消息 / 重启后）：只写 meta，
                #      下次构造 Agent 时 `_restore_execution_state` 读回。
                # 失败（条件非法 / 会话不存在）→ 既有 `error` 信封，
                # 文案取自 Agent / GoalError，前端 toast 一字不改。
                sid = str(payload.get("session_id") or "")
                mode = str(payload.get("mode") or "")
                if not sid:
                    await safe_send(ws, _envelope("error", {
                        "msg": "请先发送一条消息创建会话，再设置任务执行模式"}))
                    continue
                if mode not in VALID_EXECUTION_MODES:
                    await safe_send(ws, _envelope("error", {
                        "msg": f"未知的执行模式：{mode}"}))
                    continue
                pid = _SID_PROJECT.get(sid) or _project_of_session(sid)
                _SID_PROJECT[sid] = pid
                rt = registry.get(sid) if registry is not None else None
                # goal 的进入/退出**不许在 rt.busy 时即时生效**（评审 P1-5）：
                # `GoalController` **没有任何锁**，而本命令经 `asyncio.to_thread`、
                # `agent_loop` 在工作线程读同一对象 → 跨线程竞争。凡「当前模式或目标
                # 模式涉及 goal」且本会话 turn 在跑，一律拒绝（前端提示稍后再试）。
                # plan 无此限制：gate 自带锁，且守卫是"每次工具调用现场读"。
                if rt is not None and rt.busy and rt.agent is not None:
                    cur = rt.agent.execution_gate.mode
                    if mode == MODE_GOAL or cur == MODE_GOAL:
                        await safe_send(ws, _envelope("error", {
                            "msg": "该会话正在执行，请先停止或等本轮结束后再切换目标模式"}))
                        continue
                if rt is not None and rt.agent is not None:
                    # ① 会话在跑：薄委托给 Agent（互斥判定 / 落盘 / 推送都在里面）
                    try:
                        err = await asyncio.to_thread(
                            rt.agent.set_execution_mode, mode,
                            str(payload.get("condition") or ""))
                    except GoalError as exc:
                        await safe_send(ws, _envelope("error", {"msg": str(exc)}))
                        continue
                    except Exception as exc:  # noqa: BLE001 - 桥层兜底，绝不打死连接
                        log.error("session_exec_mode 失败 session_%s mode=%s: %s: %s",
                                  sid, mode, type(exc).__name__, exc)
                        await safe_send(ws, _envelope("error", {
                            "msg": f"切换执行模式失败：{exc}"}))
                        continue
                    if err:
                        await safe_send(ws, _envelope("error", {"msg": err}))
                        continue
                    log.info("session_exec_mode: session_%s -> %s", sid, mode)
                    continue  # 模式变更信封已由 Agent 内部推（唯一投递路径）
                # ② 未跑分支：无 Agent 可委托 → 只写 meta（同 session_permission 未跑分支）
                sm = _ensure_session_manager(pid)
                if not sm.get_session_file(sid).exists():
                    await safe_send(ws, _envelope("error", {
                        "msg": f"session {sid} not found"}))
                    continue
                condition = str(payload.get("condition") or "").strip()
                if mode == MODE_GOAL:
                    # 条件就地校验：无 Agent 时没有 GoalController 可转发，
                    # 复用 goal 的同一常量（不另造口径，文案与 GoalError 一致）
                    if not condition:
                        await safe_send(ws, _envelope("error", {
                            "msg": "goal condition cannot be empty"}))
                        continue
                    if len(condition) > MAX_GOAL_LENGTH:
                        await safe_send(ws, _envelope("error", {
                            "msg": f"goal condition cannot exceed "
                                   f"{MAX_GOAL_LENGTH} characters"}))
                        continue
                # 跨模式直接切换（2026-09-27）：本分支**不再**有互斥拒绝 —— 与 Agent
                # 侧同一口径（`Agent.set_execution_mode`）。被让位那一方的状态一并写空
                # （显式传 `None` = 写入空值，区别于省略 = 不改），否则下次构造 Agent
                # 时 `_restore_execution_state` 会把旧状态原样读回来。
                # ⚠️ 切走 plan 只清 meta 投影 + `plan_name`，**不删**工作区里的文书
                # 文件（2026-09-29 起文书在 `<工作空间>/.aiagent/plan/`，是项目文件）。
                # 切 plan → **保留** `plan_name`：同一会话重规划要覆盖自己那一份，
                # 丢了名字就会被当成"别人的文件"另起一个 `-2`（与 gate.set_plan_mode 同款）。
                # 目标运行期指标（2026-09-30 目标可见化）：goal_round / goal_started_at
                # 随三处落盘一并维护 —— 常驻目标条的重连恢复源就是 meta 这两个字段，
                # 漏清会让"切走 goal 后"的目标条带着陈旧轮次复活。
                goal_started_now = time.time()
                try:
                    if mode == MODE_PLAN:
                        await asyncio.to_thread(
                            sm.set_session_execution, sid,
                            execution_mode=MODE_PLAN, plan_status=None,
                            goal_condition=None,
                            goal_round=None, goal_started_at=None,
                            goal_tokens_at_start=None)
                    elif mode == MODE_GOAL:
                        await asyncio.to_thread(
                            sm.set_session_execution, sid,
                            execution_mode=MODE_GOAL, plan_status=None,
                            plan_name=None,
                            goal_condition=condition,
                            # 未跑会话没有 Agent/控制器 → 轮次从 0 起、起点是现在
                            goal_round=0, goal_started_at=goal_started_now,
                            goal_tokens_at_start=0)
                    else:  # normal：执行模式四字段一起归零
                        await asyncio.to_thread(
                            sm.set_session_execution, sid,
                            execution_mode=MODE_NORMAL, plan_status=None,
                            plan_name=None,
                            goal_condition=None,
                            goal_round=None, goal_started_at=None,
                            goal_tokens_at_start=None)
                except Exception as exc:  # noqa: BLE001
                    log.error("session_exec_mode 落盘失败 session_%s mode=%s: %s: %s",
                              sid, mode, type(exc).__name__, exc)
                    await safe_send(ws, _envelope("error", {
                        "msg": f"切换执行模式失败：{exc}"}))
                    continue
                log.info("session_exec_mode(未跑会话): session_%s -> %s", sid, mode)
                # 未跑会话无 Agent，自行组装与 execution_state 同形状的信封广播
                # （多窗口一致，同 permission_changed 口径）
                hub.broadcast("execution_mode_changed", {
                    "session_id": sid,
                    "mode": mode,
                    "plan_status": None,
                    "plan_path": None,
                    "goal_condition": (condition if mode == MODE_GOAL else None),
                    "goal_round": (0 if mode == MODE_GOAL else None),
                    "goal_started_at": (goal_started_now
                                        if mode == MODE_GOAL else None),
                })

            elif kind == "plan_approve":
                # 批准计划文书（2026-09-25，docs/frontend/22 §4.6#1）。
                # 语义：`plan_status=approved` + mode 回落 normal + **自动起一轮执行**。
                # ⚠ 忙碌守卫：`rt.busy` 时**只落状态**（`Agent.approve_plan` 内部完成
                # 落盘与推送），不抢跑一轮（两线程同写会话 jsonl 的既有禁忌）；返回的
                # 提示由前端 toast 展示。空闲则追加一条"[计划已批准]"续跑指令后
                # 走 `rt.start_turn`（与 chat 同一入口，状态机 / 停止链路天然复用）。
                sid = str(payload.get("session_id") or "")
                rt = registry.get(sid) if registry is not None else None
                if not sid or rt is None or rt.agent is None:
                    await safe_send(ws, _envelope("error", {
                        "msg": "当前会话没有待批准的计划（会话未开始或已结束）"}))
                    continue
                try:
                    err = await asyncio.to_thread(rt.agent.approve_plan)
                except Exception as exc:  # noqa: BLE001
                    log.error("plan_approve 失败 session_%s: %s: %s",
                              sid, type(exc).__name__, exc)
                    await safe_send(ws, _envelope("error", {
                        "msg": f"批准计划失败：{exc}"}))
                    continue
                if err:
                    await safe_send(ws, _envelope("error", {"msg": err}))
                    continue
                log.info("plan_approve: session_%s 已批准", sid)
                if rt.busy:
                    await safe_send(ws, _envelope("error", {
                        "msg": "计划已批准。当前回合结束后请再发一条消息开始执行",
                    }))
                    continue
                if rt.has_pending_interaction():
                    await safe_send(ws, _envelope("error", {
                        "msg": "计划已批准。当前有等待回答的提问，请先作答",
                    }))
                    continue
                try:
                    asyncio.create_task(rt.start_turn(PLAN_APPROVED_RESUME_TEXT))
                except Exception as exc:  # noqa: BLE001
                    log.error("plan_approve 续跑派发失败 session_%s: %s", sid, exc)
                    await safe_send(ws, _envelope("error", {
                        "msg": f"计划已批准，但启动执行失败：{exc}"}))

            elif kind == "plan_read":
                # 计划文书正文读取（docs/frontend/22 §4.5）。
                # **不复用 file_read**：后者要求前端自己带完整路径，而卡片壳只知道
                # sid；这里的路径由后端从 meta 的 `plan_name` 解析（口径唯一），
                # 点对点回执 `plan_content`（形状与 file_content 同族，前端复用解析）。
                #
                # 2026-09-29 改版：文书已在工作空间内，但读取仍**不**走
                # `read_workspace_file`（那条路会按扩展名分派 image/pdf/office 分支，
                # 与 `plan_content` 的字段契约不符）。改为"解析路径 + `read_plan_file`"。
                sid = str(payload.get("session_id") or "")
                if not sid:
                    await safe_send(ws, _envelope("plan_content", plan_content_payload(
                        "", reason="缺少 session_id")))
                    continue
                pid = _SID_PROJECT.get(sid) or _project_of_session(sid)
                _SID_PROJECT[sid] = pid
                sm = _ensure_session_manager(pid)  # 与 pid 同源，避免两处解析分叉
                try:
                    meta = await asyncio.to_thread(sm.load_meta, sid) or {}
                    ws_paths = _workspace_for_session(pid, meta)
                except Exception as exc:  # noqa: BLE001 - 空间不可用 → 降级形状
                    log.warning("plan_read 解析工作空间失败 session=%s: %s", sid, exc)
                    await safe_send(ws, _envelope("plan_content", plan_content_payload(
                        "", project_id=pid, session_id=sid,
                        reason="该工作空间的目录当前不可用（已被移动或删除）")))
                    continue
                name = meta.get("plan_name")
                path = None
                if name:
                    # `plan_relpath` 内部已把 name 清洗成**纯文件名**（目录分隔符、
                    # `..`、盘符全被剥掉，见 `paths.plan_filename`）—— 所以这条路径
                    # 恒落在工作空间内，手改 meta 也无法把读取引到外面去
                    # （联调实测：把 plan_name 改成 `../../../../etc/hosts` 会被清洗成
                    # `.aiagent/plan/hosts.md`，读不到就是读不到，不越界）。
                    # `resolve_within` 是**第二道**兜底：防的是清洗规则将来出现漏洞，
                    # 不是当前会走到这条分支 —— 留着它，代价是一次字符串运算。
                    path = resolve_within(ws_paths.workdir, plan_relpath(name))
                    if path is None:
                        log.warning("plan_read 路径越界 session=%s name=%r", sid, name)
                        await safe_send(ws, _envelope("plan_content", plan_content_payload(
                            "", project_id=pid, session_id=sid,
                            reason="计划文书的路径不在当前工作空间内")))
                        continue
                else:  # 存量会话：旧口径（元数据目录）
                    path = plan_file_for_session(
                        sid, sm.session_prefix, ws_paths.plans_dir)
                content = await asyncio.to_thread(read_plan_file, path)
                content["project_id"] = pid
                content["session_id"] = sid
                await safe_send(ws, _envelope("plan_content", content))

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
                        # 任务执行模式（2026-09-25，docs/frontend/22 §4.3）：
                        # 切会话时恢复胶囊 tag 与计划卡片外壳
                        **_exec_mode_fields(sm, sid, meta),
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
                        # 任务执行模式（2026-09-25，docs/frontend/22 §4.3）：
                        # 切会话时恢复胶囊 tag 与计划卡片外壳
                        **_exec_mode_fields(sm, sid, meta),
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
                # 2026-09-25：改读**会话自己的** Agent。原先读模块级全局 `agent`
                # （只代表 default 空间，且从不持有会话级目标）—— 多会话下必然答错。
                # 会话未构造 Agent（选中但未发消息）→ 占位文本，与"无目标"同款。
                sid_q = str(payload.get("session_id") or "")
                rt_q = (registry.get(sid_q)
                        if (registry is not None and sid_q) else None)
                if rt_q is not None and rt_q.agent is not None:
                    text = await asyncio.to_thread(rt_q.agent.goal_status)
                else:
                    text = "No goal set"
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
                # 载荷逐字段兜底（见 _sandbox_payload）：探测失败也要回执，否则
                # 前端会永远停在"读取沙盒设置…"（同权限页曾踩的"塌成加载态"）。
                await safe_send(ws, _envelope(
                    "sandbox_config", await _sandbox_payload()))

            elif kind == "sandbox_config_save":
                # 字段部分更新：sandbox_enabled（开关热生效）/
                # seatbelt_profile、bwrap_args（模板覆写，缺占位符拒存）/
                # reset（恢复默认模板）。回执为权威源，前端以回执重绘。
                #
                # 两条硬约束（2026-09-24 修）：
                # 1. **先全量校验，无错才落盘**（原先是逐字段写 + 错误累加，会出现
                #    "bwrap 已落盘、回执却说 applied=false"的半写假象）；
                # 2. **校验失败不额外发 error 信封** —— 那一封会被前端当全局 toast
                #    （agentStore `case 'error'`），而设置页的约定是"错误内联展示、
                #    不 toast"（要对着文本改，toast 一闪而过等于没提示）。
                errors: list[str] = []
                enabled = payload.get("sandbox_enabled")
                if enabled is not None and not isinstance(enabled, bool):
                    errors.append("sandbox_enabled 必须是布尔值")
                    enabled = None
                templates: list[tuple[str, str]] = []  # (tpl_kind, content)
                for field, tpl_kind in (("seatbelt_profile", "seatbelt"),
                                        ("bwrap_args", "bwrap")):
                    if field not in payload:
                        continue
                    content = payload.get(field)
                    if not isinstance(content, str):
                        errors.append(f"{field} 必须是字符串")
                        continue
                    try:
                        sandbox_mod.validate_template(tpl_kind, content)
                    except ValueError as exc:
                        errors.append(str(exc))
                        continue
                    templates.append((tpl_kind, content))
                reset = payload.get("reset")
                if reset is not None and reset not in ("seatbelt", "bwrap"):
                    errors.append(f"reset 只接受 seatbelt / bwrap，收到 {reset!r}")
                    reset = None
                if not errors:
                    try:
                        if enabled is not None:
                            await asyncio.to_thread(_save_sandbox_enabled, enabled)
                        for tpl_kind, content in templates:
                            await asyncio.to_thread(
                                sandbox_mod.save_template, tpl_kind, content)
                        if reset is not None:
                            await asyncio.to_thread(sandbox_mod.reset_template, reset)
                    except Exception as exc:  # noqa: BLE001 - 落盘失败只回回执
                        log.error("沙盒配置落盘失败: %s: %s", type(exc).__name__, exc)
                        errors.append(f"保存失败：{exc}")
                await safe_send(ws, _envelope(
                    "sandbox_config",
                    await _sandbox_payload(applied=not errors, errors=errors)))

            elif kind == "mcp_config_get":
                # 设置页读：原始条目（**含 enable:0**）+ 旁路元数据 + 各 runtime 连接状态。
                # 逐字段兜底见 _mcp_config_payload_sync：探测失败也要回执，否则前端
                # 会永远停在"读取 MCP 配置…"。
                await safe_send(ws, _envelope(
                    "mcp_config", await _mcp_config_payload()))

            elif kind == "mcp_server_upsert":
                # 新增 / 编辑 / 重命名 / 启停（`config.enable` 变化即启停）。
                # **单条 upsert，不是整份覆盖** —— MCP 是"多条目集合"，整份覆盖在
                # 多窗口场景下误伤面太大（对比 permission_config_save 的整份语义）。
                name = str(payload.get("name") or "").strip()
                raw_cfg = payload.get("config")
                original = payload.get("original_name")
                original = str(original).strip() if isinstance(original, str) else None
                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else None
                try:
                    if not isinstance(raw_cfg, dict):
                        raise ValueError("配置必须是 JSON 对象")
                    _, warnings = await asyncio.to_thread(
                        _mcp_store().upsert, name, raw_cfg, original, meta)
                    refreshed = await asyncio.to_thread(_reload_mcp_all_runtimes)
                    log.info("MCP 条目已保存: %s（已触发 %d 个会话热重载）", name, refreshed)
                    await safe_send(ws, _envelope("mcp_config", await _mcp_config_payload(
                        applied=True, warnings=warnings, msg=f"已保存「{name}」")))
                except ValueError as exc:
                    # 校验失败**只走回执 errors[]，不额外发 error 信封** —— 那一封会被
                    # 前端当全局 toast（agentStore `case 'error'`），而设置页的约定是
                    # "错误内联展示、不 toast"（要对着文本改，toast 一闪而过等于没提示）。
                    log.warning("MCP 条目保存被拒: %s → %s", name, exc)
                    await safe_send(ws, _envelope("mcp_config", await _mcp_config_payload(
                        applied=False, errors=[str(exc)], msg="保存被拒")))
                except OSError as exc:
                    log.error("MCP 配置落盘失败: %s", exc)
                    await safe_send(ws, _envelope("mcp_config", await _mcp_config_payload(
                        applied=False, errors=[f"落盘失败：{exc}"], msg="保存失败")))

            elif kind == "mcp_server_remove":
                name = str(payload.get("name") or "").strip()
                if not name:
                    await safe_send(ws, _envelope("mcp_config", await _mcp_config_payload(
                        applied=False, errors=["名称不能为空"], msg="删除被拒")))
                else:
                    try:
                        await asyncio.to_thread(_mcp_store().remove, name)
                        refreshed = await asyncio.to_thread(_reload_mcp_all_runtimes)
                        log.info("MCP 条目已删除: %s（已触发 %d 个会话热重载）", name, refreshed)
                        await safe_send(ws, _envelope("mcp_config", await _mcp_config_payload(
                            applied=True, msg=f"已删除「{name}」")))
                    except (ValueError, OSError) as exc:
                        log.error("MCP 条目删除失败: %s → %s", name, exc)
                        await safe_send(ws, _envelope("mcp_config", await _mcp_config_payload(
                            applied=False, errors=[str(exc)], msg="删除失败")))

            elif kind == "mcp_server_test":
                # 一次性试连：**不落盘、不登记**（不碰 mcp_servers.json，也不进任何
                # runtime 的 _clients，否则会污染模型工具池）。
                #
                # 两种寻址方式二选一（**为什么必须有两种**）：
                #   · `{name}`   —— 测**已保存**的条目：从磁盘读真实配置。
                #     设置页回执里的 `env`/`headers` 是**脱敏过的掩码**（••••••），
                #     拿掩码去测会把 •••••• 当成真密钥发给 server —— 对需要鉴权的
                #     条目必然假失败，用户会以为配置写错了。
                #   · `{config}` —— 测**表单里还没保存**的草稿（用户刚手填的真值）。
                probe_cfg = payload.get("config")
                probe_name = str(payload.get("name") or "").strip()
                test_error = ""
                probe_from_name = False
                if not isinstance(probe_cfg, dict) and probe_name:
                    try:
                        raw = await asyncio.to_thread(_mcp_store().load_raw)
                    except (ValueError, OSError) as exc:
                        probe_cfg, test_error = None, f"读取配置失败：{exc}"
                    else:
                        found = raw.get(probe_name)
                        if isinstance(found, dict):
                            probe_cfg, probe_from_name = found, True
                        else:
                            test_error = f"没有名为「{probe_name}」的 MCP 服务"
                if test_error:
                    await safe_send(ws, _envelope("mcp_test", {
                        "ok": False, "error": test_error, "tools": [],
                        "tool_count": 0, "resource_count": 0, "elapsed_ms": 0,
                        "refreshed": False}))
                elif not isinstance(probe_cfg, dict):
                    await safe_send(ws, _envelope("mcp_test", {
                        "ok": False, "error": "需要 config 或 name 之一", "tools": [],
                        "tool_count": 0, "resource_count": 0, "elapsed_ms": 0,
                        "refreshed": False}))
                else:
                    # 真实起子进程 / 真实建连接并等握手（最长 MCP_CONNECT_TIMEOUT=15s）
                    # → 必须下线程，否则阻塞事件循环会让所有会话的流式事件一起卡住。
                    result = await asyncio.to_thread(_mcp_test_sync, probe_cfg)
                    # 试连通过 → **顺势把状态刷成真的**（2026-10-08）。
                    # 场景：开关开着时 Zotero 没起 → 真实 connect 失败、`_last_errors`
                    # 留下错因；用户起好 Zotero 点「测试」→ 临时 session 握手成功却被
                    # stop 掉，`_clients` 里依然没有它 → 列表仍显示"连接失败"，
                    # 必须关一下再打开开关才恢复（那一次 mtime 真的变了）。
                    #
                    # 触发条件三条全中才做，缺一即刷新会骗人：
                    #   · `ok`                —— 连不上就别刷
                    #   · 寻址是 `{name}`      —— `{config}` 是**未保存的表单草稿**，
                    #     重连用的是磁盘上的旧配置，状态会显示成"新配置已生效"
                    #   · 磁盘条目 `enable=1`  —— 禁用条目刷成"已连接"是假的
                    # 仍然**不落盘**：只调 `connect()`，不碰 `mcp_servers.json`。
                    if result.get("ok") and probe_from_name and probe_name:
                        try:
                            raw_entry = (await asyncio.to_thread(
                                _mcp_store().load_raw)).get(probe_name)
                        except (ValueError, OSError):
                            raw_entry = None
                        if isinstance(raw_entry, dict) and raw_entry.get("enable"):
                            refresh = await asyncio.to_thread(
                                _mcp_reconnect_one_sync, probe_name)
                            result["refreshed"] = bool(refresh.get("refreshed"))
                            result["refresh_connected"] = refresh.get("connected", 0)
                    await safe_send(ws, _envelope("mcp_test", result))
                    # 重连后**多发一帧 `mcp_config`** 让列表状态刷新。
                    # 前端零改动即可生效：主进程 `onEvent` 是先 `webContents.send`
                    # 再 `resolvePending`（main/index.ts:296-300），渲染层
                    # `case 'mcp_config'` 本来就是整份替换。
                    # ⚠️ 必须在 `mcp_test` **之后**发：主进程按 kind 匹配 pending，
                    # 先发 `mcp_config` 不会误匹配（本次没有 mcp_config 的 pending），
                    # 但顺序反过来会让人读日志时误以为状态是试连刷的。
                    if result.get("refreshed"):
                        await safe_send(ws, _envelope(
                            "mcp_config", await _mcp_config_payload()))

            elif kind == "mcp_market_search":
                # 代理官方 registry（前端不直连：跨域、统一缓存、错误文案归口）。
                # 实测单次 0.9s~17s → 这里 20s 请求超时，前端 IPC 超时放到 30s。
                res = await asyncio.to_thread(
                    search_market,
                    str(payload.get("query") or ""),
                    str(payload.get("cursor") or ""),
                    payload.get("limit"),
                )
                await safe_send(ws, _envelope("mcp_market", res))

            elif kind == "mcp_market_resolve":
                # **纯翻译、不落盘** —— 供安装确认弹窗展示将要写入的 command/args 原文。
                # 那是"即将执行什么代码"的唯一凭据，必须在用户点确认之前看到。
                item = payload.get("item")
                if not isinstance(item, dict):
                    await safe_send(ws, _envelope("mcp_market_plan", {
                        "ok": False, "name": "", "config": {}, "env_required": [],
                        "package_args": [], "pkg": None, "warnings": [],
                        "unsupported": "条目格式非法", "error": ""}))
                else:
                    try:
                        current = await asyncio.to_thread(
                            lambda: list(_mcp_store().load_raw().keys()))
                    except (ValueError, OSError) as exc:
                        # 撞名检测读不到现有条目 → 降级成"不查重"，不值得拦下这次翻译
                        log.warning("市场翻译：读取现有 MCP 名称失败 %s: %s",
                                    type(exc).__name__, exc)
                        current = []
                    try:
                        plan = await asyncio.to_thread(resolve_market_item, item, current)
                    except Exception as exc:  # noqa: BLE001 - 分发链无兜底 try，这里必须兜住
                        log.error("市场条目翻译失败：%s: %s", type(exc).__name__, exc)
                        plan = {"ok": False, "name": "", "config": {}, "env_required": [],
                                "package_args": [], "pkg": None, "warnings": [],
                                "unsupported": f"翻译失败：{exc}", "error": ""}
                    await safe_send(ws, _envelope("mcp_market_plan", plan))

            # ── 本地包安装（设置弹窗「MCP → 本地包」，docs/frontend/23 §本地安装）──
            # 四条命令全部**点对点**且都要下线程（`npm view` / `npm install` 是
            # 秒级到分钟级的阻塞调用，留在事件循环里会卡住所有会话的流式事件）。
            # 约定：resolve 回 `mcp_pkg_plan`（新信封）；其余三条回 `mcp_config`
            # 并在 `pkg_action` 字段里带上动作结果 —— 前端只认一种列表形状。
            elif kind == "mcp_pkg_resolve":
                # **纯解析、不落盘、不下载** —— 供"下载到本地"确认区展示
                # 版本 / 哈希 / 依赖树规模 / 安装期脚本清单。
                try:
                    pkg_plan = await asyncio.to_thread(
                        plan_mcp_package,
                        str(payload.get("name") or ""),
                        str(payload.get("version") or ""))
                except Exception as exc:  # noqa: BLE001 - 分发链无兜底 try，这里必须兜住
                    log.error("本地包解析失败：%s: %s", type(exc).__name__, exc)
                    pkg_plan = plan_mcp_fail(f"解析失败：{exc}")
                await safe_send(ws, _envelope("mcp_pkg_plan", pkg_plan))

            elif kind == "mcp_pkg_install":
                # **只下载 + 校验，不写配置**（与 mcp_market_resolve 的"纯翻译"同构）。
                # 写条目仍走既有的 mcp_server_upsert：复用热重载、校验与掩码回填，
                # 而且"装到一半失败"不会留下一条指向不存在文件的配置。
                if not isinstance(payload, dict):
                    action = install_mcp_fail("载荷格式非法")
                else:
                    try:
                        action = await asyncio.to_thread(
                            install_mcp_package,
                            str(payload.get("name") or ""),
                            str(payload.get("version") or ""),
                            bin_name=str(payload.get("bin") or ""),
                            allow_scripts=bool(payload.get("allow_scripts")))
                    except Exception as exc:  # noqa: BLE001
                        log.error("本地包安装异常：%s: %s", type(exc).__name__, exc)
                        action = install_mcp_fail(f"安装失败：{exc}")
                action["action"] = "install"
                if action.get("ok"):
                    log.info("本地包已就绪：%s（%s）", action.get("slug"), action.get("dir"))
                await safe_send(ws, _envelope(
                    "mcp_config",
                    await _mcp_config_payload(
                        msg="已下载到本地" if action.get("ok") else "下载失败",
                        pkg_action=action)))

            elif kind == "mcp_pkg_remove":
                slug = str((payload or {}).get("slug") or "").strip()
                try:
                    action = await asyncio.to_thread(remove_mcp_package, slug)
                except McpInstallError as exc:
                    action = {"ok": False, "slug": slug, "freed_bytes": 0,
                              "error": str(exc)}
                except Exception as exc:  # noqa: BLE001
                    log.error("本地包卸载异常：%s: %s", type(exc).__name__, exc)
                    action = {"ok": False, "slug": slug, "freed_bytes": 0,
                              "error": f"卸载失败：{exc}"}
                action["action"] = "remove"
                if action.get("ok"):
                    log.info("本地包已卸载：%s", slug)
                await safe_send(ws, _envelope(
                    "mcp_config",
                    await _mcp_config_payload(
                        msg=action.get("msg") or "已卸载",
                        pkg_action=action)))

            elif kind == "mcp_pkg_verify":
                slug = str((payload or {}).get("slug") or "").strip()
                try:
                    action = await asyncio.to_thread(verify_mcp_package, slug)
                except McpInstallError as exc:
                    action = {"ok": False, "slug": slug, "errors": [str(exc)],
                              "checked": {}}
                except Exception as exc:  # noqa: BLE001
                    log.error("本地包复核异常：%s: %s", type(exc).__name__, exc)
                    action = {"ok": False, "slug": slug,
                              "errors": [f"复核失败：{exc}"], "checked": {}}
                action["action"] = "verify"
                await safe_send(ws, _envelope(
                    "mcp_config",
                    await _mcp_config_payload(
                        msg="校验通过" if action.get("ok") else "校验未通过",
                        pkg_action=action)))

            # ── 技能管理（设置弹窗「技能」页，docs/frontend/24）────────────────
            # 全部**点对点**：只回发起窗口、不广播（与 permission/sandbox/mcp 同约定
            # —— 广播会冲掉另一个窗口正在编辑的草稿）→ 都不进 isKnownAgentEvent 白名单。
            elif kind == "skill_config_get":
                await safe_send(ws, _envelope("skill_config", await _skill_config_payload()))

            elif kind == "skill_set_enabled":
                # 启停 = **只写旁路元数据**，绝不碰 SKILL.md（docs/frontend/24 §1.1）。
                # 落盘后必须触发一次 system prompt 重建，否则"切回对话看不到生效"。
                name = str(payload.get("name") or "").strip()
                enabled = bool(payload.get("enabled"))
                try:
                    await asyncio.to_thread(_skill_store().set_enabled, name, enabled)
                    refreshed = await asyncio.to_thread(_reload_skills_all_runtimes)
                    log.info("技能「%s」已%s（触发 %d 个 runtime 重建提示）",
                             name, "启用" if enabled else "禁用", refreshed)
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=True,
                        msg=f"已{'启用' if enabled else '禁用'}「{name}」")))
                except ValueError as exc:
                    # 校验失败**只走回执 errors[]，不额外发 error 信封**（那一封会被前端
                    # 当全局 toast，而设置页的约定是错误内联展示，同 23 篇 §3.3-2）。
                    log.warning("技能启停被拒: %s → %s", name, exc)
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=False, errors=[str(exc)], msg="操作被拒")))
                except OSError as exc:
                    log.error("技能启停落盘失败: %s", exc)
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=False, errors=[f"落盘失败：{exc}"], msg="操作失败")))

            elif kind == "skill_install":
                # 从市场安装：**后端自己重新解析 + 重新抓取**，绝不信前端带回来的
                # meta 或文件内容。两条理由：
                #   ① `resolve` 与 `fetch_files` 的结果在后端有内存 TTL 缓存，确认页刚
                #      抓过、这里几乎不额外花网络，代价可忽略；
                #   ② 前端只需回传「哪一条（item）+ 叫什么名」，协议面小得多，
                #      也不可能出现"前端传了别的文件却装成别的东西"。
                item = payload.get("item")
                want = str(payload.get("name") or "").strip()
                if not isinstance(item, dict):
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=False, errors=["缺少要安装的市场条目"], msg="安装被拒")))
                else:
                    mid = str(item.get("market_id") or "")
                    try:
                        existing = await asyncio.to_thread(
                            lambda: [s["name"] for s in _skill_store().scan()])
                        plan = await asyncio.to_thread(
                            resolve_skill_item, mid, item, existing)
                        if not plan.get("ok"):
                            raise ValueError(plan.get("unsupported") or plan.get("error")
                                             or "无法生成安装计划")
                        name = want or str(plan.get("name") or "")
                        files = await asyncio.to_thread(fetch_skill_files, mid, item)
                        if isinstance(files, dict) and files.get("__error__"):
                            raise ValueError(str(files["__error__"]))
                        await asyncio.to_thread(
                            _skill_store().install, name, files, plan.get("meta"))
                        refreshed = await asyncio.to_thread(_reload_skills_all_runtimes)
                        log.info("技能已安装: %s ← %s（触发 %d 个 runtime 重建提示）",
                                 name, mid, refreshed)
                        await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                            applied=True, warnings=list(plan.get("warnings") or []),
                            msg=f"已安装技能「{name}」")))
                    except ValueError as exc:
                        log.warning("技能安装被拒: %s → %s", item.get("id"), exc)
                        await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                            applied=False, errors=[str(exc)], msg="安装被拒")))
                    except OSError as exc:
                        log.error("技能安装落盘失败: %s", exc)
                        await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                            applied=False, errors=[f"落盘失败：{exc}"], msg="安装失败")))
                    except Exception as exc:  # noqa: BLE001 - 分发链无兜底 try，这里必须兜住
                        log.error("技能安装异常: %s: %s", type(exc).__name__, exc)
                        await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                            applied=False, errors=[f"安装失败：{type(exc).__name__}: {exc}"],
                            msg="安装失败")))

            elif kind == "skill_create":
                # 手动新建（对齐 MCP 页的「+ 手动添加」）：只有 name / description /
                # tags / body 四个字段，后端拼成一份标准 SKILL.md 落盘。
                # 这也是"自己写技能"的最短路径 —— 不必先建仓库再走市场。
                name = str(payload.get("name") or "").strip()
                description = str(payload.get("description") or "").strip()
                body = str(payload.get("body") or "")
                tags = payload.get("tags")
                if not description:
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=False,
                        errors=["description 不能为空 —— 技能列表与系统提示都靠它"],
                        msg="新建被拒")))
                elif not body.strip():
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=False, errors=["技能正文不能为空"], msg="新建被拒")))
                else:
                    try:
                        content = build_skill_md(
                            name, description,
                            tags if isinstance(tags, list) else [],
                            body)
                        await asyncio.to_thread(
                            _skill_store().install, name, {SKILL_MANIFEST_NAME: content},
                            {"source": "local", "origin": "manual"})
                        refreshed = await asyncio.to_thread(_reload_skills_all_runtimes)
                        log.info("技能已新建: %s（触发 %d 个 runtime 重建提示）", name, refreshed)
                        await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                            applied=True, msg=f"已新建技能「{name}」")))
                    except ValueError as exc:
                        log.warning("技能新建被拒: %s → %s", name, exc)
                        await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                            applied=False, errors=[str(exc)], msg="新建被拒")))
                    except OSError as exc:
                        log.error("技能新建落盘失败: %s", exc)
                        await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                            applied=False, errors=[f"落盘失败：{exc}"], msg="新建失败")))

            elif kind == "skill_remove":
                name = str(payload.get("name") or "").strip()
                if not name:
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=False, errors=["名称不能为空"], msg="删除被拒")))
                else:
                    try:
                        _, warns = await asyncio.to_thread(lambda: _skill_store().remove(name))
                        refreshed = await asyncio.to_thread(_reload_skills_all_runtimes)
                        log.info("技能已删除: %s（触发 %d 个 runtime 重建提示）", name, refreshed)
                        await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                            applied=True, warnings=list(warns or []), msg=f"已删除「{name}」")))
                    except (ValueError, OSError) as exc:
                        log.error("技能删除失败: %s → %s", name, exc)
                        await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                            applied=False, errors=[str(exc)], msg="删除失败")))

            elif kind == "skill_read":
                name = str(payload.get("name") or "").strip()
                if not name:
                    res = {"name": "", "text": "", "error": "名称不能为空"}
                else:
                    try:
                        res = await asyncio.to_thread(_skill_read_sync, name)
                    except Exception as exc:  # noqa: BLE001
                        log.error("技能正文读取失败: %s: %s", type(exc).__name__, exc)
                        res = {"name": name, "text": "",
                               "error": f"读取失败：{type(exc).__name__}: {exc}"}
                await safe_send(ws, _envelope("skill_content", res))

            elif kind == "skill_market_upsert":
                # 新增/更新一个技能源。**内置源只能改 enabled**，身份字段以代码为准
                # （否则改一次默认源地址就要求用户去编辑 JSON）。
                entry = payload.get("entry")
                try:
                    if not isinstance(entry, dict):
                        raise ValueError("源配置必须是 JSON 对象")
                    await asyncio.to_thread(upsert_skill_market, entry)
                    label = entry.get("name") or entry.get("id")
                    log.info("技能源已保存: %s", label)
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=True, msg=f"已保存技能源「{label}」")))
                except ValueError as exc:
                    log.warning("技能源保存被拒: %s", exc)
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=False, errors=[str(exc)], msg="保存被拒")))
                except OSError as exc:
                    log.error("技能源落盘失败: %s", exc)
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=False, errors=[f"落盘失败：{exc}"], msg="保存失败")))

            elif kind == "skill_market_remove":
                market_id = str(payload.get("market_id") or "").strip()
                try:
                    _, warns = await asyncio.to_thread(remove_skill_market, market_id)
                    log.info("技能源已删除: %s", market_id)
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=not warns, errors=list(warns or []) or None,
                        msg=f"已删除技能源「{market_id}」" if not warns else "删除被拒")))
                except (ValueError, OSError) as exc:
                    log.error("技能源删除失败: %s → %s", market_id, exc)
                    await safe_send(ws, _envelope("skill_config", await _skill_config_payload(
                        applied=False, errors=[str(exc)], msg="删除失败")))

            elif kind == "skill_market_search":
                # 代理各技能源（前端不直连：跨域、统一缓存、错误文案归口）。
                # 实测 git 源 1~3s、第三方 API 0.5~17s → 后端 20s，前端 IPC 放到 30s。
                res = await asyncio.to_thread(
                    search_skill_market,
                    str(payload.get("market_id") or SKILL_DEFAULT_MARKET),
                    str(payload.get("query") or ""),
                    str(payload.get("cursor") or ""),
                    payload.get("limit"),
                )
                await safe_send(ws, _envelope("skill_market", res))

            elif kind == "skill_market_resolve":
                # **纯抓取、不落盘** —— 供安装确认页展示 SKILL.md 全文与文件清单。
                item = payload.get("item")
                if not isinstance(item, dict):
                    await safe_send(ws, _envelope("skill_market_plan",
                                                  skill_plan_fail("条目格式非法")))
                else:
                    mid = str(payload.get("market_id")
                              or item.get("market_id") or SKILL_DEFAULT_MARKET)
                    try:
                        current = await asyncio.to_thread(
                            lambda: [s["name"] for s in _skill_store().scan()])
                    except Exception as exc:  # noqa: BLE001 - 撞名检测读不到就降级成不查重
                        log.warning("技能安装计划：读取现有名称失败 %s: %s",
                                    type(exc).__name__, exc)
                        current = []
                    try:
                        plan = await asyncio.to_thread(
                            resolve_skill_item, mid, item, current)
                    except Exception as exc:  # noqa: BLE001
                        log.error("技能条目解析失败：%s: %s", type(exc).__name__, exc)
                        plan = skill_plan_fail(f"解析失败：{type(exc).__name__}: {exc}")
                    await safe_send(ws, _envelope("skill_market_plan", plan))

            # ── 插件管理（设置弹窗「插件」页，docs/frontend/25）────────────────
            elif kind == "plugin_config_get":
                await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload()))

            elif kind == "plugin_set_enabled":
                name = str(payload.get("name") or "").strip()
                enabled = bool(payload.get("enabled"))
                try:
                    await asyncio.to_thread(_plugin_store().set_enabled, name, enabled)
                    refreshed = await asyncio.to_thread(_reload_skills_all_runtimes)
                    log.info("插件「%s」已%s（触发 %d 个 runtime 重建提示）",
                             name, "启用" if enabled else "禁用", refreshed)
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=True, msg=f"已{'启用' if enabled else '禁用'}「{name}」")))
                except ValueError as exc:
                    log.warning("插件启停被拒: %s → %s", name, exc)
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=False, errors=[str(exc)], msg="操作被拒")))
                except OSError as exc:
                    log.error("插件启停落盘失败: %s", exc)
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=False, errors=[f"落盘失败：{exc}"], msg="操作失败")))

            elif kind == "plugin_install":
                # 与技能安装同一条思路：**后端重新解析 + 重新抓取**，前端只回传
                # 「哪一条 + 叫什么名」。插件的 source 三种形态（相对路径 / 独立仓库 /
                # 仓库子目录）由 `plugin_market` 统一翻译，这里一行业务规则都不放。
                item = payload.get("item")
                want = str(payload.get("name") or "").strip()
                if not isinstance(item, dict):
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=False, errors=["缺少要安装的市场条目"], msg="安装被拒")))
                else:
                    mid = str(item.get("market_id") or "")
                    try:
                        existing = await asyncio.to_thread(
                            lambda: [p["name"] for p in _plugin_store().scan()])
                        plan = await asyncio.to_thread(
                            resolve_plugin_item, mid, item, existing)
                        if not plan.get("ok"):
                            raise ValueError(plan.get("unsupported") or plan.get("error")
                                             or "无法生成安装计划")
                        name = want or str(plan.get("name") or "")
                        files = await asyncio.to_thread(fetch_plugin_files, mid, item)
                        if isinstance(files, dict) and files.get("__error__"):
                            raise ValueError(str(files["__error__"]))
                        await asyncio.to_thread(
                            _plugin_store().install, name, files, plan.get("meta"))
                        refreshed = await asyncio.to_thread(_reload_skills_all_runtimes)
                        log.info("插件已安装: %s ← %s（触发 %d 个 runtime 重建提示）",
                                 name, mid, refreshed)
                        await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                            applied=True, warnings=list(plan.get("warnings") or []),
                            msg=f"已安装插件「{name}」")))
                    except ValueError as exc:
                        log.warning("插件安装被拒: %s → %s", item.get("id"), exc)
                        await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                            applied=False, errors=[str(exc)], msg="安装被拒")))
                    except OSError as exc:
                        log.error("插件安装落盘失败: %s", exc)
                        await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                            applied=False, errors=[f"落盘失败：{exc}"], msg="安装失败")))
                    except Exception as exc:  # noqa: BLE001
                        log.error("插件安装异常: %s: %s", type(exc).__name__, exc)
                        await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                            applied=False, errors=[f"安装失败：{type(exc).__name__}: {exc}"],
                            msg="安装失败")))

            elif kind == "plugin_remove":
                name = str(payload.get("name") or "").strip()
                if not name:
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=False, errors=["名称不能为空"], msg="删除被拒")))
                else:
                    try:
                        _, warns = await asyncio.to_thread(lambda: _plugin_store().remove(name))
                        refreshed = await asyncio.to_thread(_reload_skills_all_runtimes)
                        log.info("插件已删除: %s（触发 %d 个 runtime 重建提示）", name, refreshed)
                        await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                            applied=True, warnings=list(warns or []), msg=f"已删除「{name}」")))
                    except (ValueError, OSError) as exc:
                        log.error("插件删除失败: %s → %s", name, exc)
                        await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                            applied=False, errors=[str(exc)], msg="删除失败")))

            elif kind == "plugin_read":
                name = str(payload.get("name") or "").strip()
                if not name:
                    res = {"name": "", "plugin_json": "", "files": [], "error": "名称不能为空"}
                else:
                    try:
                        res = await asyncio.to_thread(_plugin_read_sync, name)
                    except Exception as exc:  # noqa: BLE001
                        log.error("插件详情读取失败: %s: %s", type(exc).__name__, exc)
                        res = {"name": name, "plugin_json": "", "files": [],
                               "error": f"读取失败：{type(exc).__name__}: {exc}"}
                await safe_send(ws, _envelope("plugin_content", res))

            elif kind == "plugin_market_upsert":
                entry = payload.get("entry")
                try:
                    if not isinstance(entry, dict):
                        raise ValueError("市场配置必须是 JSON 对象")
                    await asyncio.to_thread(upsert_plugin_market, entry)
                    label = entry.get("name") or entry.get("id")
                    log.info("插件市场已保存: %s", label)
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=True, msg=f"已保存插件市场「{label}」")))
                except ValueError as exc:
                    log.warning("插件市场保存被拒: %s", exc)
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=False, errors=[str(exc)], msg="保存被拒")))
                except OSError as exc:
                    log.error("插件市场落盘失败: %s", exc)
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=False, errors=[f"落盘失败：{exc}"], msg="保存失败")))

            elif kind == "plugin_market_remove":
                market_id = str(payload.get("market_id") or "").strip()
                try:
                    _, warns = await asyncio.to_thread(remove_plugin_market, market_id)
                    log.info("插件市场已删除: %s", market_id)
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=not warns, errors=list(warns or []) or None,
                        msg=f"已删除插件市场「{market_id}」" if not warns else "删除被拒")))
                except (ValueError, OSError) as exc:
                    log.error("插件市场删除失败: %s → %s", market_id, exc)
                    await safe_send(ws, _envelope("plugin_config", await _plugin_config_payload(
                        applied=False, errors=[str(exc)], msg="删除失败")))

            elif kind == "plugin_market_search":
                # 实测官方市场目录（314 条）单次 1~3s；读的是 raw CDN，不受 API 限额。
                res = await asyncio.to_thread(
                    search_plugin_market,
                    str(payload.get("market_id") or PLUGIN_DEFAULT_MARKET),
                    str(payload.get("query") or ""),
                    str(payload.get("cursor") or ""),
                    payload.get("limit"),
                )
                await safe_send(ws, _envelope("plugin_market", res))

            elif kind == "plugin_market_resolve":
                # **纯抓取、不落盘** —— 供安装确认页列出插件将贡献的全部组件。
                item = payload.get("item")
                if not isinstance(item, dict):
                    await safe_send(ws, _envelope("plugin_market_plan",
                                                  plugin_plan_fail("条目格式非法")))
                else:
                    mid = str(payload.get("market_id")
                              or item.get("market_id") or PLUGIN_DEFAULT_MARKET)
                    try:
                        current = await asyncio.to_thread(
                            lambda: [p["name"] for p in _plugin_store().scan()])
                    except Exception as exc:  # noqa: BLE001
                        log.warning("插件安装计划：读取现有名称失败 %s: %s",
                                    type(exc).__name__, exc)
                        current = []
                    try:
                        plan = await asyncio.to_thread(
                            resolve_plugin_item, mid, item, current)
                    except Exception as exc:  # noqa: BLE001
                        log.error("插件条目解析失败：%s: %s", type(exc).__name__, exc)
                        plan = plugin_plan_fail(f"解析失败：{type(exc).__name__}: {exc}")
                    await safe_send(ws, _envelope("plugin_market_plan", plan))

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