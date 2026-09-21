#!/usr/bin/env python3
"""工具读图（通道仍是"中性图片块 + 合成消息"，入口已并入 `run_read`）—— 2026-09-21。

被守护的链路（设计见 `docs/frontend/12-附件与文件输入.md` / `13-引用文件…` /
`14-工具读图` / `15-统一文件读取`）：

  1. 工具层  —— `run_read` 读到图片时造出**中性图片块**；缺失 / 越界返回 `str` 错误
  2. 无字节  —— 返回值与账本块里**永不出现 base64 / data URL**（只在发送边界编码）
  3. 聚合    —— `build_tool_images_message` 把一批结果收成**一条**消息；超上限不静默丢弃
  4. 展开    —— `expand_content_for_model` 产出 `image_url`；能力不符 / 文件没了 → 说清原因的占位
  4.5 大图   —— 超过长边上限的图先缩放再发；**缩放失败绝不回落原图**（2026-09-21 线上事故）
  5. 门控    —— `history_has_images` 同时覆盖附件图片块与工具图片块
  6. 顺序    —— 图片消息必须在该批 tool 消息**全部之后**追加（源码级硬约束）

> 入口变化（2026-09-21）：`view_image` 这个工具名已退役，读图片走 `run_read`
> （它按魔数/扩展名分派）。**通道本身没变**：仍然是中性块 → 合成 user 消息 →
> 发送边界编码。所以本文件守的结构性保证一条没少，只是工具名换了。

**为什么顺序那条要用源码断言**：它由 `agent_loop` 里"循环外追加"这个位置决定，
而 `agent_loop` 需要真实 LLM 才能驱动。仓库既有做法同此（见
`test_system_injection_contract` 的源码切片守卫）—— 与其造一个假的循环去测假循环，
不如把"追加点必须在循环之外"这件事钉在源码上。

**为什么这里没有"模型是否真的看到了图"的断言**：那是端到端行为，取决于 provider。
本文件守的是我们这一侧的结构性保证：像素**进了请求体**、且**只**在请求体里。

运行：`.venv/bin/python -m unittest discover -s tests -v`
"""
import json
import locale
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import agent_full_v2  # noqa: E402
import attachments as A  # noqa: E402
from streaming_client import StreamedMessage, StreamedToolCall  # noqa: E402
from tools import ToolRegistry  # noqa: E402
# 离线 Agent 夹具（绕开会真连 LLM 的 __init__，只填 agent_loop 需要的字段）。
# **刻意复用而非复制**：该夹具随引擎新增必填字段演进过（见它自己的注释），
# 复制一份就是复制一处注定会过期的东西。
from test_promise_guard import _make_offline_agent  # noqa: E402


def _is_utf8_default() -> bool:
    """`Path.read_text()` 走 locale 编码；非 UTF-8 环境下解码断言不成立。"""
    enc = str(locale.getpreferredencoding(False) or "").lower().replace("-", "")
    return enc in ("utf8", "utf8mb4")


class _TempRoot(unittest.TestCase):
    """临时工作空间夹具（**不触碰真实 ~/.aigent**）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        A.clear_expand_cache()   # 展开缓存跨用例串味会让断言假绿

    def tearDown(self):
        self._tmp.cleanup()
        A.clear_expand_cache()

    def png(self, name: str = "shot.png") -> Path:
        """写一张**真的** PNG（魔数判定要过，不能拿假字节糊）。"""
        from PIL import Image
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (200, 30, 30)).save(path)
        return path


# ══════════════════════════════════════════════════════════════════
#  1-2. 工具层：造块、无字节、错误一律是字符串
# ══════════════════════════════════════════════════════════════════

class ViewImageToolTests(_TempRoot):
    def setUp(self):
        super().setUp()
        self.reg = ToolRegistry(workdir=self.root, bash_cwd=self.root)

    def test_returns_neutral_block_without_any_bytes(self):
        path = self.png()
        out = self.reg.run_read("shot.png")
        self.assertTrue(A.is_tool_image_result(out), out)
        images = A.tool_image_items(out)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["mime"], "image/png")
        self.assertEqual(images[0]["path"], str(path))
        # 块里**不允许**出现字节：base64 只允许在发送边界(
        # `expand_content_for_model`)那一刻存在
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("base64", blob)
        self.assertNotIn("data:image", blob)
        self.assertNotIn("iVBORw0KGgo", blob)
        # 说明是**元数据**（文件名/类型/体积），不是对图片内容的转述
        self.assertIn("shot.png", out["text"])

    def test_registered_and_declared_in_schema(self):
        """定义了 schema 却忘了注册 handler（或反之）—— 两个方向都钉住。

        顺带钉住**退役**：`view_image` / `run_read_pdf` 不再出现在工具表里
        （2026-09-21 合并进 run_read），否则模型又要在多个名字之间选。
        """
        names = [t["function"]["name"] for t in self.reg.base_tools]
        self.assertIn("run_read", names)
        self.assertNotIn("view_image", names)
        self.assertNotIn("run_read_pdf", names)
        self.assertIsNotNone(self.reg.resolve_handler("run_read"))
        # 与 API 下发的名字一致：handler 里写错名字 = 模型永远调不动
        self.assertIn("run_read", self.reg.handlers)
        self.assertNotIn("view_image", self.reg.handlers)

    def test_mime_comes_from_file_signature_not_extension(self):
        """后缀撒谎时以**文件头**为准（截图存成 .png 的 JPEG 很常见）。"""
        from PIL import Image
        path = self.root / "actually_jpeg.png"
        Image.new("RGB", (8, 8), (0, 0, 255)).save(path, "JPEG")
        out = self.reg.run_read("actually_jpeg.png")
        self.assertTrue(A.is_tool_image_result(out), out)
        self.assertEqual(A.tool_image_items(out)[0]["mime"], "image/jpeg")

    def test_text_file_is_read_as_text(self):
        """文本走文本分支：不再需要"指路"，因为没有第二个工具可选了。"""
        (self.root / "a.txt").write_text("hello", encoding="utf-8")
        out = self.reg.run_read("a.txt")
        self.assertEqual(out, "hello")

    def test_text_wearing_image_extension_is_read_as_text(self):
        """后缀是 .png、内容却是文本：按**内容**读成文本，而不是报错。

        判类型魔数优先的意义就在这里 —— 模型拿到的是真实的字节内容，
        而不是一句"不是图片"。
        """
        (self.root / "fake.png").write_text("not an image", encoding="utf-8")
        self.assertEqual(self.reg.run_read("fake.png"), "not an image")

    def test_missing_file_and_escape_return_error_string(self):
        """工具层契约：任何失败都返回字符串，绝不向上抛。"""
        for bad in ("nope.png", "../outside.png", "/etc/passwd",
                    "../../../../etc/hosts"):
            out = self.reg.run_read(bad)
            self.assertIsInstance(out, str, bad)
            self.assertTrue(out.startswith("Error:"), f"{bad} -> {out}")

    def test_run_read_returns_image_block_for_images(self):
        """模型只记一个工具名时的主路径：run_read 直接给图片块，不是指路错误。"""
        self.png()
        out = self.reg.run_read("shot.png")
        self.assertTrue(A.is_tool_image_result(out), out)
        self.assertEqual(A.tool_image_items(out)[0]["name"], "shot.png")

    @unittest.skipUnless(_is_utf8_default(), "默认编码非 UTF-8，解码断言不成立")
    def test_renamed_image_is_detected_by_magic_bytes(self):
        """改名的图片（PNG 魔数、.txt 后缀）：靠**魔数**认出来并走图片分支。

        改造前它只会在文本分支抛解码错误、再被指路去别的工具 —— 现在一步到位。
        """
        (self.root / "blob.txt").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01")
        out = self.reg.run_read("blob.txt")
        self.assertTrue(A.is_tool_image_result(out), out)
        self.assertEqual(A.tool_image_items(out)[0]["mime"], "image/png")

    def test_directory_returns_pointer_error(self):
        (self.root / "sub").mkdir()
        out = self.reg.run_read("sub")
        self.assertIsInstance(out, str)
        self.assertTrue(out.startswith("Error:"), out)
        self.assertIn("目录", out)


# ══════════════════════════════════════════════════════════════════
#  3. 聚合：一批结果 → 一条合成消息
# ══════════════════════════════════════════════════════════════════

class ToolImagesMessageTests(unittest.TestCase):
    def _values(self, count: int) -> list:
        return [A.build_tool_image_result(f"/w/shot{i}.png", mime="image/png")
                for i in range(count)]

    def test_aggregates_into_single_message_with_marker(self):
        msg = A.build_tool_images_message(self._values(2))
        self.assertEqual(msg["role"], "user")
        self.assertTrue(A.is_tool_images_message(msg))
        # 1 段说明 + 2 个图片块
        self.assertEqual(len(msg["content"]), 3)
        self.assertEqual(msg["content"][0]["type"], "text")
        for block in msg["content"][1:]:
            self.assertEqual(block["type"], A.TOOL_IMAGE_BLOCK_TYPE)
        self.assertNotIn("data:image", json.dumps(msg, ensure_ascii=False))

    def test_marker_is_not_in_model_message_whitelist(self):
        """marker 只能留在 jsonl 供回放辨认，**不得**漏进 API 请求体。

        白名单在 agent_full_v2.MODEL_MSG_FIELDS；这条断言保证两边没跑偏。
        """
        import agent_full_v2
        self.assertNotIn(A.TOOL_IMAGES_MARKER, agent_full_v2.MODEL_MSG_FIELDS)

    def test_over_limit_is_explained_not_silently_dropped(self):
        msg = A.build_tool_images_message(self._values(4), limit=2)
        self.assertEqual(len(msg["content"]), 3)          # 只带 2 组
        text = msg["content"][0]["text"]
        # 上限按**组**（一次工具结果）计，不是按张 —— 具体文案见 attachments
        self.assertIn("另有 2 个文件未随附", text)
        self.assertIn("单轮图片上限 2 个", text)

    def test_garbage_values_yield_none(self):
        """畸形数据不产生空消息 —— 否则历史里会多一条没有内容的 user 行。"""
        for values in ([], [None, "x", {}, {"type": "tool_image"}],
                       [{"type": "tool_image", "image": {}}],
                       [{"type": "tool_image", "image": {"path": ""}}]):
            self.assertIsNone(A.build_tool_images_message(values), values)


# ══════════════════════════════════════════════════════════════════
#  4-5. 发送边界：展开 + 能力门控 + 短路
# ══════════════════════════════════════════════════════════════════

class ToolImageExpansionTests(_TempRoot):
    def message(self, path) -> dict:
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": "[以下是 run_read 读取的图片，内容随附：shot.png]"},
                A.tool_image_block(A.build_tool_image_result(path)),
            ],
            A.TOOL_IMAGES_MARKER: True,
        }

    def test_expands_to_image_url_data_url(self):
        path = self.png()
        out = A.expand_content_for_model(self.message(path), None,
                                         supports_image=True)
        self.assertEqual(out["content"][0]["type"], "text")
        block = out["content"][1]
        self.assertEqual(block["type"], "image_url")
        self.assertTrue(block["image_url"]["url"].startswith("data:image/png;base64,"))
        # 展开结果**不回写**账本：原消息必须还是中性块
        self.assertEqual(self.message(path)["content"][1]["type"], "tool_image")

    def test_capability_false_degrades_without_reading_disk(self):
        """能力不符 → 占位文本，且**在读盘/编码之前**就判（用不存在的路径证明）。"""
        bogus = self.root / "never-existed.png"
        out = A.expand_content_for_model(self.message(bogus), None,
                                        supports_image=False)
        self.assertEqual(len(out["content"]), 2)
        text = out["content"][1]["text"]
        self.assertIn("未发送", text)
        self.assertIn("不支持图片输入", text)
        self.assertNotIn("base64", json.dumps(out, ensure_ascii=False))

    def test_missing_file_degrades_to_text(self):
        bogus = self.root / "gone.png"
        out = A.expand_content_for_model(self.message(bogus), None,
                                         supports_image=True)
        self.assertIn("图片缺失", out["content"][1]["text"])

    def test_no_tool_image_blocks_returns_same_object(self):
        """零行为变化：没有工具图片块的消息必须**原对象返回**（不是等值副本）。"""
        msg = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        self.assertIs(A.expand_content_for_model(msg, None), msg)
        plain = {"role": "user", "content": "hi"}
        self.assertIs(A.expand_content_for_model(plain, None), plain)

    def test_history_has_images_covers_both_channels(self):
        attachment_msg = {"role": "user", "content": [
            {"type": A.ATTACHMENT_BLOCK_TYPE,
             "attachment": {"kind": "image", "name": "x.png"}}]}
        tool_msg = {"role": "user", "content": [
            {"type": A.TOOL_IMAGE_BLOCK_TYPE, "image": {"path": "/w/x.png",
                                                        "name": "x.png"}}]}
        self.assertTrue(A.history_has_images([attachment_msg]))
        self.assertTrue(A.history_has_images([tool_msg]))
        self.assertFalse(A.history_has_images(
            [{"role": "user", "content": "纯文本"}]))
        self.assertFalse(A.history_has_images([]))

    def test_text_view_labels_tool_image_without_path(self):
        """钩子/日志拿到的是文本视图 —— 只给标签，不把绝对路径当内容拼进去。"""
        got = A.text_view(self.message("/w/secret/shot.png")["content"])
        self.assertIn("[图片: shot.png]", got)
        self.assertNotIn("/w/secret", got)


# ══════════════════════════════════════════════════════════════════
#  5.5 大图缩放：Pillow 解压炸弹阈值 + "缩放失败绝不回落原图"
#      （2026-09-21 线上事故的回归守卫）
# ══════════════════════════════════════════════════════════════════

class OversizedImageTests(_TempRoot):
    """事故形状：`@` 引用了一张 22500×15016（3.4 亿像素、4.6MB）的 PNG。

    `Image.open()` 命中 Pillow 的解压炸弹阈值（默认 ≥179MP 直接抛
    `DecompressionBombError`）→ 缩放失败被当成"尺寸本来就小、不用缩" →
    **回落发原图** → provider 回 `messages[11].image[0]: You have uploaded an
    unsupported image`，整轮对话被打死（会话 `session_bM9siq5BMA` 实录）。
    两个根因各钉一条：阈值必须被抬升、失败必须与"不需要缩"分开。
    """

    def _png(self, size, name="big.png") -> Path:
        """造一张长边超限的 PNG（纯色，压缩后很小 → 判定只取决于尺寸）。"""
        from PIL import Image
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (12, 34, 56)).save(path)
        return path

    def _message(self, path) -> dict:
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": "[以下是 run_read 读取的图片，内容随附：shot.png]"},
                A.tool_image_block(A.build_tool_image_result(path)),
            ],
            A.TOOL_IMAGES_MARKER: True,
        }

    def test_bomb_guard_is_lifted_but_not_left_lifted(self):
        """Pillow 的阈值是我们自己的 DOS 防线，不该拦住用户自己的大图。

        不必真造 1.8 亿像素的图：把阈值压到极小即可复现同一个异常路径。
        同时断言它**用完即复原** —— `Image.MAX_IMAGE_PIXELS` 是模块级全局，
        永久改小/改没都会影响进程里其它图片解码。
        """
        from PIL import Image
        path = self._png((3000, 2000))          # 6MP > 被压低后的阈值
        previous = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = 100
        self.addCleanup(setattr, Image, "MAX_IMAGE_PIXELS", previous)

        data, code, _ = A._resize_jpeg_bytes(path, 1568)
        self.assertEqual(code, A._RESIZE_OK)
        self.assertIsNotNone(data)
        self.assertEqual(data[:2], b"\xff\xd8")             # JPEG 魔数
        self.assertEqual(Image.MAX_IMAGE_PIXELS, 100)       # 全局已被复原

    def test_big_image_is_downscaled_before_send(self):
        """正常出路：超过长边上限的图缩到上限再发，而不是原样递出去。"""
        from PIL import Image
        import base64
        import io
        path = self._png((3000, 2000))
        with mock.patch.object(A, "_RESIZE_ABOVE_BYTES", 1):
            A.clear_expand_cache()
            out = A.expand_content_for_model(self._message(path), None,
                                             supports_image=True)
        block = out["content"][1]
        self.assertEqual(block["type"], "image_url")
        self.assertTrue(block["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
        self.assertEqual(max(Image.open(io.BytesIO(raw)).size), 1568)

    def test_resize_failure_never_falls_back_to_original(self):
        """**事故的直接形状**：缩放没做成时，原图一个字节都不许发出去。

        复现：把本机解码上限压到 1MP，6MP 的图判为 `TOO_BIG`（判在 `load()`
        之前，不解码）。契约是"降级成说清原因的文本"，而不是把原图当成
        "缩好的图"递出去换一个与真实原因无关的 provider 400。
        """
        path = self._png((3000, 2000))
        # 体积门槛一并压低：纯色 PNG 很小，不压就根本不进缩放分支
        with mock.patch.object(A, "_RESIZE_ABOVE_BYTES", 1), \
                mock.patch.dict(os.environ,
                                {"ATTACHMENT_IMAGE_DECODE_MAX_PIXELS": "1000000"}):
            A.clear_expand_cache()
            out = A.expand_content_for_model(self._message(path), None,
                                             supports_image=True)
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("data:image", blob)          # 原图没被递出去
        self.assertNotIn("base64", blob)
        text = out["content"][1]["text"]
        self.assertIn("3000×2000", text)              # 说清是多大
        self.assertIn("超出本机解码上限", text)        # 说清为什么
        self.assertIn("sips -Z 1568", text)           # 给出可执行的下一步

    def test_expand_cache_also_remembers_failures(self):
        """失败结论同样要缓存：解码级成本（GB/秒）不能每轮 LLM 往返重算。

        注意键里含解码上限 —— 配置改了就不该命中旧结论（所以这里压上限后
        必须能拿到新的失败结论，而不是上一次的成功 data URL）。
        """
        path = self._png((3000, 2000))
        with mock.patch.object(A, "_RESIZE_ABOVE_BYTES", 1):
            A.clear_expand_cache()
            first = A._data_url_tool_image(path, "image/png")
            self.assertTrue(first[0].startswith("data:image/jpeg;base64,"))
            with mock.patch.dict(os.environ,
                                 {"ATTACHMENT_IMAGE_DECODE_MAX_PIXELS": "1000000"}):
                second = A._data_url_tool_image(path, "image/png")
                self.assertIsNone(second[0])
                self.assertIn("超出本机解码上限", second[1])
                # 再取一次：命中缓存，不能又变回成功
                self.assertEqual(A._data_url_tool_image(path, "image/png"), second)


# ══════════════════════════════════════════════════════════════════
#  6. 端到端（到请求体为止）：历史里的中性块 → 请求体里的 image_url
# ══════════════════════════════════════════════════════════════════

class ModelMessagesProjectionTests(_TempRoot):
    """`Agent._model_messages()` 是引擎与 provider 之间的最后一道边界。

    这条断言就是"模型到底拿到了什么"：**图片进了请求体、marker 没进、
    历史没被改写**。能力不符时则必须降级成说清原因的文本，而不是让 provider 报错。
    """

    def _agent(self, messages):
        import agent_full_v2
        agent = agent_full_v2.Agent.__new__(agent_full_v2.Agent)
        agent.history_messages = messages
        agent._turn_model_id = "m_test"
        return agent, agent_full_v2

    def _mock_support(self, module, value):
        return mock.patch.object(module, "model_supports_image", return_value=value)

    def test_projection_carries_image_url_and_strips_marker(self):
        path = self.png()
        msg = A.build_tool_images_message([A.build_tool_image_result(path)])
        agent, module = self._agent([{"role": "user", "content": "看看这张图"}, msg])
        with self._mock_support(module, True):
            projected = agent._model_messages()

        self.assertEqual(len(projected), 2)
        # marker 只在 jsonl 里辨认回放用，**不得**漏进请求体
        self.assertNotIn(A.TOOL_IMAGES_MARKER, projected[1])
        block = projected[1]["content"][1]
        self.assertEqual(block["type"], "image_url")
        self.assertTrue(block["image_url"]["url"].startswith("data:image/png;base64,"))
        # 展开只作用于本次请求：历史必须仍是**中性块**（账本无字节）
        self.assertEqual(msg["content"][1]["type"], A.TOOL_IMAGE_BLOCK_TYPE)
        self.assertTrue(msg[A.TOOL_IMAGES_MARKER])
        self.assertNotIn("base64", json.dumps(msg, ensure_ascii=False))

    def test_projection_degrades_when_model_lacks_image_input(self):
        path = self.png()
        msg = A.build_tool_images_message([A.build_tool_image_result(path)])
        agent, module = self._agent([msg])
        with self._mock_support(module, False):
            projected = agent._model_messages()
        blob = json.dumps(projected, ensure_ascii=False)
        self.assertNotIn("base64", blob)
        self.assertIn("不支持图片输入", blob)


# ══════════════════════════════════════════════════════════════════
#  7. 整条链路：真实 agent_loop 跑一遍（模型用脚本桩，不联网）
# ══════════════════════════════════════════════════════════════════

class _LoopStubLLM:
    """`streamed_create` 替身：按脚本返回工具调用/正文，并记录每次**请求体**。"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests: list = []

    def __call__(self, llm, sinks=None, should_stop=None, **kwargs):
        self.requests.append(kwargs.get("messages"))
        idx = min(len(self.requests), len(self.scripts)) - 1
        spec = self.scripts[idx]
        calls = None
        if spec.get("tool"):
            calls = [StreamedToolCall("call_1", spec["tool"],
                                      json.dumps(spec.get("args") or {}))]
        return (StreamedMessage(spec.get("content"), None, calls),
                spec.get("finish", "stop"),
                {"prompt_tokens": 1, "completion_tokens": 1})


class ToolImageLoopTests(unittest.TestCase):
    """工具层 → agent_loop 回放 → 发送边界投影，三次变换全走真代码。

    这是本功能唯一的**链路级**断言，专门守 §"顺序硬约束"那条：
    图片块最终必须落成 `[assistant(tool_calls), tool, 合成 user]` 这个次序，
    且第二跳请求体里真的带着 `image_url`。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        A.clear_expand_cache()
        self.addCleanup(A.clear_expand_cache)
        # `_make_offline_agent` 内部 SessionManager → ContextCompact → LLMClient()
        # 要求密钥/地址**已配置**（只校验存在、不联网）。本机没有
        # `~/.aigent/credentials.json` 时会在构造阶段就抛 ValueError，而全套测试
        # 里恰好有别的用例先往 os.environ 塞了值 —— 单独跑本文件就会挂。
        # 补一对假值让本文件**不依赖执行顺序**；请求永远发不出去（模型被脚本桩替换）。
        env = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key-not-used",
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        })
        env.start()
        self.addCleanup(env.stop)
        from PIL import Image
        Image.new("RGB", (8, 8), (10, 20, 30)).save(self.root / "shot.png")

    def _run(self, scripts):
        agent = _make_offline_agent(self.root)
        # 换成**真**工具表：桩 tools 没有 run_read 的 handler，会退化成
        # "Error: Unknown tool"，测的就不是这条链路了
        agent.tools = ToolRegistry(workdir=self.root, bash_cwd=self.root)
        stub = _LoopStubLLM(scripts)
        original = agent_full_v2.streamed_create
        agent_full_v2.streamed_create = stub
        self.addCleanup(lambda: setattr(agent_full_v2, "streamed_create", original))
        # 能力门控查的是真实模型配置；桩环境里必须显式给答案
        with mock.patch.object(agent_full_v2, "model_supports_image", return_value=True):
            agent.agent_loop()
        return agent, stub

    def test_chain_produces_tool_then_single_synthetic_message(self):
        agent, stub = self._run([
            {"tool": "run_read", "args": {"path": "shot.png"}},
            {"content": "看过了，界面正常。"},
        ])

        hist = agent.history_messages
        roles = [m["role"] for m in hist]
        self.assertEqual(roles[:2], ["system", "user"])

        # ① 合成消息**只有一条**，且排在**该批全部 tool 消息之后**
        synthetic = [m for m in hist if A.is_tool_images_message(m)]
        self.assertEqual(len(synthetic), 1, hist)
        first_tool = next(i for i, m in enumerate(hist) if m["role"] == "tool")
        first_synth = hist.index(synthetic[0])
        self.assertGreater(first_synth, first_tool)
        # 首个 tool 消息 → 合成消息之间**只能**是 tool 消息（不能插进 user 消息）
        self.assertTrue(all(m["role"] == "tool" for m in hist[first_tool:first_synth]),
                        hist[first_tool:first_synth])
        self.assertEqual([m["role"] for m in hist[first_tool:first_synth + 1]],
                         ["tool", "user"])

        # ② tool 消息只承载说明文本，且**不含字节**
        tool_msg = hist[first_tool]
        self.assertIsInstance(tool_msg["content"], str)
        self.assertIn("已读取", tool_msg["content"])
        self.assertNotIn("base64", tool_msg["content"])

        # ③ 账本里没有字节（base64 只在发送边界那一刻存在）
        self.assertNotIn("base64", json.dumps(hist, ensure_ascii=False))

        # ④ 第二跳请求体：同一个模型确实拿到了像素
        self.assertEqual(len(stub.requests), 2)
        sent = stub.requests[1]
        image_blocks = [b for m in sent if isinstance(m.get("content"), list)
                        for b in m["content"]
                        if isinstance(b, dict) and b.get("type") == "image_url"]
        self.assertEqual(len(image_blocks), 1, sent)
        self.assertTrue(image_blocks[0]["image_url"]["url"]
                        .startswith("data:image/png;base64,"))
        # 而 marker 没有漏进请求体
        self.assertFalse(any(m.get("_tool_images") for m in sent))

    def test_chain_without_image_read_is_unchanged(self):
        """对照：不读图片时历史里不该出现任何合成消息。"""
        agent, stub = self._run([{"content": "不需要看什么。"}])
        self.assertFalse(any(A.is_tool_images_message(m)
                             for m in agent.history_messages))
        self.assertEqual(len(stub.requests), 1)
        self.assertFalse(any(isinstance(m.get("content"), list)
                             for m in stub.requests[0]))

    def test_failed_image_read_still_answers_the_tool_call(self):
        """工具失败也必须回答 tool_call_id —— 否则历史留下孤儿 tool_call。"""
        agent, stub = self._run([
            {"tool": "run_read", "args": {"path": "nope.png"}},
            {"content": "找不到图，换个路径。"},
        ])
        tool_msgs = [m for m in agent.history_messages if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertTrue(tool_msgs[0]["content"].startswith("Error:"))
        # 失败 → 不该产生合成图片消息
        self.assertFalse(any(A.is_tool_images_message(m)
                             for m in agent.history_messages))


# ══════════════════════════════════════════════════════════════════
#  8. 顺序硬约束（源码级）
# ══════════════════════════════════════════════════════════════════

class ToolImageOrderingContractTests(unittest.TestCase):
    """图片消息必须在**该批 tool 消息全部落盘之后**追加，且只有一条。

    违反后果：图片消息插进两条 tool 消息之间 → 打断 assistant.tool_calls ↔
    tool 消息链（OpenAI 协议风险）；写成"每条工具结果后面跟一条" →
    同一批里出现多条合成消息，历史结构随并行工具数膨胀。
    """

    def setUp(self):
        self.src = (AGENTS_DIR / "agent_full_v2.py").read_text(encoding="utf-8")

    def test_single_image_message_appended_after_replay_loop(self):
        loop_at = self.src.rindex("for tc in response_tool_calls:")
        guard_at = self.src.index("if tool_image_values:", loop_at)
        # 循环体内**不得**出现 image_msg —— 出现即意味着"边回放边插一条"
        self.assertNotIn("image_msg", self.src[loop_at:guard_at])
        append_at = self.src.index("build_tool_images_message(tool_image_values)")
        self.assertGreater(append_at, guard_at)
        tail = self.src[append_at:append_at + 600]
        self.assertIn("history_messages.append(image_msg)", tail)
        self.assertIn("append_message_to_session", tail)   # 必须落盘，否则回放看不到

    def test_tool_message_keeps_only_the_text_note(self):
        """tool 消息只能承载说明文本 —— 图片块被摘出去交给合成消息。"""
        replay_at = self.src.rindex("for tc in response_tool_calls:")
        seg = self.src[replay_at:replay_at + 1400]
        self.assertIn("tool_image_values.append(content)", seg)
        self.assertIn("content = tool_image_text(content)", seg)

    def test_image_result_is_not_stringified(self):
        """`_execute_tool_call` 必须原样保留图片块（str() 掉 = 图彻底丢失）。"""
        gen_at = self.src.index("is_tool_image_result(tool_output)")
        self.assertIn("else str(tool_output)", self.src[gen_at:gen_at + 120])


if __name__ == "__main__":
    unittest.main()
