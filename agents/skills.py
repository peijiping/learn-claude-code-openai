#!/usr/bin/env python3
"""
技能加载器模块

该模块实现了两层技能注入机制：
- 第一层：系统提示中仅包含技能名称和简短描述（低成本）
- 第二层：按需加载完整技能内容（tool_result中返回）

技能文件存放在 skills/<name>/SKILL.md 目录中，采用 YAML frontmatter 格式：
  ---
  name: skill-name
  description: 技能描述
  tags: tag1, tag2
  ---
  技能正文内容...
"""

import re
from pathlib import Path
# `yaml` 的解析已收口到 `skill_store.parse_frontmatter`（全项目唯一实现），本模块
# 不再直接依赖它 —— 这样 `skills.py` 只依赖标准库，单独 import 的门槛更低。


class SkillLoader:
    """
    技能加载器

    扫描 skills/ 目录下的所有 SKILL.md 文件，
    解析其中的 YAML frontmatter 元数据，
    并提供两层访问接口：
    - get_descriptions(): 获取简短描述用于系统提示
    - get_content(): 获取完整内容用于按需加载
    """

    def __init__(self, skills_dir: Path):
        """
        初始化技能加载器

        Args:
            skills_dir: 技能目录路径（包含多个 <name>/SKILL.md 结构）
        """
        self.SKILLS_DIR = skills_dir      # 技能根目录
        # Build skill registry at startup (used for safe lookup in load_skill)
        self.SKILL_REGISTRY: dict[str, dict] = {}
        self._scan_skills()                  # 启动时自动加载所有技能


    # s07: Skill catalog scan (used by build_system below)
    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        """Parse YAML frontmatter from SKILL.md. Returns (meta, body).

        **委托给 `skill_store.parse_frontmatter`（全项目唯一实现）**：
        设置页要显示每个技能的 description，那份解析结果必须与模型看到的**逐字一致**
        —— 两份实现必然漂移，而这类不一致最难排查。保留本方法只是为了让既有调用方
        （含教程代码与单测）不用改。
        """
        from skill_store import parse_frontmatter

        return parse_frontmatter(text)

    def _iter_manifests(self):
        """产出 `(技能名, SKILL.md 路径)`：内置技能目录（**只收启用的**）+ 插件贡献的。

        为什么"启停"与"插件贡献"都不在这里自己判定：那是 `skill_store` /
        `plugin_store` 的职责（它们是设置页的读写门面）。**判据只能有一处** ——
        两处各判一次必然漂移，漂移的后果是"设置页显示已禁用、模型却照旧能看到"。

        导入放在函数内是刻意的：`skills.py` 要保持"只依赖 yaml 就能 import"
        （教程代码、部分单测会单独 import 它，那时 `paths`/`config` 未必可加载）。
        真出问题时降级回"直接扫目录"的老行为 —— 技能全部消失比"启停失效"严重得多。
        """
        try:
            from plugin_store import PluginStore
            from skill_store import SkillStore
        except Exception:  # noqa: BLE001 - 导入失败只降级，不能让技能整体消失
            if self.SKILLS_DIR.exists():
                for d in sorted(self.SKILLS_DIR.iterdir()):
                    if d.is_dir() and (d / "SKILL.md").exists():
                        yield d.name, d / "SKILL.md"
            return

        for name, manifest in SkillStore(self.SKILLS_DIR).iter_manifests():
            yield name, manifest
        try:
            for name, manifest in PluginStore().iter_skill_manifests():
                yield name, manifest
        except Exception:  # noqa: BLE001 - 插件扫描失败不影响内置技能
            return

    def _scan_skills(self):
        """扫描技能来源，重建 `SKILL_REGISTRY`（name / description / content）。

        ⚠️ **必须整体重建（先清空）**：本方法会被 `list_skills()` 反复调用，而
        「删除技能 / 禁用技能 / 卸插件」都要**立刻**生效。历史实现是只增不删 ——
        那时没有删除功能所以看不出来，现在会让被删掉的技能一直留在系统提示里，
        等于删除按钮是假的。

        插件贡献的技能带 `<插件名>:<技能名>` 前缀（与 Claude Code 的命名规则一致）：
        插件之间技能重名是常态（各家都爱叫 `code-review`），不加前缀必然互相覆盖。
        """
        self.SKILL_REGISTRY = {}
        for name, manifest in self._iter_manifests():
            try:
                raw = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                # 单个技能读不出来就跳过它，不能让一个坏文件把整张技能表清空
                continue
            meta, _body = self._parse_frontmatter(raw)

            # 内置技能：**键仍是 frontmatter 的 name**（与历史实现逐字一致 —— 换了
            # 键会让既有会话里模型记住的技能名失效）。插件技能则强制加 `<插件>:` 前缀
            # （各家插件都爱叫 `code-review`，不加前缀必然互相覆盖）。
            plugin, sep, local = name.partition(":")
            base = local if sep else name
            display = str(meta.get("name") or base).strip() or base
            key = f"{plugin}:{display}" if sep else display
            desc = str(meta.get("description") or "").strip() \
                or (raw.split("\n")[0].lstrip("#").strip())
            self.SKILL_REGISTRY[key] = {
                "name": key,
                "description": desc,
                "content": raw,
                "path": str(manifest),
                "source": "plugin" if sep else "local",
            }


    def list_skills(self) -> str:
        """List all skills (name + one-line description)."""
        #因为类在初始化时已经扫描加载，当前方法是为了实时获取最新的技能列表，后期可增加定时刷新而不是每次调用都刷新
        self._scan_skills()
        if not self.SKILL_REGISTRY:
            return "(no skills found)"
        return "\n".join(f"- **{s['name']}**: {s['description']}" for s in self.SKILL_REGISTRY.values())

    # 句末标点（用于描述截断时优先停在语义完整处）
    _SENTENCE_ENDS = "。！？!?.;；"
    # 句末标点位置须 ≥ 预算的这个比例，才值得为"停在句末"牺牲后面的内容
    _SENTENCE_MIN_RATIO = 0.6

    @classmethod
    def _truncate_desc(cls, text: str, max_chars: int) -> str:
        """按字符预算截断描述，**避免把句子切成残句**（如 "...review cod…"）。

        优先级：
          ① 窗口内存在句末标点、且位置足够靠后（≥ 预算 × 0.6）→ 切在句末（语义完整）
          ② 否则退回最后一个词边界（避免切在单词中间）
          ③ 都没有 → 硬截
        ②③ 会补省略号，明确表示"还有下文"，而非句型断裂。
        """
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        window = text[:max_chars]
        cut = max(window.rfind(ch) for ch in cls._SENTENCE_ENDS)
        if cut >= int(max_chars * cls._SENTENCE_MIN_RATIO):
            return window[:cut + 1]
        space = window.rfind(" ")
        if space > 0:
            return window[:space].rstrip() + "…"
        return window.rstrip() + "…"

    def list_skills_compact(self, max_desc_chars: int = 120) -> str:
        """精简技能列表（名字 + 截断后的首行描述），供 system prompt 静态段使用。

        与 list_skills() 的分工：
        - 本方法用于 system prompt 静态段：描述截断，避免部分技能 frontmatter 里
          数百字的触发词清单**每轮**都占着缓存前缀（缓存命中是打折不是免费）。
        - list_skills() 用于 list_skills 工具：返回完整描述，由模型按需获取。

        截断策略见 `_truncate_desc()`：优先停在句末，其次退回词边界。
        无技能时返回空串，让 system prompt 的「空段整体跳过」生效。
        """
        self._scan_skills()
        lines = []
        for s in self.SKILL_REGISTRY.values():
            raw = (s.get("description") or "").strip().splitlines()
            first = raw[0].strip() if raw else ""
            lines.append(
                f"- **{s['name']}**: {self._truncate_desc(first, max_desc_chars)}"
            )
        return "\n".join(lines)


    def load_skill(self, name: str) -> str:
        """Load full skill content. Lookup via registry — no path traversal."""
        skill = self.SKILL_REGISTRY.get(name)
        if not skill:
            return f"Skill not found: {name}"
        return skill["content"]

