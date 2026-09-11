#!/usr/bin/env python3
"""
s11: Error Recovery — three recovery paths + exponential backoff.

Run:  python s11_error_recovery/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s10:
  - LLM call wrapped in try/except with three recovery paths
  - Path 1: max_tokens -> escalate 8K->64K (no append on first escalation),
            then continuation prompt (max 3)
  - Path 2: prompt_too_long -> reactive compact -> retry (once)
  - Path 3: 429/529 -> exponential backoff with jitter (max 10),
            fallback model on consecutive 529
  - with_retry wrapper for transient errors
  - RecoveryState tracks escalation / compact / 529 / model

ASCII flow:
  messages -> prompt assembly -> compress+load -> [try] LLM [except] -> tools -> loop
                                                    |          |
                                              stop_reason   error type
                                              max_tokens?   prompt_too_long? -> compact
                                              escalate /    429/529? -> backoff
                                              continue      other? -> log + exit
"""

import os, subprocess, time, random, json
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
PRIMARY_MODEL = os.environ["MODEL_ID"]
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# ── Constants ──

ESCALATED_MAX_TOKENS = 64000
DEFAULT_MAX_TOKENS = 8000
MAX_RECOVERY_RETRIES = 3
MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_CONSECUTIVE_529 = 3
# 续写提示：模型输出被 max_tokens 截断后，追加到 messages 的 user 消息
# 要求：直接接着写，不要道歉、不要复述上文、从中途断掉的地方继续
CONTINUATION_PROMPT = (
    "输出已达 token 上限，直接续写 —— "
    "不要道歉，不要复述，从中途中断处接着往下写。"
)

# ── Prompt Assembly (from s10, synced) ──

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


# ── Tools (unchanged) ──

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
]

TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


# ── Error Recovery (s11 new) ──

class RecoveryState:
    """Track recovery attempts across the loop."""
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt, retry_after=None):
    """Exponential backoff with jitter. Retry-After takes priority."""
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def with_retry(fn, state: RecoveryState):
    """
    【内层异常处理】—— 只负责"透明重试"。

    职责范围：仅处理临时性错误（429 限流 / 529 服务过载），
    通过指数退避 + 抖动等待后，**用相同的入参**重新调用 fn。

    不能处理的错误（比如 prompt_too_long、auth 失败等）：
    会立即 raise 出去，交给外层 except 决定怎么恢复（通常是改输入或放弃）。
    """
    # 最多重试 MAX_RETRIES（10）次；attempt 从 0 开始
    for attempt in range(MAX_RETRIES):
        try:
            # 调用真正的 LLM 请求（lambda 封装的那行 client.messages.create）
            result = fn()
            # 成功：把 529 连续计数器清零（说明服务端恢复了）
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()

            # ── 分支 1：429 限流 ──
            # 现象：请求太快，API 拒绝；处理：等一会儿再试，**不修改任何入参**
            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)            # 指数退避 + 抖动
                print(f"  \033[33m[429 rate limit] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue                                 # 回到 for 顶部，再试一次

            # ── 分支 2：529 服务端过载 ──
            # 现象：上游模型服务器忙不过来；处理：退避等待，
            # 如果连续多次 529 且配置了 FALLBACK_MODEL，则切换到备用模型
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                # 连续 3 次 529，触发"换模型"策略
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if FALLBACK_MODEL:
                        # 切换到备用模型，并把计数器清零（在新模型上重新计数）
                        state.current_model = FALLBACK_MODEL
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" switching to {FALLBACK_MODEL}\033[0m")
                    else:
                        # 没配备用模型，只能继续重试主模型
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529 overloaded] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # ── 既不是 429 也不是 529：属于"无法靠重试解决"的错误 ──
            # 例如：prompt_too_long、context 超限、auth 失败、参数错误等。
            # 这里的 raise 是"我不处理，往上传"的信号，
            # 外层 except 会接住并按错误类型做 compact / 放弃等策略。
            raise
    # 10 次重试全部用完（429/529 始终没恢复），抛错给外层
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    """Check whether an API error indicates prompt/context too long."""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


def reactive_compact(messages: list) -> list:
    """Emergency compact — teaching version keeps last N messages.
    Real CC generates a compact summary via LLM, then retries with
    the compacted message list. Teaching version simplifies to tail
    retention since s08/s09 already cover LLM-based compact."""
    print("  \033[31m[reactive compact] trimming to last 5 messages\033[0m")
    tail = messages[-5:]
    return [{"role": "user",
             "content": "[Reactive compact] Earlier conversation trimmed. "
                        "Continue from where you left off."}, *tail]


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state: which tools exist, whether memory files exist."""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    """Main loop with error recovery wrapping LLM calls."""
    system = get_system_prompt(context)
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # ── LLM 调用：内层 with_retry 管 429/529 重试；外层 except 管其他错误 ──
        try:
            # 把 LLM 请求包成 lambda 有两个目的：
            #   1) with_retry(fn, state) 的签名要求 fn 是"无参可调用对象"，
            #      而 client.messages.create() 需要传 model/max_tokens 等参数，
            #      用 lambda 包一层，调用时直接 fn() 即可。
            #   2) 默认参数 mt=max_tokens, mdl=state.current_model 是"值快照"——
            #      lambda 定义那一行，就把这一轮的 max_tokens / model
            #      拷贝到 lambda 自己的形参里锁住；后续即使外层改了这两个变量，
            #      这个已经创建好的 lambda 用到的还是旧值。
            # 关键：lambda 写在 while 循环里，每轮迭代都会重新创建一次，
            # 所以 max_tokens 从 8K 改到 64K 之后，是靠"下一轮重新定义 lambda"
            # 让新 lambda 锁住 64K，而不是靠闭包去读变量名。
            response = with_retry(
                lambda mt=max_tokens, mdl=state.current_model:
                    client.messages.create(
                        model=mdl, system=system, messages=messages,
                        tools=TOOLS, max_tokens=mt),
                state)
        except Exception as e:
            # 【外层异常处理】—— 内层 with_retry 主动 raise 出来的"非临时错误"会到这一层。
            # 这里的策略是"修改输入后再试"或"放弃"，不是简单的重试。

            # ── Path 2：prompt 超出模型上下文窗口 ──
            # 现象：API 报错说 prompt 太长；处理：压缩 messages 后再试一次
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    # 第一次触发：做一次紧急压缩（保留最近 5 条 + 一条提示消息）
                    # messages[:] = ... 是原地替换列表内容，让外层引用也看到新内容
                    messages[:] = reactive_compact(messages)
                    # 标记"已经试过 compact"，避免下次又触发 compact 陷入死循环
                    state.has_attempted_reactive_compact = True
                    continue                              # 回到 while 顶部，用更短的 messages 再试
                # 第二次还超长 → 没救了，向用户报告错误
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": "[Error] Context too large, cannot continue."}]})
                return

            # ── 兜底：其他所有错误（auth 失败、参数错、未知异常等）──
            # 既不能重试也不能 compact，直接退出当前轮
            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}]})
            return

        # ── Path 1：max_tokens 截断恢复 ──
        # 注意：max_tokens 不是异常，是 API 正常返回的 stop_reason 之一
        # 含义：模型想继续输出但被 max_tokens 上限截断，需要扩大额度让它说完
        if response.stop_reason == "max_tokens":
            # ── 第一阶段：扩大额度（8K → 64K）──
            # 第一次遇到截断时不要把这段"断头输出"塞进 messages，
            # 因为它很可能是半句话/半个 JSON，没啥用；直接把上限拉大重试更划算
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS            # 8K → 64K
                state.has_escalated = True                   # 标记：已升级过一次
                print(f"  \033[33m[max_tokens] escalating"
                      f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
                continue                                     # 回到 while 顶部，用 64K 重发同样的请求

            # ── 第二阶段：64K 还不够，进入"续写"模式 ──
            # 此时输出截断是发生在"快要说完"的位置，把这部分内容留着
            # 然后塞一条 user 提示让模型接着写
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:  # 最多续写 3 次
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"  \033[33m[max_tokens] continuation"
                      f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
                continue                                     # 回到 while 顶部，让模型续写
            # 续写 3 次还是截断 → 放弃，告诉用户
            print("  \033[31m[max_tokens] recovery limit reached\033[0m")
            return

        # ── 正常完成：把 assistant 的回复追加到对话历史 ──
        # 注意：这里的 response.content 是模型完整返回的内容
        # （如果是 max_tokens 截断，就已经在上面 append 过了，不会走到这里）
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        # ── Tool execution ──
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

        context = update_context(context, messages)
        system = get_system_prompt(context)


if __name__ == "__main__":
    print("s11: error recovery")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        turn_start = len(history)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        for msg in history[turn_start:]:
            if msg.get("role") != "assistant":
                continue
            for block in msg["content"]:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
