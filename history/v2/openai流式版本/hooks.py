"""
hooks.py — Agent 钩子系统 (Hook System)

本模块实现了一个事件驱动的钩子(hook)系统,用于在 Agent 生命周期的关键节点
插入自定义逻辑。钩子机制是 s04 阶段的新增能力,它将原本散落在各处的横切
关注点(cross-cutting concerns),如权限检查、日志记录、输出监控等,
抽取为可插拔的回调函数,从而提高代码的可维护性和可扩展性。

事件类型 (Event Types)
─────────────────────
本系统共支持四种事件:
  1. UserPromptSubmit  — 用户提交 prompt 后、发送给 LLM 之前触发
  2. PreToolUse        — 工具调用执行前触发 (可用于阻断危险操作)
  3. PostToolUse       — 工具调用执行后触发 (可用于结果后处理)
  4. Stop              — Agent 主循环结束前触发 (可用于打印会话摘要)

工作原理 (How It Works)
───────────────────────
• 钩子系统被封装为 `HookSystem` 类,内部以 `self._hooks` 字典保存事件→回调列表。
• 通过 `hook_system.register(event, callback)` 注册钩子。
• 事件触发时,通过 `hook_system.trigger(event, *args)` 依次调用所有回调。
• 约定:若回调返回非 None 值,则视为"阻断"信号,后续钩子不再执行,
  且该返回值会作为拒绝原因回传给 Agent(用于 PreToolUse 阻断工具调用)。

对外使用 (Public API)
─────────────────────
本模块只提供 `HookSystem` 类,不提供默认实例。各引用方需自行:
    from hooks import HookSystem
    hook_system = HookSystem()
    hook_system.register_default_hooks()
如需隔离(测试或多 Agent 场景),也可为不同引用方 new 独立的 HookSystem,
互不干扰,各自维护独立的注册表与策略。

设计动机 (Why Hooks)
────────────────────
在 s03 之前,权限校验、命令黑名单等逻辑被硬编码在 Agent 主循环中。
s04 将其重构为钩子,带来以下好处:
  • 解耦:主循环不再关心具体的安全策略;
  • 可扩展:新增横切逻辑只需注册新钩子,无需修改主循环;
  • 可测试:每个钩子可独立单元测试;
  • 可配置:不同实例可拥有不同的 deny_list / destructive 策略。
"""

import json
import os

from paths import WORKDIR


# ═══════════════════════════════════════════════════════════════════════════
#  钩子系统 (HookSystem)
# ═══════════════════════════════════════════════════════════════════════════

class HookSystem:
    """
    事件驱动的钩子注册与触发器。

    每个实例独立维护一个事件→回调列表的注册表,并内置了 5 个常用钩子
    方法(权限校验、调用日志、大输出告警、上下文提示、会话摘要)。
    通过 `register_default_hooks()` 可一键注册全部内置钩子。
    """

    # ── 事件名常量 ────────────────────────────────────────────────────────
    # 集中定义避免字符串散落各处,降低拼写错误的概率。
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE       = "PreToolUse"
    POST_TOOL_USE      = "PostToolUse"
    STOP               = "Stop"

    # ── 安全策略默认值(类级别,实例可覆盖) ──────────────────────────────
    # 硬性黑名单:匹配到其中任一模式即直接阻断,无需用户确认。
    # 之所以"硬阻断",是因为这些命令一旦执行,后果不可逆或风险极高。
    DEFAULT_DENY_LIST = [
        "rm -rf /",   # 递归删除根目录 —— 经典灾难命令
        "sudo",       # 提权执行 —— 越权风险
        "shutdown",   # 关闭系统
        "reboot",     # 重启系统
        "mkfs",       # 格式化文件系统
        "dd if=",     # 低级磁盘写入
    ]

    # 软性危险清单:匹配到则弹窗提示用户二次确认,默认拒绝 (大写 N)。
    DEFAULT_DESTRUCTIVE = [
        "rm ",        # 普通删除 (注意末尾空格,避免误匹配 rmdir/rmt 等)
        "> /etc/",    # 重定向写入系统配置目录
        "chmod 777",  # 权限过度开放
    ]

    def __init__(self, silent: bool = False):
        # silent 模式：抑制所有钩子打印（cron 定时任务用，避免输出混淆主终端）
        self.silent = silent
        # 事件→回调列表注册表。顺序敏感:PreToolUse 中 permission_hook 必须
        # 排在 log_hook 之前,这样一旦权限被阻断,日志才会记录"被阻断"的状态。
        self._hooks: dict[str, list] = {
            self.USER_PROMPT_SUBMIT: [],
            self.PRE_TOOL_USE:       [],
            self.POST_TOOL_USE:      [],
            self.STOP:               [],
        }
        # 策略列表复制为实例属性,允许不同实例拥有不同策略(测试/多租户场景)。
        self.deny_list: list[str] = list(self.DEFAULT_DENY_LIST)
        self.destructive: list[str] = list(self.DEFAULT_DESTRUCTIVE)
        # MCP 破坏性工具查询回调（由外部注入 MCPManager.is_destructive，s19 真实 MCP）
        self.mcp_destructive_lookup = None

    def set_mcp_destructive_lookup(self, fn) -> None:
        """注入 MCP 破坏性工具查询函数：接收 mcp__{server}__{tool} 全名，返回 bool。"""
        self.mcp_destructive_lookup = fn

    # ── 注册与触发 ────────────────────────────────────────────────────────
    def register(self, event: str, callback):
        """
        注册一个钩子回调函数到指定事件。

        参数:
            event    — 事件名称,必须是 self._hooks 中已存在的 key 之一
            callback — 可调用对象,签名需与该事件的参数列表一致
        """
        self._hooks[event].append(callback)

    def trigger(self, event: str, *args):
        """
        触发指定事件下的所有钩子,并按注册顺序依次执行。

        执行语义:
            • 遍历 `self._hooks[event]` 列表;
            • 依次调用每个回调,传入 `*args`;
            • 一旦某个回调返回非 None 值,立即停止后续钩子执行,
              并将该返回值作为阻断原因返回 (短路行为);
            • 全部钩子都返回 None 时,返回 None (表示"放行"或"无需处理")。

        参数:
            event — 事件名称
            *args — 透传给各钩子的位置参数

        返回:
            首个返回非 None 的钩子的结果,或 None。
        """
        for callback in self._hooks[event]:
            result = callback(*args)
            if result is not None:  # 教学快捷约定:返回非 None 即视为阻断该工具调用
                return result
        return None

    # ═════════════════════════════════════════════════════════════════════
    #  内置钩子方法 (Built-in Hook Methods)
    # ═════════════════════════════════════════════════════════════════════
    # 下面 5 个方法都被设计为 bound method:既能被 `register(self.X)` 注入
    # 到注册表(此时 `self` 自动绑定,签名对外只暴露事件参数),又能在内部
    # 访问 `self.deny_list` / `self.destructive` 等策略,便于按实例定制。

    def permission_hook(self, tool_call: dict):
        """
        PreToolUse 钩子 —— 权限校验器。

        本函数是 s03 `check_permission()` 的直接迁移,但被重新挂载到
        PreToolUse 事件上,从而与主循环解耦。校验规则如下:

          ① 若工具是 bash:
              a) 命令匹配 self.deny_list 任一项 → 硬阻断 (返回拒绝原因)
              b) 命令匹配 self.destructive 任一项 → 弹窗询问用户,默认拒绝
          ② 若工具是 write_file / edit_file:
              目标路径解析后必须位于 WORKDIR 之内,否则弹窗询问用户。

        参数:
            tool_call — LangChain 风格的工具调用字典,结构为
                        {"name": 工具名(str), "args": 参数字典(dict), "id": 调用id(str)}
                        例如 {"name": "bash", "args": {"command": "rm foo"}, "id": "toolu_01"}
                        调用方约定参见 agent_full_v2.py 第 271 行
                        `hook_system.trigger("PreToolUse", tool_call)`。

        返回:
            None — 放行;
            字符串 — 拒绝原因,将阻断该工具调用并回传给 Agent。
        """
        # 把字典里的关键字段拆出来,避免后面反复用 tool_call["name"] / tool_call["args"] 的写法,
        # 风格与 check_permission.py 的 LangChain 改造保持一致。
        tool_name = tool_call.function.name
        # OpenAI SDK 返回的 function.arguments 是 JSON 字符串,需解析为 dict 才能按 key 取值
        raw_args = tool_call.function.arguments
        tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

               # ── 规则 1:bash 命令的硬黑名单 + 软危险检查 ──────────────────────
        if tool_name == "bash":
            # 1a) 硬黑名单:出现即拒绝,无需交互
            for pattern in self.deny_list:
                if pattern in tool_args.get("command", ""):
                    print(f"\n\033[2;95m⛔ Blocked: '{pattern}'\033[0m")
                    return "Permission denied by deny list"
            # 1b) 软危险:弹窗让用户决定,默认 N 即拒绝
            for kw in self.destructive:
                if kw in tool_args.get("command", ""):
                    print(f"\n\033[2;95m⚠  Potentially destructive command\033[0m")
                    print(f"\033[2;95m   Tool: {tool_name}({tool_args})\033[0m")
                    choice = input("   Allow? [y/N] ").strip().lower()
                    if choice not in ("y", "yes"):
                        return "Permission denied by user"

        # ── 规则 2:文件写入必须在工作目录之内 (防越权写入) ───────────────
        if tool_name in ("write_file", "edit_file"):
            # 把相对路径与 WORKDIR 拼接,再 resolve() 消除 ../ 之类的逃逸,
            # 最后用 is_relative_to 校验解析后的绝对路径是否仍在 WORKDIR 内。
            path = tool_args.get("path", "")
            if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
                print(f"\n\033[2;95m⚠  Writing outside workspace\033[0m")
                print(f"\033[2;95m   Tool: {tool_name}({tool_args})\033[0m")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"

        # ── 规则 3:MCP 破坏性工具 (destructiveHint) 二次确认 ─────────────
        # 与 bash 软危险审批同机制、同 UX;MCP_ALLOW_DESTRUCTIVE=true 时直接放行,
        # silent 模式( cron/非交互 )直接拒绝,避免 input() 挂死。
        if tool_name.startswith("mcp__") and self.mcp_destructive_lookup is not None:
            if self.mcp_destructive_lookup(tool_name):
                if os.environ.get("MCP_ALLOW_DESTRUCTIVE", "false").lower() == "true":
                    return None  # 显式放行
                if self.silent:
                    return "Permission denied (destructive MCP tool)"
                print(f"\n\033[2;95m⚠  Potentially destructive MCP tool\033[0m")
                print(f"\033[2;95m   Tool: {tool_name}({tool_args})\033[0m")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
        return None

    def log_hook(self, tool_call: dict):
        """
        PreToolUse 钩子 —— 通用日志记录器。

        每当 Agent 准备调用任何工具时,本钩子会以灰色字体打印一行简短的
        调用预览,方便用户在终端实时观察 Agent 的行为轨迹,便于排错与演示。

        参数:
            tool_call — LangChain 风格的工具调用字典,结构为
                        {"name": 工具名(str), "args": 参数字典(dict), "id": 调用id(str)}
                        调用方约定参见 agent_full_v2.py 第 271 行
                        `hook_system.trigger("PreToolUse", tool_call)`。
        返回:
            None (本钩子只做观察,从不阻断)。
        """
        # 拆出常用字段,保持与 permission_hook 一致的 LangChain 风格写法。
        tool_name = tool_call.function.name
        # OpenAI SDK 返回的 function.arguments 是 JSON 字符串,需解析为 dict 才能按 key 取值
        raw_args = tool_call.function.arguments
        tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        # 取 args 字典的前两个值,转为字符串后截断至 60 字符,
        # 避免长参数 (如大段代码、巨型文件) 把终端刷屏。
        args_preview = str(list(tool_args)[:2])[:60]
        # print(f"\033[2;95m[HOOK] {tool_name}({args_preview})\033[0m")
        return None

    def large_output_hook(self, tool_call: dict, output: str):
        """
        PostToolUse 钩子 —— 大输出告警。

        工具的返回结果可能非常庞大 (例如 read_file 读取大文件、bash 执行
        `git log` 返回数千行)。当输出超过 10 万字符时,本钩子会以黄色字体
        提醒用户注意,以免淹没上下文窗口或拖慢后续处理。

        参数:
            tool_call — LangChain 风格的工具调用字典 (与 PreToolUse 一致)。
            output    — 工具执行的返回结果 (任意类型,会被 str() 转换以测量长度)。
        返回:
            None (仅告警,不阻断)。
        """
        if len(str(output)) > 100000:
            print(f"\033[2;95m[HOOK] ⚠ Large output from {tool_call['name']}: "
                  f"{len(str(output))} chars\033[0m")
        return None

    def context_inject_hook(self, query: str):  # noqa: ARG001 — 钩子契约要求签名
        """
        UserPromptSubmit 钩子 —— 上下文注入提示。

        在用户输入被送往 LLM 之前打印一行灰色日志,标明当前工作目录。
        这一信息可帮助 LLM 更好地"理解"用户在哪个项目下操作,
        同时也方便用户确认 Agent 没有跑错目录。

        注意:此处仅做"提示/观察",并不真正修改 query —— 真正的上下文注入
        可在本钩子返回新字符串时实现 (本系统的约定)。

        参数:
            query — 用户原始输入的字符串 (钩子契约要求,本钩子未使用)。
        返回:
            None (不修改 query,只打印日志)。
        """
        if not self.silent:
            print(f"\033[2;95m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
        return None

    def summary_hook(self, messages: list):
        """
        Stop 钩子 —— 会话摘要。

        Agent 主循环即将退出时触发,统计本会话中共发起了多少次工具调用,
        并以一行灰色日志呈现。这对演示、教学和事后审计都很有价值。

        实现细节:
            `messages` 是完整的对话历史,元素可能是两类对象:
              ① 属性访问 .content;
              ② 普通 dict ({"role": ..., "content": ...}) —— 通过 .get() 取 content。
            工具结果以 role="tool" 标式出现,所以最直接的统计方式是
            m.get("role") == "tool"。同时为兼容旧的"content 是 list 且其中
            含 type=='tool_result' 块"的 dict 格式,仍保留对这种结构的扫描。
            Pydantic 对象没有 .get() 方法,故必须先用 isinstance 分流,
            否则会抛出 AttributeError (本次 bug 的根因)。
        """
        tool_count = 0
        for m in messages:
            # 路径 ①:LangChain Pydantic BaseMessage —— 优先按类型统计
            if m.get("role") == "tool":
                tool_count += 1
                continue
            # 路径 ②:dict —— 旧格式,需要先判断对象类型再调用 .get()
            if isinstance(m, dict):
                content = m.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("role") == "tool":
                            tool_count += 1
        if not self.silent:
            print(f"\033[2;95m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
        return None

    # ═════════════════════════════════════════════════════════════════════
    #  一键注册 (Default Registration)
    # ═════════════════════════════════════════════════════════════════════
    def register_default_hooks(self):
        """
        一键注册全部内置钩子。

        注册顺序很重要:
          • permission_hook 必须先于 log_hook —— 这样一旦权限被阻断,
            日志会反映"被阻断"的事实 (而不是显示一条最终未执行的成功日志);
          • 其余钩子顺序对功能无影响,按可读性排列。
        """
        self.register(self.USER_PROMPT_SUBMIT, self.context_inject_hook)  # 用户输入观察
        self.register(self.PRE_TOOL_USE,       self.permission_hook)      # ① 权限校验 (先)
        self.register(self.PRE_TOOL_USE,       self.log_hook)             # ② 调用日志  (后)
        self.register(self.POST_TOOL_USE,      self.large_output_hook)    # 大输出告警
        self.register(self.STOP,               self.summary_hook)         # 会话结束摘要

