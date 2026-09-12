#!/usr/bin/env python3
"""
paths.py - 路径配置（单一事实来源）

集中定义所有工作目录相关路径常量，供全项目各模块引用。

从原 tool_base.py 顶部抽离，使「路径」与「工具行为」解耦：
- 之前路径常量散落在 tool_base.py，还要经 tools.py 二次导出，三层转发易迷失
- 现在所有路径一律在此定义，其他模块 `from paths import ...` 直接引用
- AGENTS.md 规则：工作目录相关常量统一在此管理，禁止在业务模块内重复声明
"""

from pathlib import Path


# ── 根目录（启动 agent 时的当前工作目录） ──────────────────────────
ROOT_DIR = Path.cwd()

# 应用自身 home 目录（存放 skills / worktree / 项目元数据 / 应用配置）
HOME_DIR = ROOT_DIR / "WorkSpace/HomeDir"

# 技能目录
SKILLS_DIR = HOME_DIR / "skills"

# worktree 目录（git worktree 实验分支挂载点）
WORKTREE_DIR = HOME_DIR / "worktrees"

# MCP 配置目录（真实 MCP：JSON 配置 + 本地示例 server 同目录）
MCP_DIR = HOME_DIR / "mcp"
# MCP 服务器配置文件（mcpServers 格式，多服务器）
MCP_CONFIG = MCP_DIR / "mcp_servers.json"

# 工作目录（所有工具操作的沙盒根）
WORKDIR = ROOT_DIR / "WorkSpace/task1"

# 待办目录（与每个 session 绑定的轻量级任务看板）
TODO_DIR = WORKDIR / ".todo"
# 待办文件命名随 session 变化，不再用全局 TODO_FILE 常量
# 路径生成见 todo_file_for_session()

# 团队目录
TEAM_DIR = WORKDIR / ".team"

# 收件箱目录
INBOX_DIR = WORKDIR / ".inbox"

# 对话历史目录
CHAT_HISTORY_DIR = WORKDIR / ".chathistory"

# L4 / reactive 时 transcript 落盘的目录名
TRANSCRIPT_DIRNAME = WORKDIR / ".transcripts"

# L3 落盘大 tool_result 的目录名
TOOL_RESULTS_DIRNAME = WORKDIR / ".task_outputs/tool-results"

# 记忆目录
MEMORY_DIR = WORKDIR / ".memory"

# 记忆索引文件
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

# 任务目录
TASKS_DIR = WORKDIR / ".tasks"

# 持久化路径：所有 durable=True 的任务会被序列化到该文件，重启后自动恢复
DURABLE_PATH = WORKDIR /".scheduler"/ "scheduled_tasks.json"

# 工作流运行时目录（s16：快照 + journal + 输出文件，对应教程的 .runtime/）
WORKFLOW_DIR = WORKDIR / ".workflow"
# 最近一次工作流 runId（resume 入口从这里读取）
WORKFLOW_LAST_RUN = WORKFLOW_DIR / "last_run.txt"


def ensure_dirs() -> None:
    """一次性创建所有需要预先存在的目录（幂等）。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    TODO_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    DURABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)


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
