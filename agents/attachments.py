#!/usr/bin/env python3
"""
attachments.py - 会话附件（图片 / 文件）：登记、解析、发送前展开（新增）

设计见 docs/frontend/12-附件与文件输入.md。本模块是桌面端「添加文件或图片」
功能的后端实现，**叶子模块**：只依赖标准库 + `paths` + `logger` + `doc_convert`，
第三方解析库（pymupdf / python-docx / openpyxl / python-pptx / Pillow）全部
**函数内懒加载** —— 缺库只降级对应格式，不影响其它格式与整个后端启动。
文档转换（页图渲染 + 文本层）在 `doc_convert` 里做，本模块负责**编排降级链**
（转换层失败 → 回落纯文本抽取）与发送边界展开。

**禁止 import agent_full_v2 / session_manage**（会与引擎形成循环依赖）。

════════════════════════════════════════════════════════════════════════
两层表示（本模块的核心约定）
════════════════════════════════════════════════════════════════════════

- **账本形态**（磁盘 / jsonl）：`{"type":"attachment","attachment":{...}}`
  只有元数据与来源，**不含文件字节**。落盘由 ws_bridge 组装（`build_user_content`），
  历史回放时 `ws_bridge._history_to_ui` 从中 harvest 出 UI 需要的附件列表。
- **请求体形态**（发给 LLM）：图片 → `{"type":"image_url","image_url":{"url":"data:..."}}`；
  文档 → **多个块**：头部说明文本 + 正文文本，并在正文里按 `<!--img:pN-->` 锚点
  的位置**交错**插入图片块（不是把图全堆在末尾）。由
  `expand_content_for_model` 在**发送边界**（`Agent._model_messages`）现算。

为什么不在磁盘上直接存线格式（image_url + base64）：jsonl 会被整文件原子重写
（体积暴涨）、L4 摘要与 token 估算会被 base64 污染、无法缩放/去重、被厂商线格式
绑定。详见文档「取舍」一节。

════════════════════════════════════════════════════════════════════════
工具读图（`run_read` 读到图片/页图，2026-09-21）—— 同一套哲学、另一条通道
════════════════════════════════════════════════════════════════════════

模型看不见磁盘，它只看得见**请求体**。所以"给模型一个图片路径"本身毫无意义：
必须有谁把像素读进请求体。本模块同时承担两条这样的通道：

- **附件通道**（`attachment` 块）：用户在桌面端显式添加文件 → 复制副本 + 解析，
  图片在**用户自己那条消息**里展开。
- **工具通道**（`tool_image` 块）：模型调 `run_read(path)` 主动索取 →
  **零复制**（直接读工作空间里的原文件）→ 图片在一条**合成 user 消息**里展开。

工具通道必须用合成消息而不是塞进 tool 消息，原因是协议限制：Chat Completions 的
`tool` 消息 `content` 只接受 text part，图片塞不进去（只有 Anthropic Messages /
OpenAI Responses 支持工具结果带图）。所以 tool 消息只承载一句**元数据说明**
（"已读取 x.png，image/png，128KB"），图片块紧随其后作为独立消息发出。

注意说明**不是对图片内容的转述** —— 让另一个模型转述再回填是有损的二手信息，
正确做法是让主模型直接看到像素。这条边界值得反复强调：**工具的返回值形状，
就是模型能看到的东西。**

════════════════════════════════════════════════════════════════════════
目录布局（`WorkspacePaths.attachments_dir`）
════════════════════════════════════════════════════════════════════════

    .attachments/_draft/<att_id>/          ← 尚未发送（新会话此刻还没有 session_id）
        meta.json  <att_id>.<ext>  [<att_id>.txt]  [<att_id>.send.jpg]
        [<att_id>.pages/p1.jpg ...]         ← 转换层产出的页图资产（PDF）
    .attachments/<session_id>/             ← 已发送（发送时原子迁移）
        <att_id>.<ext>  <att_id>.meta.json  [<att_id>.txt]  [<att_id>.send.jpg]
        [<att_id>.pages/p1.jpg ...]

附件一律**复制**（原文件不动，只记 source_path）：只引用原路径的话，原文件被
移动/改名/删除后该条历史消息永久失效且无法自愈。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path

from logger import get_logger
from paths import (
    DRAFT_ATTACHMENTS_DIRNAME,
    PROJECTS_ROOT,
    WorkspacePaths,
)
# doc_convert 是叶子模块（只依赖标准库 + logger），模块级导入安全；pymupdf 本身
# 仍然是函数内懒加载。锚点语法与资产目录名让转换层做**唯一定义**，避免两处各写一份。
# 同理，「未提取到文本」占位串与 Office 抽取也在转换层唯一定义（2026-09-21 上移，
# 见 docs/frontend/15）—— 附件通道与工具读文档通道必须用同一套措辞与同一套判据。
import doc_convert
from doc_convert import (
    ASSETS_DIR_SUFFIX,
    EMPTY_NOTE_PREFIX,
    IMAGE_ANCHOR_RE,
    OFFICE_EXTS,
    empty_note,
    text_is_empty_note,
)

log = get_logger("attachments")

# ── 附件种类 ──────────────────────────────────────────────────────
KIND_IMAGE = "image"
KIND_DOCUMENT = "document"   # pdf / docx / xlsx / pptx（统一抽成文本供模型阅读）
KIND_TEXT = "text"           # 纯文本 / 代码：直接按文本读，不额外落 .txt

# jsonl content 里的块类型。**刻意用中性词**（不是 "image_url"）：存储形态与
# 厂商线格式解耦，`_model_messages` 负责展开。
ATTACHMENT_BLOCK_TYPE = "attachment"

# 工具读来的图片（`run_read` 读到图片 / 读到 PDF 的页图，2026-09-21）。与附件块同一
# 套哲学：中性词、**只有路径与 mime、不含字节**，只在发送边界展开成 image_url。
#
# 它和附件块长得像但**不是一回事**：附件块在 user 消息里（用户显式给出），
# 工具图片块在一条**合成 user 消息**里（紧随该批 tool 消息之后）—— 因为
# Chat Completions 的 `tool` 消息只接受 text part，图片塞不进工具结果本身。
#
# 形状：`{"type","text","images":[{path,name,mime,page?}],"source"?}`。`images` 是
# **列表**（一次读 PDF 可以带回 N 张页图）；旧数据的单数 `image` 字段仍被读取
# （见 `tool_image_items` 的归一化），但**只有历史回放会遇到**。
TOOL_IMAGE_BLOCK_TYPE = "tool_image"

# 合成消息的 marker 字段：标记"这条 user 消息承载的是工具读取的图片"。
# 它**不在 MODEL_MSG_FIELDS 白名单**里 → 落 jsonl、但不漏进 API 请求体。
TOOL_IMAGES_MARKER = "_tool_images"

# 解析产物后缀（与工程内其它"派生文件"命名习惯一致，带 att_id 前缀便于定位）
TEXT_SUFFIX = ".txt"
SEND_IMAGE_SUFFIX = ".send.jpg"
META_FILENAME = "meta.json"

# ── 支持的扩展名 ──────────────────────────────────────────────────
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
DOCUMENT_EXTS = (".pdf", ".docx", ".xlsx", ".pptx")
TEXT_EXTS = (
    # 文档/数据
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties",
    ".xml", ".html", ".htm", ".css", ".scss", ".less", ".svg",
    # 代码
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".java", ".kt", ".go", ".rs", ".rb", ".php", ".cs", ".swift", ".m", ".mm",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".sql", ".sh", ".bash", ".zsh", ".fish",
    ".bat", ".ps1", ".dockerfile", ".gradle", ".lua", ".r", ".pl", ".scala", ".dart",
)
# 老格式 Office：二进制复合文档，纯 Python 无可靠解析方案 → 明确拒绝并给指引
LEGACY_OFFICE_HINT = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
}

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".json": "application/json", ".yaml": "text/yaml", ".yml": "text/yaml",
    ".xml": "text/xml", ".html": "text/html", ".htm": "text/html",
}
DEFAULT_MIME = "application/octet-stream"

# att_id 形状（用于任何"拼路径"前的安全校验）
_ATT_ID_RE = re.compile(r"^att_[0-9A-Za-z]{6,32}$")
# 会话 id 形状（session_id 由 session_manage 生成：短 base62 或存量编号串）
_SID_RE = re.compile(r"^[0-9A-Za-z_]{1,64}$")

_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


# ══════════════════════════════════════════════════════════════════
#  可调参数（环境变量 / ~/.aigent/config/config.json，读在调用点而非导入点）
# ══════════════════════════════════════════════════════════════════
# 刻意写成函数而非常量：config.load() 合并配置进 os.environ 的时机可能晚于本模块
# 被导入（agent_full_v2 → attachments 的导入链），常量会在配置生效前被固化。

def _int_env(key: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def draft_ttl_seconds() -> int:
    """未发送草稿的存活时间（默认 72 小时），超期由 GC 清理。"""
    return _int_env("ATTACHMENT_DRAFT_TTL_SECONDS", 72 * 3600, minimum=60)


def orphan_min_age_seconds() -> int:
    """孤儿会话附件目录的清理门限（默认 24 小时）。

    门限存在的理由：会话刚建、jsonl 还没落盘的瞬间也不能误杀。
    """
    return _int_env("ATTACHMENT_ORPHAN_MIN_AGE_SECONDS", 24 * 3600, minimum=60)


def max_bytes(kind: str) -> int:
    """单附件体积上限：图片与文档分别可调（默认 10MB / 50MB）。"""
    if kind == KIND_IMAGE:
        return _int_env("ATTACHMENT_IMAGE_MAX_BYTES", 10 * 1024 * 1024, minimum=1024)
    return _int_env("ATTACHMENT_DOC_MAX_BYTES", 50 * 1024 * 1024, minimum=1024)


def text_max_chars() -> int:
    """单个文档抽取文本的字符上限（默认 30000，与 run_read 的 PDF/Office 分支同口径）。"""
    return _int_env("ATTACHMENT_TEXT_MAX_CHARS", 30000, minimum=256)


def text_total_max_chars() -> int:
    """一条消息内所有附件文本的合计上限（默认 60000）。"""
    return _int_env("ATTACHMENT_TEXT_TOTAL_MAX_CHARS", 60000, minimum=256)


def image_max_edge() -> int:
    """发送用图片的长边上限（默认 1568px）；0 = 不缩放，始终发原图。"""
    raw = os.environ.get("ATTACHMENT_IMAGE_MAX_EDGE")
    if raw is None or str(raw).strip() == "":
        return 1568
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 1568
    return value if value >= 0 else 1568


def image_decode_max_pixels() -> int:
    """发送用缩略图允许**解码**的像素上限（默认 5 亿；0 = 不限制）。

    为什么需要这条线（2026-09-21）：`Image.open()` 会命中 Pillow 自己的解压炸弹
    阈值（默认 89MP 报警、≥179MP 直接抛 `DecompressionBombError`）。那个阈值是
    防**不可信输入**的 DOS 防线，而这里读的是用户自己工作空间里的图 —— 高分屏
    截图、PDF 分块渲染出 22500×15016（3.4 亿像素）是常态，却被判成炸弹。
    所以防线换成**自己的、能说清话的**一条：超线时明确告诉模型"没发出去、
    因为多大、怎么办"，而不是让 provider 回一句难懂的 400 把整轮打死。

    5 亿像素 ≈ 解码峰值 1.5GB（RGB）；实测 3.4 亿像素的 PNG 解码+缩放
    约 1 秒 / 1.35GB。要突破这个量级（或反过来收紧到更小）改这里。
    """
    raw = os.environ.get("ATTACHMENT_IMAGE_DECODE_MAX_PIXELS")
    if raw is None or str(raw).strip() == "":
        return 500_000_000
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 500_000_000
    return value if value >= 0 else 500_000_000


def inline_max_bytes() -> int:
    """单张图片内联进请求体的字节上限（默认 15MB，base64 后约 20MB）。

    超过则降级为占位文本 —— 与其让 provider 回一个难懂的 413/400，不如在
    上下文里明确告诉模型"这张图太大没发出去"。
    """
    return _int_env("ATTACHMENT_INLINE_MAX_BYTES", 15 * 1024 * 1024, minimum=1024)


def doc_max_pages() -> int:
    """单文档最多渲染多少页**页图**（默认 20）；0 = 不限。

    只约束页图数量，**文本层永远是全量的** —— 文本是检索与无视觉模型兜底的主
    通道，不该被图像预算砍掉。
    """
    raw = os.environ.get("ATTACHMENT_DOC_MAX_PAGES")
    if raw is None or str(raw).strip() == "":
        return 20
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 20
    return value if value >= 0 else 20


def doc_max_images() -> int:
    """单文档随附的图片总数上限（默认 20）；0 = 不限。

    DeepSeek 的硬限是 600 张/请求，20 是 UX 值：再多也读不过来，只会撑大请求体。
    """
    return _int_env("ATTACHMENT_DOC_MAX_IMAGES", 20, minimum=1)


def doc_page_image_min_text() -> int:
    """页文本短于此值时判定"文本层不足以代表本页"→ 渲染页图（默认 200 字符）。

    这是页图渲染的**主判据**：文本层够密又没有图/表时，页图提供不了额外信息，
    渲染它纯属浪费请求体。调大 = 更保守地渲染（更贵、覆盖更全）。
    """
    raw = os.environ.get("ATTACHMENT_DOC_PAGE_IMAGE_MIN_TEXT")
    if raw is None or str(raw).strip() == "":
        return 200
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 200
    return value if value >= 0 else 200


def image_detail() -> str:
    """页图/内嵌图的 `detail` 档位（默认 `high`）。

    DeepSeek 的三档：`low` 推理前缩到 512×512、`high`（=`original`）保留原图、
    `auto` 自动。默认 `high` —— 保真优先，而服务端本来就把每张图的 token 封在
    1024 以内，选 `low` 省不到 token 只省请求体。留空 = 不发该字段。
    """
    value = str(os.environ.get("ATTACHMENT_IMAGE_DETAIL") or "").strip().lower()
    return value if value in ("low", "high", "original", "auto") else "high"


# ══════════════════════════════════════════════════════════════════
#  基础工具
# ══════════════════════════════════════════════════════════════════

def new_att_id() -> str:
    """生成全局唯一的附件 id（12 位 base62 随机）。

    全局唯一（而非会话内唯一）是为了让草稿区 `<att_id>/` 天然隔离并发：两个
    窗口同时选文件也不会有任何共享路径。
    """
    return "att_" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(12))


def human_size(size: int) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def mime_of(path: Path, fallback: str = "") -> str:
    if fallback:
        return fallback
    return _MIME_BY_EXT.get(path.suffix.lower(), DEFAULT_MIME)


def classify(path: Path) -> tuple[str | None, str]:
    """按扩展名判定附件种类。返回 `(kind, 错误原因)`；kind 为 None 表示不支持。"""
    ext = path.suffix.lower()
    if not ext:
        return None, "无法识别文件类型（该文件没有扩展名）"
    if ext in IMAGE_EXTS:
        return KIND_IMAGE, ""
    if ext in DOCUMENT_EXTS:
        return KIND_DOCUMENT, ""
    if ext in TEXT_EXTS:
        return KIND_TEXT, ""
    if ext in LEGACY_OFFICE_HINT:
        return None, (f"暂不支持 {ext} 老格式，请用 Office/WPS 另存为 "
                      f"{LEGACY_OFFICE_HINT[ext]} 后再添加")
    return None, f"不支持的文件类型（{ext}）"


def _draft_root(ws: WorkspacePaths) -> Path:
    return ws.attachments_dir / DRAFT_ATTACHMENTS_DIRNAME


def _session_dir(ws: WorkspacePaths, session_id: str) -> Path | None:
    sid = str(session_id or "")
    if not _SID_RE.match(sid):
        return None
    return ws.attachments_dir / sid


# ══════════════════════════════════════════════════════════════════
#  文本抽取（全部懒加载第三方库）
# ══════════════════════════════════════════════════════════════════

def extract_text(path: Path, ext: str) -> tuple[str, bool, int | None]:
    """抽取文件正文。返回 `(文本, 是否被截断, 页数|None)`。

    任何解析库缺失/文件损坏都会抛异常，由调用方（stage / expand）兜住 ——
    本函数自身不做静默降级，因为"抽不到内容"必须让用户看见。

    **Office 三种格式委托 `doc_convert.convert_office`**（2026-09-21 上移）：
    附件通道与工具读文档通道共用同一段抽取代码，否则同一个 xlsx「上传」与
    「@ 引用」会给出不同质量的结果。`.pdf` 的纯文本版本**留在这里** —— 它是
    转换层抛异常时的兜底实现（`_extract_pdf`），不是主路径。
    """
    limit = text_max_chars()
    if ext == ".pdf":
        return _extract_pdf(path, limit)
    if ext in OFFICE_EXTS:
        out = doc_convert.convert_office(path, ext, limit=limit)
        return (str(out.get("markdown") or ""),
                bool(out.get("text_truncated")),
                out.get("pages"))
    return _clip(read_text_file(path, limit * 4 + 1024), limit)



def _clip(text: str, limit: int) -> tuple[str, bool, int | None]:
    text = text or ""
    if len(text) > limit:
        return text[:limit], True, None
    return text, False, None


# ── 「未提取到文本」占位 ────────────────────────────────────────────
# 定义已上移到转换层（2026-09-21，见 docs/frontend/15）：`empty_note` /
# `text_is_empty_note` / `EMPTY_NOTE_PREFIX` 由本模块顶部导入后**原样再导出**，
# 既有调用方与测试不受影响。
#
# 为什么必须唯一定义：附件通道与工具读文档通道共用同一套"抽不到"判据。措辞或
# 前缀一旦分叉，"已降级"的判定就会在两条通道上给出不同答案 —— 而它同时是给
# 模型看的（"这里本该有内容但没读到"）和给 stage/UI 看的（记 warning）。


def _convert_document(src: Path, ext: str, dest: Path, att_id: str) -> dict:
    """文档 → 统一中间表示（Markdown + 页图资产）。

    **全部走 `doc_convert` 的统一转换层**：PDF 是「文本层 + 页图」双路，
    docx / xlsx / pptx 是 `convert_office` 的文本抽取（2026-09-21 上移，见
    docs/frontend/15）。PDF 保留"转换层抛异常 → 回落纯文本抽取"的降级链
    （最差情况等于现状）；Office 没有这条链，因为回落目标是同一段代码。

    返回 `{"body","text_truncated","pages","warnings","images","tables","converter"}`。
    `images` 是**页图资产个数**（int）；资产明细靠目录约定 `<att_id>.pages/pN.jpg`
    恢复，不进 meta —— 省 jsonl 体积，也不怕 meta 丢失。
    """
    limit = text_max_chars()
    if ext in OFFICE_EXTS:
        # Office 走统一转换层的文本抽取，**不再"回落"** —— 回落目标是同一段代码
        # （同一批懒加载库），重试一次只是把同一个异常抛两遍。抽取失败（库缺失 /
        # 文件损坏）照旧上抛，由 `_stage_one` 记成附件读取失败，与改造前一致。
        out = doc_convert.convert_office(src, ext, limit=limit)
        markdown = str(out.get("markdown") or "")
        warnings = list(out.get("warnings") or [])
        if text_is_empty_note(markdown):
            warnings.append("未提取到文本")
        return {
            "body": markdown,
            "text_truncated": bool(out.get("text_truncated")),
            "pages": out.get("pages"),
            "warnings": warnings,
            "images": 0,
            "tables": int(out.get("tables") or 0),
            "converter": str(out.get("converter") or doc_convert.OFFICE_CONVERTER),
        }
    try:
        if ext == ".pdf":
            out = doc_convert.convert_pdf(
                src, dest, att_id,
                max_edge=image_max_edge(),
                max_pages=doc_max_pages(),
                max_images=doc_max_images(),
                min_text=doc_page_image_min_text(),
            )
            markdown = str(out.get("markdown") or "")
            truncated = len(markdown) > limit
            warnings = list(out.get("warnings") or [])
            if not markdown.strip():
                warnings.append("未提取到文本")
            elif truncated:
                warnings.append("内容已截断")
            return {
                "body": markdown[:limit],
                "text_truncated": truncated,
                "pages": out.get("pages"),
                "warnings": warnings,
                "images": len(out.get("images") or []),
                "tables": int(out.get("tables") or 0),
                "converter": str(out.get("converter") or "doc_convert"),
            }
    except Exception as exc:  # noqa: BLE001 - 降级链：转换层坏了必须还能用
        log.warning("统一转换层不可用，回落纯文本抽取（%s）: %s: %s",
                    ext, type(exc).__name__, exc)

    body, truncated, pages = extract_text(src, ext)
    warnings = []
    if text_is_empty_note(body):
        warnings.append("未提取到文本")
    elif truncated:
        warnings.append("内容已截断")
    return {
        "body": body,
        "text_truncated": truncated,
        "pages": pages,
        "warnings": warnings,
        "images": 0,
        "tables": 0,
        "converter": "fallback_text",
    }


def read_text_file(path: Path, byte_cap: int | None = None) -> str:
    """按「编码探测」读纯文本：utf-8-sig → utf-8 → gbk → big5 → 有损替换。

    不做严格解码：中文用户环境里 gbk/cp936 的 txt 很常见，直接 utf-8 严格解码
    会抛 UnicodeDecodeError 把整个附件登记打死。

    `byte_cap`：只读前 N 字节（正文最终还要按字符数截断，没必要把 50MB 的日志
    整份读进内存）。**按字节切多字节字符是合法场景**，末尾残字由 `errors="replace"`
    兜住，不会抛。
    """
    if byte_cap is not None and byte_cap > 0:
        try:
            with open(path, "rb") as fh:
                data = fh.read(byte_cap)
        except OSError:
            raise
    else:
        data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "big5"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf(path: Path, limit: int) -> tuple[str, bool, int | None]:
    try:
        import fitz  # pymupdf
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RuntimeError("未安装 pymupdf，无法解析 PDF") from exc
    doc = fitz.open(str(path))
    try:
        total = len(doc)
        parts: list[str] = []
        used = 0
        read_pages = 0
        for i in range(total):
            text = (doc[i].get_text() or "").strip()
            read_pages = i + 1
            if text:
                chunk = f"--- 第 {i + 1} 页 ---\n{text}"
                parts.append(chunk)
                used += len(chunk)
            if used >= limit:
                break
        body = "\n\n".join(parts)
        truncated = read_pages < total or len(body) > limit
        if not body.strip():
            body = empty_note(".pdf")
        return body[:limit], truncated, total
    finally:
        doc.close()


# ══════════════════════════════════════════════════════════════════
#  图片预处理（Pillow 可选）
# ══════════════════════════════════════════════════════════════════

def prepare_image(src: Path, dst: Path) -> bool:
    """生成"发送用"缩略图。返回是否真的生成了。

    规则：长边 > image_max_edge，或原图 > 1.5MB 时才缩放（其余情况发原图，
    避免为了省几十 KB 反而引入一次 JPEG 有损重编码）。缩放能力由
    `_resize_jpeg_bytes` 统一提供 —— 含 Pillow 解压炸弹阈值的处理：旧实现里
    那张 3.4 亿像素的 PNG 在这里就抛 `DecompressionBombError` → 缩略图没生成
    → 原图被当成"发送用图"递了出去（2026-09-21 provider 400 的另一半原因）。

    失败仍返回 False → 发送原图（附件路径的既有约定）。与工具图路径"缩放失败
    绝不回落原图"**有意不同**：附件在登记时已按类型与体积校验过，且没有向模型
    解释原因的通路；能落到这里的只剩"超过本机解码上限"的极端图。
    """
    if dst.exists():
        return True
    edge = image_max_edge()
    if edge <= 0:
        return False
    data, code, detail = _resize_jpeg_bytes(src, edge,
                                            need_above_bytes=_RESIZE_ABOVE_BYTES)
    if data is None:
        if code == _RESIZE_NO_NEED:
            return False
        if code == _RESIZE_NO_PILLOW:
            log.info("未安装 Pillow，图片不做缩放（将发送原图）")
        else:
            log.warning("图片预处理失败（将发送原图）: %s: %s", code, detail)
        return False
    try:
        dst.write_bytes(data)
    except OSError as exc:
        log.warning("缩略图落盘失败（将发送原图）: %s: %s", dst, exc)
        return False
    return True


# ══════════════════════════════════════════════════════════════════
#  登记（staging）：复制 + 解析 → 草稿区
# ══════════════════════════════════════════════════════════════════

def stage(ws: WorkspacePaths, raw_paths: list) -> dict:
    """把一批本地路径登记为草稿附件。

    返回 `{"items": [...], "failed": [{"path":..., "reason":...}]}`。
    **单条失败不影响整批** —— 用户选了 5 个文件，不能因为其中 1 个是 .doc
    就全部失败。
    """
    items: list[dict] = []
    failed: list[dict] = []
    draft_root = _draft_root(ws)
    seen: set[str] = set()
    for raw in (raw_paths or []):
        raw_str = str(raw or "").strip()
        if not raw_str:
            continue
        if raw_str in seen:
            continue  # 同批重复路径只登记一次（前端也可能是多来源汇聚）
        seen.add(raw_str)
        try:
            item, reason = _stage_one(ws, draft_root, raw_str)
        except Exception as exc:  # noqa: BLE001 - 桥层契约：绝不向上抛
            log.error("附件登记异常 %r: %s: %s", raw_str, type(exc).__name__, exc,
                      exc_info=True)
            item, reason = None, f"处理失败：{exc}"
        if item is None:
            failed.append({"path": raw_str, "reason": reason or "未知原因"})
        else:
            items.append(item)
    if items or failed:
        log.info("附件登记: 成功 %d 个，失败 %d 个（project=%s）",
                 len(items), len(failed), ws.id)
    return {"items": items, "failed": failed}


def _stage_one(ws: WorkspacePaths, draft_root: Path,
               raw: str) -> tuple[dict | None, str]:
    src = Path(raw).expanduser()
    try:
        stat = src.stat()
    except OSError:
        return None, "文件不存在或不可读（可能已被移动/删除）"
    if src.is_dir():
        return None, "暂不支持文件夹，请选择具体文件"
    if not src.is_file():
        return None, "不是常规文件"
    kind, reason = classify(src)
    if kind is None:
        return None, reason
    if stat.st_size <= 0:
        return None, "空文件（0 字节）"
    limit = max_bytes(kind)
    if stat.st_size > limit:
        return None, f"文件过大（{human_size(stat.st_size)}，单文件上限 {human_size(limit)}）"

    att_id = new_att_id()
    dest = draft_root / att_id
    dest.mkdir(parents=True, exist_ok=False)
    ext = src.suffix.lower()
    stored = dest / f"{att_id}{ext}"
    try:
        # copy2 保留 mtime/权限；**不用 move** —— 用户的原文件必须原地留下
        shutil.copy2(src, stored)
        text_chars, text_truncated, pages = 0, False, None
        has_send_image = False
        warnings: list[str] = []
        images, tables, converter = 0, 0, ""
        if kind == KIND_IMAGE:
            has_send_image = prepare_image(stored, dest / f"{att_id}{SEND_IMAGE_SUFFIX}")
        elif kind == KIND_DOCUMENT:
            out = _convert_document(stored, ext, dest, att_id)
            body = out["body"]
            text_truncated = out["text_truncated"]
            pages = out["pages"]
            warnings = out["warnings"]
            images = out["images"]
            tables = out["tables"]
            converter = out["converter"]
            (dest / f"{att_id}{TEXT_SUFFIX}").write_text(body, encoding="utf-8")
            text_chars = len(body)
        meta = {
            "att_id": att_id,
            "kind": kind,
            "name": src.name,
            "mime": mime_of(src),
            "ext": ext,
            "size": int(stat.st_size),
            "source_path": str(src),
            "project_id": ws.id,
            "text_chars": text_chars,
            "text_truncated": bool(text_truncated),
            "pages": pages,
            # images = 随附的页图资产个数（明细靠 `<att_id>.pages/pN.jpg` 目录约定
            # 恢复，不进 meta）；tables = find_tables 命中数（无框线表格会漏）；
            # converter 记录走的哪条转换路径，出问题一眼可见。
            "images": int(images),
            "tables": int(tables),
            "converter": converter,
            "warnings": warnings,
            "has_send_image": bool(has_send_image),
            "created_at": time.time(),
        }
        (dest / META_FILENAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        # 半成品不留在草稿区（否则 GC 之前它会一直占着磁盘且无法被引用）
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return public_item(meta), ""


def public_item(meta: dict) -> dict:
    """草稿元数据 → 回给前端的形状（只暴露 UI 与后续发送需要的字段）。"""
    return {
        "att_id": meta.get("att_id", ""),
        "kind": meta.get("kind", ""),
        "name": meta.get("name", ""),
        "mime": meta.get("mime", ""),
        "ext": meta.get("ext", ""),
        "size": int(meta.get("size") or 0),
        "source_path": meta.get("source_path", ""),
        "project_id": meta.get("project_id", ""),
        "text_chars": int(meta.get("text_chars") or 0),
        "text_truncated": bool(meta.get("text_truncated")),
        "pages": meta.get("pages"),
        # 旧 meta.json 没有这四个字段 → 一律取默认值，存量附件零迁移
        "images": int(meta.get("images") or 0),
        "tables": int(meta.get("tables") or 0),
        "converter": str(meta.get("converter") or ""),
        "warnings": list(meta.get("warnings") or []),
    }


# ══════════════════════════════════════════════════════════════════
#  发送时迁移：草稿区 → 会话目录
# ══════════════════════════════════════════════════════════════════

def migrate_to_session(ws: WorkspacePaths, refs: list, session_id: str) -> list[dict]:
    """把前端带上的附件引用归位到会话目录，返回**以磁盘为准**的附件记录列表。

    前端传来的字段只当线索（att_id / ext / name 兜底），真实元数据一律重读
    `meta.json` —— 前端可以伪造字段，但伪造不出磁盘上的文件。

    健壮性优先（本函数在 chat 热路径里）：
    - 草稿区找不到 → 已在会话目录（重发同一批附件）→ 复用，不报错；
    - 两者都没有 → 返回 `missing=True` 的记录，让发送边界降级为占位文本；
    - 迁移失败（跨设备/权限）→ 记录告警但不阻断对话。
    """
    target = _session_dir(ws, session_id)
    if target is None:
        log.warning("附件迁移跳过：session_id 形状非法 %r", session_id)
        return []
    out: list[dict] = []
    for ref in (refs or []):
        if not isinstance(ref, dict):
            continue
        att_id = str(ref.get("att_id") or ref.get("id") or "")
        if not _ATT_ID_RE.match(att_id):
            log.warning("附件引用忽略：att_id 形状非法 %r", att_id)
            continue
        meta = _load_meta_anywhere(ws, att_id, ref)
        src_dir = _draft_root(ws) / att_id
        if src_dir.is_dir():
            try:
                target.mkdir(parents=True, exist_ok=True)
                _move_dir_contents(src_dir, target, att_id)
                shutil.rmtree(src_dir, ignore_errors=True)
            except OSError as exc:
                # 不阻断本轮：会话目录里可能只到位了一部分，expand 侧按存在性降级
                log.error("附件迁移失败 att_id=%s session=%s: %s: %s",
                          att_id, session_id, type(exc).__name__, exc)
        record = dict(meta or _ref_to_meta(ref))
        record["att_id"] = att_id
        record.setdefault("kind", str(ref.get("kind") or ""))
        record.setdefault("name", str(ref.get("name") or ""))
        record.setdefault("ext", str(ref.get("ext") or ""))
        record["session_dir"] = str(target)
        files = resolve_files(target, record)
        record["missing"] = files["original"] is None and files["text"] is None
        # 会话内副本的绝对路径：UI 显示缩略图 / 打开文件用它（`source_path` 是
        # 用户的原始文件路径，会话内部件在 `.attachments/<sid>/`）。
        # 迁移完成后再算 —— 草稿期的路径在迁移后就失效了。
        record["stored_path"] = str(files["original"]) if files["original"] else ""
        out.append(record)
    if out:
        log.info("附件归位: session=%s 共 %d 个（缺失 %d 个）",
                 session_id, len(out), sum(1 for r in out if r.get("missing")))
    return out


def _load_meta_anywhere(ws: WorkspacePaths, att_id: str, ref: dict) -> dict | None:
    """按 att_id 找回元数据：本空间草稿 → 本空间会话目录 → 跨空间草稿。"""
    candidates = [_draft_root(ws) / att_id / META_FILENAME]
    att_dir = ws.attachments_dir
    if att_dir.is_dir():
        candidates.extend(sorted(att_dir.glob(f"*/{att_id}.meta.json")))
    ref_pid = str((ref or {}).get("project_id") or "")
    if ref_pid and ref_pid != ws.id and _SID_RE.match(ref_pid):
        candidates.append(
            PROJECTS_ROOT / ref_pid / ".attachments" / DRAFT_ATTACHMENTS_DIRNAME
            / att_id / META_FILENAME
        )
    for path in candidates:
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _ref_to_meta(ref: dict) -> dict:
    """meta 缺失时用前端线索兜底（至少保住名字与类型，UI 才有东西可显示）。"""
    return {
        "att_id": str(ref.get("att_id") or ref.get("id") or ""),
        "kind": str(ref.get("kind") or ""),
        "name": str(ref.get("name") or ""),
        "mime": str(ref.get("mime") or ""),
        "ext": str(ref.get("ext") or ""),
        "size": int(ref.get("size") or 0),
        "source_path": str(ref.get("source_path") or ""),
        "text_chars": 0,
        "text_truncated": False,
    }


def _move_dir_contents(src_dir: Path, dst_dir: Path, att_id: str) -> None:
    """把草稿目录里的文件搬进会话目录；`meta.json` 改名为 `<att_id>.meta.json`。

    `os.replace` 在**同分区**时是原子的；跨分区抛 `OSError` → 降级 `shutil.move`
    （跨分区本来就不可能原子，本次直接接受）。
    同名先删：重发同一批附件时目标可能已存在。
    """
    for item in src_dir.iterdir():
        name = f"{att_id}.meta.json" if item.name == META_FILENAME else item.name
        dst = dst_dir / name
        if dst.exists():
            # 目录要 rmtree：`unlink` 对目录抛 IsADirectoryError。
            # 页图资产目录（`<att_id>.pages/`）就是这一路。
            if dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
            else:
                dst.unlink()
        try:
            os.replace(item, dst)
        except OSError:
            # 跨分区：shutil.move 对目录是递归搬移
            shutil.move(str(item), str(dst))


# ══════════════════════════════════════════════════════════════════
#  会话目录内的文件解析
# ══════════════════════════════════════════════════════════════════

def resolve_files(session_dir: Path | None, att: dict) -> dict:
    """在会话目录里定位该附件的三种文件。缺失的项为 None（不抛异常）。

    `session_dir` 为 None 时仍会尝试 `att["stored_path"]`（账本里记的绝对路径）——
    这样即使调用方拿不到会话目录（CLI 路径 / 老数据），也还能找到文件。
    """
    out: dict = {"original": None, "text": None, "send_image": None}
    att_id = str(att.get("att_id") or att.get("id") or "")
    if not _ATT_ID_RE.match(att_id):
        return out
    if session_dir is None:
        return _resolve_from_stored_path(out, att, att_id)
    if not session_dir.is_dir():
        return _resolve_from_stored_path(out, att, att_id)
    ext = str(att.get("ext") or "").lower()

    text_path = session_dir / f"{att_id}{TEXT_SUFFIX}"
    if ext != TEXT_SUFFIX and text_path.is_file():
        out["text"] = text_path
    send_path = session_dir / f"{att_id}{SEND_IMAGE_SUFFIX}"
    if send_path.is_file():
        out["send_image"] = send_path

    exact = session_dir / f"{att_id}{ext}" if ext else None
    if exact is not None and exact.is_file():
        out["original"] = exact
    else:
        # meta 缺失 / ext 线索不对时按前缀兜底，排除派生文件
        for cand in sorted(session_dir.glob(f"{att_id}.*")):
            if not cand.is_file():
                continue          # 跳过 `<att_id>.pages/`（页图资产目录）
            if cand.name == f"{att_id}.meta.json":
                continue
            if cand.name.endswith(SEND_IMAGE_SUFFIX):
                continue
            if ext != TEXT_SUFFIX and cand.suffix.lower() == TEXT_SUFFIX:
                continue
            out["original"] = cand
            break
    if out["original"] is None:
        return _resolve_from_stored_path(out, att, att_id)
    return out


def _resolve_from_stored_path(out: dict, att: dict, att_id: str) -> dict:
    """会话目录不可用时的兜底：用账本里记的 `stored_path` 直接定位。"""
    raw = str(att.get("stored_path") or "")
    if not raw:
        return out
    original = Path(raw)
    if not original.is_file():
        return out
    out["original"] = original
    sibling_dir = original.parent
    text_path = sibling_dir / f"{att_id}{TEXT_SUFFIX}"
    if text_path.is_file():
        out["text"] = text_path
    send_path = sibling_dir / f"{att_id}{SEND_IMAGE_SUFFIX}"
    if send_path.is_file():
        out["send_image"] = send_path
    return out


# ══════════════════════════════════════════════════════════════════
#  账本形态：组装 user 消息 content
# ══════════════════════════════════════════════════════════════════

def build_user_content(text: str, records: list) -> object:
    """组装 user 消息的 content。

    **无附件时返回 str**（与原实现逐字节一致 —— 这是零行为变化的硬保证，
    所有依赖字符串的链路：标题生成、首轮判定、日志切片，全部不受影响）；
    有附件时返回 blocks 列表。
    """
    text = text or ""
    if not records:
        return text
    blocks: list[dict] = []
    if text.strip():
        blocks.append({"type": "text", "text": text})
    for record in records:
        blocks.append({
            "type": ATTACHMENT_BLOCK_TYPE,
            "attachment": {
                "id": record.get("att_id", ""),
                "kind": record.get("kind", ""),
                "name": record.get("name", ""),
                "mime": record.get("mime", ""),
                "ext": record.get("ext", ""),
                "size": int(record.get("size") or 0),
                "source_path": record.get("source_path", ""),
                # 会话内副本（UI 缩略图 / 打开文件用；`source_path` 是用户的原始文件）
                "stored_path": record.get("stored_path", ""),
                "text_chars": int(record.get("text_chars") or 0),
                "text_truncated": bool(record.get("text_truncated")),
                "pages": record.get("pages"),
                # 解析统计 + 「诚实失败」通道（2026-09-20）：UI 靠它显示
                # 「N 页 · M 图 · K 表」并把解不出来的部分标成已降级。
                # 旧 jsonl 行没有这四个键 → 前端一律取默认值，存量零迁移。
                "images": int(record.get("images") or 0),
                "tables": int(record.get("tables") or 0),
                "converter": str(record.get("converter") or ""),
                "warnings": list(record.get("warnings") or []),
                "missing": bool(record.get("missing")),
            },
        })
    return blocks


def text_view(content) -> str:
    """多模态 content → 可读文本视图（`[图片: x.png]` + 正文），**不含任何文件字节**。

    给「钩子 / 日志 / 任何只接受字符串的调用方」用：钩子契约是"用户原始输入
    字符串"，带附件时 content 已变成数组，不能把数组直接塞给它。
    与 `context_compact.ContextCompact.content_to_str` 同一口径（那边为兼容旧
    分支保留了内联实现，改动它属于引擎层授权范围，故不在此强行合并）。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            if isinstance(block, str):
                parts.append(block)
            continue
        if block.get("type") == ATTACHMENT_BLOCK_TYPE:
            att = block.get("attachment")
            att = att if isinstance(att, dict) else {}
            label = "图片" if att.get("kind") == KIND_IMAGE else "附件"
            name = str(att.get("name") or "")
            parts.append(f"[{label}: {name}]" if name else f"[{label}]")
        elif block.get("type") == TOOL_IMAGE_BLOCK_TYPE:
            # 工具读图（run_read 读到图片/页图，2026-09-21）：块里只有路径与 mime，
            # 文本视图给一个标签即可，**绝不把路径拼进来**（路径不是内容）。
            # 一个块可能装多张图（一次读 PDF 的页图）→ 首张名字 + 总数，不摊开。
            names = [str(item.get("name") or "") for item in tool_image_items(block)]
            names = [n for n in names if n]
            if not names:
                parts.append("[图片]")
            elif len(names) == 1:
                parts.append(f"[图片: {names[0]}]")
            else:
                parts.append(f"[图片: {names[0]} 等 {len(names)} 张]")
        elif isinstance(block.get("text"), str):
            parts.append(block["text"])
    return " ".join(part for part in parts if part)


def harvest_attachments(content) -> list[dict]:
    """从 user 消息 content 里取出附件列表（供 `_history_to_ui` 回放）。

    返回的每项是**给前端渲染**的形状：id/kind/name/mime/ext/size/source_path/
    stored_path（前端用 stored_path 经 `aigent-att://` 显示缩略图、
    用 source_path 在 Finder 里定位原文件）。非 list content 与普通块一律跳过。

    同时带上解析统计（`pages` / `images` / `tables` / `warnings` / `converter`），
    让**回放出来的气泡**与发送前的 chip 显示同一套「N 页 · M 图 · K 表 / 已降级」。
    旧 jsonl 行没有这些键 → 一律取默认值，存量零迁移。
    """
    if not isinstance(content, list):
        return []
    found: list[dict] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != ATTACHMENT_BLOCK_TYPE:
            continue
        att = block.get("attachment")
        if not isinstance(att, dict):
            continue
        att_id = str(att.get("id") or "")
        if not att_id:
            continue
        found.append({
            "id": att_id,
            "kind": str(att.get("kind") or ""),
            "name": str(att.get("name") or ""),
            "mime": str(att.get("mime") or ""),
            "ext": str(att.get("ext") or ""),
            "size": int(att.get("size") or 0),
            "source_path": str(att.get("source_path") or ""),
            "stored_path": str(att.get("stored_path") or ""),
            "text_chars": int(att.get("text_chars") or 0),
            "text_truncated": bool(att.get("text_truncated")),
            "pages": att.get("pages"),
            "images": int(att.get("images") or 0),
            "tables": int(att.get("tables") or 0),
            "converter": str(att.get("converter") or ""),
            "warnings": list(att.get("warnings") or []),
            "missing": bool(att.get("missing")),
        })
    return found


# ══════════════════════════════════════════════════════════════════
#  请求体形态：发送边界展开（_model_messages 调用）
# ══════════════════════════════════════════════════════════════════

# 展开结果缓存：一轮对话内 `_model_messages` 会被调用多次（每次 LLM 往返一次），
# 没有缓存就会反复读盘 + base64 编码同一张图。键含 mtime/size，文件被替换即失效。
_EXPAND_CACHE: "OrderedDict[tuple, str]" = OrderedDict()
_EXPAND_CACHE_LOCK = threading.Lock()
_EXPAND_CACHE_MAX = 24


def _cache_get(key: tuple) -> str | None:
    with _EXPAND_CACHE_LOCK:
        value = _EXPAND_CACHE.get(key)
        if value is not None:
            _EXPAND_CACHE.move_to_end(key)
        return value


def _cache_put(key: tuple, value: str) -> None:
    with _EXPAND_CACHE_LOCK:
        _EXPAND_CACHE[key] = value
        _EXPAND_CACHE.move_to_end(key)
        while len(_EXPAND_CACHE) > _EXPAND_CACHE_MAX:
            _EXPAND_CACHE.popitem(last=False)


def clear_expand_cache() -> None:
    """清空展开缓存（测试用；会话删除后也可调用以免持有已删文件的 data URL）。"""
    with _EXPAND_CACHE_LOCK:
        _EXPAND_CACHE.clear()


def history_has_attachments(messages) -> bool:
    """这批消息里是否含有待展开的附件块。

    发送边界用它做**结构化短路**：无附件会话不去读模型能力、不做任何额外工作，
    "无附件请求体逐字节等价"就由结构保证，而不是靠"新增代码恰好没副作用"。
    """
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict) and block.get("type") == ATTACHMENT_BLOCK_TYPE
                    and isinstance(block.get("attachment"), dict)):
                return True
    return False


# ── 工具读图（run_read 读到图片/页图，2026-09-21）──────────────────────
# 与附件**刻意不同的两点**，写在这里免得后来者以为可以照抄：
#
# 1. **生命周期没有 stage 阶段**。附件在登记时就把缩放副本落盘（那时有天然的
#    dst 目录）；工具图片来自工作空间里的任意文件，而发送边界**不允许写盘**
#    （热路径 + 往用户工作空间塞派生文件是不该有的副作用）。所以缩放改在
#    **编码时于内存里做**（`_data_url_resized`），不产生任何新文件。
# 2. **图片不走 tool 消息**。Chat Completions 的 `tool` 消息只接受 text part，
#    图片塞不进去 → 工具返回值只带一句元数据说明，图片块由 agent_loop 聚合进
#    一条**合成 user 消息**（`build_tool_images_message`）。


def tool_images_max_per_turn() -> int:
    """一轮内随附给模型的工具图片数上限（默认 8）。

    上限存在的理由：图片一旦进了历史，**此后每次请求都要重新上传一遍 base64**
    （附件同理）。不设限时模型连读二十张图，请求体与每轮上传都会线性膨胀。
    超出的图**不静默丢弃**，换成一句说清原因的文本（模型才知道自己没看全）。
    """
    return _int_env("VIEW_IMAGE_MAX_PER_TURN", 8, minimum=1)


def _image_item(path, *, name: str = "", mime: str = "", page=None) -> dict:
    """一张图片的**条目**（路径 + 元数据，不含字节）。"""
    raw = str(path or "")
    p = Path(raw) if raw else Path()
    item = {
        "path": str(p) if raw else "",
        "name": name or (p.name if raw else ""),
        "mime": mime or (mime_of(p) if raw else ""),
    }
    if page is not None:
        try:
            item["page"] = int(page)
        except (TypeError, ValueError):
            pass
    return item


def tool_image_items(value) -> list[dict]:
    """工具返回值 → 图片条目列表。**唯一的归一化入口**。

    同时认两种形状：新形状 `images: [...]`（一次读 PDF 带回多张页图，2026-09-21）
    与旧形状 `image: {...}`（历史 jsonl 里的单图记录）。归一化**只在这一处做**，
    消费侧一律走它 —— 否则每加一个消费点就要各自兼容一次存量数据。
    空路径的条目在这里丢掉（畸形数据不该变成"一张没有路径的图"）。
    """
    if not isinstance(value, dict) or value.get("type") != TOOL_IMAGE_BLOCK_TYPE:
        return []
    raw = value.get("images")
    if not isinstance(raw, list):
        single = value.get("image")
        raw = [single] if isinstance(single, dict) else []
    items: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path:
            continue
        item = {
            "path": path,
            "name": str(entry.get("name") or ""),
            "mime": str(entry.get("mime") or ""),
        }
        if entry.get("page") is not None:
            item["page"] = entry["page"]
        items.append(item)
    return items


def build_tool_image_result(path, *, name: str = "", mime: str = "", page=None) -> dict:
    """**单图**工具返回值（`run_read` 读到一张图片）。中性条目 + 一句元数据说明，**不含字节**。

    返回值里带 `text` 是协议要求：tool 消息必须回答那条 `tool_call_id`，而它
    只能放文本。所以职责被拆成两半 —— "说明"走 tool 消息，"图片"走进随其后的
    合成 user 消息。说明只是**元数据**（格式、体积），**不是对图片内容的转述**：
    转述是有损的二手信息，而模型要看的是像素本身。

    多图（PDF 的文本层 + 页图）用 `build_tool_images_result`。
    """
    item = _image_item(path, name=name, mime=mime, page=page)
    try:
        size_text = (human_size(Path(item["path"]).stat().st_size)
                     if item["path"] else "体积未知")
    except OSError:
        size_text = "体积未知"
    label = item["name"] or item["path"]
    return {
        "type": TOOL_IMAGE_BLOCK_TYPE,
        "text": f"已读取图片 {label}（{item['mime'] or '未知格式'}，{size_text}），内容随附。",
        "images": [item],
    }


def build_tool_images_result(images, *, text: str = "", source: str = "") -> dict:
    """**多图**工具返回值（`run_read` 读 PDF：文本层 + N 张页图）。

    `images` 每项是 dict（`path` + 可选 `name` / `mime` / `page`）。`text` 是 tool
    消息的正文（PDF 的文本层与说明），`source` 是这一组图片的来源标签（如
    `spec.pdf`），**只用于合成消息的头部说明、不进账本块**。
    """
    items: list[dict] = []
    for entry in images or []:
        if isinstance(entry, dict):
            item = _image_item(entry.get("path"),
                               name=str(entry.get("name") or ""),
                               mime=str(entry.get("mime") or ""),
                               page=entry.get("page"))
        else:
            item = _image_item(entry)
        if item["path"]:
            items.append(item)
    out: dict = {"type": TOOL_IMAGE_BLOCK_TYPE, "text": str(text or ""),
                 "images": items}
    if str(source or "").strip():
        out["source"] = str(source).strip()
    return out


def is_tool_image_result(value) -> bool:
    """是不是工具读图的返回值（引擎据此决定"不要 str() 掉它"）。

    判据是**形状**（type + 至少一个图片字段在），不是"里面有有效路径"：空路径的
    畸形块也该被识别成图片结果、由聚合层丢掉，而不是 `str()` 成一段 JSON 塞进
    tool 消息。
    """
    return (isinstance(value, dict) and value.get("type") == TOOL_IMAGE_BLOCK_TYPE
            and (isinstance(value.get("images"), list)
                 or isinstance(value.get("image"), dict)))


def tool_image_text(value) -> str:
    """工具返回值里的说明文本（用作 tool 消息的 content）。"""
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return "已读取图片，内容随附。"


def tool_image_block(value) -> dict | None:
    """工具返回值 → 中性账本块（剥掉外层说明，只留路径与元数据）。

    一个工具结果 = **一块**（块内可以是多张图）：这样"一次 PDF 读取"在历史里仍是
    一条记录，回放时不会拆成 N 条互不相干的图片条目。
    """
    if not is_tool_image_result(value):
        return None
    items = tool_image_items(value)
    if not items:
        return None
    return {"type": TOOL_IMAGE_BLOCK_TYPE, "images": items}


def _group_label(value, items: list[dict]) -> str:
    """一组图片的标签（用于合成消息头部与"被丢掉"的提示）。"""
    source = str(value.get("source") or "").strip() if isinstance(value, dict) else ""
    if source:
        pages = [str(i.get("page")) for i in items if i.get("page") is not None]
        if pages and len(pages) == len(items):
            return f"{source} 第 {'、'.join(pages)} 页"
        return f"{source}（{len(items)} 张）"
    if len(items) == 1:
        return str(items[0].get("name") or items[0].get("path") or "")
    names = [str(i.get("name") or i.get("path") or "") for i in items[:3]]
    return "、".join(n for n in names if n)


def build_tool_images_message(values, *, limit: int | None = None) -> dict | None:
    """一批工具返回值 → **一条**合成 user 消息（说明文本 + M 个图片块）。

    必须**批量聚合成一条**：图片块不能插在两条 tool 消息之间 —— 那会打断
    assistant 的 `tool_calls` ↔ tool 消息链（协议风险）。调用方（agent_loop）
    在该批 tool 消息**全部落盘之后**才追加这一条。

    **预算按"组"计，不按"张"计**（2026-09-21）：上限 `VIEW_IMAGE_MAX_PER_TURN`
    数的是**图片承载的工具结果**个数。一次 PDF 读取是一个组，要么整组随附、要么
    整组丢掉 —— **绝不把一次文档读取按页数拦腰截断**（那样模型会以为它看到了全部
    页，比什么都不给更危险）。单组内部张数由工具自己按 `ATTACHMENT_DOC_MAX_IMAGES`
    限制，并在工具正文里点名未渲染的页。

    没有任何图片（全是畸形数据）时返回 None，调用方不追加空消息。
    """
    cap = tool_images_max_per_turn() if limit is None else limit
    blocks: list = []
    labels: list[str] = []
    dropped: list[str] = []
    for value in values or []:
        block = tool_image_block(value)
        if block is None:
            continue
        label = _group_label(value, block["images"])
        if len(blocks) >= cap:
            dropped.append(label)
            continue
        blocks.append(block)
        labels.append(label)
    if not blocks and not dropped:
        return None
    total = sum(len(b["images"]) for b in blocks)
    if total == 1:
        head = f"[以下是 run_read 读取的图片，内容随附：{labels[0]}]"
    else:
        head = (f"[以下是 run_read 读取的图片，共 {total} 张，内容随附："
                f"{'、'.join(labels)}]")
    lines = [head]
    if dropped:
        lines.append(
            f"（另有 {len(dropped)} 个文件未随附：单轮图片上限 {cap} 个，"
            f"可下一轮继续读取：{'、'.join(dropped[:5])}）"
        )
    return {
        "role": "user",
        "content": [{"type": "text", "text": "\n".join(lines)}, *blocks],
        TOOL_IMAGES_MARKER: True,
    }


def is_tool_images_message(message) -> bool:
    """这条消息是不是"承载工具图片的合成消息"（回放侧据此跳过）。"""
    return bool(isinstance(message, dict) and message.get(TOOL_IMAGES_MARKER))


# ── 图片识别（魔数优先）─────────────────────────────────────────────
# 只认这几类：`IMAGE_EXTS` 里的每一种都在此有对应签名。SVG 不在此列（它是文本，
# 走 TEXT_EXTS），所以"看起来像图片但其实该当文本读"的文件不会被误判。
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff_image_mime(path) -> str:
    """按**文件头**判图片类型；不是可识别的图片返回 ""。

    为什么不信扩展名：模型/用户手里的文件常挂着不匹配的后缀（截图存成 .txt、
    改名过的下载文件、被当图片引用的日志）。把二进制当图片发出去会污染上下文，
    甚至让 provider 直接报错；反过来把真图当文本读则是必然的解码失败。
    16 字节读一次就够（WebP 走 RIFF 容器：头部 `RIFF` + 偏移 8 起 `WEBP`）。
    """
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return ""
    for signature, mime in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def is_image_path(path) -> bool:
    """扩展名判定（**不读盘**，零 IO）——给 `run_read` 这种每轮都跑的热路径用。

    严格性与 `sniff_image_mime` 有意区分：这里只看后缀，因为它的用途是
    "在必然失败之前指路"，漏判的后果只是一次解码报错（`run_read` 的
    `UnicodeDecodeError` 分支会再兜一次），而误判的代价是白读一次盘。
    """
    return Path(str(path)).suffix.lower() in IMAGE_EXTS


def history_has_images(messages) -> bool:
    """这批消息里是否含**任何**待展开的图片（附件图片 ∪ 工具图片）。

    发送边界用它做结构化短路：无图片的会话不去读模型能力。原来只判附件块
    （`history_has_attachments`），工具图片进来后必须一起判 —— 否则走
    读图的会话会拿到"未知模型 = 按支持图片处理"的默认值，
    不支持图片的模型收到图片后会由 provider 报错。
    """
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == ATTACHMENT_BLOCK_TYPE and isinstance(block.get("attachment"), dict):
                return True
            if btype == TOOL_IMAGE_BLOCK_TYPE:
                return True
    return False


def expand_content_for_model(message, session_dir: Path | None, *,
                             supports_image: bool | None = None):
    """把一条待发送消息里的附件块展开为 provider 线格式。

    **契约**：
    - `content` 不是 list（无附件）→ **原样返回同一对象**（零行为变化）；
    - `content` 里没有附件块 → 同样原样返回，不为无附件会话制造任何差异；
    - 任何单个附件展开失败 → 降级为占位文本块，**绝不抛异常**
      （异常穿透会打死整轮 agent_loop，本轮 tool_result 全缺）。
    - 展开结果**不回写** `history_messages`：内存与 jsonl 恒为账本形态。
    - **块数会变**：一个附件块可能展开成多个结果块（文档 = 头部文本 + 正文 +
      按锚点交错的页图）。内容数组缩放了，但顺序仍是"该附件产出的块"聚集在一起。

    `supports_image`：当前生效模型是否支持图片输入（三态）。
    `False` 时图片降级为占位文本块而不是原样发出去让 provider 报错；`True`/`None`
    （未知模型）一律按支持处理。调用方 `Agent._model_messages` 负责查能力 ——
    本模块是叶子模块，不 import llm_config。
    """
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if not isinstance(content, list):
        return message
    expanded: list = []
    changed = False
    for block in content:
        if (isinstance(block, dict) and block.get("type") == ATTACHMENT_BLOCK_TYPE
                and isinstance(block.get("attachment"), dict)):
            # extend 而非 append：文档可能展开成「文本 + 图片 + 文本…」多块
            expanded.extend(_expand_one(block["attachment"], session_dir,
                                        supports_image=supports_image))
            changed = True
        elif isinstance(block, dict) and block.get("type") == TOOL_IMAGE_BLOCK_TYPE:
            # 工具读图（2026-09-21）：块里只有路径与 mime，展开成 `image_url` 或
            # 说清原因的占位文本。**一个块可能装多张图**（一次读 PDF 带回的页图），
            # 所以这里是 extend 一层循环，不是取单张。
            items = tool_image_items(block)
            if not items:
                expanded.append(block)  # 畸形块原样透传，绝不抛
                continue
            for image in items:
                expanded.extend(_expand_tool_image(image,
                                                   supports_image=supports_image))
            changed = True
        else:
            expanded.append(block)
    if not changed:
        return message
    out = dict(message)
    out["content"] = expanded
    return out


def _page_assets(session_dir: Path | None, att: dict, files: dict) -> dict[str, Path]:
    """该附件的页图资产：锚点 id（`p1`）→ 文件路径。

    靠**目录约定**恢复（`<att_id>.pages/pN.jpg`），不读 meta —— meta 丢了、
    或附件是从别处拷来的，只要目录在就能恢复。锚点写在 markdown 里，文件名与
    锚点 id 一一对应，位置信息不会错乱。
    """
    att_id = str(att.get("att_id") or att.get("id") or "")
    if not _ATT_ID_RE.match(att_id):
        return {}
    dirs = []
    if session_dir is not None:
        dirs.append(session_dir / f"{att_id}{ASSETS_DIR_SUFFIX}")
    original = files.get("original")
    if original is not None:
        dirs.append(original.parent / f"{att_id}{ASSETS_DIR_SUFFIX}")
    for directory in dirs:
        if not directory.is_dir():
            continue
        out: dict[str, Path] = {}
        try:
            for path in sorted(directory.iterdir()):
                if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
                    out[path.stem] = path
        except OSError:
            continue
        if out:
            return out
    return {}


def _image_block(url: str, detail: str | None = None) -> dict:
    """provider 线格式的图片块（Chat Completions）。

    `detail` 只对**我们生成的页图**传：那是我们主动做的保真取舍。用户上传的图片
    一概不带（`prepare_image` 已经决定过尺寸），避免给不认识该字段的兼容端点
    制造 400 风险。
    """
    image_url: dict = {"url": url}
    if detail:
        image_url["detail"] = detail
    return {"type": "image_url", "image_url": image_url}


def _document_blocks(body: str, assets: dict[str, Path],
                     supports_image: bool | None) -> list[dict]:
    """Markdown（含 `<!--img:pN-->` 锚点）→ **交错**的文本块与图片块。

    交错而不是把图堆在末尾：图片块在 content 数组里的**物理位置**就是它该在的
    位置，模型看到的是"这段文字旁边是这张图"，而不是"一大堆字，然后一大堆图"。
    锚点注释只是存储层的可读载体，位置由块顺序表达。
    """
    text = body or ""
    blocks: list[dict] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            chunk = "\n".join(pending).strip()
            if chunk:
                blocks.append(_text_block(chunk))
            pending.clear()

    pos = 0
    for match in IMAGE_ANCHOR_RE.finditer(text):
        pending.append(text[pos:match.start()])
        pos = match.end()
        flush()
        anchor = match.group(1)
        path = assets.get(anchor)
        if path is None:
            blocks.append(_text_block(f"[图片 {anchor} 缺失：资产文件未找到]"))
            continue
        if supports_image is False:
            blocks.append(_text_block(
                f"[图片 {anchor}]（未发送：当前模型不支持图片输入，"
                f"请切换到带「图片」能力的模型）"))
            continue
        url = _data_url(path, _image_mime(path, {}))
        if url is None:
            size = human_size(path.stat().st_size) if path.exists() else "未知大小"
            blocks.append(_text_block(
                f"[图片 {anchor} 未能发送：{size}，超过内联上限]"))
            continue
        blocks.append(_image_block(url, detail=image_detail()))
    pending.append(text[pos:])
    flush()
    return blocks


def _expand_one(att: dict, session_dir: Path | None, *,
                supports_image: bool | None = None) -> list[dict]:
    """单个附件块 → **一组**线格式块。任何异常都收束成占位文本块。

    返回列表而非单块：文档现在可能随附页图，要按锚点位置把文本块与图片块交错
    排开。图片/文本附件仍是单元素列表 —— 调用方统一 `extend` 即可。
    """
    name = str(att.get("name") or "附件")
    kind = str(att.get("kind") or "")
    try:
        files = resolve_files(session_dir, att)
        if kind == KIND_IMAGE:
            return [_expand_image(att, files, name, supports_image)]
        return _expand_document(att, files, session_dir, name, supports_image)
    except Exception as exc:  # noqa: BLE001 - 发送边界绝不能抛
        log.warning("附件展开失败 att_id=%s name=%s: %s: %s",
                    att.get("id"), name, type(exc).__name__, exc, exc_info=True)
        return [_text_block(f"[附件读取失败: {name}]")]


def _expand_image(att: dict, files: dict, name: str,
                  supports_image: bool | None) -> dict:
    """图片附件 → `image_url` 块；发不出去时降级为说清原因的占位文本块。"""
    if supports_image is False:
        # 能力不符 → 占位。**必须在读盘/编码之前判**：既省掉一次 base64，
        # 也保证"模型看不到的图"会明确说出来而不是静默消失。
        return _text_block(
            f"[图片: {name}]（未发送：当前模型不支持图片输入，"
            f"请切换到带「图片」能力的模型）"
        )
    path = files.get("send_image") or files.get("original")
    url = _data_url(path, _image_mime(path, att)) if path else None
    if url:
        return {"type": "image_url", "image_url": {"url": url}}
    if path is not None:
        # 文件在，但体积超内联上限：明确告知，而不是让 provider 报错
        return _text_block(
            f"[图片: {name}]（图片过大未能发送：{human_size(path.stat().st_size)}）"
        )
    return _text_block(f"[图片缺失: {name}]（原文件已被移动或删除）")


def _expand_tool_image(image: dict, *, supports_image: bool | None) -> list[dict]:
    """工具图片块 → `image_url` 块（或说清原因的占位文本）。

    与 `_expand_image`（附件）的差别只有一条：**没有 files 映射、不落盘缩放**。
    缩放改在编码时内存里做（`_data_url_tool_image`），因为发送边界不许往
    用户工作空间写派生文件。

    所有失败路径都降级成"说清原因"的文本，**绝不抛异常** —— 本函数位于
    `_model_messages` 的列表推导里，异常穿透会让本轮请求体缺消息、整轮被打死。
    """
    name = str(image.get("name") or "")
    if supports_image is False:
        # 能力不符 → 占位。**必须在读盘/编码之前判**：既省一次 base64，
        # 也保证"模型看不到的图"会明确说出来而不是静默消失。
        return [_text_block(
            f"[图片: {name}]（未发送：当前模型不支持图片输入，"
            f"请切换到带「图片」能力的模型）")]
    raw = str(image.get("path") or "")
    if not raw:
        return [_text_block(f"[图片缺失: {name}]（记录里没有路径）")]
    path = Path(raw)
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        return [_text_block(f"[图片缺失: {name or raw}]（文件已被移动或删除）")]
    mime = _image_mime(path, image)
    url, why = _data_url_tool_image(path, mime)
    if url:
        return [_image_block(url)]
    try:
        size_text = human_size(path.stat().st_size)
    except OSError:
        size_text = "体积未知"
    # 说清"为什么没发出去"+"下一步怎么办"：`why` 来自编码层（尺寸超解码上限 /
    # 缩放失败 / 超过内联上限），替换掉旧版一律写"超过内联上限"的错话。
    return [_text_block(
        f"[图片: {name or path.name}]（未能发送：{why or '未知原因'}，文件 {size_text}。"
        f"可先用 bash 缩放后再读，如 sips -Z 1568 \"{path}\" --out 小图.jpg）")]


# ══════════════════════════════════════════════════════════════════
#  "发送用"缩放核心（附件落盘版 与 工具图内存版 共用）
# ══════════════════════════════════════════════════════════════════
#
# **2026-09-21 事故与修复**（`view_image` 后 provider 回
# "You have uploaded an unsupported image"，整轮对话被打死）：
# 用户 @ 引用了一张 22500×15016（3.4 亿像素、4.6MB）的 PNG。`Image.open()`
# 命中 Pillow 的解压炸弹阈值（≥179MP 抛 `DecompressionBombError`）→ 旧代码把
# "缩放搞砸了"和"尺寸本来就小、不需要缩"混为同一个 `None` → **回落发原图** →
# provider 收到一张 3.4 亿像素的 PNG，回一句与真实原因无关的 400。
# 修复两点，缺一不可：
#   1. 阈值**抬升**（那是我方防线，不是 provider 能力），改由
#      `image_decode_max_pixels()` 接手，先读头部尺寸、再决定是否解码；
#   2. **缩放失败绝不回落原图** —— 只有"尺寸本来就 ≤ edge"才走原图
#      （状态码 `NO_NEED` 与 `FAILED` 必须分开，这正是事故的形状）。
#
# 超过这个体积才值得付一次重编码（与 `prepare_image` 的 1.5MB 同口径）：
# 为省几十 KB 而引入一次 JPEG 有损编码不划算。
_RESIZE_ABOVE_BYTES = 1_500_000

_RESIZE_OK = "ok"
_RESIZE_NO_NEED = "no_need"        # 长边/体积本来就在范围内 → 调用方发原图
_RESIZE_NO_PILLOW = "no_pillow"    # 未安装 Pillow → 无法判断，调用方按原行为处理
_RESIZE_TOO_BIG = "too_big"        # 声明尺寸超本机解码上限（**未解码**）
_RESIZE_FAILED = "failed"          # 解码/编码异常

# 只为"临时抬升 Pillow 阈值"这一瞬间串行化 `Image.open()`：检查发生在 open
# 那一刻，`load()` 不再查。这样既不把 1GB 级解码压在锁里，也避免并发线程
# 读到阈值的中间态（各自 save/restore 会互相把对方按回默认值）。
_RESIZE_OPEN_LOCK = threading.Lock()


def _resize_jpeg_bytes(path: Path, edge: int, *,
                       need_above_bytes: int = 0) -> tuple[bytes | None, str, str]:
    """把图缩到长边 `edge` 并编成 JPEG 字节。**任何异常都不外抛**。

    返回 `(JPEG 字节 | None, 状态码, 说明)`；说明是 `宽×高`（状态码
    `FAILED` 时是异常摘要）。字节为 None 时看状态码决定调用方怎么办 ——
    除 `NO_NEED` / `NO_PILLOW` 外一律**不要**回落原图。

    `need_above_bytes`：体积超过它才值得缩（`prepare_image` 传 1.5MB 复刻
    "尺寸够小但体积大 → 也缩"的老规则；工具图路径调用前已判过体积，传 0）。
    """
    try:
        from PIL import Image
    except ImportError:
        return None, _RESIZE_NO_PILLOW, ""
    try:
        stat = path.stat()
    except OSError:
        return None, _RESIZE_FAILED, "文件不可读"
    try:
        with _RESIZE_OPEN_LOCK:
            previous = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = None
            try:
                im = Image.open(path)
                width, height = im.size  # 头部解析，**尚未解码像素**
            finally:
                Image.MAX_IMAGE_PIXELS = previous
    except Exception as exc:  # noqa: BLE001 - 打不开的文件降级为说明
        log.warning("图片打开失败 %s: %s: %s", path.name, type(exc).__name__, exc)
        return None, _RESIZE_FAILED, f"无法打开（{type(exc).__name__}）"

    dims = f"{width}×{height}"
    cap = image_decode_max_pixels()
    try:
        if max(width, height) <= edge and stat.st_size <= need_above_bytes:
            return None, _RESIZE_NO_NEED, dims
        if cap and width * height > cap:
            # 判在 `load()` 之前：不解码就不会有 1GB 级内存尖峰
            return None, _RESIZE_TOO_BIG, dims
        im.load()
        if im.mode not in ("RGB", "L"):
            converted = im.convert("RGB")
            im.close()
            im = converted
        im.thumbnail((edge, edge), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue(), _RESIZE_OK, dims
    except Exception as exc:  # noqa: BLE001 - 缩放失败回落由调用方决定
        log.warning("图片缩放失败 %s（%s）: %s: %s",
                    path.name, dims, type(exc).__name__, exc, exc_info=True)
        return None, _RESIZE_FAILED, dims
    finally:
        try:
            im.close()
        except Exception:  # noqa: BLE001 - 关闭失败无需惊动调用方
            pass


def _data_url_tool_image(path: Path, mime: str) -> tuple[str | None, str]:
    """工具图片 →（`data:` URL, **失败说明**）。说明为空串表示成功。

    与附件版 `_data_url` 的差别：**大图先在内存里缩到 `image_max_edge` 再编码**，
    不落盘（发送边界不许往用户工作空间写派生文件）。缩放的成败与上限统一由
    `_resize_jpeg_bytes` 负责（含 Pillow 解压炸弹阈值的处理）。

    先试缩放、后判上限：一张 20MB 的截图缩完只有几百 KB，必须在缩放**之后**
    用编码结果去比上限，否则会误判为"太大发不出去"。

    **失败时返回的说明必须原样进上下文**：模型看不到图时得知道自己没看全、
    以及下一步怎么办（先 bash 缩放再读），而不是把 3.4 亿像素的原图硬发出去
    换一个与真实原因无关的 provider 400。
    """
    try:
        stat = path.stat()
    except OSError:
        return None, "文件不可读"
    edge = image_max_edge()
    # 缓存同时存成功与失败：失败往往是**解码级**成本（1GB/1 秒），
    # 每轮 LLM 往返重算一遍不可接受。键含 edge 与解码上限（两者都是可调配置，
    # 配置一变结论就可能不同），并用 "tool_image" 标签与 `_data_url` 区分。
    key = ("tool_image", str(path), int(stat.st_mtime_ns), int(stat.st_size),
           mime, int(edge), int(image_decode_max_pixels()))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    if edge > 0 and stat.st_size > _RESIZE_ABOVE_BYTES:
        data, code, detail = _resize_jpeg_bytes(path, edge)
        if data is not None:
            result = ("data:image/jpeg;base64,"
                      + base64.b64encode(data).decode("ascii"), "")
            _cache_put(key, result)
            return result
        if code == _RESIZE_TOO_BIG:
            result = (None, f"图像尺寸 {detail} 超出本机解码上限"
                            f"（{image_decode_max_pixels():,} 像素）")
            _cache_put(key, result)
            return result
        if code == _RESIZE_FAILED:
            result = (None, f"缩放失败（{detail}）")
            _cache_put(key, result)
            return result
        # NO_NEED：尺寸本来就在范围内，原图可直接发。
        # NO_PILLOW：本机没装 Pillow（项目的声明依赖，正常不会发生），无从判断
        # 尺寸 → 保持"发原图"的旧行为，下一条内联上限仍会兜住体积。
    if stat.st_size > inline_max_bytes():
        log.warning("工具图片超过内联上限（%s > %s），本次不发送：%s",
                    human_size(stat.st_size), human_size(inline_max_bytes()), path.name)
        return None, f"超过内联上限 {human_size(inline_max_bytes())}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("读取工具图片失败 %s: %s", path, exc)
        return None, "读取失败"
    url = f"data:{mime or DEFAULT_MIME};base64," + base64.b64encode(raw).decode("ascii")
    _cache_put(key, (url, ""))
    return url, ""


def _expand_document(att: dict, files: dict, session_dir: Path | None,
                     name: str, supports_image: bool | None) -> list[dict]:
    """文档/文本附件 → 头部说明 +（按锚点交错的正文与图片）。
    无页图时收成**单块**，与改造前的形状一致。"""
    body = _attachment_text(att, files)
    if body is None:
        return [_text_block(f"[附件缺失: {name}]（原文件已被移动或删除）")]
    body, total_truncated = _fit_total(att, body)
    assets = _page_assets(session_dir, att, files)
    header = _document_header(att, name, total_truncated, len(assets))
    tail = ""
    original = files.get("original")
    if (att.get("text_truncated") or total_truncated) and original is not None:
        # 2026-09-20：旧文案是「完整文件：{path}，可用工具继续读取」——**假承诺**。
        # 附件目录恒在 `~/.aigent/projects/<id>/.attachments/` 下（工作空间之外），
        # `safe_path()` 的 `is_relative_to(base)` 一律拒绝，`run_read`
        # 必然失败（模型随后就会说"读不到/找不到"）。只有 `bash` 不经过沙箱校验。
        # 所以这里如实说明"在哪、为什么读不了、怎么才能读"。
        tail = (f"\n…（以上为节选，完整原件位于 {original}。"
                f"该路径在本会话工作空间之外，run_read 会被沙箱拒绝；"
                f"需要完整内容时，可先用 bash 把它复制到工作空间内再读）")

    blocks = _document_blocks(body, assets, supports_image)
    # 头部说明并入首块、续读提示并入末块 —— 无页图的文档因此仍是单块，
    # 存量消息的展开形状不变。
    if blocks and blocks[0]["type"] == "text":
        blocks[0] = _text_block(f"{header}\n{blocks[0]['text']}")
    else:
        blocks.insert(0, _text_block(header))
    if tail:
        if blocks[-1]["type"] == "text":
            blocks[-1] = _text_block(f"{blocks[-1]['text']}{tail}")
        else:
            blocks.append(_text_block(tail))
    return blocks


def _document_header(att: dict, name: str, total_truncated: bool,
                     image_count: int) -> str:
    """`[附件: x.pdf]（共 12 页，含 5 张图片，已随附）` —— 让模型知道收到了什么。

    `image_count` 由**磁盘上的资产目录**现数，不读 meta：目录才是真相，meta 丢了
    也能说出正确的数量。表格数刻意**不写进头部** —— `find_tables` 对无框线表格
    命中 0，说一个偏小的数字会让模型以为表格已尽收眼底；表格的视觉真相在页图里。

    **2026-09-20 修复（模型"找不到我上传的文件"）**：旧头部只写 `[附件: 名字]`，
    对模型而言这更像"这里有个叫这个名字的东西"的**路标**，而不是"正文在此"的
    **载体** —— 于是模型拿到 word/excel 后第一反应是去工作空间 `glob`/`find` 核对
    原文件（图片附件走 `image_url` 直给像素，没有这个歧义，所以只有文档类中招）。
    而附件目录恒在 `~/.aigent/...`（工作空间之外），必然全空手而归，
    最后对用户说"文件在工作空间里没有实体文件"。修法：头部**自己说清正文在哪**，
    未截断时明确"正文已完整给出，不必再找磁盘"，截断时才给出可继续读取的路径。
    """
    truncated = bool(att.get("text_truncated") or total_truncated)
    header = f"[附件: {name}]"
    notes = []
    if truncated:
        notes.append("内容已截断")
    if att.get("pages"):
        notes.append(f"共 {att['pages']} 页")
    if image_count:
        notes.append(f"含 {image_count} 张图片，已随附")
    if notes:
        header += "（" + "，".join(notes) + "）"
    if truncated:
        header += "\n（下方仅为节选，完整原件路径见本条附件文本末尾）"
    else:
        header += ("\n（以下就是该文件的完整正文，已随消息一并给出；"
                   "无需再在磁盘或工作空间中查找、读取这个文件）")
    return header


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _image_mime(path: Path, att: dict) -> str:
    """图片实际要发送的那个文件的 MIME。

    **必须以磁盘上的那个文件为准**：登记时若生成了缩放副本
    （`<att_id>.send.jpg`），发送的就是 JPEG，而附件元数据里的 mime 仍是原图的
    （如 image/png）—— 先取原 mime 会把 JPEG 字节标成 PNG 发给 provider。
    扩展名不认识时才回退到元数据里声明的 mime。
    """
    return (_MIME_BY_EXT.get(path.suffix.lower())
            or str(att.get("mime") or "")
            or DEFAULT_MIME)


def _attachment_text(att: dict, files: dict) -> str | None:
    """取文档/文本附件的正文。优先用登记时落盘的 `.txt`，其次现场重提取。"""
    limit = text_max_chars()
    # byte_cap：中文按 3 字节算，乘以 4 留足余量；正文随后还会按**字符数**精确截断
    byte_cap = limit * 4 + 1024
    text_path = files.get("text")
    if text_path is not None:
        body = read_text_file(text_path, byte_cap)
        if len(body) > limit:
            body = body[:limit]
            att["text_truncated"] = True
        return body
    original = files.get("original")
    if original is None:
        return None
    # 会话目录里的 .txt 被删（或 meta 丢失）→ 现场从原件重提取一次
    body, truncated, pages = extract_text(original, original.suffix.lower())
    if truncated:
        att["text_truncated"] = True
    if pages and not att.get("pages"):
        att["pages"] = pages
    return body


def _fit_total(att: dict, body: str) -> tuple[str, bool]:
    """单条消息内附件文本合计超限时按比例收紧（防止一条消息塞进十个各 3 万字的文档）。"""
    limit = text_total_max_chars()
    if len(body) <= limit:
        return body, False
    return body[:limit], True


def _data_url(path: Path, mime: str) -> str | None:
    """本地文件 → `data:` URL（带 mtime/size 缓存）。超内联上限返回 None。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size > inline_max_bytes():
        log.warning("图片超过内联上限（%s > %s），本次不发送：%s",
                    human_size(stat.st_size), human_size(inline_max_bytes()), path.name)
        return None
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size), mime)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("读取附件失败 %s: %s", path, exc)
        return None
    url = f"data:{mime or DEFAULT_MIME};base64," + base64.b64encode(raw).decode("ascii")
    _cache_put(key, url)
    return url


# ══════════════════════════════════════════════════════════════════
#  清理
# ══════════════════════════════════════════════════════════════════

def remove_session_attachments(ws: WorkspacePaths, session_id: str) -> None:
    """删除某会话的附件目录。

    只由「清空会话」「永久删除会话」调用 —— **归档/还原不动附件**（软删除语义，
    还原后消息里的图片必须还在）。
    """
    session_dir = _session_dir(ws, session_id)
    if session_dir is None or not session_dir.is_dir():
        return
    shutil.rmtree(session_dir, ignore_errors=True)
    # 展开缓存按 (路径, mtime, size) 键控，文件已删 → 缓存项永不再命中；
    # att_id 全局唯一无法按会话精确回收，整体清空（只有几十项，重建成本可忽略）。
    clear_expand_cache()
    log.info("附件目录已删除: session=%s", session_id)


def gc_drafts(ws: WorkspacePaths) -> int:
    """清理超期未发送的草稿附件，返回删除数量。"""
    draft_root = _draft_root(ws)
    if not draft_root.is_dir():
        return 0
    cutoff = time.time() - draft_ttl_seconds()
    removed = 0
    for child in draft_root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    if removed:
        log.info("草稿附件 GC: 清理 %d 个（project=%s，TTL=%ds）",
                 removed, ws.id, draft_ttl_seconds())
    return removed


def gc_orphan_session_dirs(ws: WorkspacePaths) -> int:
    """清理「会话已不存在」的附件目录，返回删除数量。

    判定必须同时满足：会话 jsonl **与** meta 都不存在（归档只改 meta 状态、
    jsonl 原样保留 → 天然不会被这里误删），且目录 mtime 超过门限（避免误杀
    刚创建、jsonl 还在写过程中的会话）。
    """
    root = ws.attachments_dir
    if not root.is_dir():
        return 0
    cutoff = time.time() - orphan_min_age_seconds()
    removed = 0
    for child in root.iterdir():
        if not child.is_dir() or child.name == DRAFT_ATTACHMENTS_DIRNAME:
            continue
        sid = child.name
        if not _SID_RE.match(sid):
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        try:
            alive = (any(ws.chat_history_dir.glob(f"*{sid}.jsonl"))
                     or any(ws.chat_history_dir.glob(f"*{sid}.meta.json")))
        except OSError:
            continue
        if alive:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    if removed:
        log.info("孤儿附件目录 GC: 清理 %d 个（project=%s）", removed, ws.id)
    return removed
