#!/usr/bin/env python3
"""附件存储 / 解析 / 展开的离线回归测试 —— 2026-09-20。

覆盖 `agents/attachments.py` 的全部对外契约（设计见
`docs/frontend/12-附件与文件输入.md`）：

  1. 登记        —— 复制而非移动（原文件必须在）、meta 落盘、拒绝非法输入
  2. 解析        —— pdf / docx / xlsx / pptx / 纯文本 / 图片 各自可抽到内容
  3. 归位        —— 草稿区 → 会话目录；幂等；att_id 找不到时标记 missing 而不报错
  4. 展开        —— 发送边界：图片 → image_url(data URL)、文档 → 文本块；
                    文件缺失 / 体积超限 / 块畸形 **一律降级不抛**
  5. 账本/回放   —— build_user_content / text_view / harvest_attachments
  6. 清理        —— 草稿 TTL、孤儿会话目录门限、只删目标会话、归档/还原不动

全部用例用临时目录承载（`WorkspacePaths` 注入），**不触碰真实 `~/.aigent`**。
运行：`.venv/bin/python -m unittest discover -s tests -v`（pytest 未安装，用内置 unittest）
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import attachments as A  # noqa: E402
from paths import WorkspacePaths  # noqa: E402


def _workspace(root: Path) -> WorkspacePaths:
    """临时工作空间的路径束（data_root 与 workdir 都指向测试临时目录）。"""
    return WorkspacePaths("default", root, root, bash_cwd=root)


class _FixtureMixin:
    """生成真实可解析的样例文件（不 mock 解析库 —— 要测的就是它们真能read）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = _workspace(self.root)
        self.src = self.root / "src"
        self.src.mkdir(parents=True, exist_ok=True)
        A.clear_expand_cache()
        self.addCleanup(A.clear_expand_cache)

    def _file(self, name: str, text: str) -> Path:
        p = self.src / name
        p.write_text(text, encoding="utf-8")
        return p

    def _png(self, name: str = "shot.png", size=(12, 12)) -> Path:
        from PIL import Image
        p = self.src / name
        Image.new("RGB", size, (200, 30, 30)).save(p)
        return p

    def _pdf(self, name: str = "doc.pdf") -> Path:
        import fitz
        p = self.src / name
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Hello Attachment PDF")
        doc.save(str(p))
        doc.close()
        return p

    def _docx(self, name: str = "doc.docx") -> Path:
        import docx
        p = self.src / name
        d = docx.Document()
        d.add_paragraph("Word 正文第一段")
        d.add_paragraph("Word 正文第二段")
        d.save(str(p))
        return p

    def _xlsx(self, name: str = "book.xlsx") -> Path:
        import openpyxl
        p = self.src / name
        wb = openpyxl.Workbook()
        wb.active["A1"] = "列一"
        wb.active["B1"] = 42
        wb.save(str(p))
        return p

    def _pptx(self, name: str = "deck.pptx") -> Path:
        from pptx import Presentation
        p = self.src / name
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(0, 0, 400, 100)
        box.text_frame.text = "幻灯片正文内容"
        prs.save(str(p))
        return p

    def _png_src(self, name: str = "inner.png", size=(60, 30)) -> Path:
        """一张带可见内容的图，用来构造"只有图片"的文档。"""
        from PIL import Image
        p = self.src / name
        im = Image.new("RGB", size, (255, 255, 255))
        for x in range(0, size[0], 4):
            for y in range(0, size[1], 4):
                im.putpixel((x, y), (0, 0, 0))
        im.save(p)
        return p

    def _docx_image_only(self, name: str = "onlyimg.docx") -> Path:
        """只含一张图、没有任何段落文字的 docx。"""
        import docx
        p = self.src / name
        d = docx.Document()
        d.add_picture(str(self._png_src()))
        d.save(str(p))
        return p

    def _pptx_image_only(self, name: str = "onlyimg.pptx") -> Path:
        """只含一张图、没有任何文本框的 pptx。"""
        from pptx import Presentation
        from pptx.util import Inches
        p = self.src / name
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_picture(str(self._png_src()), Inches(1), Inches(1))
        prs.save(str(p))
        return p

    def _xlsx_sheets_only(self, name: str = "blank.xlsx") -> Path:
        """只有工作表骨架、没有任何单元格内容的工作簿。"""
        import openpyxl
        p = self.src / name
        openpyxl.Workbook().save(str(p))
        return p

    def _pdf_image_only(self, name: str = "scan.pdf") -> Path:
        """只有图片、**没有文本层**的 PDF —— 本次事故的形态。"""
        import fitz
        p = self.src / name
        doc = fitz.open()
        page = doc.new_page()
        page.insert_image(fitz.Rect(50, 50, 250, 250),
                          filename=str(self._png_src()))
        doc.save(str(p))
        doc.close()
        return p

    def _pdf_mixed(self, name: str = "mixed.pdf", pages: int = 3) -> Path:
        """交替的「密集文本页 / 纯图片页」。文本页不该渲染页图，图片页必须渲染。"""
        import fitz
        p = self.src / name
        doc = fitz.open()
        for i in range(pages):
            page = doc.new_page()
            if i % 2 == 0:
                page.insert_textbox(fitz.Rect(40, 40, 560, 700),
                                    "Body text paragraph. " * 20, fontsize=10)
            else:
                page.insert_image(fitz.Rect(40, 40, 400, 300),
                                  filename=str(self._png_src()))
        doc.save(str(p))
        doc.close()
        return p

    def _pages_dir(self, item: dict, sid: str | None = None) -> Path:
        base = (self.ws.attachments_dir / sid) if sid else \
            (self.ws.attachments_dir / "_draft" / item["att_id"])
        return base / f"{item['att_id']}.pages"

    def _stage_ok(self, path: Path) -> dict:
        result = A.stage(self.ws, [str(path)])
        self.assertEqual(result["failed"], [], f"登记应当成功：{result['failed']}")
        self.assertEqual(len(result["items"]), 1)
        return result["items"][0]


# ══════════════════════════════════════════════════════════════════
class StageTests(_FixtureMixin, unittest.TestCase):
    """登记：复制而非移动 + 元数据 + 非法输入的可读拒绝。"""

    def test_stage_copies_file_and_leaves_original(self):
        src = self._file("note.txt", "正文内容")
        item = self._stage_ok(src)
        self.assertEqual(item["kind"], "text")
        self.assertEqual(item["name"], "note.txt")
        self.assertTrue(A._ATT_ID_RE.match(item["att_id"]))
        self.assertTrue(src.is_file(), "原文件必须原地留下（只复制，绝不 move）")
        draft = self.ws.attachments_dir / "_draft" / item["att_id"]
        self.assertTrue((draft / "meta.json").is_file())
        self.assertTrue((draft / f"{item['att_id']}.txt").is_file())

    def test_stage_text_file_does_not_duplicate_txt(self):
        """纯文本类**不**额外落 .txt（原件本身就是文本，展开时直接读它）。"""
        item = self._stage_ok(self._file("a.md", "# 标题"))
        draft = self.ws.attachments_dir / "_draft" / item["att_id"]
        self.assertEqual(item["kind"], "text")
        self.assertEqual(sorted(p.name for p in draft.iterdir()),
                         sorted(["meta.json", f"{item['att_id']}.md"]))

    def test_stage_pdf_extracts_pages(self):
        item = self._stage_ok(self._pdf())
        self.assertEqual(item["kind"], "document")
        self.assertEqual(item["pages"], 1)
        self.assertGreater(item["text_chars"], 0)

    def test_stage_office_formats_extract_text(self):
        for maker, expect in ((self._docx, "Word 正文第一段"),
                              (self._xlsx, "列一"),
                              (self._pptx, "幻灯片正文内容")):
            with self.subTest(maker=maker.__name__):
                item = self._stage_ok(maker())
                self.assertEqual(item["kind"], "document")
                draft = self.ws.attachments_dir / "_draft" / item["att_id"]
                body = (draft / f"{item['att_id']}.txt").read_text(encoding="utf-8")
                self.assertIn(expect, body)

    def test_stage_image_kind_and_no_text(self):
        item = self._stage_ok(self._png())
        self.assertEqual(item["kind"], "image")
        self.assertEqual(item["text_chars"], 0)

    def test_stage_image_resizes_only_oversized(self):
        small = self._stage_ok(self._png("small.png", (40, 40)))
        big = self._stage_ok(self._png("big.png", (2400, 300)))
        small_dir = self.ws.attachments_dir / "_draft" / small["att_id"]
        big_dir = self.ws.attachments_dir / "_draft" / big["att_id"]
        self.assertFalse((small_dir / f"{small['att_id']}.send.jpg").exists())
        self.assertTrue((big_dir / f"{big['att_id']}.send.jpg").exists())

    def test_stage_rejects_with_readable_reasons(self):
        cases = [
            (self.src / "nope.txt", "不存在"),
            (self.src, "文件夹"),
            (self._devnull(), "空文件"),
        ]
        for path, needle in cases:
            with self.subTest(path=str(path)):
                item, reason = A._stage_one(self.ws, self.ws.attachments_dir
                                            / "_draft", str(path))
                self.assertIsNone(item)
                self.assertIn(needle, reason)

    def _devnull(self) -> Path:
        p = self.src / "empty.txt"
        p.write_text("", encoding="utf-8")
        return p

    def test_stage_rejects_legacy_office_with_hint(self):
        p = self._file("old.doc", "x")
        result = A.stage(self.ws, [str(p)])
        self.assertEqual(result["items"], [])
        self.assertIn(".docx", result["failed"][0]["reason"])

    def test_stage_rejects_unknown_extension(self):
        result = A.stage(self.ws, [str(self._file("weird.zzz", "x"))])
        self.assertIn("不支持的文件类型", result["failed"][0]["reason"])

    def test_stage_rejects_oversized_with_limit_in_message(self):
        # 上限下限是 1KB（_int_env 的 minimum），故用 1024 作为收紧后的上限
        p = self.src / "big.txt"
        p.write_text("x" * 4000, encoding="utf-8")
        with mock.patch.dict(os.environ, {"ATTACHMENT_DOC_MAX_BYTES": "1024"}):
            result = A.stage(self.ws, [str(p)])
        self.assertEqual(result["items"], [])
        self.assertIn("过大", result["failed"][0]["reason"])

    def test_partial_failure_does_not_block_batch(self):
        """用户选 5 个文件，不能因为其中 1 个不支持就全失败。"""
        good = self._file("ok.txt", "fine")
        bad = self._file("bad.doc", "x")
        result = A.stage(self.ws, [str(bad), str(good)])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(result["failed"]), 1)

    def test_duplicate_path_in_one_batch_staged_once(self):
        p = self._file("dup.txt", "x")
        result = A.stage(self.ws, [str(p), str(p)])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["failed"], [])

    def test_unsupported_kind_never_raises(self):
        """桥层契约：stage 对任何输入都必须返回结构，不抛异常。

        空白路径直接跳过（不算失败），不存在的路径进 failed。
        """
        result = A.stage(self.ws, ["", "   ", "/no/such/path"])
        self.assertEqual(result["items"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["path"], "/no/such/path")


# ══════════════════════════════════════════════════════════════════
class EmptyContentHonestyTests(_FixtureMixin, unittest.TestCase):
    """抽不到内容时必须**显式留痕**，而不是静默变成空正文。

    本次事故的原始现象是"前端显示解析成功（text_chars=23）"—— 占位串本身的字符数。
    所以 `text_chars > 0` 从来不是"解析成功"的证据，必须另有 warnings 与占位串
    本身作为判据。

    批次 2 之后要分清**两种"抽不到"**，它们不是一回事：

    - 扫描件 / 纯图片 PDF：内容**已经**通过页图交付给模型了 → 不是失败；
    - docx / xlsx / pptx 抽空：**什么都没交付** → 才是失败，必须给占位。
    """

    def _draft_body(self, item: dict) -> str:
        return (self.ws.attachments_dir / "_draft" / item["att_id"]
                / f"{item['att_id']}.txt").read_text(encoding="utf-8")

    def _assert_nothing_delivered(self, item: dict, needle: str) -> None:
        """真正的失败：正文只有占位串，且没有任何图片随附。"""
        self.assertEqual(item["warnings"], ["未提取到文本"])
        self.assertEqual(item["converter"], "fallback_text")
        self.assertEqual(item["images"], 0)
        body = self._draft_body(item)
        self.assertIn("未提取到文本", body)
        self.assertIn(needle, body)
        self.assertTrue(A.text_is_empty_note(body))
        # 占位串本身贡献了 text_chars —— 单看这个数会被误导
        self.assertEqual(item["text_chars"], len(body))

    def test_docx_with_only_an_image_is_marked(self):
        self._assert_nothing_delivered(self._stage_ok(self._docx_image_only()), "文档")

    def test_pptx_with_only_an_image_is_marked(self):
        self._assert_nothing_delivered(self._stage_ok(self._pptx_image_only()), "幻灯片")

    def test_xlsx_with_no_cell_content_is_marked(self):
        """工作表标题不算内容：否则会抽到 `--- 工作表: Sheet ---` 被判成有内容。"""
        self._assert_nothing_delivered(self._stage_ok(self._xlsx_sheets_only()), "工作簿")

    def test_scanned_pdf_is_delivered_as_image_not_marked_empty(self):
        """扫描件**不再是**"未提取到文本"：无文本层 → 渲染整页图随附，模型看得到。

        这条正是本次改造的靶心 —— 改前它只抽到一句占位串，模型什么都拿不到。
        """
        item = self._stage_ok(self._pdf_image_only())
        self.assertEqual(item["converter"], "pymupdf")
        self.assertEqual(item["images"], 1)
        self.assertNotIn("未提取到文本", self._draft_body(item))
        self.assertTrue(any("无文本层" in w for w in item["warnings"]),
                        item["warnings"])
        assets = (self.ws.attachments_dir / "_draft" / item["att_id"]
                  / f"{item['att_id']}.pages")
        self.assertEqual(sorted(p.name for p in assets.iterdir()), ["p1.jpg"])

    def test_documents_with_content_have_no_warning(self):
        for maker, converter in ((self._pdf, "pymupdf"),
                                 (self._docx, "fallback_text"),
                                 (self._xlsx, "fallback_text"),
                                 (self._pptx, "fallback_text")):
            with self.subTest(maker=maker.__name__):
                item = self._stage_ok(maker())
                self.assertEqual(item["warnings"], [])
                self.assertEqual(item["converter"], converter)
                self.assertFalse(A.text_is_empty_note(self._draft_body(item)))

    def test_text_is_empty_note_only_matches_placeholders(self):
        self.assertTrue(A.text_is_empty_note("（未提取到文本）"))
        self.assertTrue(A.text_is_empty_note("  （未提取到文本：幻灯片可能只含图片）"))
        self.assertFalse(A.text_is_empty_note("正常正文里提到未提取到文本这个词"))
        self.assertFalse(A.text_is_empty_note(""))
        self.assertFalse(A.text_is_empty_note(None))

    def test_new_meta_fields_default_on_old_records(self):
        """旧 meta.json 没有新增字段 → public_item 取默认值，存量零迁移。"""
        out = A.public_item({"att_id": "att_old0000001", "kind": "document",
                             "name": "a.pdf"})
        self.assertEqual(out["images"], 0)
        self.assertEqual(out["tables"], 0)
        self.assertEqual(out["converter"], "")
        self.assertEqual(out["warnings"], [])


# ══════════════════════════════════════════════════════════════════
class DocumentPageImageTests(_FixtureMixin, unittest.TestCase):
    """文档随附页图：登记 → 迁移 → 展开的全链路契约。

    这是改造的产出面：模型不再只拿到文本层，而是"文本 + 该页的图"。三件事必须
    成立：① 页图资产跟着附件迁移；② 展开时按锚点位置**交错**排块；③ 模型无视觉
    能力时页图位置降级为占位，既不静默消失、也不让 provider 报错。
    """

    def _prepare(self, item: dict, sid: str) -> tuple[dict, Path]:
        records = A.migrate_to_session(
            self.ws, [{"att_id": item["att_id"], "kind": item["kind"],
                       "name": item["name"], "ext": item["ext"]}], sid)
        return ({"role": "user", "content": A.build_user_content("看", records)},
                self.ws.attachments_dir / sid)

    def test_page_assets_survive_draft_to_session_migration(self):
        """页图资产目录必须跟着附件搬家 —— 漏了它展开发不出图（踩过的坑）。"""
        item = self._stage_ok(self._pdf_image_only())
        self.assertEqual(sorted(p.name for p in self._pages_dir(item).iterdir()),
                         ["p1.jpg"])
        self._prepare(item, "SIDP00001")
        moved = self._pages_dir(item, "SIDP00001")
        self.assertEqual(sorted(p.name for p in moved.iterdir()), ["p1.jpg"])
        self.assertFalse(self._pages_dir(item).exists(), "草稿区的资产应已搬走")

    def test_scan_pdf_expands_to_text_then_image(self):
        item = self._stage_ok(self._pdf_image_only())
        msg, session_dir = self._prepare(item, "SIDP00002")
        out = A.expand_content_for_model(msg, session_dir)
        self.assertEqual([b["type"] for b in out["content"]],
                         ["text", "text", "image_url"])
        header = out["content"][1]["text"]
        self.assertIn(f"[附件: {item['name']}]", header)
        self.assertIn("含 1 张图片，已随附", header)
        self.assertTrue(out["content"][2]["image_url"]["url"]
                        .startswith("data:image/jpeg;base64,"))

    def test_page_image_carries_detail_hint(self):
        item = self._stage_ok(self._pdf_image_only())
        msg, session_dir = self._prepare(item, "SIDP00003")
        out = A.expand_content_for_model(msg, session_dir)
        img = [b for b in out["content"] if b["type"] == "image_url"][0]
        self.assertEqual(img["image_url"]["detail"], "high")

    def test_user_image_has_no_detail_field(self):
        """用户上传的图片不加 detail：那是给我们自己生成的页图做的保真取舍；
        用户图片的尺寸已由 `prepare_image` 定过，多一个字段只会给不认识它的
        兼容端点制造 400 风险。"""
        item = self._stage_ok(self._png())
        msg, session_dir = self._prepare(item, "SIDP00004")
        out = A.expand_content_for_model(msg, session_dir)
        img = [b for b in out["content"] if b["type"] == "image_url"][0]
        self.assertNotIn("detail", img["image_url"])

    def test_interleaved_blocks_keep_page_order(self):
        """图片块必须落在**它那一页的文本之后、下一页文本之前** ——
        这正是"交错"与"把图全堆在末尾"的本质区别。"""
        item = self._stage_ok(self._pdf_mixed(pages=4))
        self.assertEqual(item["images"], 2)          # 只有第 2、4 页需要渲染
        msg, session_dir = self._prepare(item, "SIDP00005")
        out = A.expand_content_for_model(msg, session_dir)
        blocks = out["content"]
        img_idx = [i for i, b in enumerate(blocks) if b["type"] == "image_url"]
        self.assertEqual(len(img_idx), 2)
        p2 = next(i for i, b in enumerate(blocks)
                  if b["type"] == "text" and "第 2 页" in b.get("text", ""))
        p3 = next(i for i, b in enumerate(blocks)
                  if b["type"] == "text" and "第 3 页" in b.get("text", ""))
        self.assertLess(p2, img_idx[0])
        self.assertLess(img_idx[0], p3)
        self.assertLess(p3, img_idx[1])

    def test_page_images_degrade_to_placeholder_without_vision(self):
        item = self._stage_ok(self._pdf_image_only())
        msg, session_dir = self._prepare(item, "SIDP00006")
        out = A.expand_content_for_model(msg, session_dir, supports_image=False)
        self.assertNotIn("image_url", [b["type"] for b in out["content"]])
        self.assertTrue(any("未发送" in b.get("text", "") for b in out["content"]))
        self.assertNotIn("base64", json.dumps(out["content"]))

    def test_missing_asset_degrades_without_raising(self):
        """资产文件被手工删掉 → 明确占位；不抛，也不静默少一张图。"""
        item = self._stage_ok(self._pdf_image_only())
        msg, session_dir = self._prepare(item, "SIDP00007")
        for leftover in self._pages_dir(item, "SIDP00007").iterdir():
            leftover.unlink()
        A.clear_expand_cache()
        out = A.expand_content_for_model(msg, session_dir)      # 不抛即通过
        self.assertTrue(any("缺失" in b.get("text", "") for b in out["content"]),
                        [b.get("text", "") for b in out["content"]])

    def test_meta_records_converter_and_counts(self):
        item = self._stage_ok(self._pdf_mixed(pages=4))
        self.assertEqual(item["images"], 2)
        self.assertEqual(item["converter"], "pymupdf")
        self.assertGreaterEqual(item["tables"], 0)


# ══════════════════════════════════════════════════════════════════
class HistoryAttachmentScanTests(unittest.TestCase):
    """`history_has_attachments` —— 发送边界的短路判据。

    它决定无附件会话是否完全绕开能力查询与展开逻辑，所以对**畸形输入必须保守**
    （返回 False = 不做展开），否则一个异常形状会白白把整条链路拉起来。
    """

    def _block(self) -> dict:
        return {"type": A.ATTACHMENT_BLOCK_TYPE,
                "attachment": {"id": "att_x", "kind": "image", "name": "a.png"}}

    def test_detects_attachment_blocks(self):
        self.assertTrue(A.history_has_attachments(
            [{"role": "user",
              "content": [{"type": "text", "text": "x"}, self._block()]}]))
        self.assertTrue(A.history_has_attachments(
            [{"role": "user", "content": "纯文本"},
             {"role": "user", "content": [self._block()]}]))

    def test_ignores_everything_else(self):
        cases = [
            [],
            None,
            [{"role": "user", "content": "纯文本"}],
            [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            [{"role": "user", "content": [{"type": A.ATTACHMENT_BLOCK_TYPE,
                                           "attachment": "not-a-dict"}]}],
            [{"role": "user", "content": None}],
            [{"role": "user"}],
            ["not-a-message"],
            [{"role": "user",
              "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}],
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(A.history_has_attachments(case))


# ══════════════════════════════════════════════════════════════════
class MigrateTests(_FixtureMixin, unittest.TestCase):
    """归位：草稿区 → 会话目录，幂等，找不到时标记 missing。"""

    def _ref(self, item: dict) -> dict:
        return {"att_id": item["att_id"], "kind": item["kind"],
                "name": item["name"], "ext": item["ext"]}

    def test_migrate_moves_files_and_removes_draft(self):
        item = self._stage_ok(self._file("a.txt", "hi"))
        records = A.migrate_to_session(self.ws, [self._ref(item)], "SID0000001")
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["missing"])
        session_dir = self.ws.attachments_dir / "SID0000001"
        self.assertTrue((session_dir / f"{item['att_id']}.txt").is_file())
        self.assertTrue((session_dir / f"{item['att_id']}.meta.json").is_file())
        self.assertFalse((self.ws.attachments_dir / "_draft" / item["att_id"]).exists())

    def test_migrate_is_idempotent(self):
        item = self._stage_ok(self._file("b.txt", "hi"))
        ref = self._ref(item)
        first = A.migrate_to_session(self.ws, [ref], "SID0000002")
        second = A.migrate_to_session(self.ws, [ref], "SID0000002")
        self.assertFalse(first[0]["missing"])
        self.assertFalse(second[0]["missing"])
        session_dir = self.ws.attachments_dir / "SID0000002"
        self.assertTrue((session_dir / f"{item['att_id']}.txt").is_file())

    def test_migrate_unknown_att_id_marks_missing_without_raising(self):
        records = A.migrate_to_session(
            self.ws, [{"att_id": "att_deadbeef00", "name": "x.txt",
                       "kind": "text", "ext": ".txt"}], "SID0000003")
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["missing"])

    def test_migrate_ignores_malformed_refs(self):
        records = A.migrate_to_session(
            self.ws, ["junk", {"att_id": "../../etc/passwd"},
                      {"att_id": "xx"}], "SID0000004")
        self.assertEqual(records, [])

    def test_migrate_rejects_illegal_session_id(self):
        item = self._stage_ok(self._file("c.txt", "hi"))
        records = A.migrate_to_session(self.ws, [self._ref(item)], "../evil")
        self.assertEqual(records, [])


# ══════════════════════════════════════════════════════════════════
class ResolveTests(_FixtureMixin, unittest.TestCase):
    def test_resolve_finds_original_text_and_send_image(self):
        big = self._stage_ok(self._png("big.png", (2400, 300)))
        A.migrate_to_session(
            self.ws, [{"att_id": big["att_id"], "kind": "image",
                       "name": big["name"], "ext": big["ext"]}], "SID0000005")
        session_dir = self.ws.attachments_dir / "SID0000005"
        self.assertIsNotNone(A.resolve_files(session_dir, big)["original"])
        self.assertIsNotNone(A.resolve_files(session_dir, big)["send_image"])

    def test_resolve_document_text_file(self):
        item = self._stage_ok(self._pdf())
        A.migrate_to_session(
            self.ws, [{"att_id": item["att_id"], "kind": "document",
                       "name": item["name"], "ext": item["ext"]}], "SID0000006")
        session_dir = self.ws.attachments_dir / "SID0000006"
        files = A.resolve_files(session_dir, item)
        self.assertIsNotNone(files["text"])
        self.assertIsNotNone(files["original"])

    def test_resolve_returns_none_for_missing_dir(self):
        files = A.resolve_files(self.root / "nope", {"att_id": "att_abc123"})
        self.assertEqual(files, {"original": None, "text": None, "send_image": None})


# ══════════════════════════════════════════════════════════════════
class ExpandTests(_FixtureMixin, unittest.TestCase):
    """发送边界展开：契约是「不抛异常 + 无附件时原样返回同一对象」。

    这里刻意**走生产路径**取待发送消息（migrate 的真实记录 → build_user_content），
    而不是手搓附件块 —— 手搓会漏掉 `pages` / `text_truncated` 这类字段，
    测出来的"通过"和线上不是一回事。
    """

    def _prepare(self, item: dict, sid: str) -> tuple[dict, Path]:
        """归位 + 组装真实 user 消息，返回 `(消息, 会话目录)`。"""
        records = A.migrate_to_session(
            self.ws, [{"att_id": item["att_id"], "kind": item["kind"],
                       "name": item["name"], "ext": item["ext"]}], sid)
        msg = {"role": "user", "content": A.build_user_content("看看", records)}
        return msg, self.ws.attachments_dir / sid

    def _block(self, att_id: str, kind: str, name: str, ext: str) -> dict:
        """手工块：只用于「文件不在磁盘上」这类降级用例。"""
        return {"type": A.ATTACHMENT_BLOCK_TYPE, "attachment": {
            "id": att_id, "kind": kind, "name": name, "ext": ext}}

    def test_non_list_content_returns_same_object(self):
        msg = {"role": "user", "content": "纯文本"}
        self.assertIs(A.expand_content_for_model(msg, None), msg)

    def test_list_without_attachment_blocks_returns_same_object(self):
        msg = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        self.assertIs(A.expand_content_for_model(msg, None), msg)

    def test_malformed_attachment_block_passes_through_unchanged(self):
        msg = {"role": "user", "content": [
            {"type": A.ATTACHMENT_BLOCK_TYPE, "attachment": "not-a-dict"}]}
        self.assertIs(A.expand_content_for_model(msg, None), msg)

    def test_image_expands_to_data_url(self):
        item = self._stage_ok(self._png())
        msg, session_dir = self._prepare(item, "SID0000007")
        out = A.expand_content_for_model(msg, session_dir)
        block = out["content"][1]
        self.assertEqual(block["type"], "image_url")
        self.assertTrue(block["image_url"]["url"].startswith("data:image/png;base64,"))
        # 不回写原消息
        self.assertIsNot(out, msg)
        self.assertEqual(msg["content"][1]["type"], A.ATTACHMENT_BLOCK_TYPE)
        self.assertNotIn("base64", json.dumps(msg["content"]))

    def test_image_prefers_resized_copy_for_sending(self):
        item = self._stage_ok(self._png("big.png", (2400, 300)))
        msg, session_dir = self._prepare(item, "SID0000008")
        out = A.expand_content_for_model(msg, session_dir)
        self.assertTrue(
            out["content"][1]["image_url"]["url"].startswith("data:image/jpeg"))

    def test_missing_image_degrades_to_placeholder(self):
        msg = {"role": "user", "content": [
            self._block("att_missing001", "image", "gone.png", ".png")]}
        out = A.expand_content_for_model(msg, self.root / "nowhere")
        self.assertEqual(out["content"][0]["type"], "text")
        self.assertIn("缺失", out["content"][0]["text"])

    def test_image_without_vision_capability_degrades_not_dropped(self):
        """text-only 模型：图片降级为**明确的占位**，而不是原样发出去让 provider
        报错，更不是静默丢弃 —— 模型必须知道自己没看到东西。"""
        item = self._stage_ok(self._png())
        msg, session_dir = self._prepare(item, "SID0000009")
        out = A.expand_content_for_model(msg, session_dir, supports_image=False)
        types = [b["type"] for b in out["content"]]
        self.assertNotIn("image_url", types)
        self.assertTrue(any("未发送" in b.get("text", "") for b in out["content"]))
        self.assertNotIn("base64", json.dumps(out["content"]))

    def test_image_still_sent_when_capability_unknown(self):
        """supports_image=None（未识别模型）→ 按支持处理，不本地误拦。"""
        item = self._stage_ok(self._png())
        msg, session_dir = self._prepare(item, "SID000000A")
        out = A.expand_content_for_model(msg, session_dir, supports_image=None)
        self.assertIn("image_url", [b["type"] for b in out["content"]])

    def test_vision_capability_does_not_affect_documents(self):
        """文档走文本内联，与图片能力无关 —— text-only 模型完全可用。"""
        item = self._stage_ok(self._file("note.txt", "正文"))
        msg, session_dir = self._prepare(item, "SID000000B")
        out = A.expand_content_for_model(msg, session_dir, supports_image=False)
        # 两块：用户正文 + 附件文本块（都不是图片，故能力门控不该动它们）
        self.assertEqual([b["type"] for b in out["content"]], ["text", "text"])
        self.assertIn("[附件: note.txt]", out["content"][1]["text"])
        self.assertIn("正文", out["content"][1]["text"])

    def test_document_expands_to_text_block_with_header(self):
        item = self._stage_ok(self._pdf())
        msg, session_dir = self._prepare(item, "SID0000009")
        block = A.expand_content_for_model(msg, session_dir)["content"][1]
        self.assertEqual(block["type"], "text")
        self.assertIn(f"[附件: {item['name']}]", block["text"])
        self.assertIn("Hello Attachment PDF", block["text"])
        self.assertIn("共 1 页", block["text"])

    def test_oversized_image_degrades_with_readable_note(self):
        # 造一张 >1KB 的图（下限 1KB 是 _int_env 的 minimum），再把内联上限收到 1KB
        from PIL import Image
        p = self.src / "noise.png"
        Image.frombytes("RGB", (60, 60), os.urandom(60 * 60 * 3)).save(p)
        item = self._stage_ok(p)
        msg, session_dir = self._prepare(item, "SID0000010")
        with mock.patch.dict(os.environ, {"ATTACHMENT_INLINE_MAX_BYTES": "1024"}):
            out = A.expand_content_for_model(msg, session_dir)
        self.assertEqual(out["content"][1]["type"], "text")
        self.assertIn("过大", out["content"][1]["text"])

    def test_expand_never_raises_on_broken_session_dir(self):
        """会话目录指向一个文件（不是目录）等异常输入也必须收成占位文本。"""
        weird = self.root / "weird"
        weird.write_text("not a dir", encoding="utf-8")
        out = A.expand_content_for_model(
            {"role": "user", "content": [
                self._block("att_x1234567", "document", "a.pdf", ".pdf")]}, weird)
        self.assertEqual(out["content"][0]["type"], "text")

    def test_data_url_cache_invalidates_on_change(self):
        item = self._stage_ok(self._png())
        msg, session_dir = self._prepare(item, "SID0000011")
        first = A.expand_content_for_model(msg, session_dir)["content"][1]
        second = A.expand_content_for_model(msg, session_dir)["content"][1]
        self.assertEqual(first, second)
        # 换掉文件内容（mtime 变化）→ 缓存必须失效
        from PIL import Image
        target = session_dir / f"{item['att_id']}.png"
        Image.new("RGB", (12, 12), (10, 200, 10)).save(target)
        os.utime(target, (time.time() + 2,) * 2)
        third = A.expand_content_for_model(msg, session_dir)["content"][1]
        self.assertNotEqual(third["image_url"]["url"], first["image_url"]["url"])


# ══════════════════════════════════════════════════════════════════
class LedgerShapeTests(_FixtureMixin, unittest.TestCase):
    """账本形态：build_user_content / text_view / harvest_attachments。"""

    def test_build_user_content_returns_str_without_attachments(self):
        """这是零行为变化的硬保证：无附件必须是 str，且就是原文。"""
        self.assertEqual(A.build_user_content("hi", []), "hi")
        self.assertIsInstance(A.build_user_content("", []), str)

    def test_build_user_content_shape(self):
        records = [{"att_id": "att_abcd1234", "kind": "image", "name": "a.png",
                    "mime": "image/png", "ext": ".png", "size": 3}]
        blocks = A.build_user_content("看看", records)
        self.assertIsInstance(blocks, list)
        self.assertEqual(blocks[0], {"type": "text", "text": "看看"})
        self.assertEqual(blocks[1]["type"], A.ATTACHMENT_BLOCK_TYPE)
        att = blocks[1]["attachment"]
        self.assertEqual(att["id"], "att_abcd1234")
        self.assertEqual(att["name"], "a.png")
        self.assertNotIn("data:", json.dumps(blocks), "账本里绝不能出现文件字节")

    def test_build_user_content_omits_empty_text_block(self):
        blocks = A.build_user_content("   ", [{"att_id": "att_abcd1234"}])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], A.ATTACHMENT_BLOCK_TYPE)

    def test_harvest_roundtrip(self):
        records = [{"att_id": "att_abcd1234", "kind": "image", "name": "a.png",
                    "mime": "image/png", "ext": ".png", "size": 7,
                    "source_path": "/x/a.png",
                    "stored_path": "/y/.attachments/S1/att_abcd1234.png"}]
        harvested = A.harvest_attachments(A.build_user_content("x", records))
        self.assertEqual(harvested[0]["id"], "att_abcd1234")
        self.assertEqual(harvested[0]["name"], "a.png")
        self.assertEqual(harvested[0]["size"], 7)
        self.assertEqual(harvested[0]["stored_path"],
                         "/y/.attachments/S1/att_abcd1234.png")

    def test_harvest_skips_non_attachment_content(self):
        self.assertEqual(A.harvest_attachments("plain"), [])
        self.assertEqual(A.harvest_attachments([{"type": "text", "text": "x"}]), [])
        self.assertEqual(
            A.harvest_attachments([{"type": "attachment", "attachment": {}}]), [])

    def test_ledger_and_harvest_carry_parse_stats(self):
        """解析统计要走到回放路径 —— 否则气泡里的 chip 显示不出
        「12 页 · 5 图 · 3 表」，也标不出"已降级"。"""
        records = [{"att_id": "att_abcd1234", "kind": "document", "name": "r.pdf",
                    "ext": ".pdf", "size": 100, "pages": 12, "text_chars": 900,
                    "text_truncated": False, "images": 5, "tables": 3,
                    "converter": "pymupdf",
                    "warnings": ["第 1、2 页无文本层，已按图像发送"]}]
        blocks = A.build_user_content("看", records)
        att = blocks[1]["attachment"]
        self.assertEqual(att["pages"], 12)
        self.assertEqual(att["images"], 5)
        self.assertEqual(att["tables"], 3)
        self.assertEqual(att["converter"], "pymupdf")
        self.assertEqual(att["warnings"], ["第 1、2 页无文本层，已按图像发送"])

        harvested = A.harvest_attachments(blocks)[0]
        self.assertEqual(harvested["pages"], 12)
        self.assertEqual(harvested["images"], 5)
        self.assertEqual(harvested["tables"], 3)
        self.assertEqual(harvested["converter"], "pymupdf")
        self.assertEqual(harvested["text_chars"], 900)
        self.assertEqual(harvested["warnings"],
                         ["第 1、2 页无文本层，已按图像发送"])

    def test_old_ledger_rows_yield_default_stats(self):
        """旧 jsonl 行没有这些键 → 一律取默认值，存量零迁移。"""
        blocks = A.build_user_content("看", [{"att_id": "att_abcd1234",
                                             "kind": "document", "name": "a.pdf"}])
        harvested = A.harvest_attachments(blocks)[0]
        self.assertEqual(harvested["images"], 0)
        self.assertEqual(harvested["tables"], 0)
        self.assertEqual(harvested["converter"], "")
        self.assertEqual(harvested["warnings"], [])
        self.assertIsNone(harvested["pages"])

    def test_text_view_reads_attachments_without_bytes(self):
        content = A.build_user_content("看看这个", [
            {"att_id": "att_abcd1234", "kind": "image", "name": "a.png"}])
        text = A.text_view(content)
        self.assertIn("看看这个", text)
        self.assertIn("[图片: a.png]", text)
        self.assertNotIn("base64", text)
        self.assertEqual(A.text_view("plain"), "plain")


# ══════════════════════════════════════════════════════════════════
class GarbageCollectionTests(_FixtureMixin, unittest.TestCase):
    def _age(self, path: Path, seconds: float) -> None:
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_gc_drafts_removes_only_expired(self):
        old_dir = self.ws.attachments_dir / "_draft" / "att_old0000001"
        new_dir = self.ws.attachments_dir / "_draft" / "att_new0000001"
        old_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)
        (old_dir / "meta.json").write_text("{}", encoding="utf-8")
        (new_dir / "meta.json").write_text("{}", encoding="utf-8")
        self._age(old_dir, 4 * 24 * 3600)
        self.assertEqual(A.gc_drafts(self.ws), 1)
        self.assertFalse(old_dir.exists())
        self.assertTrue(new_dir.exists())

    def test_gc_orphan_removes_dir_without_session(self):
        orphan = self.ws.attachments_dir / "GONE000001"
        orphan.mkdir(parents=True)
        self._age(orphan, 3 * 24 * 3600)
        self.assertEqual(A.gc_orphan_session_dirs(self.ws), 1)
        self.assertFalse(orphan.exists())

    def test_gc_orphan_keeps_trashed_session_attachments(self):
        """归档（trash）只改 meta 状态、jsonl 原样保留 —— 附件必须留着。"""
        sid = "TRASHED001"
        (self.ws.chat_history_dir).mkdir(parents=True, exist_ok=True)
        (self.ws.chat_history_dir / f"session_{sid}.jsonl").write_text(
            '{"role":"user","content":"x"}\n', encoding="utf-8")
        (self.ws.chat_history_dir / f"session_{sid}.meta.json").write_text(
            '{"status":"trashed"}', encoding="utf-8")
        att_dir = self.ws.attachments_dir / sid
        att_dir.mkdir(parents=True)
        self._age(att_dir, 5 * 24 * 3600)
        self.assertEqual(A.gc_orphan_session_dirs(self.ws), 0)
        self.assertTrue(att_dir.exists())

    def test_gc_orphan_respects_min_age(self):
        fresh = self.ws.attachments_dir / "NEWONEOF01"
        fresh.mkdir(parents=True)
        self.assertEqual(A.gc_orphan_session_dirs(self.ws), 0)
        self.assertTrue(fresh.exists())

    def test_gc_orphan_never_touches_draft_dir(self):
        draft = self.ws.attachments_dir / "_draft"
        draft.mkdir(parents=True)
        self._age(draft, 30 * 24 * 3600)
        A.gc_orphan_session_dirs(self.ws)
        self.assertTrue(draft.exists())

    def test_remove_session_attachments_targets_one_session_only(self):
        keep = self.ws.attachments_dir / "KEEP000001"
        drop = self.ws.attachments_dir / "DROP000001"
        keep.mkdir(parents=True)
        drop.mkdir(parents=True)
        A.remove_session_attachments(self.ws, "DROP000001")
        self.assertFalse(drop.exists())
        self.assertTrue(keep.exists())

    def test_remove_session_attachments_ignores_illegal_id(self):
        A.remove_session_attachments(self.ws, "../..")  # 不应抛，也不应删任何东西
        self.assertTrue(self.ws.attachments_dir.parent.exists())


if __name__ == "__main__":
    unittest.main()
