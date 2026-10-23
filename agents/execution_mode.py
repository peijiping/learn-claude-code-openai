#!/usr/bin/env python3
"""
execution_mode.py - 任务执行模式（计划 plan / 目标 goal）的判定与状态

设计文档：docs/frontend/22-任务执行模式（计划与目标）.md（唯一权威源）。

本模块**只管 plan**：
- `ExecutionGate`：每 Agent 实例一个，持有 plan 的内存态（模式投影 / 文书状态 /
  文书路径），并给出 PreToolUse 守卫的判定 `blocked_reason()`。
- `bash_is_read_only()`：plan 模式专用的**窄只读判定**（比权限链更严）。

为什么不复用权限链的 ⑦-2 白名单（2026-09-25 评审结论，勿回退）
──────────────────────────────────────────────────────────
权限链（`permission.py`）的安全白名单语义是「**够安全、可免审批**」，不是「只读」：
`BUILTIN_SAFE_COMMANDS` 里有 `find` / `env` / `git branch` / `du` / `echo`，配合
`pattern_matches` 的 token 前缀匹配，下面这些会**全数命中白名单**：

    find . -exec rm -rf {} +      env X=1 rm -rf /      git branch -D main

且 ⑦-2 只是 `_bash_category_decision` 循环里的一句 `if`，被 ⑥ 会话记忆与 ⑦-1
危险模式**前后夹持**（⑦-1 必须先于白名单 —— `cat x > /etc/passwd` 曾被静默放行），
单独抽出即**判定变松**。故本模块只复用权限链的**原语**（分段 / token / 模式匹配），
**不复用那份白名单策略数据**，`permission.py` 一行不改。

铁律（写在此处并在测试中断言）：
    plan 的放行集合 **严格小于** 权限链的 allow 集合。

goal 不在本模块
──────────────
goal 的唯一真相是 `goal_controller.active`；`execution_mode == "goal"` 只是它的
**投影**。goal 模式的进入 / 退出由 `Agent.set_execution_mode()` 转发**既有**
`set_goal` / `clear_goal` 完成，判定逻辑（评估器 + Stop 七分支）一行不改；
goal 模式也不拦任何工具（既有行为）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from permission import (
    BUILTIN_DANGEROUS,
    BUILTIN_DENY,
    cross_pattern_matches,
    deny_tokens_in_command,
    pattern_matches,
    split_command_segments,
    tokenize,
)
from refs import file_preview_max_bytes
from logger import get_logger

log = get_logger("execution_mode")

# ═══════════════════════════════════════════════════════════════════════════
#  常量：模式 / 文书状态 / 工具名
# ═══════════════════════════════════════════════════════════════════════════

MODE_NORMAL = "normal"
MODE_PLAN = "plan"
MODE_GOAL = "goal"
VALID_EXECUTION_MODES = (MODE_NORMAL, MODE_PLAN, MODE_GOAL)

# 计划文书状态：None（未产出）/ ready（待批准）/ approved（已批准，模式已回落）
PLAN_STATUS_READY = "ready"
PLAN_STATUS_APPROVED = "approved"
VALID_PLAN_STATUSES = (PLAN_STATUS_READY, PLAN_STATUS_APPROVED)

PLAN_WRITE_TOOL = "plan_write"

# ── plan 允许的只读工具（allowlist：**未列入者一律阻断**，安全默认）─────────
# 工具名已按 agents/tools.py 逐名核对（`"name": "..."` 字面量）——这些名字
# **真实存在**。反面例子记牢：`read_file` / `glob` / `view_image` 都**不存在**
# （`view_image` 已于 2026-09-21 并入 `run_read`）。写错名字不会报错，只会表现为
# "模型怎么老被拦"，极难排查。
PLAN_READONLY_TOOLS = frozenset({
    # 只读探索
    "run_read",              # 文本/图片/PDF/Office 统一读入口（只读）
    "run_glob",              # 纯搜索，不读内容
    "ask_user",              # 向用户澄清是规划的必要动作
    PLAN_WRITE_TOOL,         # 计划模式的本职出口
    "load_skill", "list_skills",
    "list_tasks", "get_task",
    "check_background",
    "list_crons", "list_mcp", "list_worktrees", "check_inbox",
    # 刻意**不**列入（都是写/副作用，走"其余 → 阻断"兜底）：
    #   run_write / run_edit / write_memory / forget_memory /
    #   create_task / claim_task / complete_task / update_task / delete_task /
    #   schedule_cron / cancel_cron / run_workflow /
    #   spawn_teammate / send_message / request_shutdown / request_plan / review_plan /
    #   create_worktree / remove_worktree / keep_worktree /
    #   connect_mcp / mcp__* / sub_agent
})

# ═══════════════════════════════════════════════════════════════════════════
#  bash 窄只读判定（plan 专用）
# ═══════════════════════════════════════════════════════════════════════════

# 只读命令头（**显式枚举的只读子集**）。它已核对为 `BUILTIN_SAFE_COMMANDS`
# 的真子集，因此 "head ∈ 本集合" 天然满足"plan 放行集合 ⊂ 权限链 allow 集合"。
# 刻意**不含** `env` / `find -exec` 这类"启动器"语义的命令头（见下）。
PLAN_BASH_READONLY_HEADS = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "find",
    "which", "file", "stat", "du", "df", "echo", "date",
    "git",   # git 另受子命令白名单约束（见 PLAN_BASH_GIT_SUBCOMMANDS）
})

# git 只放行这些子命令
PLAN_BASH_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "branch"})
# `git branch` 的额外 token 只允许这些（`git branch foo` 会**创建分支**，
# `git branch -D/-d/-m/-M/-c/-C` 会改/删分支 → 一律不在此列）
PLAN_BASH_GIT_BRANCH_SAFE_FLAGS = frozenset({
    "-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose",
    "--list", "--merged", "--no-merged", "--contains", "--sort", "--format",
})

# 否决位 A：子串（出现即 False）
#   `>`      —— 覆盖 `>` / `>>` / `2>&1` / `>&`：重定向是**写**的最短路径
#   `<<`     —— 覆盖 `<<` / `<<<`：here-doc / here-string 同属重定向语义
#   `$(`、`` ` `` —— 命令替换：`echo "$(rm -rf x)"` 会以 `echo` 为头绕过
PLAN_BASH_DENY_SUBSTRINGS: tuple[str, ...] = (">", "<<", "$(", "`")

# 否决位 B：token 精确匹配（出现即 False）
#   -exec/-execdir/-ok/-okdir/-delete/-fprint*/-fls* —— `find` 的执行与落盘动作
#   --output —— `git diff --output=<file>` 会写文件（`git diff` 是放行头）
#   tee/dd/xargs/truncate/sponge —— 写通道
#   --pre —— `rg --pre <cmd>` 会执行外部命令
#
# 刻意**不含** `-i` / `-o`（与早期草案的差异，理由是可验证的）：
#   它们的"写"语义只对 `sed -i` / `perl -i` / `sort -o` / `curl -o` 成立，
#   而这些命令头**本身不在只读白名单内**（head 检查已先拦下）；
#   对放行头（`ls -i` / `grep -i` / `df -i` / `grep -o` / `find … -o`）
#   它们是**只读开关**。拦它们只会误伤最常用的 `grep -i` 探索，零安全收益。
PLAN_BASH_DENY_TOKENS = frozenset({
    "--output", "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls", "-fls0",
    "tee", "dd", "xargs", "truncate", "sponge", "--pre",
})

# 否决位 C：按命令头的额外禁区。
#   `date -s` / `date --set` 会**改系统时钟**（`-s` 对其它头是只读开关，
#   故按头精确限定，不做全局 token 否决）。
PLAN_BASH_PER_HEAD_DENY: dict[str, frozenset[str]] = {
    "date": frozenset({"-s", "--set"}),
}

# 命令替换 / 重定向之外的兜底：`sudo` 等硬拒绝与危险模式在 ④ 步叠加（见下）


def _deny_token_hit(tok: str, deny: frozenset) -> bool:
    """token 是否命中否决集 —— 含 `=` 值与短选项聚合两种形态。

    ⚠️ 为什么不能只做 `tok in deny`（测试抓到的两个**真实绕过**）：
      · `--output=FILE`（`git diff --output=patch.diff`）与 `--output FILE` 等价，
        token 却是 `--output=patch.diff` → 精确匹配漏过 → 静默写文件；
      · `--set=now`（`date --set=now`）同理会漏过 → 静默改系统时钟；
      · `date -s2020-01-01`（短选项带值）等价 `date -s 2020-01-01` → 同样要拦。

    三种形态：
      ① 精确：`--output` / `-exec` / `tee`
      ② 长选项带值：`--xxx=VALUE` → 取 `=` 前的选项名
      ③ 短选项聚合：`-sVALUE` → 取头两个字符（仅当该短选项在否决集里）
    """
    if tok in deny:
        return True
    if tok.startswith("--"):
        eq = tok.find("=")
        return eq > 2 and tok[:eq] in deny
    if tok.startswith("-") and len(tok) > 2:
        return ("-" + tok[1]) in deny
    return False


def _plan_bash_deny_hit(segment: str) -> str | None:
    """段内否决位（子串 + token）命中 → 返回人类可读的原因；未命中 None。"""
    if any(sub in segment for sub in PLAN_BASH_DENY_SUBSTRINGS):
        return "命令包含重定向或命令替换（`>` / `<<` / `$(` / 反引号）"
    for tok in tokenize(segment):
        if _deny_token_hit(tok, PLAN_BASH_DENY_TOKENS):
            return f"命令包含写通道或危险开关（{tok.split('=', 1)[0]}）"
    return None


def bash_is_read_only(command: str, store: dict | None = None) -> bool:
    """整条 bash 命令是否**只读**（plan 模式专用判定；比权限链更严）。

    逐段判定（`;` `&&` `||` `|` 换行分段），**每段都必须过**才返回 True：

      ① 解析失败 / 空 → False（fail-closed，与权限链同款）
      ② 任一段含否决位（重定向 / 命令替换 / 写通道开关）→ False
      ③ 段首 token 必须在 `PLAN_BASH_READONLY_HEADS`（显式只读子集）
         · `git` → 子命令必须在 `PLAN_BASH_GIT_SUBCOMMANDS`；
           `git branch` 的其余 token 必须是安全开关（拒绝 `git branch foo` / `-D`）
         · `env` → **只允许裸 `env`**（打印环境变量）。`env X=1 cmd` 是启动器，
           且 `env PATH=/tmp/evil ls` 会换掉实际执行的二进制 → 一律 False
         · `date` → 拒绝 `-s` / `--set`
      ④ 叠加**更严**的既有层（不得比权限链更松）：
         · `permission.deny_tokens_in_command`（敏感路径 token）命中 → False
         · `BUILTIN_DENY ∪ store.deny_patterns` 命中 → False
         · `BUILTIN_DANGEROUS ∪ store.dangerous_patterns` 命中（含跨段模式）→ False
      ⑤ 全面通过 → True

    `store` 为 `permission.PermissionGate.store.load()` 的结果 dict（可为 None：
    此时只用内置清单，仍然 fail-closed）。
    """
    segments = split_command_segments(str(command or ""))
    if not segments:
        return False

    # ② 段级否决位（先做：与权限链"越严的越先判"同款）
    for seg in segments:
        if _plan_bash_deny_hit(seg):
            return False

    # ④a 敏感路径 token（`cat ~/.ssh/id_rsa` 这类）
    if deny_tokens_in_command(str(command or "")):
        return False

    # ④b 硬拒绝 + 危险模式（含跨段模式）。store 不可用时只用内置清单。
    store = store if isinstance(store, dict) else {}
    deny_patterns = list(BUILTIN_DENY) + list(store.get("deny_patterns") or [])
    dangerous = list(BUILTIN_DANGEROUS) + list(store.get("dangerous_patterns") or [])
    token_lists = [tokenize(seg) for seg in segments]
    for seg in segments:
        for pattern in deny_patterns + dangerous:
            if (pattern_matches(pattern, seg)
                    or cross_pattern_matches(pattern, token_lists)):
                return False

    # ③ 逐段命令头白名单
    for seg in segments:
        tokens = tokenize(seg)
        if not tokens:
            return False
        head = tokens[0]
        if head == "env":
            # 裸 `env`（只打印环境）才算只读；带任何参数都是启动器语义
            if len(tokens) != 1:
                return False
            continue
        if head not in PLAN_BASH_READONLY_HEADS:
            return False
        forbidden = PLAN_BASH_PER_HEAD_DENY.get(head)
        if forbidden and any(_deny_token_hit(t, forbidden) for t in tokens[1:]):
            return False
        if head == "git":
            if len(tokens) < 2:
                return False
            sub = tokens[1]
            if sub not in PLAN_BASH_GIT_SUBCOMMANDS:
                return False
            if sub == "branch":
                extra = tokens[2:]
                if extra and not all(t in PLAN_BASH_GIT_BRANCH_SAFE_FLAGS
                                     for t in extra):
                    return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  计划文书路径（**口径已迁到 `paths.py`**，2026-09-29）
# ═══════════════════════════════════════════════════════════════════════════
# 2026-09-25 版这里放过 `plan_relpath(sid)` / `plan_file_for(paths, sid)` /
# `plans_dirname()` —— 那一版的落点是"由 sid 唯一决定"的
# `<元数据目录>/plans/session_<sid>.md`。2026-09-29 改成"工作空间内 + 模型命名"
# 之后，路径再也不由 sid 决定，于是整套口径收进 `paths.py`：
#
#   `paths.plan_dir_for(workdir)`             → <工作空间根>/.aiagent/plan/
#   `paths.plan_filename(raw_name)`           → 清洗模型给的名字（恒 .md 结尾）
#   `paths.plan_relpath(raw_name)`            → 相对工作空间的展示/读取路径
#   `paths.resolve_plan_path(dir, name, prev)` → 同名不撞车的最终落点
#   `paths.plan_display_path(status, name, legacy)` → 三个出口共用的对外路径判据
#
# **刻意不再在这里留同名包装**：两个模块各有一个 `plan_relpath` 是纯粹的
# 事故隐患（签名还不一样），读代码的人一定会拿错。
# 存量会话（升级前产出的文书）仍走 `paths.plan_file_for_session` 回退读取。


# ═══════════════════════════════════════════════════════════════════════════
#  计划文书的读取（供 `plan_read` 命令；**不经沙箱**）
# ═══════════════════════════════════════════════════════════════════════════
# 放在本模块（而不是 ws_bridge）的两个理由：
#   ① 这是 plan 域的职责（与 `plan_file_for` 同源，路径与读取不分家）；
#   ② `ws_bridge` 有**模块级副作用**（import 即构造全局 Agent 并写一行日志），
#      纯文件函数留在那里就没法在不污染 `~/.aigent` 的前提下单测。

def plan_content_payload(path_str: str, *, project_id: str = "",
                         session_id: str = "", name: str = "",
                         size: int = 0, mtime: float = 0.0, text: str = "",
                         lines: int = 0, too_large: bool = False,
                         truncated: bool = False, reason: str = "") -> dict:
    """`plan_read` 回执的统一形状（**所有分支都从这一个构造函数出去**）。

    与 ws_bridge 的 `file_content_disabled` 同一套字段 —— 前端不必为"计划文书"
    再写一套解析（契约见 docs/frontend/22 §4.5：复用既有降级字段形状）。
    """
    return {
        "project_id": project_id,
        "session_id": session_id,
        "path": str(path_str or ""),
        "name": name,
        "size": int(size),
        "mtime": float(mtime),
        "encoding": "utf-8" if not reason else "",
        "binary": False,
        "too_large": bool(too_large),
        "truncated": bool(truncated),
        "lines": int(lines),
        "text": text,
        "reason": reason,
    }


def read_plan_file(path: Path) -> dict:
    """读取计划文书正文（**元数据目录**内，**不经沙箱**，绝不抛异常）。

    ⚠️ 为什么**不能**复用 `refs.read_workspace_file`：它的第一步
    `resolve_within(workdir, …)` 把根**钉死在工作区**，而计划文书落在
    `<data_root>/plans/`（`~/.aigent/projects/<id>/`）—— **天然在工作区之外**，
    走那条路只会得到"路径不在当前工作空间内"。落点由 sid 唯一决定、内容只由
    Agent 写入，没有越界面，故直读。

    大小上限复用 `refs.file_preview_max_bytes()`（512KB，与文件预览同口径）；
    超限**一点内容都不读**（"宁可不给，不给半个"），给 `too_large=True` + 人话 reason。
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return plan_content_payload(str(path), reason="计划文书不存在（可能已被清理）")
    except (OSError, ValueError) as exc:
        # ValueError：路径含 NUL 字符这类连 stat 都进不去的情况（`stat()` 会直接抛
        # ValueError 而不是 OSError）—— 本函数契约是**绝不抛异常**。
        log.warning("计划文书 stat 失败 %s: %s", path, exc)
        return plan_content_payload(str(path), reason="无法访问计划文书")
    if not path.is_file():
        return plan_content_payload(
            str(path), name=path.name, reason="计划文书不是一个文件")

    cap = file_preview_max_bytes()
    if int(st.st_size) > cap:
        return plan_content_payload(
            str(path), name=path.name, size=int(st.st_size),
            mtime=float(st.st_mtime), too_large=True,
            reason=f"计划文书过大（{st.st_size} 字节，上限 {cap}）")
    try:
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        log.warning("计划文书读取失败 %s: %s", path, exc)
        return plan_content_payload(
            str(path), name=path.name, reason="读取计划文书失败")
    text = data.decode("utf-8", errors="replace")
    return plan_content_payload(
        str(path), name=path.name, size=int(st.st_size), mtime=float(st.st_mtime),
        text=text, lines=(text.count("\n") + 1 if text else 0))


# ═══════════════════════════════════════════════════════════════════════════
#  阻断文案（回填模型，必须可行动）
# ═══════════════════════════════════════════════════════════════════════════

def block_message(tool_name: str, detail: str = "") -> str:
    """plan 模式下工具被拦时的 tool_result 文案。

    契约（与 permission_hook 同款）：返回**字符串即阻断**，由 agent_loop
    回填为该 tool_call 的 result。文案必须可行动 —— 模型看到后应该知道
    "改用 plan_write 提交计划"，而不是反复重试同一个写操作。
    """
    lines = ["Error: Plan mode is active — this tool is blocked."]
    if detail:
        lines.append(detail)
    lines.append(
        "Write your implementation plan with plan_write, "
        "then wait for user approval."
    )
    lines.append(f"Blocked tool: {tool_name}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
#  ExecutionGate：plan 的内存态 + 判定（每 Agent 实例一个）
# ═══════════════════════════════════════════════════════════════════════════

class ExecutionGate:
    """计划模式的守卫与状态（**只管 plan**，不持有任何 goal 状态）。

    与 `PermissionGate` 的分工：权限回答"允不允许 / 要不要审批"（规则级），
    本门回答"这一轮以什么方式干"（模式级）。两者正交，判定链**不合并** ——
    钩子层按「plan 守卫 → 权限」的顺序各判一次，plan 拦下的集合是权限链的子集。

    线程模型：`blocked_reason` 由 turn 工作线程（PreToolUse）调用，而
    `set_plan_mode` / `approve_plan` 由事件循环经 `asyncio.to_thread` 调用 ——
    跨线程读写，故状态访问一律持锁（本类**自带锁**，与 GoalController 不同）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 模式（UI 投影）：normal / plan / goal。goal 的真相不在这里。
        self.mode: str = MODE_NORMAL
        # 文书状态：None / "ready" / "approved"（**plan 的真源**）
        self.plan_status: str | None = None
        # 文书路径（str | None）；仅用于展示与回执，真相仍是磁盘
        self.plan_path: str | None = None
        # 文书文件名（`<name>.md`，2026-09-29 起由**模型**给名字）。
        # 它是 meta 里 `plan_name` 的唯一来源 —— 路径再也不由 sid 推出，
        # 所以这份名字必须落盘，否则切会话/重启后就找不到自己的文书了。
        self.plan_name: str | None = None

    # ── 状态读写 ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """当前状态的原子快照（供信封 / session_history 统一形状）。"""
        with self._lock:
            return {
                "mode": self.mode,
                "plan_status": self.plan_status,
                "plan_path": self.plan_path,
                "plan_name": self.plan_name,
            }

    def set_mode(self, mode: str) -> None:
        """设置模式投影（非法值忽略）。"""
        if mode not in VALID_EXECUTION_MODES:
            return
        with self._lock:
            self.mode = mode

    def set_plan_mode(self) -> None:
        """进入计划模式：清掉上一份文书的**状态**（文件保留，下次产出即覆盖）。

        `plan_name` **刻意保留**：它记的是"本会话当前的文书文件"，重规划时
        `paths.resolve_plan_path(..., previous_name=plan_name)` 据此直接覆盖自己那一份
        （而不是被当成"别人的文件"另起一个 `-2`）。文书路径仅在 `plan_status`
        非空时才对外可见 —— 见 `Agent._plan_display_path`。
        """
        with self._lock:
            self.mode = MODE_PLAN
            self.plan_status = None

    def mark_plan_ready(self, path: str | Path | None,
                        name: str | None = None) -> None:
        """`plan_write` 成功：文书就绪，等待批准。"""
        with self._lock:
            self.plan_status = PLAN_STATUS_READY
            if path is not None:
                self.plan_path = str(path)
            if name:
                self.plan_name = str(name)

    def approve_plan(self) -> None:
        """批准计划：文书转 approved（模式回落由 Agent 统一负责）。"""
        with self._lock:
            self.plan_status = PLAN_STATUS_APPROVED

    def clear_plan(self) -> None:
        """退出计划模式：清状态 + 清路径 + 清文件名（模式回落由 Agent 统一负责）。"""
        with self._lock:
            self.plan_status = None
            self.plan_path = None
            self.plan_name = None

    def restore_plan_from_meta(self, meta: dict | None) -> None:
        """init_session / switch_session 时从会话元数据恢复 plan 状态。

        只认合法状态值；meta 无记录（存量会话）→ 回到"未产出文书"。
        `plan_name` 一并恢复 —— 写入端与恢复端必须同源，否则切会话后
        `plan_path` 会退化成存量口径（读不到自己刚写的文书）。
        """
        meta = meta if isinstance(meta, dict) else {}
        status = meta.get("plan_status")
        if status not in VALID_PLAN_STATUSES:
            status = None
        name = meta.get("plan_name")
        with self._lock:
            self.plan_status = status
            self.plan_name = (str(name) if isinstance(name, str) and name.strip()
                              else None)

    # ── 判定（PreToolUse 守卫的唯一入口）────────────────────────────────
    def blocked_reason(self, tool_name: str, tool_args: Any = None,
                       store: dict | None = None) -> str | None:
        """plan 激活**且文书未批准**时，判断该工具调用是否应被阻断。

        返回：None = 放行；字符串 = 阻断原因（作为 tool_result 回填模型）。
        非 plan 模式 → 恒 None（零开销放行，normal / goal 的路径逐字节不变）。
        """
        with self._lock:
            mode, status = self.mode, self.plan_status
        if mode != MODE_PLAN or status == PLAN_STATUS_APPROVED:
            return None
        name = str(tool_name or "")
        if name == "bash":
            command = str((tool_args or {}).get("command") or "")
            if not bash_is_read_only(command, store):
                return block_message(
                    name,
                    "Only read-only shell commands are allowed in plan mode "
                    "(no redirection, no command substitution, no write channels).",
                )
            return None
        if name in PLAN_READONLY_TOOLS:
            return None
        return block_message(name)

    # ── 便捷判据 ───────────────────────────────────────────────────────
    def plan_blocks_writes(self) -> bool:
        """plan 激活且文书未批准 → 写操作应被拦（供提示词注入判据用）。"""
        with self._lock:
            return self.mode == MODE_PLAN and self.plan_status != PLAN_STATUS_APPROVED
