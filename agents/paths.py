#!/usr/bin/env python3
"""
paths.py - 路径配置（单一事实来源）

集中定义所有工作目录相关路径常量，供全项目各模块引用。

从原 tool_base.py 顶部抽离，使「路径」与「工具行为」解耦：
- 之前路径常量散落在 tool_base.py，还要经 tools.py 二次导出，三层转发易迷失
- 现在所有路径一律在此定义，其他模块 `from paths import ...` 直接引用
- AGENTS.md 规则：工作目录相关常量统一在此管理，禁止在业务模块内重复声明
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

from config import AIGENT_HOME, migrate_legacy


# ── 根目录（启动 agent 时的当前工作目录） ──────────────────────────
ROOT_DIR = Path.cwd()

# 应用自身 home 目录（用户级，存放 skills / worktree / MCP 配置 / 应用配置）。
# 位于 ~/.aigent（config.py 定义），原 WorkSpace/HomeDir 内容由 migrate_legacy 一次性搬迁。

# 技能目录
SKILLS_DIR = AIGENT_HOME / "skills"

# worktree 目录（git worktree 实验分支挂载点）
WORKTREE_DIR = AIGENT_HOME / "worktrees"

# MCP 配置目录（真实 MCP：JSON 配置 + 本地示例 server 同目录）
MCP_DIR = AIGENT_HOME / "mcp"
# MCP 服务器配置文件（mcpServers 格式，多服务器）
MCP_CONFIG = MCP_DIR / "mcp_servers.json"

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


def workspace_paths(project_id: str, root: Path | str | None = None) -> WorkspacePaths:
    """构造某工作空间的路径束。

    - `project_id == "default"`：`root` 忽略（可省），沙箱根取遗留沙盒 `WORKDIR`
      —— 存量行为一字不变。
    - 自定义空间：**必须**给 `root`（其真实目录，由 `project_registry` 从
      projects.json 解出）。缺失即抛 `ValueError`：宁可响亮失败，也不要静默
      把沙箱根落到元数据目录上（那是数据灾难级错误）。
    """
    if project_id == DEFAULT_PROJECT_ID:
        return WorkspacePaths(DEFAULT_PROJECT_ID, DATA_ROOT, WORKDIR)
    if root is None:
        raise ValueError(f"自定义工作空间 {project_id!r} 缺少真实目录 root")
    return WorkspacePaths(project_id, PROJECTS_ROOT / project_id, Path(root))

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
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    TODO_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    DURABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MCP_DIR.mkdir(parents=True, exist_ok=True)
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
