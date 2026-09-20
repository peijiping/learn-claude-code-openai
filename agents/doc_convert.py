#!/usr/bin/env python3
"""
doc_convert.py - 统一转换层（文档 → Markdown + 页面图片资产）

════════════════════════════════════════════════════════════════════════
为什么需要这一层
════════════════════════════════════════════════════════════════════════

为每种格式各写一套"正则式"文本抽取，注定追不上版式复杂度：表格被打散成单词、
图表整块丢失、扫描件抽到空。**根因不是某个 bug，是架构选型。**

正解与 Claude / OpenAI 官方对 PDF 的既有做法一致 —— 每页渲染成位图，与文本层
**一起**交给模型。表格、图表、多栏排版这些本地抽取器永远追不上的东西，恰恰是
视觉模型最强的地方；而触发本次改造的"纯图片 PDF"根本不需要 OCR，渲染成页图
模型就能读。

成本口径（DeepSeek 实测口径，2026-09）：服务端会把每张图自动缩放到约
1300×1300 像素量级，**单图 token 封顶 1024**。所以图像 token 不是风险，
真正的约束是请求体大小（内联图 48 MiB）。本模块的缩放不省 token，
意义是省请求体与上传耗时。

════════════════════════════════════════════════════════════════════════
模块边界
════════════════════════════════════════════════════════════════════════

**叶子模块**：只依赖标准库 + 懒加载 `pymupdf`。**不 import `attachments` /
引擎模块** —— 降级链由 `attachments._convert_document` 编排（本模块抛异常或
不可用时，回落今天的纯文本抽取，最差情况等于现状）。

契约（`convert_pdf`）：

    {
      "markdown": str,   # 每页 `--- 第 N 页 ---` + 文本层 + `<!--img:pN-->` 锚点
      "pages": int,      # 原文档总页数
      "images": [{"id","page","path","width","height"}],
      "tables": int,     # 识别到的表格数（find_tables 命中数，无框线表格会漏）
      "converter": "pymupdf",
      "warnings": [str], # "诚实失败"通道：解不出来/没渲染的东西显式记下来
    }

图片资产落在 `out_dir/<att_id>.pages/pN.jpg`。锚点 id（`pN`）**就是文件名去掉
后缀**，展开侧据此把 Markdown 切成交错的文本块与图片块；不依赖 meta.json，
meta 丢了也能恢复。
"""
from __future__ import annotations

import re
from pathlib import Path

from logger import get_logger

log = get_logger("doc_convert")

# 图片资产目录后缀：`<att_id>.pages/`
ASSETS_DIR_SUFFIX = ".pages"

# 渲染页图时 zoom 的上限。有些 PDF 画布极小（如 200×100pt），按长边算出的
# zoom 会大到几百倍，渲染出天文数字像素 —— 必须夹住。
RENDER_ZOOM_CAP = 6.0
RENDER_MIN_ZOOM = 0.1

# 页图 JPEG 质量。比 prepare_image 的 q82 高一些：页面上的小字对 JPEG 伪影
# 远比照片敏感，而服务端反正会把图缩到 ~1300px，体积和清晰度都还有余量。
PAGE_JPEG_QUALITY = 88

# Markdown 里的图片锚点。`pN` = 页图，`fN` = 图内嵌要素（后续批次）。
IMAGE_ANCHOR_RE = re.compile(r"<!--\s*img:([A-Za-z][0-9]{1,4})\s*-->")


def asset_dir(out_dir: Path, att_id: str) -> Path:
    """该附件的图片资产目录。"""
    return Path(out_dir) / f"{att_id}{ASSETS_DIR_SUFFIX}"


def _page_has_picture(page) -> bool:
    """页面是否含内嵌位图（图表、照片、扫描页）。"""
    try:
        return bool(page.get_images(full=True))
    except Exception as exc:  # noqa: BLE001 - 探测失败不能拦住建页图
        log.debug("get_images 失败（按无图处理）: %s", exc)
        return False


def _page_has_drawings(page) -> bool:
    """页面是否有矢量绘制（线条/方框/曲线）。

    只用来区分"真空白页"与"纯矢量图形页"：没有文本、没有位图、没有表格，
    但画了一堆线条的页（示意图、流程图）**必须**渲染，否则内容就丢了。
    它**不参与**"文本够密就不渲染"的判断 —— 正文页常有页眉横线/表格框线，
    让矢量参与那个判断会导致几乎所有页都渲染。
    """
    try:
        return bool(page.get_drawings())
    except Exception as exc:  # noqa: BLE001
        log.debug("get_drawings 失败（按无矢量处理）: %s", exc)
        return False


def _count_tables(page) -> int:
    """`page.find_tables()` 命中数。**无框线、靠位置对齐的表格会漏**（实测命中 0），
    所以它只用来"多触发一次页图渲染"，不能当成表格数目的真相。"""
    try:
        finder = page.find_tables()
    except Exception as exc:  # noqa: BLE001 - 版本/API 差异不该毁掉整篇转换
        log.debug("find_tables 不可用（按 0 处理）: %s", exc)
        return 0
    return len(getattr(finder, "tables", []) or [])


def _needs_page_image(text: str, has_picture: bool, tables: int, min_text: int,
                      has_drawings: bool = False) -> bool:
    """这一页的**文本层是否足以代表页面内容**。

    文本层够密、又没有图/表 → 页图提供不了额外信息，渲染它纯属浪费请求体
    （请求体才是真正的成本约束，DeepSeek 内联上限 48 MiB）。反过来，出现下列
    任一信号就渲染：

    - 页面**完全没有文本**：只要还有位图/表格/矢量图形，就必须渲染；
      三者都没有才是真的空白页，跳过（不浪费一张图的额度）；
    - 文本层很短（低于 `min_text`）→ 大概率是扫描件或图为主的一页（事故形态）；
    - 有内嵌位图 → 有图表/照片；
    - find_tables 命中 → 有可识别的表格（行列关系正是文本层会丢掉的）。
    """
    if not text.strip():
        return bool(has_picture or tables or has_drawings)
    if len(text) < max(0, int(min_text)):
        return True
    return bool(has_picture or tables)


def _render_page(fitz, page, dest_dir: Path, page_no: int,
                 max_edge: int) -> dict | None:
    """整页渲染成 JPEG。返回资产描述，失败返回 None（调用方记 warning）。"""
    try:
        rect = page.rect
        long_edge = max(float(rect.width), float(rect.height)) or 1.0
        zoom = 1.0
        if max_edge and max_edge > 0:
            zoom = float(max_edge) / long_edge
        zoom = max(RENDER_MIN_ZOOM, min(zoom, RENDER_ZOOM_CAP))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        name = f"p{page_no}.jpg"
        path = dest_dir / name
        # Pillow 不参与：PyMuPDF 自己就能落 JPEG，少一次解码/编码
        pix.save(str(path), jpg_quality=PAGE_JPEG_QUALITY)
        return {
            "id": f"p{page_no}",
            "page": page_no,
            "path": str(path),
            "width": int(pix.width),
            "height": int(pix.height),
        }
    except Exception as exc:  # noqa: BLE001 - 单页渲染失败不该毁掉整篇转换
        log.warning("第 %d 页渲染失败: %s: %s", page_no, type(exc).__name__, exc)
        return None


def convert_pdf(src: Path, out_dir: Path, att_id: str, *,
                max_edge: int = 1568, max_pages: int = 20, max_images: int = 20,
                min_text: int = 200) -> dict:
    """PDF → Markdown（含图片锚点）+ 页面图片资产。

    解析库缺失 / 文件损坏一律抛异常，由调用方（`attachments._convert_document`）
    回落纯文本抽取 —— 本模块不做静默降级，"抽不到内容"必须让用户看见。
    """
    try:
        import fitz  # pymupdf
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RuntimeError("未安装 pymupdf，无法转换 PDF") from exc
    # 官方静音开关：首次调用 find_tables() 会往 stdout 打一条
    # "Consider using the pymupdf_layout package…" 的推荐语，会污染应用日志。
    no_recommend = getattr(fitz, "no_recommend_layout", None)
    if callable(no_recommend):
        no_recommend()

    doc = fitz.open(str(src))
    try:
        total = len(doc)
        dest_dir = asset_dir(out_dir, att_id)
        parts: list[str] = []
        images: list[dict] = []
        warnings: list[str] = []
        tables_total = 0
        scan_pages: list[int] = []
        skipped_pages: list[int] = []
        # max_pages 只约束**页图数量**，文本层永远是全量的 —— 文本是检索与
        # 无视觉模型兜底的主通道，不该被图像预算砍掉。
        render_budget = total if max_pages <= 0 else min(total, max_pages)
        image_budget = max_images if max_images > 0 else total

        any_text = False
        for i in range(total):
            page = doc[i]
            page_no = i + 1
            text = (page.get_text() or "").strip()
            has_picture = _page_has_picture(page)
            tables = _count_tables(page)
            tables_total += tables

            parts.append(f"--- 第 {page_no} 页 ---")
            if text:
                parts.append(text)
                any_text = True

            if not _needs_page_image(text, has_picture, tables, min_text,
                                     _page_has_drawings(page)):
                continue
            if page_no > render_budget or len(images) >= image_budget:
                skipped_pages.append(page_no)
                continue
            if not dest_dir.is_dir():
                dest_dir.mkdir(parents=True, exist_ok=True)
            asset = _render_page(fitz, page, dest_dir, page_no, max_edge)
            if asset is None:
                warnings.append(f"第 {page_no} 页图像渲染失败")
                continue
            images.append(asset)
            parts.append(f"<!--img:{asset['id']}-->")
            if not text:
                # 只有**真的把这一页当图发出去**了才这么说。被跳过的空白页不该
                # 产生"已按图像发送"的告警 —— 那是假话。
                scan_pages.append(page_no)

        if scan_pages:
            preview = "、".join(str(p) for p in scan_pages[:5])
            more = f" 等 {len(scan_pages)} 页" if len(scan_pages) > 5 else ""
            warnings.append(f"第 {preview} 页无文本层{more}，已按图像发送")
        if skipped_pages:
            warnings.append(
                f"仅渲染前 {render_budget} 页图像（共 {total} 页），文本层保留全量")
        # 表格数不写进 warnings：`tables` 已单独成字段，写进 warnings 会让每个
        # 带表格的 PDF 都被 UI 标成"已降级"，而它其实完全正常。
        # 没有任何页图时清掉空目录，免得会话目录里留一堆空壳
        if dest_dir.is_dir() and not any(dest_dir.iterdir()):
            dest_dir.rmdir()

        markdown = "\n".join(parts)
        # 既没有文本、也没有页图 → 真的什么都没读到（如全空白 PDF）。交回调用方
        # 判定"未提取到文本"，而不是让一堆页码标记冒充内容。
        if not images and not any_text:
            markdown = ""

        return {
            "markdown": markdown,
            "pages": total,
            "images": images,
            "tables": tables_total,
            "converter": "pymupdf",
            "warnings": warnings,
        }
    finally:
        doc.close()


def find_anchors(markdown: str) -> list[str]:
    """Markdown 里出现过的锚点 id，按出现顺序（展开侧切块用）。"""
    return IMAGE_ANCHOR_RE.findall(markdown or "")
