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

# 记忆目录
MEMORY_DIR = DATA_ROOT / ".memory"

# 记忆索引文件
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

# 任务目录
TASKS_DIR = DATA_ROOT / ".tasks"

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
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
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


def todo_file_for_session(session_num: int) -> Path:
    """
    返回指定 session 编号对应的 todo 文件路径。

    todo 是会话内轻量级任务看板，与 chat history 一一绑定：
    每个 session 有独立 todo 文件，会话切换时同步切换。

    文件命名：.todo/session_<N>.todo.json（与 .chathistory/session_<N>.jsonl 同 N）。
    """
    return TODO_DIR / f"session_{session_num}.todo.json"


# 模块导入即保证目录存在（保持原 tool_base.py 的导入期副作用）。
ensure_dirs()
