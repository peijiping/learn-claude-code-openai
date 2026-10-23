#!/usr/bin/env python3
"""
paths.py - 路径配置（单一事实来源）

集中定义所有工作目录相关路径常量，供全项目各模块引用。

从原 tool_base.py 顶部抽离，使「路径」与「工具行为」解耦：
- 之前路径常量散落在 tool_base.py，还要经 tools.py 二次导出，三层转发易迷失
- 现在所有路径一律在此定义，其他模块 `from paths import ...` 直接引用
- AGENTS.md 规则：工作目录相关常量统一在此管理，禁止在业务模块内重复声明
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from config import AIGENT_HOME, CONFIG_DIR, migrate_legacy


# ── 根目录（启动 agent 时的当前工作目录） ──────────────────────────
ROOT_DIR = Path.cwd()

# 应用自身 home 目录（用户级，存放 skills / worktree / MCP 配置 / 应用配置）。
# 位于 ~/.aigent（config.py 定义），原 WorkSpace/HomeDir 内容由 migrate_legacy 一次性搬迁。
# 散落的配置文件（config.json / credentials.json / llmconfig.json / providers.json /
# permissions.json）自 2026-09-22 起统一收在 `~/.aigent/config/`（CONFIG_DIR）下。

# 技能目录（每个技能一个子目录，内含 SKILL.md）
SKILLS_DIR = AIGENT_HOME / "skills"
# 技能旁路元数据（设置页用：source / market_id / market_name / publisher /
# installed_at / enabled）。**刻意与技能本体分离**（docs/frontend/24），三条理由：
#   1. 技能目录必须保持**可直接拷给别的 Agent 用**的原样（SKILL.md + 附属文件）；
#      往里塞一个 .aigent-meta.json 会污染这份"可移植性"；
#   2. 启用/禁用状态若写进 SKILL.md 的 frontmatter，等于每次启停都要**改写用户
#      手写的技能正文**（还会过期、还会与上游更新冲突）；
#   3. 市场来源信息（从哪装的）与技能内容无关，混在一起会让"更新技能"变成
#      "合并两份不同来源的数据"。
SKILL_SOURCES = AIGENT_HOME / "skills_sources.json"
# 已注册的技能市场源（用户添加的 git 仓库 / 索引地址），设置页可增删。
SKILL_MARKETS = AIGENT_HOME / "skill_markets.json"

# 插件目录（每个插件一个子目录，内含 .claude-plugin/plugin.json 清单）。
# 采用 Claude Code 插件规范 —— 插件是"可分发的能力包"，可贡献 skills /
# commands / hooks / MCP 服务器等组件（docs/frontend/25）。
PLUGINS_DIR = AIGENT_HOME / "plugins"
# 插件旁路元数据（与 SKILL_SOURCES 同理：不污染插件目录本体）。
PLUGIN_SOURCES = AIGENT_HOME / "plugins_sources.json"
# 已注册的插件市场源（`marketplace.json` 所在的 git 仓库）。默认预置官方市场。
PLUGIN_MARKETS = AIGENT_HOME / "plugin_markets.json"

# worktree 目录（git worktree 实验分支挂载点）
WORKTREE_DIR = AIGENT_HOME / "worktrees"

# MCP 配置目录（真实 MCP：JSON 配置 + 本地示例 server 同目录）
MCP_DIR = AIGENT_HOME / "mcp"
# MCP 服务器配置文件（mcpServers 格式，多服务器）
MCP_CONFIG = MCP_DIR / "mcp_servers.json"
# MCP 条目旁路元数据（设置页用：source / market_id / installed_at / publisher）。
# **刻意与 MCP_CONFIG 分离**（docs/frontend/23）：
#   1. mcp_servers.json 保持标准 mcpServers 格式，可直接拷给别的 MCP 客户端用；
#   2. mcp_manager.maybe_reload() 靠 `new[name] != old.get(name)` 判配置变化，
#      元数据若内嵌进条目，改个 installed_at 就会触发整条断连重连。
MCP_SOURCES = MCP_DIR / "mcp_sources.json"
# MCP **本地包**安装根（2026-10-07，docs/frontend/23 §本地安装）。
# `~/.aigent/mcp/pkgs/<包@版本>/` —— 一个包一个专属目录（内含 node_modules 与
# 我们写的 `aigent-meta.json`）。要点：
#   1. 落在 `MCP_DIR` 之下是刻意的：与既有的"应用自身产物只在 `~/.aigent` 下"
#      收口习惯一致（同 sandbox/），不往用户的全局 npm 前缀里塞东西；
#   2. 目录名带版本 → 同包不同版本可并存，同名同版天然幂等；
#   3. 卸载 = 删这个目录，没有任何跨目录的副作用（故 `mcp_installer.remove` 只
#      允许删本目录的直接子目录）。
MCP_PKGS_DIR = MCP_DIR / "pkgs"

# 工作目录（所有工具操作的沙盒根；项目选择阶段改为用户可选）
WORKDIR = ROOT_DIR / "WorkSpace/task1"

# ── 项目运行时数据根（用户级，分项目隔离） ────────────────────────
# 会话/待办/任务/记忆等运行时数据不再放项目内，对齐 Claude Code
# ~/.claude/projects/<项目slug>/ 模型（见 docs/frontend/05）。
# 单项目阶段 slug 固定 default；项目选择阶段改为按工作目录派生。
PROJECTS_ROOT = AIGENT_HOME / "projects"
DEFAULT_PROJECT_SLUG = "default"
# 预留：不属于任何项目的独立会话（项目选择阶段启用）
INDEPENDENT_PROJECT_SLUG = "_independent"
DATA_ROOT = PROJECTS_ROOT / DEFAULT_PROJECT_SLUG

# ── 工作空间（多项目）路径口径（2026-09-18）────────────────────────────
# 设计见 docs/frontend/11-工作空间管理.md。要点：
# - `projects.json` 是**自定义工作空间的唯一索引**（名称 / 真实目录 / 元数据目录名）；
#   默认工作空间**不在文件里也必须可用** —— 它恒存在且固定为 `default`。
# - 元数据目录 = `PROJECTS_ROOT/<id>/`，内部结构与 default 完全同构
#   （WORKSPACE_SUBDIRS）；`id` 为 "default" 或 "ws" + 10 位 base62 短码
#   （选择的目录可能重名，故目录名不取文件夹名，取不重复短码）。
# - **存量零迁移**：default 目录保持原名不重命名、不搬迁。
PROJECTS_INDEX = PROJECTS_ROOT / "projects.json"
DEFAULT_PROJECT_ID = DEFAULT_PROJECT_SLUG
DEFAULT_PROJECT_NAME = "默认"
PROJECT_ID_PREFIX = "ws"
PROJECT_ID_LEN = 10  # 与 SESSION_ID_LEN 一致的短码长度（见 session_manage.new_session_id）

# 元数据目录内部必须存在的子目录（与 default 同构）。
# 注意：`.todo` 已下线（2026-09-16）不再创建；`.transcripts` / `.task_outputs`
# 由 ContextCompact 在运行期按需创建，无需预先建。
WORKSPACE_SUBDIRS = (
    ".chathistory", ".tasks", ".memory", ".inbox", ".team", ".workflow", ".scheduler",
)

# default 工作空间的**草稿目录**（2026-09-20）：
# default 不再绑定仓库内 `ROOT_DIR/WorkSpace/task1`（该常量仅作存量会话回退），
# 桌面端**新建**的 default 会话统一落到这个固定草稿区 —— 文件工具与 run_bash
# 同根（此前"文件工具走 WORKDIR、bash 走进程 cwd"的两个落点就此收口）。
# 放元数据目录下（而非某个真实目录）：default 没有"自己的目录"，scratch 就是
# 它的落点 —— Finder 可直达（`在 Finder 中打开`）、整体可随时清空。
DEFAULT_SCRATCH_DIR = DATA_ROOT / "scratch"

# ── 会话附件目录（2026-09-20，桌面端「添加文件或图片」）────────────────────
# 布局（设计见 docs/frontend/12-附件与文件输入.md）：
#
#   ~/.aigent/projects/<id>/.attachments/
#       _draft/<att_id>/             ← 尚未发送（新会话此刻还没有 session_id）
#           meta.json / <att_id>.<ext> / <att_id>.txt / <att_id>.send.jpg
#       <session_id>/<att_id>.<ext>  ← 已发送（发送时原子迁移过来）
#
# 与 `.transcripts` / `.task_outputs` 一样由运行期按需创建（不进
# WORKSPACE_SUBDIRS）：没有附件的用户永远不会有这个目录。
ATTACHMENTS_DIRNAME = ".attachments"
DRAFT_ATTACHMENTS_DIRNAME = "_draft"

# ── 计划文书目录（任务执行模式，docs/frontend/22）─────────────────────────────
# **2026-09-29 改版：文书落到工作空间里，不再进元数据目录。**
#
#   旧（2026-09-25 ~ 2026-09-29）：~/.aigent/projects/<id>/plans/session_<sid>.md
#   新（2026-09-29 起）：          <工作空间根>/.aiagent/plan/<模型命名>.md
#
# 为什么改：计划文书回答的是"接下来要怎么改**这个项目**"，跟代码放在一起才看得见、
# 能进版本库、能直接在编辑器里改。落在 `~/.aigent` 的那一版，用户根本找不到它。
#
# 改版带来的三条**连带约束**（改动时三处必须一起看）：
#   1. 文件名由**模型**给（`plan_write` 的 `name` 参数）→ 落盘前必须清洗
#      （`plan_filename` / `resolve_plan_path`，只让模型定名字、定不了目录）；
#   2. 路径再也无法由 sid 推出 → meta 落 `plan_name`（非绝对路径，工作空间整体
#      搬家也不会失效），前端据此拼右栏标签路径；
#   3. 会话删除/清空**不再**删文书 —— 它此时是工作区里的项目文件（用户资产），
#      不是会话的临时产物。旧的两处级联清理随之删除，见 docs/frontend/22 §4.5。
#
# 与既有 `.aigent/`（工具缓存，在 `refs.DEFAULT_IGNORE_DIRS` 里被剪枝）刻意区分：
# `.aiagent/` 是**给用户看的产物目录**，不进忽略清单 —— 计划文书要能在文件树里看见。
PLAN_DIR_PARTS = (".aiagent", "plan")
# 计划文书单份文件名长度上限（含扩展名）：模型给的 name 超长即截断
PLAN_NAME_MAX = 60

# ⚠️ **旧落点，只用于存量会话回退读取**（2026-09-29 起的写入一律走 `PLAN_DIR_PARTS`）。
# 删掉它会让升级前的会话"卡片一下读不到正文"；相关测试见 tests/test_plan_artifact.py。
PLANS_DIRNAME = "plans"


@dataclass(frozen=True)
class WorkspacePaths:
    """一个工作空间的**路径束**（同空间所有目录的唯一口径）。

    为什么要这个束：多工作空间下"会话/任务/记忆/沙箱根在哪"不再能由模块级常量
    回答（那些常量恒指 default），必须按会话所属空间解析。把解析结果收成一个
    不可变对象注入 `Agent`，下游各依赖就都能拿到自己那份目录。

    - `workdir`：该工作空间的**工具沙箱根**（run_bash 的 cwd、read/write/glob
      的相对路径基准、系统提示词里的工作目录）。
    - `data_root`：其元数据目录（`~/.aigent/projects/<id>/`）。
    """

    id: str
    data_root: Path
    workdir: Path
    # run_bash 的缺省工作目录（None = 进程 cwd，历史行为）。规则收口在路径束上
    # （2026-09-20，原在 Agent.__init__ 里推导）：
    # - default（CLI / 存量会话回退）= None → 进程 cwd；
    # - 自定义空间 = 选定的真实目录（bash 与文件工具同根）；
    # - 桌面端新建 default 会话 = DEFAULT_SCRATCH_DIR（草稿区，同根）。
    bash_cwd: Path | None = None

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT_PROJECT_ID

    @property
    def chat_history_dir(self) -> Path:
        return self.data_root / ".chathistory"

    @property
    def tasks_dir(self) -> Path:
        return self.data_root / ".tasks"

    @property
    def memory_dir(self) -> Path:
        return self.data_root / ".memory"

    @property
    def memory_index(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    @property
    def inbox_dir(self) -> Path:
        return self.data_root / ".inbox"

    @property
    def team_dir(self) -> Path:
        return self.data_root / ".team"

    @property
    def workflow_dir(self) -> Path:
        return self.data_root / ".workflow"

    @property
    def scheduler_dir(self) -> Path:
        return self.data_root / ".scheduler"

    @property
    def durable_path(self) -> Path:
        return self.scheduler_dir / "scheduled_tasks.json"

    @property
    def transcript_dir(self) -> Path:
        return self.data_root / ".transcripts"

    @property
    def tool_results_dir(self) -> Path:
        return self.data_root / ".task_outputs" / "tool-results"

    @property
    def attachments_dir(self) -> Path:
        """会话附件根（`.attachments/`）。

        其下再按会话隔离：`<session_id>/` = 已发送的附件，
        `_draft/` = 尚未发送（还没有 session_id）的草稿附件。
        任何"附件在哪个空间"的解析都必须经本属性 —— 与其它运行期目录同一口径
        （模块级常量只代表 default 空间）。
        """
        return self.data_root / ATTACHMENTS_DIRNAME

    @property
    def plans_dir(self) -> Path:
        """**旧**计划文书根（`<data_root>/plans/`，2026-09-25 版落点）。

        ⚠️ 2026-09-29 起写入统一走 `plan_dir_for(workdir)`（工作空间内的
        `.aiagent/plan/`）；本属性只服务于**存量会话的回退读取**，不要在新代码里
        用它算落点。新口径见 `PLAN_DIR_PARTS` 上方的说明。
        """
        return self.data_root / PLANS_DIRNAME

    @property
    def plan_dir(self) -> Path:
        """**现**计划文书根（`<工作空间根>/.aiagent/plan/`，2026-09-29 起）。

        与 `plans_dir` 并存是刻意的：前者是写入与展示口径，后者是存量回退口径。
        """
        return plan_dir_for(self.workdir)


def workspace_paths(project_id: str, root: Path | str | None = None) -> WorkspacePaths:
    """构造某工作空间的路径束。

    - `project_id == "default"`：`root` 忽略（可省），沙箱根取遗留沙盒 `WORKDIR`
      —— 存量行为一字不变（CLI 与存量会话的回退口径；桌面端新建 default 会话
      走 `default_scratch_paths()`，见 2026-09-20）。
    - 自定义空间：**必须**给 `root`（其真实目录，由 `project_registry` 从
      projects.json 解出）。缺失即抛 `ValueError`：宁可响亮失败，也不要静默
      把沙箱根落到元数据目录上（那是数据灾难级错误）。bash 与文件工具同根。
    """
    if project_id == DEFAULT_PROJECT_ID:
        return WorkspacePaths(DEFAULT_PROJECT_ID, DATA_ROOT, WORKDIR)
    if root is None:
        raise ValueError(f"自定义工作空间 {project_id!r} 缺少真实目录 root")
    return WorkspacePaths(
        project_id, PROJECTS_ROOT / project_id, Path(root), bash_cwd=Path(root)
    )


def default_scratch_paths() -> WorkspacePaths:
    """桌面端**新建** default 会话的路径束：沙箱根 = 草稿目录（bash 同根）。

    只在"桌面端新建会话"路径使用（ws_bridge）；CLI 与 `Agent(workspace=None)`
    仍走 `workspace_paths("default")` 的遗留语义，零迁移。
    """
    return WorkspacePaths(
        DEFAULT_PROJECT_ID, DATA_ROOT, DEFAULT_SCRATCH_DIR, bash_cwd=DEFAULT_SCRATCH_DIR
    )

# 待办目录（与每个 session 绑定的轻量级任务看板）
TODO_DIR = DATA_ROOT / ".todo"
# 待办文件命名随 session 变化，不再用全局 TODO_FILE 常量
# 路径生成见 todo_file_for_session()

# 团队目录
TEAM_DIR = DATA_ROOT / ".team"

# 收件箱目录
INBOX_DIR = DATA_ROOT / ".inbox"

# 对话历史目录（会话 jsonl + 元数据 index.jsonl）
CHAT_HISTORY_DIR = DATA_ROOT / ".chathistory"

# L4 / reactive 时 transcript 落盘的目录名
TRANSCRIPT_DIRNAME = DATA_ROOT / ".transcripts"

# L3 落盘大 tool_result 的目录名
TOOL_RESULTS_DIRNAME = DATA_ROOT / ".task_outputs/tool-results"

# 日志目录（按日期分文件，见 agents/logger.py）
LOG_DIR = AIGENT_HOME / "logs"

# 记忆目录
MEMORY_DIR = DATA_ROOT / ".memory"

# 记忆索引文件
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

# 任务目录
TASKS_DIR = DATA_ROOT / ".tasks"

# ── 任务文件：一个会话一个 JSON，文件内以「组」为 key（2026-09-16 第二次改造）──
# 布局（替代原「每任务一文件 .tasks/task_<scope>_<ts>_<rand>.json」）：
#
#   ~/.aigent/projects/default/.tasks/<scope>.json
#   {
#     "version": 1,
#     "scope": "session_Kx7mQ2vT8p",
#     "updated_at": 1758000000.123,
#     "groups": {
#       "g_1758000000_0001": [ {任务}, {任务} ],   ← 一次"派活"= 一个 key
#       "g_1758000123_0002": [ {任务} ]
#     }
#   }
#
# 好处：文件数 = 会话数（原方案是任务数）；面板/回放一次读盘拿到整组，
# 多组历史天然共存于同一文件，不再靠文件名排序去"猜"分组。
GLOBAL_TASK_SCOPE_KEY = "_global"

# 持久化路径：所有 durable=True 的任务会被序列化到该文件，重启后自动恢复
DURABLE_PATH = DATA_ROOT / ".scheduler" / "scheduled_tasks.json"

# 工作流运行时目录（s16：快照 + journal + 输出文件，对应教程的 .runtime/）
WORKFLOW_DIR = DATA_ROOT / ".workflow"
# 最近一次工作流 runId（resume 入口从这里读取）
WORKFLOW_LAST_RUN = WORKFLOW_DIR / "last_run.txt"


def ensure_dirs() -> None:
    """一次性创建所有需要预先存在的目录（幂等）。

    先执行一次性迁移（WorkSpace/HomeDir → ~/.aigent、项目内运行时数据
    → ~/.aigent/projects/default/），再创建运行时目录。
    """
    migrate_legacy(ROOT_DIR / "WorkSpace" / "HomeDir")
    migrate_workspace_data()
    # 默认工作空间的元数据子目录：按 WORKSPACE_SUBDIRS 统一创建（与
    # 自定义工作空间完全同构的同一份口径），再补各自的落点。
    for name in WORKSPACE_SUBDIRS:
        (DATA_ROOT / name).mkdir(parents=True, exist_ok=True)
    # default 草稿目录：桌面端新建 default 会话的沙箱根（2026-09-20）
    DEFAULT_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    # 计划文书目录（2026-09-25，任务执行模式）：default 空间预先建；自定义空间
    # 由 plan_write 运行期按需 mkdir（与 .attachments 同策略）。
    (DATA_ROOT / PLANS_DIRNAME).mkdir(parents=True, exist_ok=True)
    # 配置文件目录（2026-09-22）：config.json / credentials.json / llmconfig.json /
    # providers.json / permissions.json 的落点（migrate_legacy 已尝试搬迁旧顶层文件）
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    TODO_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    DURABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    # MCP 本地包安装根（2026-10-07）：预先建好，让"批量安装"与"首次打开本地包
    # 列表"都不必各自 mkdir（与 SKILLS_DIR / PLUGINS_DIR 同策略）。
    MCP_PKGS_DIR.mkdir(parents=True, exist_ok=True)
    # 技能 / 插件的运行时落点（docs/frontend/24、25）。技能目录此前由
    # migrate_legacy 顺带搬过来，不保证存在 → 这里统一补建（幂等）。
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)


# 项目内遗留的运行时数据目录名（曾挂在 WorkSpace/task1 下）
_LEGACY_RUNTIME_DIRNAMES = (
    ".chathistory", ".todo", ".tasks", ".team", ".inbox",
    ".transcripts", ".task_outputs", ".memory", ".workflow", ".scheduler",
)


def migrate_workspace_data() -> None:
    """一次性把项目内 WorkSpace/task1 下的运行时数据目录搬到 DATA_ROOT。

    - 仅搬运 _LEGACY_RUNTIME_DIRNAMES 中的目录，WorkSpace/task1 本身保留
      （它仍是工具操作沙盒，本次只迁运行时数据，不迁沙盒）
    - 源不存在或目标已存在则跳过（幂等，可重复执行）
    """
    legacy_root = WORKDIR
    if not legacy_root.exists():
        return
    moved = []
    for name in _LEGACY_RUNTIME_DIRNAMES:
        src = legacy_root / name
        dst = DATA_ROOT / name
        if not src.is_dir() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            moved.append(name)
        except OSError as e:
            print(f"[迁移] 搬运 {src} → {dst} 失败：{e}")
    if moved:
        print(f"[迁移] 运行时数据已搬迁到 {DATA_ROOT}：{', '.join(moved)}")


def todo_file_for_session(session_id: str) -> Path:
    """
    返回指定会话对应的 todo 文件路径。

    ⚠️ todo 已于 2026-09-16 下线（见 agents/todo_manager.py）。本函数保留仅为
    兼容存量数据与 tests/test_session_id_naming.py 的断言，**不应被新代码调用**。

    todo 是会话内轻量级任务看板，与 chat history 一一绑定：
    每个 session 有独立 todo 文件，会话切换时同步切换。

    文件命名：.todo/session_<id>.todo.json（与 .chathistory/session_<id>.jsonl 同 id）。
    id 为短随机串（新会话）或存量编号字符串（"6"），调用方统一传 str。
    """
    return TODO_DIR / f"session_{session_id}.todo.json"


# ── 任务文件路径（一个会话一个 JSON，组为 key）──────────────────────────
# 布局与设计理由见 TASKS_DIR 上方注释；实现细节见 agents/task_manager.py。


def task_scope_key(scope: str | None) -> str:
    """scope → 任务文件名（不含 `.json` 后缀）。scope 为空（旧全局看板）用哨兵名。"""
    return scope or GLOBAL_TASK_SCOPE_KEY


def task_scope_file(scope: str | None, tasks_dir: Path | None = None) -> Path:
    """scope → 该作用域**唯一**的任务文件路径。

    ⚠️ **这是 scope ↔ 文件名口径的唯一出处**（`task_manager` 直接 import 本函数，
    读写与 `SessionManager` 的级联清理必须同源）：原先两处各持一份 glob 规则，
    任何一边漂移都会导致"清理静默失效"。
    守护测试：`tests/test_session_task_cascade.py`。
    """
    base = tasks_dir if tasks_dir is not None else TASKS_DIR
    return base / f"{task_scope_key(scope)}.json"


def task_file_for_session(session_id: str, session_prefix: str = "session_",
                          tasks_dir: Path | None = None) -> Path:
    """指定会话对应的任务文件路径（会话 id 是短随机串或存量编号字符串）。

    `tasks_dir` 为该会话所属工作空间的任务目录（缺省 = default 空间）。
    """
    return task_scope_file(f"{session_prefix}{session_id}", tasks_dir)


def plan_file_for_session(session_id: str, session_prefix: str = "session_",
                          plans_dir: Path | None = None) -> Path:
    """**旧**口径：指定会话的计划文书路径（`<plans_dir>/<prefix><sid>.md`）。

    ⚠️ 2026-09-29 起**只用于存量会话的回退读取**（旧会话的文书确实躺在那里）。
    新写入一律走 `plan_dir_for` + `resolve_plan_path`（工作空间内、模型命名）。

    `plans_dir` 为该会话**所属工作空间**的旧 `plans/` 目录（缺省 = default 空间）。
    """
    base = plans_dir if plans_dir is not None else (DATA_ROOT / PLANS_DIRNAME)
    return base / f"{session_prefix}{session_id}.md"


# ── 计划文书（2026-09-29 起口径：工作空间内 + 模型命名）─────────────────────
# 这是「计划文书路径」的**唯一出处**：写入端（Agent 的 `plan_write` 闭包）、
# 读取端（`ws_bridge.plan_read`）、展示端（`session_manage.list_sessions` 的
# `plan_path`）全部经这里，任何一处自己拼字符串都会造成"写得到、读不到"。

# 文件名里**不允许**出现的字符（Windows 保留字符 + 控制字符 + 反斜杠）。
# 目录分隔符 `/` 也在此列 —— 模型只能定名字，定不了目录。
_PLAN_NAME_BAD = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]+")


def plan_filename(raw_name: str | None, fallback: str = "plan") -> str:
    """把（模型给的）名字清洗成一个安全的**文件名**（恒以 `.md` 结尾）。

    安全约束（缺一不可 —— 这是模型可控的唯一一段路径）：
      · 只取 basename：`..` / `a/b` / `/etc/x` / `C:\\x` 里的目录部分全部剥掉；
      · 去控制字符与 Windows 保留字符（含反斜杠），空白折叠成 `-`；
      · 去首尾的 `.`、`-`、空白（`..` / `.hidden` / `-` 这类会被清成空 → 走 fallback）；
      · 截断到 `PLAN_NAME_MAX`（截断后可能又露出尾部点号，再清一次）；
      · 清洗后为空 → `fallback`（保证**永远**有一个能落盘的名字）。
    """
    text = str(raw_name or "").strip()
    # Windows 上的目录分隔符也要当分隔符切（`Path` 在 macOS 上不认反斜杠）
    text = text.replace("\\", "/").split("/")[-1]
    text = _PLAN_NAME_BAD.sub("-", text)
    text = re.sub(r"\s+", "-", text)
    if text.lower().endswith(".md"):
        text = text[:-3]
    text = text.strip(" .-")
    if len(text) > PLAN_NAME_MAX:
        text = text[:PLAN_NAME_MAX].strip(" .-")
    if not text:
        text = str(fallback or "plan").strip(" .-") or "plan"
    return f"{text}.md"


def plan_relpath(raw_name: str | None) -> str:
    """文书**相对工作空间**的路径（POSIX 风格，恒为 `.aiagent/plan/<name>.md`）。

    为什么给相对路径而不是绝对路径：① meta 里不存绝对路径，工作空间整体搬家后
    仍然有效；② 右栏标签、`file_read` 的路径口径本来就是"相对工作空间根"。
    """
    return str(PurePosixPath(*PLAN_DIR_PARTS) / plan_filename(raw_name))


def plan_dir_for(workdir: Path | str) -> Path:
    """文书目录（绝对）：`<工作空间根>/.aiagent/plan/`。"""
    return Path(workdir).joinpath(*PLAN_DIR_PARTS)


def plan_display_path(plan_status: str | None, plan_name: str | None,
                      legacy_path: str | None = None) -> str | None:
    """计划文书**对外**的路径 —— 三个出口共用的唯一判据。

    出口共三处，必须逐字同口径，否则症状是"某一条通道漏了回退逻辑"（表现为
    重连后卡片点不开 / 标签路径对不上）：
      ① `Agent.execution_state()` → `execution_mode_changed` 广播；
      ② `ws_bridge._exec_mode_fields()` → `session_history`；
      ③ `SessionManager.list_sessions()` → `sessions` 列表（断线重连的唯一通道）。

    | `plan_status` | `plan_name` | 返回 |
    | --- | --- | --- |
    | 空 | 任意 | `None`（**没有计划状态就没有路径** —— `plan_name` 在重规划期间会先于状态存在，拿它当判据会让前端渲染一张空卡片） |
    | 非空 | 有 | 相对工作空间的 `.aiagent/plan/<name>.md`（新口径） |
    | 非空 | 无 | `legacy_path`（**存量会话**：文书还在旧的元数据目录里） |
    """
    if not plan_status:
        return None
    if plan_name:
        return plan_relpath(plan_name)
    return legacy_path or None


def resolve_plan_path(plan_dir: Path | str, raw_name: str | None,
                      previous_name: str | None = None) -> Path:
    """算出这份计划文书的**最终落点**（同名不撞车）。

    规则（`previous_name` = 本会话上一版文书的文件名，可为空）：
      1. 目标不存在，或目标就是**本会话上一版**的文件 → 直接用（重规划 = 覆盖）；
      2. 目标存在且属于**别的会话** → 依次试 `<stem>-2.md` / `-3.md` …（最多 98 次），
         绝不覆盖别人的计划；
      3. 全都占满（极端）→ 仍返回原目标（退化为覆盖，不阻断落盘）。

    为什么必须有这一层：文件名现在由模型给，两个会话取同一个"重构方案"是完全可能的，
    而文书一旦互相覆盖，前端卡片就会显示另一份计划的正文。
    """
    base = Path(plan_dir)
    name = plan_filename(raw_name)
    candidate = base / name
    if previous_name and name == plan_filename(previous_name):
        return candidate
    if not candidate.exists():
        return candidate
    stem = name[:-3]
    for i in range(2, 100):
        alt = base / f"{stem}-{i}.md"
        if not alt.exists():
            return alt
    return candidate


def task_files_for_session(session_id: str, session_prefix: str = "session_",
                           tasks_dir: Path | None = None) -> list[Path]:
    """返回指定会话的任务文件：0 或 1 个（文件不存在 → 空列表）。

    一个会话只有一个文件，但仍返回 list：调用方（`session_manage` 的两处级联
    清理）按列表遍历删除，改签名会牵动无关代码；且「不存在 → 空列表」对
    删除语义完全等价。回收站（trash/restore）**不动**这些文件 —— 还原后任务还在。

    `tasks_dir`（2026-09-18 新增）：该会话**所属工作空间**的 `.tasks` 目录。
    多工作空间下模块级 `TASKS_DIR` 只代表 default，故 `SessionManager` 显式传入
    `chat_history_dir.parent / ".tasks"`。口径仍唯一落在 `task_scope_file`。
    """
    path = task_file_for_session(session_id, session_prefix, tasks_dir)
    return [path] if path.exists() else []


# 模块导入即保证目录存在（保持原 tool_base.py 的导入期副作用）。
ensure_dirs()
