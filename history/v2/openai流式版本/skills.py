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
import yaml


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
        """Parse YAML frontmatter from SKILL.md. Returns (meta, body)."""
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, parts[2].strip()

    

    def _scan_skills(self):
        """Scan skills/ dir, populate SKILL_REGISTRY with name/description/content."""
        if not self.SKILLS_DIR.exists():
            return
        for d in sorted(self.SKILLS_DIR.iterdir()):
            if not d.is_dir():
                continue
            manifest = d / "SKILL.md"
            if manifest.exists():
                raw = manifest.read_text()
                meta, body = self._parse_frontmatter(raw)
                name = meta.get("name", d.name)
                desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
                self.SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}


    def list_skills(self) -> str:
        """List all skills (name + one-line description)."""
        #因为类在初始化时已经扫描加载，当前方法是为了实时获取最新的技能列表，后期可增加定时刷新而不是每次调用都刷新
        self._scan_skills()
        if not self.SKILL_REGISTRY:
            return "(no skills found)"
        return "\n".join(f"- **{s['name']}**: {s['description']}" for s in self.SKILL_REGISTRY.values())


    def load_skill(self, name: str) -> str:
        """Load full skill content. Lookup via registry — no path traversal."""
        skill = self.SKILL_REGISTRY.get(name)
        if not skill:
            return f"Skill not found: {name}"
        return skill["content"]

