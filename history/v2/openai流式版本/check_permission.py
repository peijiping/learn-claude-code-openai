#!/usr/bin/env python3
"""
check_permission.py - 智能体工具调用的权限检查模块（LangChain 版本）

本模块在工具真正执行之前插入"三道闸门"检查，构成最小可用的权限系统：

    +-------+    +-----------+    +-----------+    +-----------+    +------+
    | 工具  | -> | Gate 1    | -> | Gate 2    | -> | Gate 3    | -> | 执行 |
    | 调用  |    | 硬拒绝列表|    | 规则匹配  |    | 用户确认  |    | 工具 |
    +-------+    +-----------+    +-----------+    +-----------+    +------+
         |             |               |                |
         v             v               v                v
      (正常)        (直接拒绝)     (询问用户)       (用户拒绝？)

调用方只需要在 Agent 循环里加一行：

    if not check_permission(tool_call):
        continue

注意：本文件中的 `WORKDIR` 由调用方在外部 import 后注入，
通常取自 `tools` 模块（定义如 `WORKDIR = Path.cwd() / "WorkSpace"`），
代表"工作空间根目录"，用于判断文件操作是否越界。
"""

# ---------------------------------------------------------------------------
# Gate 1：硬拒绝列表（Hard Deny List）
# ---------------------------------------------------------------------------
# 这些模式属于"绝对不允许"的高危操作，无论上下文如何都必须直接拦截。
# 选用子串匹配（`in`）而不是正则，目的是降低复杂度并让规则一目了然。
# 例如：
#   "rm -rf /"        —— 递归删除根目录
#   "sudo"            —— 提权执行
#   "shutdown/reboot" —— 关机/重启
#   "mkfs"            —— 格式化文件系统
#   "dd if="          —— 块设备读写
#   "> /dev/sda"      —— 直接覆写磁盘
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]


def check_deny_list(command: str) -> str | None:
    """
    在 Bash 命令字符串中扫描硬拒绝列表。

    参数:
        command: 即将执行的 shell 命令原文

    返回:
        如果命中拒绝模式，返回拦截原因字符串；
        否则返回 None，表示放行到下一道闸门。
    """
    # 遍历所有危险模式；只要命令中包含任一模式就立刻拦截
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    # 没有命中任何危险模式
    return None

from pathlib import Path
from paths import WORKDIR

# ---------------------------------------------------------------------------
# Gate 2：规则匹配（Rule Matching）
# ---------------------------------------------------------------------------
# 与 Gate 1 的"硬编码黑名单"不同，这里的规则是"上下文相关"的：
#   - 对哪些工具生效（tools 字段）
#   - 用什么条件判断（check 字段，接收 args 字典，返回 True 表示"命中规则"）
#   - 命中后给用户什么提示（message 字段）
#
# 任何一条规则命中，都会把工具调用挂起、交给 Gate 3 询问用户。
PERMISSION_RULES = [
    # 规则 1：写入/编辑文件时，目标路径必须在 WORKDIR 之内
    # WORKDIR / args.get("path", "")  —— 拼出"以工作目录为基准的相对路径"
    # .resolve()                      —— 解析符号链接、"../" 等，拿到绝对真实路径
    # .is_relative_to(WORKDIR)        —— 判断绝对路径是否仍在 WORKDIR 范围内
    # not ...                         —— 范围外则视为"越权写入"
    {"tools": ["write_file", "edit_file"],
     "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
     "message": "Writing outside workspace"},

    # 规则 2：Bash 命令中包含某些"潜在破坏性"关键字
    # 注意：这里使用的关键字比 DENY_LIST 更宽松（带空格、模糊匹配），
    # 因此命中后不是直接拒绝，而是交给 Gate 3 让用户确认。
    {"tools": ["bash"],
     "check": lambda args: any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
     "message": "Potentially destructive command"},
]


def check_rules(tool_name: str, args: dict) -> str | None:
    """
    遍历 PERMISSION_RULES，找出第一条与当前工具调用匹配的规则。

    参数:
        tool_name: 工具名称，例如 "bash"、"write_file"
        args:      工具参数，字典形式

    返回:
        命中的规则提示信息；没有任何规则命中则返回 None。
    """
    for rule in PERMISSION_RULES:
        # 工具名匹配 且 条件函数返回 True ——> 视为命中
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# ---------------------------------------------------------------------------
# Gate 3：用户确认（User Approval）
# ---------------------------------------------------------------------------
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    """
    在终端中向用户发起确认请求。

    参数:
        tool_name: 工具名
        args:      工具参数
        reason:    为什么需要确认（来自 Gate 2 的 message）

    返回:
        "allow"  —— 用户同意放行
        "deny"   —— 用户拒绝（默认行为，输入非 y/yes 即视为拒绝）

    实现细节:
        - 使用 ANSI 转义序列给提示信息上色：
            \033[33m...\033[0m  黄色（警告）
            \033[31m...\033[0m  红色（严重）
        - input() 是阻塞的，Agent 循环会在这里暂停，等待用户键入。
    """
    # 黄色警告：让用户先看到"为什么要打断我"
    print(f"\n\033[33m⚠  {reason}\033[0m")
    # 打印工具名 + 参数，方便用户判断是否放行
    print(f"   Tool: {tool_name}({args})")
    # [y/N] 中 N 大写表示"默认拒绝"，符合最小权限原则
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


# ---------------------------------------------------------------------------
# 主入口：把三道闸门串成一条流水线
# ---------------------------------------------------------------------------
def check_permission(tool_call: dict) -> bool:
    """
    对单个工具调用执行完整的权限检查流水线。

    参数:
        tool_call: LangChain 风格的工具调用字典，结构为：
                   {
                       "name": 工具名称（str）,
                       "args": 工具参数（dict）,
                       "id":   调用 id（str，可选）
                   }
                   例如：
                   {"name": "bash", "args": {"command": "rm foo.txt"}, "id": "toolu_01"}

    返回:
        True  —— 通过所有闸门，允许执行
        False —— 任意闸门拒绝，应跳过该次调用
    """
    # 先把字典里的关键字段拆出来，避免后面反复用 tool_call["name"] 这种写法
    tool_name = tool_call["name"]
    tool_args = tool_call["args"]

    # ---- Gate 1：硬拒绝列表 ----
    # 只对 bash 工具做命令级检查（其他工具没有"command"字段）
    if tool_name == "bash":
        reason = check_deny_list(tool_args.get("command", ""))
        if reason:
            # 命中硬黑名单：红色 ⛔ 输出 + 直接拒绝，不再询问用户
            print(f"\n\033[31m⛔ {reason}\033[0m")
            return False

    # ---- Gate 2：规则匹配 ----
    reason = check_rules(tool_name, tool_args)
    if reason:
        # ---- Gate 3：用户确认 ----
        # 命中规则后必须询问用户；用户拒绝则直接返回 False
        decision = ask_user(tool_name, tool_args, reason)
        if decision == "deny":
            return False

    # 顺利通过三道闸门，放行
    return True
