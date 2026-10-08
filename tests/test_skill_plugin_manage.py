#!/usr/bin/env python3
"""技能 / 插件管理（agents/{skill_store,plugin_store,skill_market,plugin_market,
store_io}.py + skills.py 的启停与插件注入）回归测试 —— 2026-09-30。

守护八件事（见 docs/frontend/24、25）：

1. **frontmatter 单一实现**：`skills.SkillLoader._parse_frontmatter` 与
   `skill_store.parse_frontmatter` 必须**同一份**（设置页显示的 description 与模型
   看到的必须逐字一致）；语法错误降级成空 meta，绝不抛。
2. **`build_skill_md` 的往返**：description 里带 `:` / 引号 / 换行时仍能被解析回原值
   —— 这是"手动新建技能"最容易出问题的地方（裸拼 YAML 会产出坏 frontmatter）。
3. **启停只写旁路元数据**：开关技能后 SKILL.md 的**字节内容完全不变**（技能目录要
   保持可直接拷给别的 Agent 的原样）。
4. **`SkillLoader` 的整体重建**：删除 / 禁用技能后必须**立刻**从注册表消失
   （历史实现只增不删 → 删除按钮是假的）。
5. **插件技能命名空间**：插件贡献的技能以 `<插件>:<技能>` 出现在注册表里；插件禁用
   后立即消失；同名技能不会互相覆盖。
6. **安装的原子性**：校验不过一个字节都不写；写入中途失败要回滚（不留半装目录）；
   同名目录拒绝覆盖。
7. **路径安全**：`safe_relpath` 挡住 `..` / 绝对路径 / Windows 盘符；删除时符号链接
   只 unlink 不跟随（否则一个同名链接就能让"删除技能"端掉别的目录）。
8. **市场源的规则**：内置源不能删、身份字段以代码为准；`plugin_market._source_of`
   认得 Claude Code 的四种 source 形态、并**明确拒绝**本地路径（不能静默忽略）。

⚠️ 全程用临时目录，**绝不碰真实的 `~/.aigent`**；网络调用一律不打（`search` 的失败
路径只验证"未知源 → error 文案而不是抛异常"）。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import json
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

import httpx  # noqa: E402

import plugin_market  # noqa: E402
import plugin_store  # noqa: E402
import skill_market  # noqa: E402
import skill_store  # noqa: E402
import store_io  # noqa: E402
from skills import SkillLoader  # noqa: E402


def _write_skill(root: Path, name: str, description: str = "desc", body: str = "正文") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    md = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    (d / "SKILL.md").write_text(md, encoding="utf-8")
    return d


def _write_plugin(root: Path, name: str, *, skills=(), commands=(), hooks=None,
                  mcp=None, manifest=True) -> Path:
    d = root / name
    (d / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    if manifest:
        (d / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "description": "测试插件"}, ensure_ascii=False),
            encoding="utf-8")
    for s in skills:
        _write_skill(d / "skills", s, description=f"{s} 的说明")
    for c in commands:
        (d / "commands").mkdir(parents=True, exist_ok=True)
        (d / "commands" / f"{c}.md").write_text(f"# {c}\n", encoding="utf-8")
    if hooks:
        (d / "hooks").mkdir(parents=True, exist_ok=True)
        (d / "hooks" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    if mcp:
        (d / ".mcp.json").write_text(json.dumps({"mcpServers": mcp}), encoding="utf-8")
    return d


class TestFrontmatterAndNaming(unittest.TestCase):
    """1 / 2：frontmatter 单一实现 + build_skill_md 往返。"""

    def test_parse_matches_skill_loader(self):
        """两处解析必须同源 —— 这是"设置页描述与模型看到的不一致"的根因防线。"""
        text = "---\nname: demo\ndescription: 做某件事\ntags: a, b\n---\n\n正文"
        loader = SkillLoader(Path(tempfile.mkdtemp()) / "none")
        self.assertEqual(loader._parse_frontmatter(text),
                         skill_store.parse_frontmatter(text))

    def test_bad_yaml_degrades_without_raising(self):
        for bad in ("---\nname: [unclosed\n---\nbody", "---\n---\n", "no frontmatter"):
            with self.subTest(bad=bad):
                meta, body = skill_store.parse_frontmatter(bad)
                self.assertIsInstance(meta, dict)
                self.assertIsInstance(body, str)

    def test_build_skill_md_roundtrip_with_tricky_description(self):
        """description 里出现冒号 / 引号 / 换行是常态，裸拼 YAML 必然坏。"""
        desc = 'Use this when: the user says "review" and\nneeds a checklist — 中文也行'
        md = skill_store.build_skill_md("demo", desc, ["a", "b"], "正文第一行")
        meta, body = skill_store.parse_frontmatter(md)
        self.assertEqual(meta["name"], "demo")
        self.assertEqual(meta["description"], desc)
        self.assertEqual(meta["tags"], ["a", "b"])
        self.assertIn("正文第一行", body)

    def test_name_validation(self):
        for bad in ("", "  x", "..", ".hidden", "a/b", "x" * 65, "bad name"):
            with self.subTest(bad=bad):
                self.assertTrue(skill_store.validate_name(bad), f"{bad!r} 应被拒")
        for good in ("pdf", "my-skill", "a.b_c-1"):
            with self.subTest(good=good):
                self.assertEqual(skill_store.validate_name(good), [])

    def test_unique_name_dedup(self):
        self.assertEqual(skill_store.unique_name("pdf", []), "pdf")
        self.assertEqual(skill_store.unique_name("pdf", ["pdf"]), "pdf-2")
        self.assertEqual(skill_store.unique_name("pdf", ["pdf", "pdf-2"]), "pdf-3")
        # 非白名单字符要折掉，结果仍必须是合法目录名
        self.assertEqual(skill_store.validate_name(skill_store.unique_name("My Skill!", [])), [])

    def test_safe_relpath(self):
        self.assertEqual(skill_store.safe_relpath("./a/b.md"), "a/b.md")
        self.assertEqual(skill_store.safe_relpath("a\\b.md"), "a/b.md")
        for bad in ("/abs", "../up", "a/../../b", "", "C:/x", "a\x00b"):
            with self.subTest(bad=bad):
                self.assertIsNone(skill_store.safe_relpath(bad), f"{bad!r} 应被拒")


class TestSkillStore(unittest.TestCase):
    """3 / 6 / 7：启停只写旁路、安装原子性、路径安全。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.skills = self.tmp / "skills"
        self.skills.mkdir()
        self.sources = self.tmp / "skills_sources.json"
        self.store = skill_store.SkillStore(self.skills, self.sources)

    def test_scan_includes_disabled_and_reports_meta(self):
        _write_skill(self.skills, "alpha", "做 A 事")
        _write_skill(self.skills, "beta", "做 B 事")
        self.store.set_enabled("beta", False)
        rows = {r["name"]: r for r in self.store.scan()}
        self.assertEqual(sorted(rows), ["alpha", "beta"])
        self.assertTrue(rows["alpha"]["enabled"])
        self.assertFalse(rows["beta"]["enabled"])
        self.assertEqual(rows["alpha"]["description"], "做 A 事")
        self.assertTrue(rows["alpha"]["has_manifest"])
        self.assertIn("SKILL.md", rows["alpha"]["files"])

    def test_set_enabled_does_not_touch_skill_md(self):
        d = _write_skill(self.skills, "alpha")
        before = (d / "SKILL.md").read_bytes()
        self.store.set_enabled("alpha", False)
        self.store.set_enabled("alpha", True)
        self.assertEqual((d / "SKILL.md").read_bytes(), before,
                         "启停绝不能改写用户的 SKILL.md")

    def test_set_enabled_unknown_name_raises(self):
        with self.assertRaises(ValueError):
            self.store.set_enabled("nope", False)

    def test_iter_manifests_only_enabled(self):
        _write_skill(self.skills, "alpha")
        _write_skill(self.skills, "beta")
        self.store.set_enabled("beta", False)
        names = [n for n, _ in self.store.iter_manifests()]
        self.assertEqual(names, ["alpha"])

    def test_scan_survives_missing_manifest(self):
        (self.skills / "broken").mkdir()
        rows = {r["name"]: r for r in self.store.scan()}
        self.assertFalse(rows["broken"]["has_manifest"])
        self.assertTrue(rows["broken"]["warnings"], "缺 SKILL.md 必须给出原因")

    def test_remove_is_idempotent_and_drops_metadata(self):
        _write_skill(self.skills, "alpha")
        self.store.set_enabled("alpha", False)
        rows, warnings = self.store.remove("alpha")
        self.assertEqual(rows, [])
        self.assertEqual(warnings, [])
        self.assertFalse((self.skills / "alpha").exists())
        self.assertNotIn("alpha", self.store.load_sources())
        # 再删一次不报错
        rows, _ = self.store.remove("alpha")
        self.assertEqual(rows, [])

    def test_remove_symlink_only_unlinks(self):
        """符号链接只删链接本身 —— 跟随删会端掉链接指向的真实目录。"""
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        (self.skills / "linked").symlink_to(outside, target_is_directory=True)
        _, warnings = self.store.remove("linked")
        self.assertFalse((self.skills / "linked").exists())
        self.assertTrue((outside / "keep.txt").exists(), "目标目录绝不能被动到")
        self.assertTrue(warnings, "只删了链接应给出说明")

    def test_install_writes_files_and_metadata(self):
        rows = self.store.install("gamma", {
            "SKILL.md": "---\nname: gamma\ndescription: G\n---\n\n正文",
            "scripts/run.py": "print('hi')\n",
        }, {"source": "market", "market_id": "m:1"})
        row = next(r for r in rows if r["name"] == "gamma")
        self.assertTrue(row["enabled"])
        self.assertEqual(row["source"], "market")
        self.assertEqual(row["market_id"], "m:1")
        self.assertEqual(sorted(row["files"]), ["SKILL.md", "scripts/run.py"])
        self.assertTrue((self.skills / "gamma" / "scripts" / "run.py").is_file())

    def test_install_rejects_duplicate(self):
        _write_skill(self.skills, "gamma")
        with self.assertRaises(ValueError):
            self.store.install("gamma", {"SKILL.md": "x"})

    def test_install_validation_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.store.install("delta", {"other.md": "no manifest"})
        self.assertFalse((self.skills / "delta").exists())
        with self.assertRaises(ValueError):
            self.store.install("delta", {"../escape.md": "x", "SKILL.md": "y"})
        self.assertFalse((self.skills / "delta").exists())
        self.assertFalse((self.tmp / "escape.md").exists())

    def test_install_rollback_on_write_failure(self):
        """写到一半失败必须回滚 —— 半装目录比装不上更难排查。"""
        real_write = Path.write_text

        def boom(self, *a, **kw):
            if self.name == "SKILL.md":
                raise OSError("disk full")
            return real_write(self, *a, **kw)

        with mock.patch.object(Path, "write_text", boom):
            with self.assertRaises(OSError):
                self.store.install("eps", {"SKILL.md": "x"})
        self.assertFalse((self.skills / "eps").exists(), "失败后不该留下半个目录")

    def test_corrupt_sources_detected_by_strict_loader(self):
        self.sources.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            skill_store.load_sources_strict(self.sources)
        # 宽容路径只降级、不抛
        self.assertEqual(self.store.load_sources(), {})


class TestPluginStore(unittest.TestCase):
    """5 / 6：组件清单、命名空间、启停。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plugins = self.tmp / "plugins"
        self.plugins.mkdir()
        self.store = plugin_store.PluginStore(self.plugins, self.tmp / "sources.json")

    def test_inventory_unions_disk_and_manifest(self):
        _write_plugin(self.plugins, "combo", skills=["s1", "s2"], commands=["c1"],
                      hooks={"PreToolUse": [{"a": 1}, {"b": 2}]}, mcp={"srv": {}})
        p = self.store.scan()[0]
        self.assertEqual(p["components"]["skills"], ["s1", "s2"])
        self.assertEqual(p["components"]["commands"], ["c1"])
        self.assertEqual(len(p["components"]["hooks"]), 1)
        self.assertIn("PreToolUse", p["components"]["hooks"][0])
        self.assertIn("（2 条）", p["components"]["hooks"][0])
        self.assertEqual(p["components"]["mcp_servers"], ["srv"])
        self.assertTrue(p["has_manifest"])
        self.assertEqual(p["wired"], ["skills"])

    def test_missing_manifest_is_reported_not_hidden(self):
        _write_plugin(self.plugins, "nomanifest", skills=["s1"], manifest=False)
        p = self.store.scan()[0]
        self.assertFalse(p["has_manifest"])
        self.assertTrue(p["warnings"])
        # 清单缺失 → 组件清单必须清空（不能展示"仿佛有效"的技能）
        self.assertEqual(p["components"]["skills"], [])

    def test_plugin_skills_are_namespaced_and_respect_enabled(self):
        _write_plugin(self.plugins, "pack", skills=["review"])
        _write_plugin(self.plugins, "other", skills=["review"])
        names = sorted(n for n, _ in self.store.iter_skill_manifests())
        self.assertEqual(names, ["other:review", "pack:review"],
                         "不同插件的同名技能必须靠命名空间区分")
        self.store.set_enabled("pack", False)
        self.assertEqual([n for n, _ in self.store.iter_skill_manifests()], ["other:review"])

    def test_install_requires_plugin_json(self):
        with self.assertRaises(ValueError):
            self.store.install("bad", {"README.md": "x"})
        self.assertFalse((self.plugins / "bad").exists())

    def test_install_and_remove_roundtrip(self):
        rows = self.store.install("pack", {
            ".claude-plugin/plugin.json": json.dumps({"name": "pack"}),
            "skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\n\nbody",
        }, {"source": "market", "market_id": "m:p"})
        row = next(r for r in rows if r["name"] == "pack")
        self.assertEqual(row["source"], "market")
        rows, _ = self.store.remove("pack")
        self.assertEqual(rows, [])
        self.assertFalse((self.plugins / "pack").exists())


class TestSkillLoaderIntegration(unittest.TestCase):
    """4 / 5：引擎侧真的按启停与插件贡献来构建技能表。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.skills = self.tmp / "skills"
        self.skills.mkdir()
        self.plugins = self.tmp / "plugins"
        self.plugins.mkdir()
        self.sources = self.tmp / "skills_sources.json"
        self.store = skill_store.SkillStore(self.skills, self.sources)
        # 让 skills.py 扫到临时插件目录（否则会读真实的 ~/.aigent/plugins）
        self._p1 = mock.patch.object(plugin_store, "PLUGINS_DIR", self.plugins)
        self._p2 = mock.patch.object(plugin_store, "PLUGIN_SOURCES",
                                     self.tmp / "plugins_sources.json")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p2.stop()
        self._p1.stop()

    def test_disabled_and_deleted_disappear_immediately(self):
        _write_skill(self.skills, "alpha", "A 事")
        _write_skill(self.skills, "beta", "B 事")
        loader = SkillLoader(self.skills)
        self.assertEqual(sorted(loader.SKILL_REGISTRY), ["alpha", "beta"])

        self.store.set_enabled("beta", False)
        loader.list_skills_compact()  # 触发重扫
        self.assertEqual(sorted(loader.SKILL_REGISTRY), ["alpha"])

        # 删除必须立刻生效（历史实现"只增不删"会让删除按钮变成假的）
        self.store.remove("alpha")
        loader.list_skills_compact()
        self.assertEqual(sorted(loader.SKILL_REGISTRY), [])

    def test_plugin_contributed_skill_is_loaded_and_namespaced(self):
        _write_skill(self.skills, "alpha", "本地技能")
        _write_plugin(self.plugins, "pack", skills=["review"])
        loader = SkillLoader(self.skills)
        self.assertEqual(sorted(loader.SKILL_REGISTRY), ["alpha", "pack:review"])
        self.assertEqual(loader.SKILL_REGISTRY["pack:review"]["source"], "plugin")
        # load_skill 必须能按注册表里的键取到正文
        self.assertIn("review", loader.load_skill("pack:review"))

    def test_local_skill_id_is_unchanged(self):
        """内置技能的注册表键仍是 frontmatter 的 name —— 换了键会让既有会话里
        模型记住的技能名失效。"""
        _write_skill(self.skills, "dir-name", "描述")
        loader = SkillLoader(self.skills)
        self.assertIn("dir-name", loader.SKILL_REGISTRY)

    def test_plugin_skill_vanishes_when_plugin_disabled(self):
        _write_plugin(self.plugins, "pack", skills=["review"])
        plugin_store.PluginStore(self.plugins,
                                 self.tmp / "plugins_sources.json").set_enabled("pack", False)
        loader = SkillLoader(self.skills)
        self.assertEqual(sorted(loader.SKILL_REGISTRY), [])


class TestMarketRegistries(unittest.TestCase):
    """8：市场源的规则（内置源 / 校验 / 未知源不抛）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.skill_markets = self.tmp / "skill_markets.json"
        self.plugin_markets = self.tmp / "plugin_markets.json"

    def test_skill_builtins_present_and_only_toggleable(self):
        rows = skill_market.list_markets(self.skill_markets)
        ids = [r["id"] for r in rows]
        self.assertIn(skill_market.DEFAULT_MARKET_ID, ids)
        self.assertTrue(all(r["builtin"] for r in rows))
        # 改内置源的非 enabled 字段 → 一律丢弃（身份以代码为准）
        skill_market.upsert_market({"id": "anthropics-official", "name": "黑客",
                                    "type": "git", "repo": "evil/repo", "enabled": 0},
                                   self.skill_markets)
        row = next(r for r in skill_market.list_markets(self.skill_markets)
                   if r["id"] == "anthropics-official")
        self.assertEqual(row["name"], "Anthropic 官方技能")
        self.assertEqual(row["repo"], "anthropics/skills")
        self.assertFalse(row["enabled"])

    def test_skill_custom_market_crud(self):
        skill_market.upsert_market({"id": "team", "name": "团队技能", "type": "git",
                                    "repo": "acme/skills", "enabled": 1},
                                   self.skill_markets)
        self.assertIn("team", [r["id"] for r in skill_market.list_markets(self.skill_markets)])
        rows, warnings = skill_market.remove_market("team", self.skill_markets)
        self.assertEqual(warnings, [])
        self.assertNotIn("team", [r["id"] for r in rows])
        # 内置源拒绝删除 → 有明确原因，不是静默忽略
        rows, warnings = skill_market.remove_market("anthropics-official", self.skill_markets)
        self.assertTrue(warnings)
        self.assertIn("anthropics-official", [r["id"] for r in rows])

    def test_skill_market_validation(self):
        self.assertTrue(skill_market.validate_market({"id": "x"}))
        self.assertTrue(skill_market.validate_market(
            {"id": "x", "name": "n", "type": "git", "repo": ""}))
        self.assertTrue(skill_market.validate_market(
            {"id": "x", "name": "n", "type": "api", "provider": "nope"}))
        self.assertTrue(skill_market.validate_market(
            {"id": "x", "name": "n", "type": "index", "url": "ftp://x"}))
        self.assertEqual(skill_market.validate_market(
            {"id": "x", "name": "n", "type": "git", "repo": "a/b"}), [])

    def test_skill_search_unknown_market_returns_error_not_raise(self):
        """分发链没有兜底 try —— 这里抛出去会掀掉整条 WS 连接。"""
        res = skill_market.search("nope", "", "", None, self.skill_markets)
        self.assertEqual(res["items"], [])
        self.assertTrue(res["error"])
        self.assertEqual(res["market_id"], "nope")

    def test_skill_search_disabled_market_is_refused_with_reason(self):
        skill_market.upsert_market({"id": "anthropics-official", "enabled": 0},
                                   self.skill_markets)
        res = skill_market.search("anthropics-official", "", "", None, self.skill_markets)
        self.assertTrue(res["error"])
        self.assertIn("停用", res["error"])

    def test_plan_fail_shapes_are_stable(self):
        """前端按固定键取值 —— 失败载荷少一个键就会渲染出 undefined。"""
        for key in ("ok", "name", "warnings", "unsupported", "error", "meta",
                    "files", "file_count", "total_bytes"):
            with self.subTest(key=key):
                self.assertIn(key, skill_market.plan_fail("x"))
                self.assertIn(key, plugin_market.plan_fail("x"))
        self.assertFalse(skill_market.plan_fail("x")["ok"])
        self.assertFalse(plugin_market.plan_fail("x")["ok"])
        self.assertEqual(skill_market.plan_fail("boom")["unsupported"], "boom")
        self.assertEqual(plugin_market.plan_fail("boom")["unsupported"], "boom")
        # 插件计划额外要带组件清单的键（确认页直接读它渲染）
        for key in ("components", "component_counts", "wired", "skill_previews", "plugin_json"):
            self.assertIn(key, plugin_market.plan_fail("x"))

    def test_plugin_market_registry_and_validation(self):
        rows = plugin_market.list_markets(self.plugin_markets)
        self.assertIn(plugin_market.DEFAULT_MARKET_ID, [r["id"] for r in rows])
        self.assertTrue(plugin_market.validate_market(
            {"id": "x", "name": "n", "repo": ""}))
        self.assertTrue(plugin_market.validate_market(
            {"id": "x", "name": "n", "repo": "not a repo!!"}))
        self.assertEqual(plugin_market.validate_market(
            {"id": "x", "name": "n", "repo": "acme/plugins"}), [])
        rows, warnings = plugin_market.remove_market(
            plugin_market.DEFAULT_MARKET_ID, self.plugin_markets)
        self.assertTrue(warnings)
        self.assertIn(plugin_market.DEFAULT_MARKET_ID, [r["id"] for r in rows])

    def test_plugin_source_four_forms_and_local_rejection(self):
        market = {"id": "m", "name": "m", "repo": "anthropics/claude-plugins-official",
                  "ref": "main"}
        cases = [
            ("./plugins/x", ("anthropics/claude-plugins-official", "main",
                             "plugins/x", "relpath", "")),
            ({"source": "url", "url": "https://github.com/o/r.git", "sha": "abc"},
             ("o/r", "abc", "", "url", "")),
            ({"source": "git-subdir", "url": "https://github.com/o/r.git",
              "path": "plugins/p", "ref": "v1"},
             ("o/r", "v1", "plugins/p", "git-subdir", "")),
            ({"source": "github", "repo": "o/r"},
             ("o/r", "main", "", "github", "")),
        ]
        for src, expect in cases:
            with self.subTest(src=src):
                self.assertEqual(plugin_market._source_of(src, market), expect)

        for src in ("/abs/path", "~/local", "./../up",
                    {"source": "local", "path": "./x"}, {"source": "weird"}, ""):
            with self.subTest(src=src):
                repo, ref, prefix, kind, reason = plugin_market._source_of(src, market)
                self.assertEqual(kind, "invalid")
                self.assertTrue(reason, "拒绝必须给原因，不能静默忽略")

    def test_plugin_search_unknown_market_returns_error_not_raise(self):
        res = plugin_market.search("nope", "", "", None, self.plugin_markets)
        self.assertEqual(res["items"], [])
        self.assertTrue(res["error"])

    def test_plugin_inventory_of_files_root_skill(self):
        inv = plugin_store.inventory_of_files({"SKILL.md": "", "plugin.json": ""}, {})
        self.assertEqual(inv["skills"], ["."])
        inv2 = plugin_store.inventory_of_files(
            {"commands/a.md": "", "agents/b.md": ""}, {})
        self.assertEqual(inv2["commands"], ["a"])
        self.assertEqual(inv2["agents"], ["b"])


class TestDefaultLayout(unittest.TestCase):
    """默认落点必须与 `paths` 里的常量**逐字等价**。

    两个 store 的元数据路径是"跟着目录走"算出来的（自定义目录 → 元数据在旁边），
    这条等价是那个设计的正确性前提 —— 一旦某天有人把 SKILLS_DIR 挪到别的层级，
    这里会立刻红，而不是悄悄变成"技能读 A 目录、启停写 B 文件"。
    """

    def test_skill_store_defaults_match_paths_constants(self):
        import paths  # noqa: PLC0415

        store = skill_store.SkillStore()
        self.assertEqual(store.skills_dir, paths.SKILLS_DIR)
        self.assertEqual(store.sources_path, paths.SKILL_SOURCES)

    def test_plugin_store_defaults_match_paths_constants(self):
        import paths  # noqa: PLC0415

        store = plugin_store.PluginStore()
        self.assertEqual(store.plugins_dir, paths.PLUGINS_DIR)
        self.assertEqual(store.sources_path, paths.PLUGIN_SOURCES)

    def test_custom_dir_gets_self_consistent_metadata_path(self):
        tmp = Path(tempfile.mkdtemp())
        store = skill_store.SkillStore(tmp / "my-skills")
        self.assertEqual(store.sources_path, tmp / "skills_sources.json")


class TestStoreIO(unittest.TestCase):
    """store_io 的错误语义与 MCP 侧逐字一致（读严 / 宽松 / 启用判定）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_strict_read_semantics(self):
        p = self.tmp / "a.json"
        self.assertEqual(store_io.read_json_strict(p, {}), {})
        p.write_text("[1,2]", encoding="utf-8")
        with self.assertRaises(ValueError):
            store_io.read_json_strict(p, {}, expect=dict)
        p.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            store_io.read_json_strict(p, {})

    def test_lenient_read_degrades(self):
        p = self.tmp / "b.json"
        p.write_text("{broken", encoding="utf-8")
        self.assertEqual(store_io.read_json_lenient(p, {}), {})

    def test_write_is_atomic_and_private(self):
        p = self.tmp / "c.json"
        store_io.write_json_atomic(p, {"k": "v"})
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"k": "v"})
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual([f.name for f in self.tmp.iterdir()], ["c.json"],
                         "原子写不该留下 .tmp 残件")

    def test_is_enabled_matches_mcp_store(self):
        import mcp_store  # noqa: PLC0415 - 只在断言两处口径时导入

        for value in (0, "0", False, "false", 1, "1", True, "", None, "False"):
            with self.subTest(value=value):
                self.assertEqual(store_io.is_enabled(value), mcp_store.is_enabled(value))


class TestRawChannelFallback(unittest.TestCase):
    """raw 抓取的多通道回退（2026-09-30 修复）。

    根因：国内网络 raw.githubusercontent.com 整段挂起（TLS 层 0 字节超时）而
    api.github.com 可达 → 「市场搜得到条目、点安装没反应」。锁住四件事：
    URL 构造（含 jsDelivr 跳过带斜杠分支）、失败换通道、404 不换通道、
    成功通道被记住（避免每个文件都重新撞一次超时）。
    """

    def setUp(self):
        self._saved = dict(skill_market._raw_state)
        skill_market._raw_state["good"] = ""
        skill_market._raw_state["dead_until"] = {}

    def tearDown(self):
        skill_market._raw_state.clear()
        skill_market._raw_state.update(self._saved)

    @staticmethod
    def _client_cls(resps):
        """按调用顺序出响应的假 httpx.Client（元素是状态码或异常）。"""
        calls: list[str] = []

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **k):
                calls.append(url)
                item = resps[len(calls) - 1]
                if isinstance(item, Exception):
                    raise item
                r = mock.Mock()
                r.status_code = item
                r.content = b"hello"
                return r

        _FakeClient.calls = calls
        return _FakeClient

    def test_raw_url_for(self):
        self.assertEqual(
            skill_market._raw_url_for(
                "https://raw.githubusercontent.com", "o/r", "main", "a/b.md"),
            "https://raw.githubusercontent.com/o/r/main/a/b.md")
        self.assertEqual(
            skill_market._raw_url_for(
                "https://cdn.jsdelivr.net/gh", "o/r", "main", "a/b.md"),
            "https://cdn.jsdelivr.net/gh/o/r@main/a/b.md")
        self.assertIsNone(
            skill_market._raw_url_for(
                "https://cdn.jsdelivr.net/gh", "o/r", "feat/x", "a.md"),
            "jsDelivr 的 @ref 表达不了带斜杠的分支 → 跳过该通道而不是拼出错误 URL")

    def test_falls_back_to_mirror_on_connect_error(self):
        cls = self._client_cls([httpx.ConnectTimeout("dead"), 200])
        with mock.patch.object(skill_market.httpx, "Client", cls):
            data = skill_market._raw_bytes("o/r", "main", "SKILL.md")
        self.assertEqual(data, b"hello")
        self.assertEqual(len(cls.calls), 2)
        self.assertTrue(cls.calls[0].startswith("https://raw.githubusercontent.com/"))
        self.assertTrue(cls.calls[1].startswith("https://cdn.jsdelivr.net/gh/"))
        self.assertEqual(skill_market._raw_state["good"], "https://cdn.jsdelivr.net/gh")

    def test_good_channel_is_remembered(self):
        cls = self._client_cls([200])
        with mock.patch.object(skill_market.httpx, "Client", cls):
            skill_market._raw_state["good"] = "https://cdn.jsdelivr.net/gh"
            data = skill_market._raw_bytes("o/r", "main", "SKILL.md")
        self.assertEqual(data, b"hello")
        self.assertEqual(len(cls.calls), 1, "成功通道要被记住：下一次直接走它")
        self.assertTrue(cls.calls[0].startswith("https://cdn.jsdelivr.net/gh/"))

    def test_404_does_not_retry_other_channels(self):
        cls = self._client_cls([404])
        with mock.patch.object(skill_market.httpx, "Client", cls):
            data = skill_market._raw_bytes("o/r", "main", "nope.md")
        self.assertIsNone(data)
        self.assertEqual(len(cls.calls), 1,
                         "404 是确定结果：换通道也不会有，不该把每次 404 变成两倍请求")

    def test_all_channels_dead_returns_none(self):
        cls = self._client_cls(
            [httpx.ConnectTimeout("dead"), httpx.ConnectTimeout("dead")])
        with mock.patch.object(skill_market.httpx, "Client", cls):
            data = skill_market._raw_bytes("o/r", "main", "SKILL.md")
        self.assertIsNone(data)
        for base in skill_market._RAW_BASES:
            self.assertIn(base, skill_market._raw_state["dead_until"],
                          "失败通道要进冷却，别让后续调用重新撞超时")


if __name__ == "__main__":
    unittest.main()
