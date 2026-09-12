#!/usr/bin/env python3
"""
s09_code_cc.py —— 记忆系统 v2 (贴合真实 CC 行为)
═══════════════════════════════════════════════════
相对 s09_code.py 的核心改动：

  [改动 1] 移除"事后分析"模式
    删掉 select_relevant_memories / load_memories / extract_memories /
    consolidate_memories —— 不再每轮结束额外调 LLM 抽取记忆。

  [改动 2] 记忆写入改为 Tool
    新增 write_memory / forget_memory 两个工具，模型在对话中自行判断
    何时调用，即时落盘。无需额外 LLM 调用。

  [改动 3] 取消记忆正文注入
    MEMORY.md 索引始终在 SYSTEM 提示中（模型能看到"有什么"），
    但不再向用户消息前面拼接记忆正文。模型需要详情时可调 read_file 读。

  [改动 4] SYSTEM 提示加入触发规则
    告诉模型在哪些场景下应该调用 write_memory / forget_memory，
    而非依赖"事后分析"。

  [改动 5] agent_loop 简化
    去掉 pre_compress 快照、memory_turn 追踪、reactive_compact ——
    这些是 s08 压缩管道的残留，与记忆无关。

依赖：s08 (context compact)
使用：
    python s09_memory/s09_code_cc.py
    需要：pip install anthropic python-dotenv  +  .env 中配置 ANTHROPIC_API_KEY
"""

import os, subprocess, json, time, re
from pathlib import Path

# ─── readline 增强 ───────────────────────────────────────────────────
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"): os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ─── 路径与客户端初始化 ────────────────────────────────────────────
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"; MEMORY_DIR.mkdir(exist_ok=True)
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# ─── [改动 1] 删除：MEMORY_TYPES（不再需要全局常量） ───────────────
# 原 s09_code.py: MEMORY_TYPES = ["user", "feedback", "project", "reference"]
# 改为：类型定义直接写进 write_memory tool 的 input_schema enum 中


# ═══════════════════════════════════════════════════════════
#  记忆文件基础设施（与 s09_code.py 相同，保留复用）
# ═══════════════════════════════════════════════════════════
#  保留原因：write_memory_file / _rebuild_index / read_memory_index /
#  read_memory_file / list_memory_files / _parse_frontmatter 是基础的
#  文件存储层，Tool handler 和 SYSTEM 都需要它们。

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """极简 YAML frontmatter 解析器。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def write_memory_file(name: str, mem_type: str, description: str, body: str):
    """写入单条记忆文件，并重建索引。"""
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
    )
    _rebuild_index()
    return filepath


def _rebuild_index():
    """扫描 MEMORY_DIR 下所有 *.md，重建 MEMORY.md 索引。"""
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")


def read_memory_index() -> str:
    """读 MEMORY.md 索引全文。注入 SYSTEM 提示用。"""
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text().strip()
    return text if text else ""


# ─── [改动 2] 新增：记忆写入/删除工具 handler ──────────────────────

def run_write_memory(name: str, mem_type: str, description: str, body: str) -> str:
    """
    write_memory 工具的实际处理函数。
    模型通过 tool call 调用此函数，即时落盘。
    """
    try:
        path = write_memory_file(name, mem_type, description, body)
        print(f"\033[33m[Memory saved: {name} ({mem_type})]\033[0m")
        return f"Saved memory to {path.name}"
    except Exception as e:
        return f"Error saving memory: {e}"


def run_forget_memory(name: str) -> str:
    """
    forget_memory 工具的实际处理函数。
    模型调用此函数删除一条记忆。
    """
    try:
        slug = name.lower().replace(" ", "-").replace("/", "-")
        path = MEMORY_DIR / f"{slug}.md"
        if not path.exists():
            # 也尝试直接按文件名加载（模型可能传已有 slug）
            path = MEMORY_DIR / name
            if not path.exists():
                return f"Error: memory '{name}' not found"
        path.unlink()
        _rebuild_index()
        print(f"\033[33m[Memory deleted: {name}]\033[0m")
        return f"Deleted memory '{name}'"
    except Exception as e:
        return f"Error deleting memory: {e}"


# ─── [改动 3] 删除：load_memories / select_relevant_memories ─────────
# 原 s09_code.py 中有以下函数被整体删除：
#   - select_relevant_memories()    — 额外 LLM 调用选相关记忆
#   - load_memories()               — 在 user 消息前拼记忆正文
#   - extract_memories()            — turn 结束后分析对话抽取记忆
#   - consolidate_memories()        — 全量整合/做梦
#
# 理由：这些函数依赖"事后分析"模式——每轮结束额外调 LLM 去猜哪些值得记。
# 真实 CC 的模式是"模型在对话中即时判断"，通过 write_memory tool 直接写。


# ═══════════════════════════════════════════════════════════
#  [改动 4] SYSTEM 提示：加入触发规则
# ═══════════════════════════════════════════════════════════

def build_system() -> str:
    """
    拼装 SYSTEM 提示。
    关键变化：加入明确的记忆触发规则，模型据此决定何时调 write_memory。
    MEMORY.md 索引始终在提示中，模型能直接看到已有什么记忆。
    """
    index = read_memory_index()
    memories_section = f"\n\n## Available memories (MEMORY.md index)\n{index}" if index else ""

    # 显式的记忆触发规则（类似真实 CC 的 system prompt）
    memory_rules = """
## Memory system
You have a persistent memory system at `.memory/`. You can save and retrieve facts across sessions.

### When to save a memory
Call `write_memory` when the user:
- States a personal preference ("I like X", "don't use Y", "always do Z")
- Corrects your approach ("no, do it this way")
- Approves an approach ("yes, keep doing that", "good choice")
- Reveals a project fact or constraint ("we're using X", "deadline is Y", "stakeholder is Z")
- Mentions an external resource ("check the docs at ...", "bug tracker is ...")
- Asks you to "remember" something explicitly

### What to save
- **user**: The user's role, preferences, habits (e.g. "prefers spaces over tabs")
- **feedback**: Guidance about how to work (e.g. "don't mock databases in tests")
- **project**: Current goals, deadlines, architecture decisions (e.g. "using PostgreSQL 16")
- **reference**: Pointers to external resources (e.g. "API docs at docs.example.com")

### How to use memories
- Check the Available memories section above before each turn
- If a memory is relevant to the current task, read its file with `read_file .memory/<filename>`
- If the user contradicts a saved memory, call `forget_memory` to remove the old one
- Keep memories concise and factual. One sentence per entry."""
    return (
        f"You are a coding agent at {WORKDIR}."
        f"{memories_section}"
        f"{memory_rules}"
    )


# ─── 子 agent SYSTEM 提示（不加载记忆，子任务不需要知道记忆规则）───
# [改动] 子 agent 不暴露记忆工具，避免子任务意外污染记忆
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s08: 基础工具集
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired: return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines): lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path); file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content); return f"Wrote {len(content)} bytes to {path}"
    except Exception as e: return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text: return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e: return f"Error: {e}"

def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e: return f"Error: {e}"

def extract_text(content) -> str:
    if not isinstance(content, list): return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")

# ─── 子 agent ──────────────────────────────────────────────────────
SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
]
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}

def spawn_subagent(description: str) -> str:
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use": break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result: break
        if not result: result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  FROM s08: 上下文压缩管道（保留，没改动）
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000; KEEP_RECENT = 3; PERSIST_THRESHOLD = 30000

def estimate_size(msgs):
    return len(str(msgs))

def _block_type(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

def _message_has_tool_use(msg):
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(_block_type(block) == "tool_use" for block in content)

def _is_tool_result_message(msg):
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)

def snip_compact(msgs, mx=50):
    if len(msgs) <= mx: return msgs
    head_end, tail_start = 3, len(msgs) - (mx - 3)
    if head_end > 0 and _message_has_tool_use(msgs[head_end - 1]):
        while head_end < len(msgs) and _is_tool_result_message(msgs[head_end]):
            head_end += 1
    if (tail_start > 0 and tail_start < len(msgs)
            and _is_tool_result_message(msgs[tail_start])
            and _message_has_tool_use(msgs[tail_start - 1])):
        tail_start -= 1
    if head_end >= tail_start:
        return msgs
    return msgs[:head_end] + [{"role": "user", "content": f"[snipped {tail_start - head_end} msgs]"}] + msgs[tail_start:]

def collect_tool_results(msgs):
    blocks = []
    for mi, msg in enumerate(msgs):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result": blocks.append((mi, bi, block))
    return blocks

def micro_compact(msgs):
    tr = collect_tool_results(msgs)
    if len(tr) <= KEEP_RECENT: return msgs
    for _, _, b in tr[:-KEEP_RECENT]:
        if len(b.get("content", "")) > 120: b["content"] = "[Earlier tool result compacted.]"
    return msgs

def persist_large(tid, out):
    if len(out) <= PERSIST_THRESHOLD: return out
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    p = TOOL_RESULTS_DIR / f"{tid}.txt"
    if not p.exists(): p.write_text(out)
    return f"<persisted-output>\nFull: {p}\nPreview:\n{out[:2000]}\n</persisted-output>"

def tool_result_budget(msgs, mx=200_000):
    last = msgs[-1] if msgs else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return msgs
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= mx: return msgs
    for _, block in sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True):
        if total <= mx: break
        c = str(block.get("content", ""))
        if len(c) <= PERSIST_THRESHOLD: continue
        block["content"] = persist_large(block.get("tool_use_id", "?"), c)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return msgs

def write_transcript(msgs):
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    p = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with p.open("w") as f:
        for m in msgs: f.write(json.dumps(m, default=str) + "\n")
    return p

def summarize_history(msgs):
    conv = json.dumps(msgs, default=str)[:80000]
    r = client.messages.create(model=MODEL, messages=[{"role": "user", "content":
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings, 3. files changed, 4. remaining work, 5. user constraints.\n\n" + conv}],
        max_tokens=2000)
    return extract_text(r.content).strip()

def compact_history(msgs):
    write_transcript(msgs)
    summary = summarize_history(msgs)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# ═══════════════════════════════════════════════════════════
#  [改动 2] TOOLS 定义：新增 write_memory / forget_memory
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents. Use `read_file .memory/<filename>` to read a memory's full details.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "task", "description": "Launch a subagent to handle a subtask.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    # ── [改动 2] 新增：记忆工具 ──────────────────────────────
    {"name": "write_memory",
     "description": "Save a piece of information to persistent memory. "
                    "Use when the user states a preference, corrects you, "
                    "approves an approach, reveals a project fact, or asks you to remember something. "
                    "The memory will be available in future sessions.",
     "input_schema": {"type": "object",
       "properties": {
         "name": {"type": "string", "description": "Short kebab-case identifier, e.g. 'user-preference-tabs'"},
         "type": {"type": "string",
                   "enum": ["user", "feedback", "project", "reference"],
                   "description": "user=preference/habit, feedback=guidance/correction, project=fact/decision, reference=external pointer"},
         "description": {"type": "string", "description": "One-line summary shown in MEMORY.md index"},
         "body": {"type": "string", "description": "Full detail in markdown. Include context and rationale."}
       },
       "required": ["name", "type", "description", "body"]}},
    {"name": "forget_memory",
     "description": "Delete a memory by its name or filename. "
                    "Use when the user contradicts a saved memory or asks you to forget something.",
     "input_schema": {"type": "object",
       "properties": {
         "name": {"type": "string", "description": "Name of the memory to delete (slug or filename)"}
       },
       "required": ["name"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "task": spawn_subagent,
    "write_memory": run_write_memory,       # [改动 2] 新增
    "forget_memory": run_forget_memory,     # [改动 2] 新增
}


# ═══════════════════════════════════════════════════════════
#  [改动 5] agent_loop 简化
# ═══════════════════════════════════════════════════════════
#  相对原版 s09_code.py 删除：
#    - pre_compress 快照（不再事后抽取记忆，不需要了）
#    - memory_turn 追踪（不再注入记忆正文）
#    - memories_content / load_memories 调用
#    - 压缩前快照保存
#    - turn 结束后的 extract_memories + consolidate_memories
#  保留：
#    - s08 的三段压缩管道（budget → snip → micro）
#    - compact_history 全量压缩
#    - prompt_too_long 兜底

MAX_REACTIVE_RETRIES = 1

def agent_loop(messages: list):
    """
    主 agent 循环。
    相对原版 s09 简化了记忆相关逻辑：
    - 模型通过 write_memory/forget_memory 工具自主管理记忆
    - 不再有"事后分析"抽取
    - SYSTEM 提示中包含记忆索引和触发规则
    """
    reactive_retries = 0

    # [改动 5] 删除：
    #   memories_content = load_memories(messages)
    #   memory_turn = len(messages) - 1 if ... else None
    # 理由：不再需要注入记忆正文，MEMORY.md 索引已包含在 SYSTEM 中

    # SYSTEM 提示中包含记忆索引
    system = build_system()

    while True:
        # [改动 5] 删除 pre_compress 快照 — 不再需要事后抽取记忆

        # ── s08 三段压缩管道 ─────────────────────────────────
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)

        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        try:
            # [改动 5] 删除 memories_content 拼接逻辑
            # 不再需要 .copy() + 临时替换，直接发 messages
            response = client.messages.create(
                model=MODEL, system=system, messages=messages, tools=TOOLS, max_tokens=8000
            )
            reactive_retries = 0
        except Exception as e:
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                # [改动 5] 简化：直接用 compact_history 代替 reactive_compact
                # reactive_compact 与 compact_history 功能重叠，保留一个更简洁
                write_transcript(messages)
                summary = summarize_history(messages)
                messages[:] = [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]
                reactive_retries += 1
                continue
            raise

        messages.append({"role": "assistant", "content": response.content})

        # [改动 5] 删除 extract_memories / consolidate_memories
        # 记忆写入由模型通过 write_memory tool 自主完成
        if response.stop_reason != "tool_use":
            return

        # ── 分派 tool_use ────────────────────────────────────
        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})


# ═══════════════════════════════════════════════════════════
#  REPL 入口（与 s09_code.py 相同）
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("s09-cc: Memory — Tool-based writing (CC realistic mode)")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try:
            query = input("\033[36ms09-cc >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text": print(block.text)
        print()
