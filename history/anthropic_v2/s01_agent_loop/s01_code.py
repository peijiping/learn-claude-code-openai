#!/usr/bin/env python3
"""
s01_agent_loop.py - 智能体循环（The Agent Loop）

整个 AI 编程智能体的秘密可以浓缩成一个非常简单的模式：

    while stop_reason == "tool_use":
        response = LLM(messages, tools)   # 调用大模型
        execute tools                      # 执行模型请求的工具
        append results                     # 把工具执行结果回填到对话历史

    +----------+      +-------+      +---------+
    |  用户    | ---> | 大模型| ---> | 工具执行 |
    |  提示词  |      | (LLM) |      |         |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |  tool_result  |
                          +---------------+
                          （循环继续）

这是 AI 智能体的核心循环：把工具执行的结果回填给模型，
让模型自己决定是继续调用工具还是结束对话。
生产环境中的智能体会在这个基础上再叠加策略（policy）、
钩子（hooks）以及生命周期控制等机制。

使用方式：
    pip install anthropic python-dotenv
    ANTHROPIC_API_KEY=... python s01_agent_loop/code.py
"""

import os
import subprocess

# ── 解决 macOS 下 libedit 处理中文输入退格异常的兼容性补丁 ──
try:
    import readline
    # macOS 自带的 readline 底层是 libedit，在输入中文时按退格键
    # 会把多字节字符逐字节删除，导致输入错乱。下面这四行
    # readline 配置可以禁用 libedit 的特殊字符处理，从而修复该问题。
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    # 在不支持 readline 的平台（如 Windows）上跳过即可
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# 从项目根目录的 .env 文件加载环境变量；
# override=True 表示允许 .env 中的值覆盖系统已有的同名环境变量，
# 方便开发者在本地灵活切换不同模型 / API Key。
load_dotenv(override=True)

# 如果用户配置了 ANTHROPIC_BASE_URL（常见于使用第三方兼容网关时），
# 则主动移除可能造成冲突的 ANTHROPIC_AUTH_TOKEN，
# 让 SDK 只通过 BASE_URL 完成认证，避免重复鉴权失败。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化 Anthropic 客户端；base_url 可用于切换到任何兼容 Anthropic 协议的代理网关。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量中读取要使用的大模型 ID（例如 claude-3-5-sonnet-latest 等）。
MODEL = os.environ["MODEL_ID"]

# 系统提示词：告诉模型自己是谁、能做什么、应该如何行动。
# 注意：这是一段会发给模型的字符串，不是注释，所以保持英文以避免改变模型行为。
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# ── 工具定义：只对外暴露一个 bash 工具 ────────────────────────────
# 工具的 schema 必须符合 Anthropic 官方规定的 JSON Schema 格式，
# 模型会依据这个 schema 判断何时调用工具以及如何传参。
# 同样地，name / description / input_schema 的内容都会作为提示发给模型，
# 属于"功能字符串"而非注释，保持英文。
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


# ── 工具执行 ────────────────────────────────────────
def run_bash(command: str) -> str:
    """
    执行模型给出的 shell 命令，并把执行结果以字符串形式返回。

    为了避免模型误操作造成不可逆损失，这里内置了一个非常基础的
    "危险命令黑名单"，命中后将直接拒绝执行并返回错误提示。
    """
    # 危险命令黑名单：包含下列任一子串的命令将被拦截。
    # 注意这是非常粗糙的字符串匹配，仅作演示用途，
    # 生产环境应当使用更严格的策略层（如权限系统、沙箱等）。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        # 通过 subprocess 执行命令，各参数含义如下：
        #   shell=True  —— 整条 command 交由 shell 解析，允许使用管道、重定向等；
        #   cwd=os.getcwd() —— 让命令在当前工作目录下执行；
        #   capture_output=True —— 捕获标准输出与标准错误；
        #   text=True —— 让输出按文本（str）而非字节（bytes）返回；
        #   timeout=120 —— 单条命令最长允许运行 120 秒。
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        # 把 stdout 和 stderr 拼到一起，方便模型一次性看到全部输出。
        out = (r.stdout + r.stderr).strip()
        # 单次返回内容最多截断为 50000 字符，避免超长输出撑爆上下文窗口。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 命令执行超过 120 秒仍未结束
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        # 找不到可执行文件，或其它操作系统层面的错误
        return f"Error: {e}"


# ── 核心模式：不断调用模型并执行工具，直到模型决定停止 ──
def agent_loop(messages: list):
    """
    智能体主循环。

    工作流程：
        1) 把当前对话历史发给大模型；
        2) 解析模型的回复；
        3) 如果模型没有要求调用工具（stop_reason != "tool_use"），
           说明模型已经给出最终答复，结束循环；
        4) 否则依次执行模型请求的每一个工具调用，
           并把执行结果以 tool_result 的形式追加回对话历史；
        5) 回到第 1 步，让模型继续处理新的信息。
    """
    while True:
        # 1) 调用大模型，传入完整对话历史 + 工具定义
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,  # 限制单次回复的最大 token 数
        )

        # 2) 把模型这一轮的回复（可能既包含文本块也包含工具调用块）
        #    整体追加到对话历史中，以便后续多轮对话中保留上下文。
        messages.append({"role": "assistant", "content": response.content})

        # 3) stop_reason 表示模型停止生成的原因：
        #    - "end_turn"：模型认为对话可以结束；
        #    - "tool_use"：模型希望调用工具；
        #    - "max_tokens"：达到最大 token 限制；
        #    - "stop_sequence"：触发了自定义停止符。
        #    只要不是 tool_use，就直接退出循环。
        if response.stop_reason != "tool_use":
            return

        # 4) 模型请求了至少一个工具调用，需要逐个执行。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 用黄色把正在执行的命令回显到终端，便于人类观察调试。
                print(f"\033[33m$ {block.input['command']}\033[0m")
                # 真正执行 bash 命令并拿到结果
                output = run_bash(block.input["command"])
                # 在终端上只预览输出的前 200 个字符，避免刷屏影响阅读。
                print(output[:200])
                # 把工具结果封装成 tool_result 块，并保留 tool_use_id
                # 以便模型把结果对应到具体的工具调用上。
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

        # 5) 把这一轮所有工具的执行结果作为 user 角色的消息追加回对话历史。
        #    下一次循环里模型就能"看到"这些结果，并决定下一步动作。
        messages.append({"role": "user", "content": results})


# ── 入口 ──────────────────────────────────────────
if __name__ == "__main__":
    print("s01: Agent Loop")
    print("输入问题，回车发送。输入 q 退出。\n")

    # history 保存整个对话历史，每轮用户输入都会追加进去，
    # 同一个进程内的多轮对话共享这份历史，从而具备上下文记忆能力。
    history = []
    while True:
        try:
            # 青色提示符，等待用户输入问题
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            # 收到 Ctrl+D / Ctrl+C 时优雅退出 REPL
            break
        # 输入为空或 q/exit 时退出循环
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 把用户问题作为 user 角色加入对话历史
        history.append({"role": "user", "content": query})
        # 启动智能体循环，让模型自由地调用工具直到给出最终答案
        agent_loop(history)
        # 循环结束后，最后一条消息是 assistant 的完整回复，
        # 从中抽取文本块并打印到终端供用户阅读。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                # 只关心 type == "text" 的内容块，tool_use 等其它类型无需打印
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
