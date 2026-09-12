#!/usr/bin/env python3
"""
s09_memory.py —— 记忆系统 (Memory System)
══════════════════════════════════════════════
为 coding agent 提供"跨会话、跨进程"持久化知识。

本课是 v2 教程 s09 章节：在 s08 (上下文压缩) 之上叠加"记忆层"，
让 agent 在下一次启动时仍能回忆起用户偏好、项目事实、外部参考。

────────────────────────
存储布局 (在 WORKDIR 下)
────────────────────────
    .memory/
      MEMORY.md           ← 索引文件，每条记忆一行 (总长 ≤ 200 行)
      <slug>.md           ← 单条记忆文件，Markdown + YAML frontmatter
      user_profile.md     ← 用户偏好 (示例)
      project_facts.md    ← 项目事实 (示例)

每个记忆文件结构示例：
    ---
    name: user-preference-tabs
    description: 用户喜欢用 tab 缩进
    type: user
    ---
    详细正文 markdown ...

────────────────────────
在 agent_loop 中的位置
────────────────────────
    1. 加载 MEMORY.md 索引 → 注入 SYSTEM 提示 (永远在场，token 很省)
    2. 根据"最近对话"选相关记忆 → 把内容追加到当前用户消息前面
    3. 跑 s08 的压缩管道 (snip / micro / reactive)
    4. 一轮 turn 跑完 → 从"压缩前快照"抽取新记忆
    5. 文件数 ≥ 阈值 → 触发"整合 (Consolidate / Dream)"

依赖：s08 (context compact)
使用：
    python s09_memory/code.py
    需要：pip install anthropic python-dotenv  +  .env 中配置 ANTHROPIC_API_KEY
"""

import os, subprocess, json, time, re
from pathlib import Path

# ─── readline 增强：让交互式输入不破坏 ANSI 转义 / 不响应奇怪的控制键 ───
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    # Windows 等没有 readline 的环境直接跳过
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载 .env；override=True 允许 .env 覆盖已有的环境变量
load_dotenv(override=True)
# 如果用户配置了 ANTHROPIC_BASE_URL (走代理/中转)，就不需要 ANTHROPIC_AUTH_TOKEN
if os.getenv("ANTHROPIC_BASE_URL"): os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ─── 路径与客户端初始化 ────────────────────────────────────────────
WORKDIR = Path.cwd()                                       # 当前工作目录
MEMORY_DIR = WORKDIR / ".memory"; MEMORY_DIR.mkdir(exist_ok=True)  # 记忆目录
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"                    # 索引文件
SKILLS_DIR = WORKDIR / "skills"                            # 技能目录 (s05 引入)
TRANSCRIPT_DIR = WORKDIR / ".transcripts"                  # 压缩前的对话快照 (s08 引入)
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"      # 大输出外置目录
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # SDK 客户端
MODEL = os.environ["MODEL_ID"]                             # 使用的模型 ID


# ═══════════════════════════════════════════════════════════
#  NEW in s09: 记忆系统核心实现
# ═══════════════════════════════════════════════════════════
#  以下是 s09 新增的全部记忆相关代码，遵循"索引 + 详情"两级结构：
#    - MEMORY.md     索引：每条记忆一行（name + 文件名 + 一句话描述）
#    - *.md          详情：完整的 Markdown 正文 + YAML frontmatter
#  索引永远跟着 SYSTEM 进上下文，详情按需注入。

# 4 类记忆：用户偏好 / 反馈 (含负面纠正) / 项目事实 / 外部参考
MEMORY_TYPES = ["user", "feedback", "project", "reference"]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    极简 YAML frontmatter 解析器。

    输入格式（记忆文件的固定写法）：
        ---
        name: xxx
        description: yyy
        type: user
        ---
        正文 ...

    返回 (meta_dict, body_text)。
    - 没有 frontmatter 或解析失败 → 返回 ({}, 原文)
    - 注意：这是手写的简化实现，不支持嵌套、多行、列表等复杂 YAML
    """
    if not text.startswith("---"):
        return {}, text
    # 用 "---" 切 3 段：['', 'key: val\n...', '\n正文\n']
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    # 中间那段是 key: value 形式的元数据
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            # 去掉首尾空白 + 引号（支持 name: "foo" / name: 'foo'）
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def write_memory_file(name: str, mem_type: str, description: str, body: str):
    """
    写入单条记忆文件，并重建索引。

    参数：
        name        - 人类可读的名字（用于索引展示）
        mem_type    - user / feedback / project / reference
        description - 一句话摘要（用于索引行 + 选相关记忆时的关键词匹配）
        body        - 完整 Markdown 正文

    返回：写入的文件路径。
    """
    # 把名字转成 URL 友好的 slug：空格 -> '-', '/' -> '-'
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    # 注意：frontmatter 的格式被多处依赖（_parse_frontmatter、_rebuild_index），
    # 改这里需要同步改那两个地方。
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
    )
    # 写完一条就重建索引，让 MEMORY.md 始终保持"最新且与磁盘一致"
    _rebuild_index()
    return filepath


def _rebuild_index():
    """
    扫描 MEMORY_DIR 下所有 *.md（跳过 MEMORY.md 自身），
    解析每条的 frontmatter，生成"一行一条"的索引。

    输出格式：
        - [name](filename.md) — description
    """
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue  # 跳过索引文件本身
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        # 取不到 name 就用文件名做兜底；取不到 description 就用正文首行前 80 字
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")


def read_memory_index() -> str:
    """
    读 MEMORY.md 索引全文。
    - 每次 agent_loop 一开始都会调用 → 注入到 SYSTEM
    - 索引本身很短（每行 1 条），对 token 友好
    """
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text().strip()
    return text if text else ""


def read_memory_file(filename: str) -> str | None:
    """
    读单条记忆的完整内容（YAML + 正文都返回）。
    用于"选了相关记忆 → 注入完整正文到当前用户消息"这一步。
    """
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text()


def list_memory_files() -> list[dict]:
    """
    列出所有记忆文件的元信息。
    返回 list[dict]，每个 dict 含 filename / name / description / type / body。
    用于：抽取记忆时让 LLM 看到"已有什么"避免重复；选相关记忆时建目录。
    """
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename": f.name,
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type", "user"),
            "body": body,
        })
    return result


def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """
    "从所有记忆中选相关的"。
    是记忆系统的检索器：拿最近对话去匹配记忆目录，返回最多 max_items 个文件名。

    实现策略（两步）：
        1) 主路径：构造 (最近对话 + 记忆目录) 提示词，让 LLM 选相关索引号
        2) 兜底：LLM 调用失败时，用关键词匹配 (name + description 子串)

    返回：选中的 memory 文件名列表（不含路径），调用方据此读完整正文。
    """
    files = list_memory_files()
    if not files:
        return []  # 还没攒下任何记忆，直接返回

    # ── 1. 收集最近 3 条 user 消息的文本 ─────────────────────────
    recent_texts = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            # content 可能是字符串，也可能是 content-block 列表（tool_result 等）
            if isinstance(content, list):
                # 仅提取 text 类型的 block，丢弃 tool_result / image 等
                content = " ".join(
                    str(getattr(b, "text", "")) for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
    # 拼起来 + 截断到 2000 字符，避免提示词过长
    recent = " ".join(reversed(recent_texts))[:2000]

    if not recent.strip():
        return []  # 没有可参考的对话内容，没法选相关

    # ── 2. 构造"记忆目录"（让 LLM 看的版本：只暴露 name + description）──
    catalog_lines = []
    for i, f in enumerate(files):
        catalog_lines.append(f"{i}: {f['name']} — {f['description']}")
    catalog = "\n".join(catalog_lines)

    # 让 LLM 只返回 JSON 数组，比要求它"自然语言回答哪些相关"更稳定
    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        # 一次小调用（max_tokens=200 足够装下 JSON 数组）
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        text = extract_text(response.content).strip()
        # 模型偶尔会带"```json"之类包装，用正则强行抽出 [...]
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                # 防御：只接受合法 int 且在范围内
                if isinstance(idx, int) and 0 <= idx < len(files):
                    selected.append(files[idx]["filename"])
                    if len(selected) >= max_items:
                        break
            return selected
    except Exception:
        # LLM 不可用 / JSON 解析失败 → 落到下面的关键词兜底
        pass

    # ── 3. 兜底：纯字符串匹配 ──────────────────────────────────────
    # 取最近文本里所有 > 3 字符的词当关键词，
    # 看哪个记忆的 name+description 命中得多
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    return selected


def load_memories(messages: list) -> str:
    """
    "把相关记忆装到当前用户消息前面"。

    返回值格式（XML 风格标记，让模型能区分"记忆"和"用户原话"）：
        <relevant_memories>
        --- memory 1 完整内容（含 frontmatter）---
        --- memory 2 完整内容 ---
        </relevant_memories>

    返回空字符串代表"这次没选到相关记忆"，调用方据此决定是否需要前缀拼接。
    """
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""

    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def extract_memories(messages: list):
    """
    "从最近对话里抽新记忆"。

    调用时机：agent_loop 跑完一个 turn（模型不再 tool_use，准备返回自然语言）后。
    关键设计：传入的 messages 必须是"压缩前"的快照 (pre_compress)，
    否则 snip / micro_compact 会把用户原话吃掉，模型就抽不到东西了。

    流程：
        1) 把传入的所有消息 → 拼成 "role: content" 形式的对话稿
           （tool_result 内容以 [tool output] 标记纳入，单条限 1500 字符）
        2) 同时把"已有记忆目录"喂给 LLM，让它避免重复
        3) 让 LLM 返回 JSON 数组，每条含 name/type/description/body
        4) 遍历写盘（write_memory_file 内部会重建索引）

    修复（相对原版）：
        - 不再硬切 messages[-10:]：调用方负责传"本 turn 的消息切片"（见 agent_loop）
        - 区分 block 类型：text 取 .text；tool_result 取 .content（带 [tool output] 标记）
        - 嵌套 tool_result.content 是 list 的情况会递归拍平
    """
    # ── 1. 把所有传入消息 → 转成纯文本对话稿 ────────────────────────
    dialogue_parts = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for b in content:
                # 兼容 SDK 对象和手搓 dict 两种 block 形式
                btype = getattr(b, "type", None)
                if btype is None and isinstance(b, dict):
                    btype = b.get("type")
                if btype == "text":
                    text = getattr(b, "text", "")
                    if not text and isinstance(b, dict):
                        text = b.get("text", "")
                    if text:
                        parts.append(str(text))
                elif btype == "tool_result":
                    # tool_result.content 可能是字符串或 list（嵌套 content-block）
                    rc = getattr(b, "content", "")
                    if isinstance(b, dict) and not rc:
                        rc = b.get("content", "")
                    if isinstance(rc, list):
                        # 嵌套 list：递归拍平（只取 text）
                        rc = " ".join(
                            str(getattr(x, "text", "") or (x.get("text", "") if isinstance(x, dict) else ""))
                            for x in rc
                            if (getattr(x, "type", None) or (x.get("type") if isinstance(x, dict) else None)) == "text"
                        )
                    # 单条 tool 输出限 1500 字符，避免 read_file/grep 等大输出把 dialogue 撑成单 tool 复读
                    parts.append(f"[tool output] {str(rc)[:1500]}")
            content = " ".join(parts)
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return  # 没有任何可分析的对话内容

    # ── 2. 把"已有什么"告诉 LLM，避免它重复造轮子 ─────────────────
    existing = list_memory_files()
    existing_desc = "\n".join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"

    # ── 3. 设计提示词：明确输出 schema (name/type/description/body) ───
    # 4 类记忆的选择规则是模型可学习的（见 MEMORY_TYPES），
    # 提示词只列名字 + 含义，不强制 1:1 命中，由 LLM 自行判断。
    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=800
        )
        text = extract_text(response.content).strip()
        # 防御性抽取 JSON 数组（re.search 默认非贪婪，遇见首个 ] 就停，
        # 配合 re.DOTALL 跨行匹配）
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return  # 模型认为没新东西

        # ── 4. 落盘：每条都过 write_memory_file（顺带重建索引）─────
        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            # description / body 任一为空视为无效记忆，跳过
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            # 黄色提示，让用户在 REPL 里直观看到"刚存了几条"
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception:
        # 任何异常（网络/JSON 解析/落盘）都静默吞掉，避免影响主循环
        pass


# 当 .memory 下文件数 ≥ 10 才触发"整合"——既避免无意义的全量重写，也保证
# 调用一次 LLM 有足够收益。
CONSOLIDATE_THRESHOLD = 10

def consolidate_memories():
    """
    "整合 / 做梦 (Dream)"。

    当记忆条数太多 / 出现重复 / 互相矛盾时，把所有记忆丢给 LLM 让它
    去重 + 合并 + 提炼，目标是"用更少的条目表达更多知识"。

    风险点：这是一次"全量重写"——会先把所有旧 *.md 删掉再写新条目。
    触发频率由 CONSOLIDATE_THRESHOLD 决定，副作用可控。
    """
    files = list_memory_files()
    # 数量不够就别折腾（避免 LLM 浪费 token，也避免无意义重写）
    if len(files) < CONSOLIDATE_THRESHOLD:
        return

    # ── 拼成一份"目录 + 正文"的长文给 LLM 看 ─────────────────────
    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    # 4 条硬性规则（去重 / 删过期 / 限 30 / 偏好优先）
    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"  # 16k 字符上限，防爆 token
    )

    try:
        response = client.messages.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=3000
        )
        text = extract_text(response.content).strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # ── "全量替换"：先清空（保留 MEMORY.md）再写新条目 ──────────
        # 注意：这里用 unlink 而非覆盖 write，因为 LLM 可能要丢弃一部分记忆。
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)

        # 打印整合前后的条数对比，方便用户感知
        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception:
        # 整合失败不能影响主流程；下次 turn 再试
        pass


# ═══════════════════════════════════════════════════════════
#  构造 SYSTEM 提示
# ═══════════════════════════════════════════════════════════
#  SYSTEM 提示的设计哲学（与 s08 共同）：
#    - "稳定身份 + 稳定格式"  → 容易触发 prompt cache（同一段前缀只算一次 token）
#    - 每轮都会变的内容（记忆索引）放在固定位置、同一格式，便于缓存命中
#    - 完整记忆正文不放 SYSTEM（太长），只放索引；正文走"用户消息前缀"注入
def build_system() -> str:
    """
    拼装主 agent 的 SYSTEM 提示。
    关键点：每次都从磁盘读最新 MEMORY.md 索引，这样新写入的记忆
    在下一轮立刻可见。
    """
    index = read_memory_index()
    # 索引非空时才追加 "Memories available:" 小节，避免出现空标题
    memories_section = f"\n\nMemories available:\n{index}" if index else ""
    return (
        f"You are a coding agent at {WORKDIR}."
        f"{memories_section}\n"
        "Relevant memories are injected below. Respect user preferences from memory.\n"
        "When the user says 'remember' or expresses a clear preference, extract it as a memory."
    )

# 子 agent（被 task 工具调起的）的 SYSTEM 提示。
# 比主 agent 简单：不带记忆索引、不要求"记住"——子任务应当专注 + 收敛。
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s08 (skeleton): 基础工具集
# ═══════════════════════════════════════════════════════════
#  以下是从 s02-s08 复制过来的"工具实现 + 路径安全 + 子 agent"。
#  s09 在它们之上叠加了记忆系统；s09 没有改任何工具实现，
#  只是把"读 / 写 / 改"这些动作产生的对话流作为"待抽取记忆的语料"。

def safe_path(p: str) -> Path:
    """
    路径安全检查：把 p 解析为绝对路径后，必须仍在 WORKDIR 之内。
    防止模型写出 "../../etc/passwd" 这种穿越路径。
    解析失败 / 越界都抛 ValueError。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """
    bash 工具：subprocess 跑 shell 命令。
    关键限制：
        - timeout=120s：超长命令及时打断，避免 agent 挂死
        - 截断到 50k 字符：防上下文被一次大输出撑爆
    """
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired: return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    """
    读文件工具。
    limit 用于"只想看前 N 行"——返回的尾部会带 "(N more lines)" 提示。
    """
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines): lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """
    写文件工具。会自动建父目录。
    返回写入字节数（人话格式），不返回内容（避免上下文冗余）。
    """
    try:
        file_path = safe_path(path); file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content); return f"Wrote {len(content)} bytes to {path}"
    except Exception as e: return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """
    精确编辑工具：把文件里第一次出现的 old_text 替换为 new_text。
    比 write_file 更安全（只动目标片段），适合小修改。
    找不到 old_text → 返回错误，不抛异常。
    """
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text: return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e: return f"Error: {e}"

def run_glob(pattern: str) -> str:
    """
    glob 工具：在 WORKDIR 内用 shell-style 通配符找文件。
    会再次过滤：仅保留仍属于 WORKDIR 的命中（防御符号链接越界）。
    """
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e: return f"Error: {e}"

def extract_text(content) -> str:
    """
    从 Anthropic SDK 返回的 content（可能是 str 或 list[Block]）里抽纯文本。
    只取 type=='text' 的 block，忽略 tool_use / image 等。
    """
    if not isinstance(content, list): return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")

# ─── 子 agent (简化自 s06-s07) ──────────────────────────────────────
# 子 agent 有自己的工具集（更少）+ 独立循环，最多 30 轮。
# 它不应该再 "task" 套娃（Do not delegate further），所以 SUB_TOOLS 不含 task。
SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
]
# name -> handler 的查表，让 dispatch 一行搞定
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}

def spawn_subagent(description: str) -> str:
    """
    启动一个子 agent 干活，返回它的"自然语言总结"。

    设计：
        - 用 SUB_SYSTEM 隔离任务边界
        - 自己跑一个 30 轮的 mini agent_loop（不复用主循环，避免记忆污染）
        - 每步打印紫色日志便于主 agent 在 REPL 里"看见"子 agent 在干啥
    """
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.messages.create(model=MODEL, system=SUB_SYSTEM,
            messages=messages, tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        # 不再 tool_use → 自然结束
        if response.stop_reason != "tool_use": break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                # 灰色子日志：子 agent 的 tool 实际执行情况，方便主 agent 调试
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
    # 取最后一条 assistant 消息的文本作为"子 agent 的总结"
    result = extract_text(messages[-1]["content"])
    if not result:
        # 兜底：倒着找一条非空 assistant 文本
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result: break
        if not result: result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  FROM s08 (skeleton): 上下文压缩管道
# ═══════════════════════════════════════════════════════════
#  s08 的核心是：让超长对话也能继续跑下去，而不必无限堆 token。
#  本文件没有改这些实现，只在 agent_loop 里按顺序串起来调用。
#  共 5 步（在 s08 中也按此顺序生效）：
#    1) tool_result_budget  → 把单条 tool_result 截到 200k 字符以内（太大就外置）
#    2) snip_compact        → 对话太长（> 50 条）时把中间一大段压成占位符
#    3) micro_compact       → 保留最近 KEEP_RECENT=3 条 tool_result，其他替换为占位
#    4) compact_history     → 整体超 50k 字符 → 调 LLM 总结，把历史压成 1 条
#    5) reactive_compact    → 上面 4 步都拦不住（API 报 prompt_too_long）时再救一次
#
# 几个关键阈值：
#   CONTEXT_LIMIT       = 50000   估算字符数（用 str() 长度，不是真 token）
#   KEEP_RECENT         = 3       micro_compact 保留的最近几条 tool_result
#   PERSIST_THRESHOLD   = 30000   单条 tool_result 超过这个就外置到 .task_outputs
CONTEXT_LIMIT = 50000; KEEP_RECENT = 3; PERSIST_THRESHOLD = 30000

def estimate_size(msgs):
    """用 str(msgs) 的字符数作为"上下文大小"——粗估，但比真算 token 简单 100 倍。"""
    return len(str(msgs))

def _block_type(block):
    """统一拿 block.type——SDK 既可能给 dict，也可能给对象。"""
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

def _message_has_tool_use(msg):
    """判断一条 assistant 消息里是否含 tool_use block。"""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(_block_type(block) == "tool_use" for block in content)

def _is_tool_result_message(msg):
    """判断一条 user 消息是否是"对 tool_use 的回应"（即装着 tool_result block）。"""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)

def snip_compact(msgs, mx=50):
    """
    "剪枝压缩"：当消息数 > mx 时，把"中间一大段"换成一条占位 user 消息。
    保留头 3 条 + 尾 (mx-3) 条，中间的全部丢。
    关键不变量：snip 的边界必须不"切在 tool_use / tool_result 中间"——
    否则模型下一轮会看到一条孤儿 tool_result，行为不可预测。
    """
    if len(msgs) <= mx: return msgs
    head_end, tail_start = 3, len(msgs) - (mx - 3)
    # 如果 head 末是 tool_use，要继续跳过它之后的 tool_result 们（保持配对）
    if head_end > 0 and _message_has_tool_use(msgs[head_end - 1]):
        while head_end < len(msgs) and _is_tool_result_message(msgs[head_end]):
            head_end += 1
    # 同样地，如果 tail 头是"孤立的 tool_result"（前面那条被 snip 掉了），
    # 要把它连同前一条 tool_use 一起保留（tail_start 前移一位）
    if (tail_start > 0 and tail_start < len(msgs)
            and _is_tool_result_message(msgs[tail_start])
            and _message_has_tool_use(msgs[tail_start - 1])):
        tail_start -= 1
    # 防御：极端情况下 head 和 tail 重叠 / 错位 → 不动
    if head_end >= tail_start:
        return msgs
    # 拼回 [head 段] + [占位] + [tail 段]
    return msgs[:head_end] + [{"role": "user", "content": f"[snipped {tail_start - head_end} msgs]"}] + msgs[tail_start:]

def collect_tool_results(msgs):
    """
    扫一遍所有 user 消息，收集所有 tool_result block 及其 (msg_idx, block_idx)。
    给 micro_compact / tool_result_budget 用。
    """
    blocks = []
    for mi, msg in enumerate(msgs):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result": blocks.append((mi, bi, block))
    return blocks

def micro_compact(msgs):
    """
    "微观压缩"：只对旧的 tool_result 动手，把它们的 content 缩成
    "[Earlier tool result compacted.]"——保留最近 KEEP_RECENT 条不动。
    为什么只动 tool_result？因为它是"会话中最容易爆体积的"
    （ls 一下、cat 一下都可能上万行）。
    """
    tr = collect_tool_results(msgs)
    if len(tr) <= KEEP_RECENT: return msgs
    # tr[:-KEEP_RECENT] 切片 = "除最近 3 条外的所有"
    # 只在原 block.content 长度 > 120 字符时才替换，避免对短输出也搞破坏
    for _, _, b in tr[:-KEEP_RECENT]:
        if len(b.get("content", "")) > 120: b["content"] = "[Earlier tool result compacted.]"
    return msgs

def persist_large(tid, out):
    """
    "大输出外置"：单条 tool_result 超过 PERSIST_THRESHOLD (30k) 字符时，
    把全文写到 .task_outputs/<tool_use_id>.txt，模型这里只看到路径 + 预览。
    """
    if len(out) <= PERSIST_THRESHOLD: return out
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    p = TOOL_RESULTS_DIR / f"{tid}.txt"
    # 已存在就不重写（同一 tool_use_id 可能被多次持久化调用）
    if not p.exists(): p.write_text(out)
    return f"<persisted-output>\nFull: {p}\nPreview:\n{out[:2000]}\n</persisted-output>"

def tool_result_budget(msgs, mx=200_000):
    """
    控制最后一条 user 消息里所有 tool_result 的总字符数 ≤ mx。
    触发条件：最后一条 user 消息里装着 tool_result。
    策略：按"由大到小"顺序逐个外置，直到总字符数达标。
    不动 PERSIST_THRESHOLD 以内的小输出（没必要）。
    """
    last = msgs[-1] if msgs else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return msgs
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= mx: return msgs
    # 按 block.content 长度降序处理，大的优先外置
    for _, block in sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True):
        if total <= mx: break
        c = str(block.get("content", ""))
        # 太小的也不外置（直接外置等于浪费一次 IO）
        if len(c) <= PERSIST_THRESHOLD: continue
        block["content"] = persist_large(block.get("tool_use_id", "?"), c)
        # 重新算一次 total
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return msgs

def write_transcript(msgs):
    """
    写"压缩前快照"到 .transcripts/，方便事后回溯。
    文件名带时间戳，永远只增不覆盖（每次压缩都是一次"存档"）。
    """
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    p = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with p.open("w") as f:
        for m in msgs: f.write(json.dumps(m, default=str) + "\n")
    return p

def summarize_history(msgs):
    """
    调一次 LLM 把一段对话压成摘要。
    提示词里写明"要保留的 5 类信息"：当前目标 / 关键发现 / 改过的文件 /
    剩余工作 / 用户约束。给一个小目标胜过让 LLM 自己发挥。
    """
    conv = json.dumps(msgs, default=str)[:80000]
    r = client.messages.create(model=MODEL, messages=[{"role": "user", "content":
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings, 3. files changed, 4. remaining work, 5. user constraints.\n\n" + conv}],
        max_tokens=2000)
    return extract_text(r.content).strip()

def compact_history(msgs):
    """
    "硬压缩"：把整段历史压成 1 条带 [Compacted] 标记的 user 消息。
    同时写一份 transcript 留底。
    """
    write_transcript(msgs)
    summary = summarize_history(msgs)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]

def reactive_compact(msgs):
    """
    "反应式压缩"：被 API 报 prompt_too_long 时兜底用。
    比 compact_history 温和一点——只压"前 5 条之前"的内容，
    留 5 条最近上下文不丢（让模型还有"现场感"）。
    同样不切在 tool_use / tool_result 中间。
    """
    write_transcript(msgs)
    tail_start = max(0, len(msgs) - 5)
    if (tail_start > 0 and tail_start < len(msgs)
            and _is_tool_result_message(msgs[tail_start])
            and _message_has_tool_use(msgs[tail_start - 1])):
        tail_start -= 1
    summary = summarize_history(msgs[:tail_start])
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *msgs[tail_start:]]


# ═══════════════════════════════════════════════════════════
#  Tool Definitions (skeleton — 工具更少，凸显"记忆"主题)
# ═══════════════════════════════════════════════════════════
#  Anthropic SDK 的 tool 描述格式：name + description + JSON Schema 的 input_schema。
#  description 是给模型看的"何时用这工具"的指引——写得越具体，模型越准。
#  s09 故意少放工具：bash / read / write / edit / glob / task，聚焦"记忆"主题。

TOOLS = [
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
    {"name": "task", "description": "Launch a subagent to handle a subtask.",
     "input_schema": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
]

# 工具名 → 处理函数的查表。
# dispatch 时一行查表：handler = TOOL_HANDLERS.get(block.name)
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "task": spawn_subagent,
}


# ═══════════════════════════════════════════════════════════
#  agent_loop —— s09 核心：注入记忆 + turn 结束后抽取
# ═══════════════════════════════════════════════════════════
#  agent_loop 是整个 agent 的大脑，每"一轮"人机对话 = 一次 agent_loop 调用。
#  s09 相对 s08 的关键变化（按代码行序）：
#    1) 循环开始前  → 用 load_memories 预加载相关记忆内容 (本 turn 固定不变)
#    2) 循环开始前  → 记下"当前用户消息索引" memory_turn
#    3) 循环开始前  → 调用 build_system 把 MEMORY.md 索引拼进 SYSTEM
#    4) 每次循环    → 先存一份"压缩前快照" pre_compress（供 extract_memories 用）
#    5) 每次循环    → 跑 s08 三段压缩 + (必要时) 整段 compact
#    6) 发请求前    → 把 memories_content 拼到当前用户消息前面 (不改原 list)
#    7) 循环结束    → 从 pre_compress 抽新记忆 + 尝试整合

# 兜底压缩最多重试 1 次（再压会丢太多上下文）
MAX_REACTIVE_RETRIES = 1

def agent_loop(messages: list):
    """
    主 agent 循环。每次调用 = 跑完一个 turn（用户输入 → 模型可能调多个 tool → 最终回答）。

    参数 messages 是"跨 turn 累积"的对话历史，调用方需要传 in 引用进来，
    本函数会原地修改它（压缩、append assistant 消息、append tool_result 等）。
    """
    reactive_retries = 0

    # ── [s09] 预加载：本 turn 要注入的相关记忆正文 ─────────────────
    # 关键：load_memories 只在循环外调用一次。结果复用到循环里所有 LLM 请求。
    # 原因：tool 还在跑的时候再"选相关记忆"会让工具输出污染判断，反而更乱。
    memories_content = load_memories(messages)
    # 记下"当前用户消息在 messages 里的下标"，等会儿注入要靠它。
    # 兜底：如果最后一条不是纯字符串 user 消息（是 tool_result 列表等），就跳过注入。
    memory_turn = len(messages) - 1 if messages and isinstance(messages[-1].get("content"), str) else None

    # ── [s09] 每 turn 重建 SYSTEM（保证新写入的记忆下一轮才可见）──
    # 注意：这是每 turn 重读一次磁盘（_rebuild_index 跑在 write_memory_file 里，
    # 索引是即时的；但 build_system 本身不缓存）。
    system = build_system()

    while True:
        # ── [s09] 存"压缩前快照" ────────────────────────────────────
        # extract_memories 需要看"用户原话"，但循环里 messages 会被 snip / micro 改写，
        # 所以在压缩前先存一份。
        # 这一步是 s09 的精髓："先用干净语料抽记忆，再继续跑压缩"，
        # 否则压缩过的"占位符"对 LLM 抽取毫无价值。
        pre_compress = [m if isinstance(m, dict) else {"role": m.get("role",""),
            "content": str(m.get("content",""))} for m in messages]

        # ── [s08] 三段压缩管道（顺序敏感）──────────────────────────
        # budget → snip → micro：从小动作到大动作逐级收紧
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)

        # 整体还是太大 → 整段总结（写 transcript 留底 → 换成 1 条摘要消息）
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        try:
            # ── [s09] 构造"发给模型的 messages"，但不污染原 list ────
            # 直接 messages[:] = ... 会把 memories_content 写进 history，
            # 下一轮再选记忆时就会把"上次注入的正文"也算进语料，造成重复放大。
            # 所以走 .copy() + 临时替换。
            request_messages = messages
            if memories_content and memory_turn is not None and memory_turn < len(messages):
                # 浅拷贝（list of dict），然后只改当前 user 消息这一格的 content
                request_messages = messages.copy()
                request_messages[memory_turn] = {
                    **messages[memory_turn],
                    # 关键拼法：<relevant_memories>...</relevant_memories> 块 + 双换行 + 用户原话
                    # XML 标记让模型清楚"这部分是记忆、下面才是用户说的话"
                    "content": memories_content + "\n\n" + messages[memory_turn]["content"],
                }
            response = client.messages.create(
                model=MODEL, system=system, messages=request_messages, tools=TOOLS, max_tokens=8000
            )
            # 请求成功 → 重置 reactive 计数
            reactive_retries = 0
        except Exception as e:
            # ── [s08] 兜底：API 报 prompt_too_long → 走 reactive 压缩 ──
            # 两种常见错误文案都识别；只重试一次（避免无限循环）
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue  # 回到 while 开头再发一次
            # 真的炸了 → 往上抛，让 main 退
            raise

        # 把模型回复追加到历史
        messages.append({"role": "assistant", "content": response.content})

        # ── [s09] turn 自然结束 → 抽记忆 + 整合 ──────────────────
        # stop_reason != "tool_use" 表示模型想给最终回答了（end_turn / max_tokens / stop_seq）
        if response.stop_reason != "tool_use":
            # 修复：传"本 turn 的消息切片 + final assistant response"
            # - memory_turn 是本 turn user 消息在 messages 里的下标（L890 记下）
            # - pre_compress[memory_turn:] 拿本 turn 的完整对话（不被 [-10:] 裁剪）
            # - 末尾补上 final response：pre_compress 在 L903 抓快照时它还没 append 进来
            if memory_turn is not None and memory_turn < len(pre_compress):
                turn_msgs = pre_compress[memory_turn:] + [{"role": "assistant", "content": response.content}]
            else:
                # 兜底：memory_turn 不可用时退到"全量 + final response"
                turn_msgs = pre_compress + [{"role": "assistant", "content": response.content}]
            extract_memories(turn_msgs)
            consolidate_memories()
            return  # 本 turn 结束

        # ── 还在 tool_use：分派每个 tool_use block ────────────────
        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            # 青色 "> 工具名" 提示
            print(f"\033[36m> {block.name}\033[0m")
            # 查表 dispatch：模型填的 input 直接 **kwargs 给 handler
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            # 截断到 200 字符打印，避免 REPL 被刷屏
            print(str(output)[:200])
            # 把结果打包成 SDK 期望的 tool_result block 格式
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        # 把所有 tool_result 装成一条 user 消息追加 → 下轮发给模型
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    """
    REPL 入口（"Read-Eval-Print Loop" = 读取-执行-打印 循环）：
        1) 打印欢迎语
        2) 进入一个 while，每次读一行用户输入
        3) 拼成 user 消息 append 到 history
        4) 调 agent_loop(hist) 跑完一个 turn
        5) 把模型最后一条回复里的 text 块打印出来
        6) 回到 while 顶部等待下一行
    """
    print("s09: Memory — persistent cross-session knowledge")
    print("输入问题，回车发送。输入 q 退出。\n")
    # 跨 turn 累积的对话历史：会持续在内存里，进程退出就丢（除非已落盘到 .memory）
    history = []
    while True:
        try:
            # 青色提示符 "s09 >> "，让 REPL 更易读
            query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D / Ctrl+C → 优雅退出
            break
        # q / exit / 空行 都视为"退出"
        if query.strip().lower() in ("q", "exit", ""): break
        # 追加用户消息到 history
        history.append({"role": "user", "content": query})
        # 跑一个 turn；agent_loop 内部会原地改 history
        agent_loop(history)
        # 把模型最终回答里的 text 块挨个打印（assistant 消息里也可能有 tool_use + text 混合，
        # 这里只取 text 类型的展示给用户看）
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text": print(block.text)
        print()  # 空行分隔每轮对话
