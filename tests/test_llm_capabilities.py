#!/usr/bin/env python3
"""模型能力声明与预置目录的回归测试 —— 2026-09-20。

覆盖 `agents/llm_config.py` 里三处与"图片能力"相关的契约：

  1. `caps_allow_image`      —— 纯函数三态（含 image / 明确不含 / 元数据缺失）
  2. `model_supports_image`  —— 按条目 id 查能力；配置读不到一律按"支持"
  3. `_normalize_model`      —— `capability_source` 语义：
                                auto 每次从预置目录**重新推导**（官方改声明要能传导），
                                manual 用户显式指定，绝不覆盖
  4. `load_provider_catalog` —— 目录版本闸门：版本落后时内置覆盖既有厂商，
                                用户自建厂商两档都保留

背景：官方 2026-09 起 `deepseek-flash` 支持图像理解、`deepseek-v4-pro` 不支持，
而内置目录当时把两者都标成了纯文本。修正内置值本身不够 —— 若 `auto` 不再推导、
或 providers.json 的旧值把内置压住，改目录等于没改。

全部用例用临时目录承载，**不触碰真实 `~/.aigent`**。
运行：`.venv/bin/python -m unittest discover -s tests -v`（pytest 未安装，用内置 unittest）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import llm_config as L  # noqa: E402


# ══════════════════════════════════════════════════════════════════
class CapsAllowImageTests(unittest.TestCase):
    """三态规则本身（与前端 modelSupportsImage、ws_bridge 同一口径）。"""

    def test_explicit_image_is_allowed(self):
        self.assertTrue(L.caps_allow_image({"input": ["text", "image"]}))
        self.assertTrue(L.caps_allow_image({"input": ["image"]}))

    def test_explicit_text_only_is_denied(self):
        self.assertFalse(L.caps_allow_image({"input": ["text"]}))
        self.assertFalse(L.caps_allow_image({"input": ["text", "audio"]}))

    def test_unknown_shapes_are_allowed(self):
        """元数据缺失/形状不认识 → 放过。宁可让 provider 回真实错误，
        也不要因为"我们不知道"就本地静默吞掉图片。"""
        for caps in (None, {}, {"input": []}, {"input": "text"},
                     "nonsense", {"output": ["text"]}, {"input": None}):
            with self.subTest(caps=caps):
                self.assertTrue(L.caps_allow_image(caps))


# ══════════════════════════════════════════════════════════════════
class ModelSupportsImageTests(unittest.TestCase):
    def test_lookup_by_entry_id(self):
        with mock.patch.object(L, "get_model_by_id",
                               lambda mid: {"capabilities": {"input": ["text", "image"]}}):
            self.assertTrue(L.model_supports_image("m_1"))
        with mock.patch.object(L, "get_model_by_id",
                               lambda mid: {"capabilities": {"input": ["text"]}}):
            self.assertFalse(L.model_supports_image("m_1"))

    def test_unknown_model_is_allowed(self):
        with mock.patch.object(L, "get_model_by_id", lambda mid: None):
            self.assertTrue(L.model_supports_image("m_missing"))
            self.assertTrue(L.model_supports_image(None))

    def test_read_failure_never_raises(self):
        """发送边界的调用方依赖本函数不抛异常。"""
        def boom(mid):
            raise RuntimeError("配置读坏了")
        with mock.patch.object(L, "get_model_by_id", boom):
            self.assertTrue(L.model_supports_image("m_1"))


# ══════════════════════════════════════════════════════════════════
class NormalizeModelCapabilityTests(unittest.TestCase):
    """capability_source 的语义：auto 重新推导，manual 绝不覆盖。"""

    CATALOG = {"deepseek": {"models": [
        {"id": "deepseek-flash",
         "display_name": "deepseek-flash",
         "tags": ["1M", "图片"],
         "capabilities": {"input": ["text", "image"], "output": ["text"]}},
    ]}}
    CONN = {"provider": "deepseek"}

    def _normalize(self, model: dict) -> dict:
        return L._normalize_model(model, self.CATALOG, self.CONN)

    def test_auto_refreshes_from_preset(self):
        """核心：auto 的陈旧值（纯文本）必须被预置目录的新声明覆盖，
        否则修正内置目录传导不到既有安装。"""
        out = self._normalize({
            "id": "m_1", "model": "deepseek-flash", "capability_source": "auto",
            "capabilities": {"input": ["text"], "output": ["text"]},
        })
        self.assertEqual(out["capabilities"]["input"], ["text", "image"])
        self.assertEqual(out["capability_source"], "auto")

    def test_manual_is_never_overwritten(self):
        """用户显式指定过的值一律尊重 —— 即使与预置目录相左。"""
        out = self._normalize({
            "id": "m_1", "model": "deepseek-flash", "capability_source": "manual",
            "capabilities": {"input": ["text"], "output": ["text"]},
        })
        self.assertEqual(out["capabilities"]["input"], ["text"])
        self.assertEqual(out["capability_source"], "manual")

    def test_auto_keeps_stored_when_preset_has_no_capabilities(self):
        catalog = {"deepseek": {"models": [{"id": "odd-model"}]}}
        out = L._normalize_model(
            {"id": "m_1", "model": "odd-model", "capability_source": "auto",
             "capabilities": {"input": ["text", "image"], "output": ["text"]}},
            catalog, self.CONN)
        self.assertEqual(out["capabilities"]["input"], ["text", "image"])

    def test_unknown_model_defaults_to_text(self):
        out = self._normalize({"id": "m_1", "model": "brand-new"})
        self.assertEqual(out["capabilities"]["input"], ["text"])
        self.assertEqual(out["capability_source"], "manual")


# ══════════════════════════════════════════════════════════════════
class ProviderCatalogVersionTests(unittest.TestCase):
    """目录版本闸门：没有它，providers.json 的旧值会把内置修正永久压住。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "providers.json"
        patcher = mock.patch.object(L, "PROVIDER_CATALOG_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_first_load_materializes_with_current_version(self):
        L.load_provider_catalog()
        self.assertTrue(self.path.exists())
        self.assertEqual(self._read()["version"], L.CATALOG_VERSION)

    def test_same_version_keeps_user_edits(self):
        """同版本：文件为准 —— 用户手工微调 providers.json 必须保留。"""
        L.load_provider_catalog()
        data = self._read()
        data["providers"]["deepseek"]["models"][0]["display_name"] = "我改的名字"
        data["providers"]["myown"] = {"name": "自建厂商", "models": []}
        self.path.write_text(json.dumps(data), encoding="utf-8")

        got = L.load_provider_catalog()["providers"]
        self.assertEqual(got["deepseek"]["models"][0]["display_name"], "我改的名字")
        self.assertIn("myown", got)

    def test_stale_version_rebuilds_builtin_but_keeps_custom(self):
        """版本落后：内置全量覆盖既有厂商，用户自建厂商两档都保留。"""
        L.load_provider_catalog()
        data = self._read()
        data["version"] = 0
        data["providers"]["deepseek"]["models"][0]["display_name"] = "陈旧的名字"
        data["providers"]["myown"] = {"name": "自建厂商", "models": []}
        self.path.write_text(json.dumps(data), encoding="utf-8")

        got = L.load_provider_catalog()["providers"]
        builtin_name = L._DEFAULT_CATALOG["providers"]["deepseek"]["models"][0]["display_name"]
        self.assertEqual(got["deepseek"]["models"][0]["display_name"], builtin_name)
        self.assertIn("myown", got)
        # 回写后版本已同步，下一次加载不再重建
        self.assertEqual(self._read()["version"], L.CATALOG_VERSION)

    def test_broken_file_falls_back_to_builtin(self):
        self.path.write_text("{ 这不是 json", encoding="utf-8")
        got = L.load_provider_catalog()["providers"]
        self.assertIn("deepseek", got)

    def test_builtin_declares_deepseek_vision_correctly(self):
        """盯住本次修正的官方口径（2026-09 核对）。

        `deepseek-flash` 支持图像理解；`deepseek-v4-pro` 不支持。旧的
        `deepseek-v4-flash-vision-exp` 已下线，请求由 V4.1-Flash 承接 —— 能力同 flash。
        """
        models = {m["id"]: m for m in L._DEFAULT_CATALOG["providers"]["deepseek"]["models"]}
        self.assertEqual(models["deepseek-flash"]["capabilities"]["input"], ["text", "image"])
        self.assertEqual(models["deepseek-v4-flash"]["capabilities"]["input"], ["text", "image"])
        self.assertEqual(models["deepseek-v4-pro"]["capabilities"]["input"], ["text"])
        self.assertEqual(
            models["deepseek-v4-flash-vision-exp"]["capabilities"]["input"], ["text", "image"])


if __name__ == "__main__":
    unittest.main()
