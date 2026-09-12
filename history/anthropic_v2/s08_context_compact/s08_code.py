#!/usr/bin/env python3
"""
s08_context_compact.py —— 上下文压缩（Context Compact）

本课程是 v2 教程的第八节，主题是"四层压缩管道"。
当 Agent 长时间运行后，对话历史会不断膨胀，最终可能撞到模型的上下文窗口上限。
Claude Code 源码里给出的解法是：把压缩拆成多个层级，按"便宜优先、昂贵兜底"的顺序执行。

────────────────────────────────────────────────────────────────────
四层压缩管道（每次调用 LLM 前都会跑一遍）：
────────────────────────────────────────────────────────────────────

    L1: snip_compact       —— 当消息条数 > 50 时，把中间一段消息直接裁掉
    L2: micro_compact      —— 把"较旧"的 tool_result 替换成占位文本
    L3: tool_result_budget —— 把超大的工具输出（> 30KB）落盘，前端只保留 2KB 预览
    L4: compact_history    —— 调用一次 LLM，对整段历史做摘要压缩

    兜底：reactive_compact —— 当以上四层都不够、API 仍返回 prompt_too_long 时触发

────────────────────────────────────────────────────────────────────
管道整体执行顺序（与 Claude Code 源码一致）：
────────────────────────────────────────────────────────────────────

    ┌─────────────────────────────────────────────────────────────┐
    │  messages[]                                                 │
    │    ↓                                                        │
    │  L3 budget ─→ L1 snip ─→ L2 micro ─→ [token > threshold?]  │
    │                                      ├─ No  → LLM           │
    │                                      └─ Yes → L4 summary    │
    │                                              ↓              │
    │                                          LLM call           │
    │                                    [prompt_too_long?]        │
    │                                      └─ Yes → reactive      │
    └─────────────────────────────────────────────────────────────┘

核心心法：便宜优先，昂贵兜底；执行顺序与 CC 源码保持一致：
budget → snip → micro → auto。

本节基于 s07（技能加载）扩展而来。运行方式：

    python s08_context_compact/code.py
    依赖：pip install anthropic python-dotenv；.env 中需要 ANTHROPIC_API_KEY
"""

# ───────────────────────────────────────────────────────────────────
# 标准库与第三方依赖
# ───────────────────────────────────────────────────────────────────
import ast, json, os, subprocess, time  # ast 用于解析 Python 字面量；json 写 transcript；subprocess 跑 shell；time 给 transcript 命名
from pathlib import Path                # 用 Path 做跨平台路径处理

# readline 让交互式输入支持行编辑（上下方向键调出历史命令）。
# macOS/Linux 自带 readline，Windows 上没有；这里用 try/except 优雅降级。
try:
    import readline
    # 关闭 TTY 特殊字符绑定，避免 Ctrl-C 之类的组合键在 REPL 里失效
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic  # Anthropic 官方 SDK，提供 messages.create() 等高层 API
from dotenv import load_dotenv    # 从 .env 文件加载环境变量

# 加载 .env（override=True 允许 .env 覆盖系统中已有的同名变量）。
# 教程里为了演示"自定义 base_url"做了这一行：
#   - 如果设置了 ANTHROPIC_BASE_URL，说明要走代理/中转
#   - 此时清掉 ANTHROPIC_AUTH_TOKEN，避免中转地址和官方 token 不匹配的告警
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ───────────────────────────────────────────────────────────────────
# 全局路径与客户端
# ───────────────────────────────────────────────────────────────────
WORKDIR = Path.cwd()                                    # 当前工作目录，作为 Agent 的"沙箱根目录"
SKILLS_DIR = WORKDIR / "skills"                          # 技能目录（来自 s07）
TRANSCRIPT_DIR = WORKDIR / ".transcripts"                # 压缩前先把完整历史写到该目录，方便事后追溯
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"  # 落盘工具结果（> 30KB 时用得到）
# 初始化 Anthropic 客户端；base_url 可选，用于接入中转/代理
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]                          # 使用的模型 ID（从环境变量读）
CURRENT_TODOS: list[dict] = []                          # 全局 Todo 状态（被 todo_write 工具更新）


# ═════════════════════════════════════════════════════════════════════
# s07 继承：技能注册表（Skill Registry）
# ═════════════════════════════════════════════════════════════════════
# 技能以 SKILL.md 文件形式放在 skills/ 目录下，文件顶部用 YAML frontmatter
# 声明 name / description。正文是完整的提示词/指令。Agent 启动时扫描一次。

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    解析 SKILL.md 顶部的 YAML 风格 frontmatter。
    约定：开头必须是 ---，再用 --- 结束，中间是简单的 key: value 行。
    这里不引入 PyYAML，保持零依赖。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)              # 只切第一个冒号，value 里允许出现 ":"
            meta[k.strip()] = v.strip().strip('"').strip("'")  # 去掉两端引号
    return meta, parts[2].strip()                  # 返回 (元数据字典, 正文)

# 技能注册表：name -> { name, description, content }
SKILL_REGISTRY: dict[str, dict] = {}

def _scan_skills():
    """
    扫描 skills/ 目录，把所有 SKILL.md 读入 SKILL_REGISTRY。
    启动时调用一次即可。
    """
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            # 优先用 frontmatter 的 name/description；缺失时回退到目录名/首行
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}

# 启动时一次性扫描
_scan_skills()

def list_skills() -> str:
    """返回技能目录的简短列表，供 SYSTEM 提示词使用。"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())

def load_skill(name: str) -> str:
    """按名字加载技能全文，让 LLM 拿到完整指令。"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]


# ═════════════════════════════════════════════════════════════════════
# 系统提示词
# ═════════════════════════════════════════════════════════════════════
# 主 Agent 的系统提示词：附带工作目录和技能目录。
def build_system() -> str:
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )

SYSTEM = build_system()  # 启动后只构建一次

# 子 Agent 的系统提示词：故意更"小"，不包含技能目录、压缩工具等元能力。
# 因为子 Agent 任务范围窄、上下文短，不需要这些开销。
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═════════════════════════════════════════════════════════════════════
# 基础工具（继承自 s02-s07，未改动）
# ═════════════════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """
    把相对路径解析为绝对路径，并校验它没有逃出 WORKDIR。
    防止 LLM 用 "../" 之类的路径越权访问沙箱外。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """跑 shell 命令并返回 stdout+stderr。120 秒超时，输出截断到 5 万字符。"""
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """读文件内容，可选地只读前 limit 行（节省上下文）。"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """写文件，自动创建父目录。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """精确文本替换（只替换一次）。"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    """glob 模式匹配文件，过滤掉逃出 WORKDIR 的结果。"""
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"

def _normalize_todos(todos):
    """
    容忍多种入参格式：
    - 已经是 list[dict] 就直接走校验
    - 字符串先按 JSON 解析，失败再按 Python 字面量解析
    - 校验每条至少含 content + status，且 status 必须是合法三态之一
    返回 (normalized_list, error_message)；出错时 list 为 None。
    """
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    """更新全局 todo 列表，并即时打印带颜色的进度条，方便用户观察。"""
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]  # 33m = 黄色
    for t in CURRENT_TODOS:
        icon = {
            "pending": " ",
            "in_progress": "\033[36m▸\033[0m",   # 36m = 青色
            "completed": "\033[32m✓\033[0m",      # 32m = 绿色
        }[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

def extract_text(content) -> str:
    """从 LLM 返回的 content 块中抽取纯文本，兼容 list/str 两种形式。"""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


# ═════════════════════════════════════════════════════════════════════
# 子 Agent（继承自 s06-s07，未改动）
# ═════════════════════════════════════════════════════════════════════
# 子 Agent 用一份独立的小工具集（没有 compact/todo/load_skill 等"元能力"），
# 跑自己的 agent_loop，最长 30 轮；返回最后一条 assistant 文本作为结果。

SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]
SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}

def spawn_subagent(description: str) -> str:
    """
    起一个子 Agent 处理复杂子任务；返回它的最终结论文本。
    整段循环最多 30 轮；不会再去调用 compact。
    """
    print(f"\n\033[35m[Subagent spawned]\033[0m")  # 35m = 品红
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        # 收集本轮 tool_use 的执行结果
        results = []
        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)  # 触发 Pre 钩子
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)  # 触发 Post 钩子
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

    # 抽最后一条 assistant 文本作为结论
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═════════════════════════════════════════════════════════════════════
# s08 新增：四层压缩管道
# ═════════════════════════════════════════════════════════════════════
# 三个阈值常量（按字符数估算，不是真正的 token）：
#   CONTEXT_LIMIT      = 50_000   超过这个大小，认为该触发 L4 摘要压缩
#   KEEP_RECENT        = 3        micro_compact 保留最近 3 条 tool_result 不动
#   PERSIST_THRESHOLD  = 30_000   超过这个大小的工具输出会被落盘
CONTEXT_LIMIT = 50000
KEEP_RECENT = 3
PERSIST_THRESHOLD = 30000

def estimate_size(msgs):
    """粗略估算消息列表的体积（用 str() 长度代替 token 数）。"""
    return len(str(msgs))

def _block_type(block):
    """
    兼容 dict 和 SDK 对象两种 block 形式：
    - 落盘后的 transcript 是 dict
    - SDK 实时返回的是带有 .type 属性的对象
    """
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

def _message_has_tool_use(msg):
    """判断一条 assistant 消息里是否包含 tool_use 块。"""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(_block_type(block) == "tool_use" for block in content)

def _is_tool_result_message(msg):
    """判断一条 user 消息是否是 tool_result（工具执行回执）。"""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)


# ───────────────────────────────────────────────────────────────────
# L1：snip_compact —— 裁掉中间一段消息
# ───────────────────────────────────────────────────────────────────
# 思路：当消息条数 > 50 时，保留头 3 条 + 尾 47 条，中间整段用一个
# "[snipped N messages]" 占位符替代。
# 关键技巧：调整边界时不能把"tool_use 块"和它对应的"tool_result 块"
# 拆开——否则 LLM 会看到没有回执的 tool_use，行为异常。
def snip_compact(messages, max_messages=50):
    if len(messages) <= max_messages:
        return messages
    keep_head, keep_tail = 3, max_messages - 3
    head_end, tail_start = keep_head, len(messages) - keep_tail
    # 头边界调整：若头一条恰好是 tool_result（其 tool_use 已被裁掉），跳过它
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1
    # 尾边界调整：若尾段第一条是 tool_result 但上一条没对应的 tool_use（被切到中间了），回退一格
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    if head_end >= tail_start:
        return messages  # 调整后窗口已无重叠，直接放弃压缩
    snipped = tail_start - head_end
    return (
        messages[:head_end]
        + [{"role": "user", "content": f"[snipped {snipped} messages]"}]
        + messages[tail_start:]
    )


# ───────────────────────────────────────────────────────────────────
# L2：micro_compact —— 旧 tool_result 用占位文本替换
# ───────────────────────────────────────────────────────────────────
# 思路：只对"较长"的旧 tool_result 做替换；最近 KEEP_RECENT 条不动。
# 不直接删除是为了保持 Anthropic API 的约束：每条 tool_use 必须有对应 tool_result。

def collect_tool_results(messages):
    """扫描所有消息，把 (message_index, block_index, block) 元组收集起来。"""
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
    return blocks

def micro_compact(messages):
    """
    替换所有"较旧"且"长度 > 120 字符"的 tool_result。
    占位文本提示 LLM：旧结果已丢弃，若需要可重新调用工具。
    """
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT:
        return messages
    for _, _, block in tool_results[:-KEEP_RECENT]:
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# ───────────────────────────────────────────────────────────────────
# L3：tool_result_budget —— 超大工具输出落盘
# ───────────────────────────────────────────────────────────────────
# 思路：当单条 tool_result > 30KB 时，把全文写到 .task_outputs/tool-results/<id>.txt，
# 给 LLM 的 content 只保留路径 + 2KB 预览。落盘后该 tool_result 体积立刻缩到几 KB。

def persist_large_output(tool_use_id, output):
    """
    把超大工具输出写到磁盘，返回"路径 + 2KB 预览"的占位文本。
    若文件已存在则跳过写入（避免重复劳动）。
    """
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output)
    return (
        f"<persisted-output>\n"
        f"Full output: {path}\n"
        f"Preview:\n{output[:2000]}\n"
        f"</persisted-output>"
    )

def tool_result_budget(messages, max_bytes=200_000):
    """
    监控最后一条 user 消息里所有 tool_result 的总字节数。
    超过 max_bytes 时，按从大到小依次把超大块落盘，直到总量达标。
    注意：只处理"最后一条 user 消息"——历史消息已经在更早的循环里被预算控制过。
    """
    last = messages[-1] if messages else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list):
        return messages
    blocks = [(i, b) for i, b in enumerate(last["content"])
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes:
        return messages
    # 按体积从大到小排序，优先落盘最大的
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for _, block in ranked:
        if total <= max_bytes:
            break
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue  # 体积不到阈值，落到磁盘不划算，跳过
        tid = block.get("tool_use_id", "unknown")
        block["content"] = persist_large_output(tid, content)
        # 重新计算总量
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


# ───────────────────────────────────────────────────────────────────
# L4：autoCompact —— LLM 整段摘要
# ───────────────────────────────────────────────────────────────────
# 思路：当 L1-L3 都不够、消息体积仍 > 50KB 时调用一次 LLM，
# 让模型把整段历史压缩成结构化摘要，再把 messages 替换为单条摘要消息。
# 这一步会消耗一次 API 调用，所以放在所有"零成本"压缩手段之后。

def write_transcript(messages):
    """
    在压缩前把当前完整历史写到 .transcripts/transcript_<timestamp>.jsonl。
    万一摘要丢失关键信息，可以从磁盘恢复。
    """
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path

def summarize_history(messages):
    """
    用 LLM 把整段历史压缩成结构化摘要。
    提示词强调保留 5 类关键信息：目标、发现/决策、文件改动、剩余工作、用户约束。
    """
    conversation = json.dumps(messages, default=str)[:80000]  # 再保险，截到 80KB
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
        "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n"
        + conversation
    )
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,  # 摘要本身不能太大
    )
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip() or "(empty summary)"

def compact_history(messages):
    """
    把整段 messages 替换为一条带摘要的 user 消息。
    摘要前缀加 [Compacted] 标记，方便回放时识别。
    """
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# ───────────────────────────────────────────────────────────────────
# 兜底：reactive_compact —— 在 API 报错时触发
# ───────────────────────────────────────────────────────────────────
# 思路：四层都跑了还是超长，调用方会收到 prompt_too_long 错误。
# 这时再触发 reactive compact：保留最近 5 条消息不动，前面全部让 LLM 摘要。
# 与 L4 的差异：L4 是"主动预防"，L4 不够才会撞上 reactive；reactive 是"被动兜底"。

def reactive_compact(messages):
    """
    兜底压缩：把最早的一段历史让 LLM 摘要，保留最近 5 条原样。
    边界同样要避开把 tool_use 和它对应 tool_result 拆开的情况。
    """
    transcript = write_transcript(messages)
    tail_start = max(0, len(messages) - 5)
    # 边界回退：若要保留的第一条是 tool_result 且上一条含 tool_use，多保留 1 条
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    summary = summarize_history(messages[:tail_start])
    return (
        [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}]
        + messages[tail_start:]
    )


# ═════════════════════════════════════════════════════════════════════
# 工具定义（继承自 s07，新增 compact 工具）
# ═════════════════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
    {"name": "task", "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
    {"name": "load_skill", "description": "Load the full content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    # s08 新增：compact 工具——LLM 可主动调用，触发 compact_history
    {"name": "compact", "description": "Summarize earlier conversation to free context space.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}}},
]

# 工具名 → Python 函数的映射；compact 不在这里，会在 agent_loop 里特判
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
    "task": spawn_subagent, "load_skill": load_skill,
}


# ═════════════════════════════════════════════════════════════════════
# 钩子（继承自 s04，未改动）
# ═════════════════════════════════════════════════════════════════════
# 钩子本质上是事件订阅：PreToolUse 在工具执行前触发，可阻断；PostToolUse 在执行后触发。
# 多个钩子按注册顺序串联，任意一个返回非 None 即视为"阻断"。

HOOKS = {"PreToolUse": [], "PostToolUse": []}
def trigger_hooks(event, *args):
    """触发某个事件的所有钩子；只要有一个返回非 None，就立即用这个结果阻断工具。"""
    for cb in HOOKS[event]:
        r = cb(*args)
        if r is not None:
            return r
    return None

# 黑名单：禁止 LLM 跑的危险命令
DENY_LIST = ["rm -rf /", "sudo", "shutdown"]
def permission_hook(block):
    """PreToolUse 钩子：拦截危险 shell 命令。"""
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                return "Permission denied"
    return None
def log_hook(block):
    """PreToolUse 钩子：所有工具调用前打印一行日志（灰色）。"""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None

HOOKS["PreToolUse"].append(permission_hook)
HOOKS["PreToolUse"].append(log_hook)


# ═════════════════════════════════════════════════════════════════════
# agent_loop —— s08 核心：每次 LLM 调用前都跑一遍压缩管道
# ═════════════════════════════════════════════════════════════════════

# reactive 兜底最多重试 1 次：再不够就把异常抛出去，不无限循环
MAX_REACTIVE_RETRIES = 1

def agent_loop(messages: list):
    """
    经典的 REPL 循环，但 s08 在每轮 LLM 调用前都先跑压缩管道：
        L3 budget → L1 snip → L2 micro → [超阈值?] → L4 summary → LLM
                                                                  ↓ (报错)
                                                            reactive
    """
    reactive_retries = 0
    while True:
        # ─── 零成本压缩三连（顺序与 CC 源码一致）───
        messages[:] = tool_result_budget(messages)  # L3：先把超大工具输出落盘
        messages[:] = snip_compact(messages)        # L1：再裁掉中间消息
        messages[:] = micro_compact(messages)       # L2：最后把旧 tool_result 替换成占位

        # ─── 仍有压力 → 调用一次 LLM 摘要 ───
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        # ─── 真正调用 LLM；失败时尝试 reactive 兜底 ───
        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM,
                messages=messages, tools=TOOLS, max_tokens=8000,
            )
            reactive_retries = 0  # 调用成功，重置兜底计数
        except Exception as e:
            err = str(e).lower()
            if ("prompt_too_long" in err or "too many tokens" in err) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue  # 重新走 while 顶部，再压缩一次再调
            raise  # 别的异常或已重试过，直接抛

        # ─── 处理 LLM 返回：可能含 tool_use 块 ───
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return  # 模型没要调工具，对话结束

        # 收集本轮所有 tool_use 的执行结果
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")  # 青色，提示"模型在调工具"

            # s08 新增：compact 工具的特判路径
            # LLM 主动调用 compact 时，立即触发 compact_history 并把"已压缩"
            # 提示作为 tool_result 返回，然后 break 跳出本轮——下一轮循环会用
            # 新的精简 messages 重新调 LLM。
            if block.name == "compact":
                messages[:] = compact_history(messages)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "[Compacted. Conversation history has been summarized.]",
                })
                messages.append({"role": "user", "content": results})
                break  # 本轮结束；下一轮 while 顶部会基于压缩后的 messages 继续

            # 普通工具：先过 Pre 钩子
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)})
                continue
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            trigger_hooks("PostToolUse", block, output)
            print(str(output)[:200])  # 截断到 200 字符刷屏
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        else:
            # for 循环没有被 break 走完（说明本轮没调 compact）
            messages.append({"role": "user", "content": results})
            continue
        # 走到这里说明 compact 被调用过：results 已在循环内追加，直接继续
        continue


# ═════════════════════════════════════════════════════════════════════
# REPL 入口
# ═════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("s08: Context Compact — four-layer compaction pipeline")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")  # 青色提示符
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印模型最后一轮的纯文本回复
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
