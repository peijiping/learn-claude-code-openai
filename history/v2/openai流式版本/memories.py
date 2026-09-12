#!/usr/bin/env python3
"""
memories.py - 持久化记忆管理

模型可通过 write_memory / forget_memory 两个工具即时落盘记忆条目，
存储在 .memory/ 目录，索引由 MEMORY.md 维护。

设计要点（与 s09 课程保持一致）：
- 每条记忆是一个 *.md 文件，YAML frontmatter 记录 name / type / description
- 每次写/删后扫描目录重建 MEMORY.md 索引，注入 SYSTEM 提示
- 不再额外起 LLM 抽取/整合（由模型在调用 write_memory 时自决）
"""

from pathlib import Path

from paths import MEMORY_DIR


# ═══════════════════════════════════════════════════════════
#  MemoryStore：记忆文件 + 索引管理
# ═══════════════════════════════════════════════════════════

class MemoryStore:
    """
    记忆存储器。

    把模型通过 tool call 传来的条目落到 .memory/<slug>.md，
    并自动维护 MEMORY.md 索引，方便下次会话注入 SYSTEM。
    """

    INDEX_FILENAME = "MEMORY.md"

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.index_path = memory_dir / self.INDEX_FILENAME

    # ── frontmatter 解析 ───────────────────────────────────

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        """极简 YAML frontmatter 解析器。"""
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        meta = {}
        for line in parts[1].strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
        return meta, parts[2].strip()

    # ── 索引维护 ──────────────────────────────────────────

    def _rebuild_index(self) -> None:
        """扫描 memory_dir 下所有 *.md，重建 MEMORY.md 索引。"""
        lines = []
        for f in sorted(self.memory_dir.glob("*.md")):
            if f.name == self.INDEX_FILENAME:
                continue
            raw = f.read_text()
            meta, body = self._parse_frontmatter(raw)
            name = meta.get("name", f.stem)
            desc = meta.get("description", body.split("\n")[0][:80])
            lines.append(f"- [{name}]({f.name}) — {desc}")
        self.index_path.write_text("\n".join(lines) + "\n" if lines else "")

    def read_index(self) -> str:
        """读 MEMORY.md 索引全文。注入 SYSTEM 提示用。

        若索引缺失或为空（说明磁盘上的 *.md 与索引失同步），
        主动调用 _rebuild_index() 重建，保证 SYSTEM 提示永远反映真实状态。
        """
        if not self.index_path.exists() or not self.index_path.read_text().strip():
            self._rebuild_index()
        if not self.index_path.exists():
            return ""
        text = self.index_path.read_text().strip()
        return text if text else ""

    # ── 工具 handler ──────────────────────────────────────

    def write(self, name: str, mem_type: str, description: str, body: str) -> str:
        """
        write_memory 工具 handler。
        模型通过 tool call 调用此方法，即时落盘。
        """
        try:
            slug = name.lower().replace(" ", "-").replace("/", "-")
            filepath = self.memory_dir / f"{slug}.md"
            filepath.write_text(
                f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
            )
            self._rebuild_index()
            # print(f"\033[33m[Memory saved: {name} ({mem_type})]\033[0m")
            return f"Saved memory to {filepath.name}"
        except Exception as e:
            return f"Error saving memory: {e}"

    def forget(self, name: str) -> str:
        """
        forget_memory 工具 handler。
        模型通过 tool call 调用此方法删除一条记忆。
        """
        try:
            slug = name.lower().replace(" ", "-").replace("/", "-")
            path = self.memory_dir / f"{slug}.md"
            if not path.exists():
                # 也尝试直接按文件名加载（模型可能传已有 slug）
                path = self.memory_dir / name
                if not path.exists():
                    return f"Error: memory '{name}' not found"
            path.unlink()
            self._rebuild_index()
            # print(f"\033[33m[Memory deleted: {name}]\033[0m")
            return f"Deleted memory '{name}'"
        except Exception as e:
            return f"Error deleting memory: {e}"
