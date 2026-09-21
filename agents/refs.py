#!/usr/bin/env python3
"""
refs.py - 工作空间「引用文件/文件夹」（@-mention）：扁平列目录、中性引用块、发送前展开（新增）

设计见 docs/frontend/13-引用文件与文件夹（@-mention）.md。本模块是桌面端「@ 引用」
功能的后端实现，**叶子模块**：只依赖标准库 + `logger`，**禁止 import
attachments / agent_full_v2 / session_manage**（会与引擎形成循环依赖）。

════════════════════════════════════════════════════════════════════════
与「附件」（attachments.py）的分工 —— 本模块存在的全部理由
════════════════════════════════════════════════════════════════════════

| | 附件 | 引用（本模块） |
|---|---|---|
| 存储 | 复制副本到 `.attachments/` | **零复制、零存储** |
| 位置 | 工作空间**之外**（`~/.aigent/...`） | 工作空间**之内**（沙箱根之下） |
| 模型可读性 | 副本被 `safe_path` 拒绝 → 只能预注入内容 | `run_read` 直接可读 → 只给路径 |
| 注入内容 | 全文（有 30000/60000 字封顶） | **只有路径与类型** |
| 内容时效 | 快照，原件改动不影响 | 现读；但**引用会失效**（删/改名/移走） |

两条通道**协议字段、块类型、模块、CSS 前缀、文档章节全部独立**。引用唯一复用的
是附件的表现层约定（点对点应答信封 / chip 四态 / 「账本块 + 发送边界展开 + harvest」
这套结构），不碰它的 stage / migrate 链路。

════════════════════════════════════════════════════════════════════════
两层表示（与附件同构）
════════════════════════════════════════════════════════════════════════

- **账本形态**（磁盘 / jsonl）：`{"type":"ref","ref":{"path","name","is_dir","project_id"}}`
  **只有路径与元数据**，没有任何文件字节。由 ws_bridge 组装并落盘；历史回放时
  `ws_bridge._history_to_ui` 用 `harvest_refs` 取回 UI 需要的列表。
- **请求体形态**（发给 LLM）：**一个**说明文本块（所有引用合并成一段路径清单 +
  「内容不在上下文中，需要时用 run_read」的硬约束）。由 `expand_ref_blocks_for_model`
  在**发送边界**（`Agent._model_messages`）现算，**不回写 history**。

提示词约束刻意写在**这条注入块里**而不是系统提示词：系统提示词有「逐字节相同 →
跨会话共享前缀缓存」的硬约束，为一个只在带引用消息上才成立的约束去污染所有会话的
L0 冻结段不划算。

════════════════════════════════════════════════════════════════════════
零行为变化（硬保证）
════════════════════════════════════════════════════════════════════════

`attach_ref_blocks(content, [])` 与 `expand_ref_blocks_for_model(message)`
在「无引用」时**返回传入的同一个对象**（不是等值副本）。无附件的会话请求体因此
与改造前**逐字节一致** —— 这条由结构保证，而不是靠"新增代码恰好没副作用"。
"""
from __future__ import annotations

import os
from collections import deque
from pathlib import Path

from logger import get_logger

log = get_logger("refs")

# jsonl content 里的块类型。与 attachments.ATTACHMENT_BLOCK_TYPE 平级、互不相干。
REF_BLOCK_TYPE = "ref"

# ── 默认忽略清单 ──────────────────────────────────────────────────
# 按**目录名整棵剪枝**：命中即不进入该目录，也不列出该目录本身。
# 依据是「列出来对用户没有价值，反而会淹没有效项」：依赖目录、构建产物、
# 版本控制内部结构、各种缓存。用户可在 ~/.aigent/config.json 用
# REF_LIST_IGNORE（逗号分隔）追加，但**不能**移除默认项。
DEFAULT_IGNORE_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", "out", "target", ".next", ".nuxt",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", "coverage", ".tox", ".eggs",
    # 我们自己写进工作空间的派生目录（2026-09-21）：`run_read` 读 PDF 时把页图
    # 栅格化到 `<workdir>/.aigent/pages/<key>/`（见 doc_convert.tool_cache_dir）。
    # 它**对用户没有任何引用价值**，列出来只会淹没真实文件；而引用候选里一旦
    # 出现它，用户还能选中引用一个缓存图，那更是荒谬。
    ".aigent",
})

# 文件级忽略（只跳过这些具体文件名，不做模式匹配）
DEFAULT_IGNORE_FILES = frozenset({".DS_Store"})


# ══════════════════════════════════════════════════════════════════
#  可调参数（环境变量 / ~/.aigent/config.json，读在调用点而非导入点）
# ══════════════════════════════════════════════════════════════════
# 与 attachments 同款：写成函数而非常量，因为 config.load() 合并配置进 os.environ
# 的时机可能晚于本模块被导入（agent_full_v2 → ws_bridge → refs 的导入链），
# 常量会在配置生效前被固化。

def _int_env(key: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def max_entries() -> int:
    """一次列目录返回的条目数上限（默认 3000）。

    上限存在的理由是**体感与性能**而不是安全：工作空间里一个 frontend/node_modules
    就有上千个文件，不做裁剪列表会长到无法浏览、遍历也会明显变慢。超出时
    `truncated=True`，前端在列表底部提示"已截断"。
    """
    return _int_env("REF_LIST_MAX_ENTRIES", 3000, minimum=1)


def ignore_dirs() -> frozenset[str]:
    """生效的忽略目录名集合 = 默认清单 ∪ `REF_LIST_IGNORE`（逗号分隔）。"""
    raw = os.environ.get("REF_LIST_IGNORE") or ""
    extra = {name.strip() for name in raw.split(",") if name.strip()}
    return DEFAULT_IGNORE_DIRS | extra


# ══════════════════════════════════════════════════════════════════
#  路径安全（语义镜像 tools.safe_path）
# ══════════════════════════════════════════════════════════════════

def resolve_within(workdir, raw) -> Path | None:
    """把前端传来的路径规范化到工作空间内；越界 / 非法一律返回 None。

    与 `tools.safe_path` **同一判定口径**（`resolve()` 后用 `is_relative_to(base)`
    判定），但输入形态不同：safe_path 收的是工具参数、由模型提供，可以直接抛
    `ValueError`；这里收的是前端线索，**必须吞掉一切异常返回 None**（一条越界引用
    不该打死整轮发送）。

    相对路径按工作空间根解析（与 run_read 一致）；`~` 会展开。
    """
    try:
        base = Path(workdir).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        # resolve() 同时解掉符号链接 —— 链出工作空间的路径会在这里现出原形并被拒绝
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != base and not resolved.is_relative_to(base):
        return None
    return resolved


def is_under(workdir, raw) -> bool:
    """`resolve_within` 的布尔快捷式（测试与 ws_bridge 用）。"""
    return resolve_within(workdir, raw) is not None


# ══════════════════════════════════════════════════════════════════
#  扁平列目录（BFS + 忽略清单 + 上限）
# ══════════════════════════════════════════════════════════════════

def list_workspace(workdir, *, limit: int | None = None,
                   ignore: frozenset[str] | None = None) -> dict:
    """扁平列出工作空间内的所有目录与文件（**无层级**）。

    返回 `{"workdir", "items", "truncated", "total_seen", "skipped"}`，
    `items` 每项为 `{"path", "name", "type"}`（type 取 `"dir"` / `"file"`）。
    前端用 `path` 去掉 `name` 即可还原所在目录，故**不重复发 dir 字段**。

    **算法取舍：BFS 而非 DFS。** 平铺列表 + 条数上限的组合下，DFS 会让第一个
    庞大子目录吃满全部名额，同级目录在列表里根本看不见；BFS 保证同一深度先铺满，
    截断时被砍掉的是最深条目，用户视野内的浅层条目永远完整。

    其它约定：
    - 每个目录内部先目录后文件，各自按 `name.casefold()` 升序 → 结果确定、可比对。
    - **符号链接一律跳过**：防环；且链出工作空间的 symlink 交给模型去读必然被
      `safe_path` 拒绝，不该由我们主动推荐一次注定失败的尝试。
    - 权限错误 / 并发删除：逐目录吞 `OSError` 并计入 `skipped`，**不中断整轮**。
    - **绝不抛异常**（本函数在 `asyncio.to_thread` 里跑，异常会让前端一直转圈）。
    """
    cap = limit if limit is not None else max_entries()
    ignore_names = ignore if ignore is not None else ignore_dirs()

    result: dict = {
        "workdir": "",
        "items": [],
        "truncated": False,
        "total_seen": 0,
        "skipped": 0,
    }
    if not str(workdir or "").strip():
        # 空 workdir 绝不能被当成"当前目录"：Path("") == Path(".")，
        # resolve() 会落到进程 cwd（Electron 拉起后端时 = 仓库目录），
        # 于是"列个工作空间"会变成把整个仓库列出来。宁可返回空。
        return result
    try:
        base = Path(workdir).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return result
    result["workdir"] = str(base)

    items: list[dict] = []
    total_seen = 0
    skipped = 0
    truncated = False
    queue: deque[Path] = deque([base])

    while queue and not truncated:
        current = queue.popleft()
        try:
            with os.scandir(current) as scan:
                entries = list(scan)
        except OSError:
            skipped += 1
            continue

        subdirs: list = []
        plain_files: list = []
        for entry in entries:
            total_seen += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in ignore_names:
                        continue
                    subdirs.append(entry)
                elif entry.is_file(follow_symlinks=False):
                    if entry.name in DEFAULT_IGNORE_FILES:
                        continue
                    plain_files.append(entry)
            except OSError:
                skipped += 1
                continue

        subdirs.sort(key=lambda e: e.name.casefold())
        plain_files.sort(key=lambda e: e.name.casefold())

        for kind, group in (("dir", subdirs), ("file", plain_files)):
            for entry in group:
                if len(items) >= cap:
                    truncated = True
                    break
                items.append({
                    "path": str(Path(entry.path)),
                    "name": entry.name,
                    "type": kind,
                })
            if truncated:
                break

        if not truncated:
            for entry in subdirs:
                queue.append(Path(entry.path))

    result["items"] = items
    result["truncated"] = truncated
    result["total_seen"] = total_seen
    result["skipped"] = skipped
    return result


# ══════════════════════════════════════════════════════════════════
#  账本形态：规范化引用记录 + 组装 user 消息 content
# ══════════════════════════════════════════════════════════════════

def normalize_ref(workdir, raw) -> dict | None:
    """前端线索 → **以磁盘为准**的引用记录；越界/非法返回 None。

    `name` 与 `is_dir` **一律重新从磁盘取**，不采信前端字段（前端字段可以伪造，
    伪造不出磁盘）。文件在挑选后消失时仍接受（`is_dir=False`）——用户点过它，
    静默丢弃比留一条"当前不存在"的提示更糟。
    """
    if not isinstance(raw, dict):
        return None
    resolved = resolve_within(workdir, raw.get("path"))
    if resolved is None:
        log.warning("引用路径越界或非法，已丢弃: %r", str(raw.get("path"))[:200])
        return None
    try:
        is_dir = resolved.is_dir()
    except OSError:
        is_dir = False
    return {
        "path": str(resolved),
        "name": resolved.name or str(resolved),
        "is_dir": bool(is_dir),
        "project_id": str(raw.get("project_id") or ""),
    }


def normalize_refs(workdir, raw_refs) -> list[dict]:
    """批量规范化 + 按 path 去重（保留首次出现），供 ws_bridge 一次调用。"""
    if not isinstance(raw_refs, list):
        return []
    seen: set[str] = set()
    records: list[dict] = []
    for raw in raw_refs:
        record = normalize_ref(workdir, raw)
        if record is None or record["path"] in seen:
            continue
        seen.add(record["path"])
        records.append(record)
    return records


def build_ref_block(ref: dict) -> dict:
    """引用记录 → 账本块（落 jsonl 的形态，**只有路径与元数据**）。"""
    return {
        "type": REF_BLOCK_TYPE,
        "ref": {
            "path": str(ref.get("path") or ""),
            "name": str(ref.get("name") or ""),
            "is_dir": bool(ref.get("is_dir")),
            "project_id": str(ref.get("project_id") or ""),
        },
    }


def attach_ref_blocks(content, refs) -> object:
    """把引用块挂到 user 消息 content 上。

    **无引用时返回传入的同一个对象**（零行为变化的硬保证）：
    - content 是 str（无附件）且有引用 → `[text块?, *ref块]`
    - content 是 list（有附件）且有引用 → `[*原块, *ref块]`（新列表，不改原对象）
    """
    if not isinstance(refs, list) or not refs:
        return content
    blocks = [build_ref_block(r) for r in refs if isinstance(r, dict)
              and str(r.get("path") or "").strip()]
    if not blocks:
        return content
    if isinstance(content, str):
        head = [{"type": "text", "text": content}] if content.strip() else []
        return head + blocks
    if isinstance(content, list):
        return list(content) + blocks
    return blocks


def history_has_refs(messages) -> bool:
    """这批消息里是否含有待展开的引用块（结构化短路用）。"""
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict) and block.get("type") == REF_BLOCK_TYPE
                    and isinstance(block.get("ref"), dict)):
                return True
    return False


def harvest_refs(content) -> list[dict]:
    """从 user 消息 content 里取出引用列表（供 `_history_to_ui` 回放）。

    返回**给前端渲染**的形状 `{"path","name","is_dir"}`。刻意**不做 stat**：
    回放要保持廉价（`_history_to_ui` 拿不到 workdir），且引用是否仍然有效对
    "这轮引用了什么"这个事实没有影响。
    """
    if not isinstance(content, list):
        return []
    found: list[dict] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != REF_BLOCK_TYPE:
            continue
        ref = block.get("ref")
        if not isinstance(ref, dict):
            continue
        path = str(ref.get("path") or "")
        if not path:
            continue
        found.append({
            "path": path,
            "name": str(ref.get("name") or ""),
            "is_dir": bool(ref.get("is_dir")),
        })
    return found


def ref_title_hint(raw_refs) -> str:
    """只有引用、没有正文时的标题兜底（`[引用] a.ts`）。

    通常用不到 —— 胶囊在 text 里序列化出 `@相对路径`，纯引用消息的 text 也非空。
    留它是防御性的：前端将来若改成不往 text 里写 token，首轮不至于得到空标题。
    """
    if not isinstance(raw_refs, list):
        return ""
    for item in raw_refs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            path = str(item.get("path") or "").strip()
            name = path.rsplit("/", 1)[-1] if path else ""
        if name:
            return f"[引用] {name}"
    return ""


# ══════════════════════════════════════════════════════════════════
#  请求体形态：发送边界展开（_model_messages 调用）
# ══════════════════════════════════════════════════════════════════

def _existence_note(path: str) -> str:
    """引用↔磁盘的现实核对：文件在挑选之后被删/改名/移走时，如实标注。

    只写在注入文本里、**不改账本块** —— 历史消息记录的是"当时引用了什么"，
    不该被后来的磁盘状态改写。
    """
    try:
        if Path(path).exists():
            return ""
    except (OSError, ValueError):
        return ""
    return "，当前不存在，可能已被删除或移动"


def _render_ref_text(refs: list[dict]) -> str:
    """把一批引用渲染成**一个**说明文本块（模型可见的全部引用信息）。"""
    lines = ["[用户引用了以下工作空间路径（仅路径与类型，文件内容不在上下文中）]"]
    for ref in refs:
        path = str(ref.get("path") or "")
        if not path:
            continue
        kind = "目录" if ref.get("is_dir") else "文件"
        lines.append(f"- {path}（{kind}{_existence_note(path)}）")
    lines.append("")
    lines.append(
        "需要内容时请用 run_read 按上述绝对路径读取 —— 它按类型自动分派"
        "（文本、图片、PDF、Word/Excel/PPT 都是同一个工具，PDF 会同时给出文本层"
        "与页图）；"
        "需要查看目录内容时用 run_glob 或 bash（工作目录即工作空间根）。"
    )
    lines.append("不要在未读取的情况下猜测、复述或杜撰这些文件的内容。")
    return "\n".join(lines)


def expand_ref_blocks_for_model(message):
    """把一条消息里的引用块展开为**一个**说明文本块（发送边界调用）。

    **契约**：
    - `content` 不是 list、或不含引用块 → **原样返回同一对象**（零行为变化）。
    - 含引用块 → 返回新的 dict；所有引用块被替换成末尾**一个** text 块，
      其余块（正文 / 已展开的附件块）顺序不变。
    - **绝不抛异常**：本函数处在 `_model_messages` 的列表推导里，一旦穿透就会
      让本轮请求体缺一条消息、整轮对话被打死。任何意外都降级为"原样返回"。
    """
    try:
        if not isinstance(message, dict):
            return message
        content = message.get("content")
        if not isinstance(content, list):
            return message

        refs: list[dict] = []
        remaining: list = []
        has_ref = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == REF_BLOCK_TYPE:
                has_ref = True
                ref = block.get("ref")
                # 空路径的块是畸形数据，不计入 refs —— 否则会注入一段只有标题与
                # 指令、却一条路径都没有的空说明（纯噪声）。
                if isinstance(ref, dict) and str(ref.get("path") or "").strip():
                    refs.append(ref)
                continue
            remaining.append(block)
        if not has_ref:
            return message

        if refs:
            remaining.append({"type": "text", "text": _render_ref_text(refs)})
        expanded = dict(message)
        expanded["content"] = remaining
        return expanded
    except Exception as exc:  # noqa: BLE001 — 工具/展开层契约：绝不向上抛
        log.error("expand_ref_blocks_for_model 异常: %s: %s", type(exc).__name__, exc,
                  exc_info=True)
        return message
