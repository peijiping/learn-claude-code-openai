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
from doc_convert import ASSETS_DIR_SUFFIX, IMAGE_ANCHOR_RE

log = get_logger("attachments")

# ── 附件种类 ──────────────────────────────────────────────────────
KIND_IMAGE = "image"
KIND_DOCUMENT = "document"   # pdf / docx / xlsx / pptx（统一抽成文本供模型阅读）
KIND_TEXT = "text"           # 纯文本 / 代码：直接按文本读，不额外落 .txt

# jsonl content 里的块类型。**刻意用中性词**（不是 "image_url"）：存储形态与
# 厂商线格式解耦，`_model_messages` 负责展开。
ATTACHMENT_BLOCK_TYPE = "attachment"

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
#  可调参数（环境变量 / ~/.aigent/config.json，读在调用点而非导入点）
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
    """单个文档抽取文本的字符上限（默认 30000，与 run_read_pdf 同口径）。"""
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
    """
    limit = text_max_chars()
    if ext == ".pdf":
        return _extract_pdf(path, limit)
    if ext == ".docx":
        return _clip(_extract_docx(path), limit)
    if ext == ".xlsx":
        return _clip(_extract_xlsx(path), limit)
    if ext == ".pptx":
        return _clip(_extract_pptx(path), limit)
    return _clip(read_text_file(path, limit * 4 + 1024), limit)


def _clip(text: str, limit: int) -> tuple[str, bool, int | None]:
    text = text or ""
    if len(text) > limit:
        return text[:limit], True, None
    return text, False, None


# ── 「未提取到文本」占位 ────────────────────────────────────────────
# 抽空时正文只可能是这样一句占位（各格式措辞不同，前缀统一）。它同时是**哨兵**：
#   ① 给模型 —— 明确知道"这里本该有内容但没读到"，不会基于残缺上下文硬答；
#   ② 给 stage/UI —— 据此记 warning、显示"已降级"，而不是把空内容当"解析成功"。
# 在此之前只有 PDF 有占位，docx/xlsx/pptx 抽空会返回空串，前端照样显示
# "已提取 0 字"，用户与模型都不知道内容丢了。
EMPTY_NOTE_PREFIX = "（未提取到文本"


def _empty_note(ext: str) -> str:
    if ext == ".pdf":
        return "（未提取到文本，可能是扫描件或纯图片 PDF）"
    if ext == ".docx":
        return "（未提取到文本：文档可能只含图片、图表或文本框）"
    if ext == ".xlsx":
        return "（未提取到文本：工作簿可能只含图片或图表）"
    if ext == ".pptx":
        return "（未提取到文本：幻灯片可能只含图片）"
    return "（未提取到文本）"


def text_is_empty_note(text: str) -> bool:
    """正文是否只是「未提取到文本」占位（stage / UI 据此判定"已降级"）。"""
    return str(text or "").lstrip().startswith(EMPTY_NOTE_PREFIX)


def _convert_document(src: Path, ext: str, dest: Path, att_id: str) -> dict:
    """文档 → 统一中间表示（Markdown + 页图资产）。

    **优先走 `doc_convert` 的统一转换层**（PDF 页图 + 文本层双路），不可用或失败时
    回落今天的纯文本抽取 —— 降级链保证"最差情况等于现状"，不引入回归。

    返回 `{"body","text_truncated","pages","warnings","images","tables","converter"}`。
    `images` 是**页图资产个数**（int）；资产明细靠目录约定 `<att_id>.pages/pN.jpg`
    恢复，不进 meta —— 省 jsonl 体积，也不怕 meta 丢失。
    """
    limit = text_max_chars()
    try:
        import doc_convert
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
            # 与 tools.run_read_pdf 同一口径的提示，避免用户以为"文件坏了"
            body = _empty_note(".pdf")
        return body[:limit], truncated, total
    finally:
        doc.close()


def _extract_docx(path: Path) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 python-docx，无法解析 .docx") from exc
    document = docx.Document(str(path))
    parts = [p.text.strip() for p in document.paragraphs if p.text and p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    body = "\n".join(parts)
    # 抽空必须留痕：否则一行表格都没有的 docx 会静默变成空正文
    return body if body.strip() else _empty_note(".docx")


def _extract_xlsx(path: Path) -> str:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 openpyxl，无法解析 .xlsx") from exc
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        parts: list[str] = []
        data_rows = 0
        for sheet in wb.worksheets:
            rows: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells))
            # 工作表标题**不算内容** —— 否则一个只有空表的工作簿也会抽出
            # "--- 工作表: Sheet1 ---"，看起来"解析成功"实则什么都没读到
            parts.append(f"--- 工作表: {sheet.title} ---")
            parts.extend(rows)
            data_rows += len(rows)
        body = "\n".join(parts)
        return body if data_rows else _empty_note(".xlsx")
    finally:
        wb.close()


def _extract_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 python-pptx，无法解析 .pptx") from exc
    prs = Presentation(str(path))
    parts: list[str] = []
    text_shapes = 0
    for idx, slide in enumerate(prs.slides, start=1):
        bodies: list[str] = []
        for shape in slide.shapes:
            frame = getattr(shape, "text_frame", None)
            if frame is not None and (frame.text or "").strip():
                bodies.append(frame.text.strip())
        # 同 xlsx：「--- 第 N 页 ---」标题不算内容，否则纯图片幻灯片会被当成有正文
        parts.append(f"--- 第 {idx} 页 ---")
        parts.extend(bodies)
        text_shapes += len(bodies)
    body = "\n".join(parts)
    return body if text_shapes else _empty_note(".pptx")


# ══════════════════════════════════════════════════════════════════
#  图片预处理（Pillow 可选）
# ══════════════════════════════════════════════════════════════════

def prepare_image(src: Path, dst: Path) -> bool:
    """生成"发送用"缩略图。返回是否真的生成了。

    规则：长边 > image_max_edge，或原图 > 1.5MB 时才缩放（其余情况发原图，
    避免为了省几十 KB 反而引入一次 JPEG 有损重编码）。Pillow 缺失或失败一律
    返回 False → 发送原图（功能不降级，只是多花点 token）。
    """
    if dst.exists():
        return True
    edge = image_max_edge()
    if edge <= 0:
        return False
    try:
        from PIL import Image
    except ImportError:
        log.info("未安装 Pillow，图片不做缩放（将发送原图）")
        return False
    try:
        with Image.open(src) as im:
            im.load()
            width, height = im.size
            if max(width, height) <= edge and src.stat().st_size <= 1_500_000:
                return False
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((edge, edge), Image.LANCZOS)
            im.save(dst, "JPEG", quality=82, optimize=True)
        return True
    except Exception as exc:  # noqa: BLE001 - 预处理失败不能拦住附件可用
        log.warning("图片预处理失败（将发送原图）: %s: %s", type(exc).__name__, exc)
        return False


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
        tail = f"\n…（上方为节选，完整文件：{original}，可用工具继续读取）"

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
    """
    header = f"[附件: {name}]"
    notes = []
    if att.get("text_truncated") or total_truncated:
        notes.append("内容已截断")
    if att.get("pages"):
        notes.append(f"共 {att['pages']} 页")
    if image_count:
        notes.append(f"含 {image_count} 张图片，已随附")
    if notes:
        header += "（" + "，".join(notes) + "）"
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
