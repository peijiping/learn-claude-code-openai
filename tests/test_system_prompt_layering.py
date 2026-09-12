"""system prompt 分层重构 离线回归测试。

运行方式（无需额外依赖，pytest 未安装也能跑；装了 pytest 同样可收集）::

    cd /Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main
    .venv/bin/python -m unittest discover -s tests -v

覆盖内容（全部离线，不调 LLM、不读真实项目目录）：
  1. workspace 指令加载 —— 显式 workspace_dir 生效；AGENTS.md 在候选名里
  2. 缺文件不崩         —— 目录为空时整段消失，不抛异常
  3. 分段顺序           —— identity → tools → skills → memory规则 → workspace
  4. 工具名不再枚举     —— 冗余且会与实际下发集合漂移
  5. 记忆索引不在 prompt —— 结构上也无 memory 依赖
  6. boundary 已移除    —— OpenAI 兼容端点没有 cache_control，该标记无效果
  7. 技能列表精简       —— 长描述截断；list_skills() 仍返回完整描述
  8. 空技能段跳过       —— 无技能时该段整体消失
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from skills import SkillLoader  # noqa: E402
from tools import ToolRegistry  # noqa: E402
from system_prompt import (  # noqa: E402
    DEFAULT_WORKSPACE_FILES,
    SKILL_DESC_MAX_CHARS,
    SystemPromptBuilder,
)

# > 120 字符的单行描述。注意：不能含冒号（会破坏 YAML frontmatter），
# 且必须 strip（YAML 会去掉首尾空白，否则与解析回来的值比对不上）。
LONG_DESC = ("trigger " + "alpha beta gamma " * 30).strip()

SKILL_MD = "---\nname: {name}\ndescription: {desc}\n---\n\n正文内容\n"


def _make_skill_loader(base: Path, skills: list) -> SkillLoader:
    """在 base/skills/<name>/SKILL.md 造技能，返回 SkillLoader。"""
    skills_root = base / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    for name, desc in skills:
        d = skills_root / name
        d.mkdir(exist_ok=True)
        (d / "SKILL.md").write_text(
            SKILL_MD.format(name=name, desc=desc), encoding="utf-8"
        )
    return SkillLoader(skills_root)


def _make_builder(base: Path, skills: list = None):
    """构造 builder：技能目录与**工作空间**都落在 base 下，与真实环境隔离。

    注意：工作区指令文件（AGENTS.md / CLAUDE.md / AGENT.md）是**从 workdir 读**的，
    所以测试要把它们放进 base（base 即 workdir）。
    """
    loader = _make_skill_loader(base, skills if skills else [])
    return SystemPromptBuilder(
        workdir=base,
        skills=loader,
        tools=ToolRegistry(),
    )


class WorkspaceInstructionTests(unittest.TestCase):
    """指令文件加载：来源必须是**工作空间**（workdir）。"""

    def test_agents_md_loaded_from_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "AGENTS.md").write_text("# 项目规则\n唯一标记-A7X\n", encoding="utf-8")
            prompt = _make_builder(base).build_system_prompt()

            self.assertIn("以下是工作区根目录下的 AGENTS.md 文件内容", prompt)
            self.assertIn("唯一标记-A7X", prompt)

    def test_agents_md_is_first_default_filename(self):
        self.assertEqual(DEFAULT_WORKSPACE_FILES[0], "AGENTS.md")
        self.assertIn("CLAUDE.md", DEFAULT_WORKSPACE_FILES)
        self.assertIn("AGENT.md", DEFAULT_WORKSPACE_FILES)

    def test_multiple_instruction_files_all_loaded(self):
        """CLAUDE.md + AGENT.md 并存时两个都要加载（workspace 约定见 task1）。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "CLAUDE.md").write_text("规则来自-CLAUDE\n", encoding="utf-8")
            (base / "AGENT.md").write_text("规则来自-AGENT\n", encoding="utf-8")
            prompt = _make_builder(base).build_system_prompt()

            self.assertIn("规则来自-CLAUDE", prompt)
            self.assertIn("规则来自-AGENT", prompt)

    def test_workspace_resolution_uses_workdir(self):
        """回归：不能再从 chat_history_dir / 仓库根之类的外部路径反推。"""
        with tempfile.TemporaryDirectory() as td_a, tempfile.TemporaryDirectory() as td_b:
            base_a, base_b = Path(td_a), Path(td_b)
            (base_a / "AGENTS.md").write_text("来自A\n", encoding="utf-8")
            (base_b / "AGENTS.md").write_text("来自B\n", encoding="utf-8")

            pa = _make_builder(base_a).build_system_prompt()
            pb = _make_builder(base_b).build_system_prompt()

            self.assertIn("来自A", pa)
            self.assertNotIn("来自B", pa)
            self.assertIn("来自B", pb)

    def test_repo_root_agents_md_is_not_injected(self):
        """回归本 Bug：仓库根的 AGENTS.md 是给"开发本项目的编码助手"看的，
        不是给终端用户的助手看的 —— 工作空间为空时它不应被注入。
        """
        repo_agents = ROOT / "AGENTS.md"
        self.assertTrue(repo_agents.is_file(), "本仓库根确实有 AGENTS.md（前提）")
        self.assertIn("核心心法", repo_agents.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as td:
            prompt = _make_builder(Path(td)).build_system_prompt()

        self.assertNotIn("以下是工作区根目录下的", prompt)
        self.assertNotIn("核心心法", prompt)

    def test_missing_workspace_files_skips_section_without_crash(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = _make_builder(Path(td)).build_system_prompt()
            self.assertNotIn("以下是工作区根目录下的", prompt)

    def test_builder_takes_only_workdir_for_instructions(self):
        """构造参数收敛：不再有独立的 workspace_dir（避免"两个目录必须一致"的隐患）。"""
        with tempfile.TemporaryDirectory() as td:
            b = _make_builder(Path(td))
            self.assertFalse(hasattr(b, "memory"))
            self.assertFalse(hasattr(b, "chat_history_dir"))
            self.assertFalse(hasattr(b, "workspace_dir"))
            self.assertTrue(hasattr(b, "workdir"))


class LayeringTests(unittest.TestCase):
    """分层顺序与内容精简。"""

    def _prompt(self, skills=None, agents_md="# 规则\n"):
        self._td = tempfile.TemporaryDirectory()
        base = Path(self._td.name)
        if agents_md:
            (base / "AGENTS.md").write_text(agents_md, encoding="utf-8")
        self.addCleanup(self._td.cleanup)
        return _make_builder(base, skills=skills).build_system_prompt()

    def test_section_order_follows_change_frequency(self):
        prompt = self._prompt(skills=[("demo-skill", "一行描述")])
        order = [
            "# 回复输出格式（Markdown）",   # L0 identity
            "# 工具使用策略",               # L0 tools
            "# 技能（Skills）",             # L0 skills
            "# 记忆系统（memory）",         # L0 memory 规则
            "以下是工作区根目录下的",        # L1 workspace（最末尾）
        ]
        positions = [prompt.index(m) for m in order]
        self.assertEqual(positions, sorted(positions), "分段顺序必须按变化频率递增")

    def test_tool_names_not_enumerated(self):
        prompt = self._prompt()
        self.assertNotIn("可用工具：", prompt)
        self.assertNotIn("- bash（", prompt)
        self.assertIn("工具清单由 API 每轮下发", prompt)

    def test_memory_index_not_in_prompt(self):
        prompt = self._prompt()
        self.assertNotIn("当前已保存的记忆", prompt)
        self.assertIn("以对话上下文中的 `<memory_index>` 块为准", prompt)

    def test_static_boundary_removed(self):
        prompt = self._prompt()
        self.assertNotIn("DYNAMIC_BOUNDARY", prompt)

    def test_empty_skills_section_skipped(self):
        prompt = self._prompt(skills=[])
        self.assertNotIn("# 技能（Skills）", prompt)

    def test_long_skill_description_truncated_in_prompt(self):
        prompt = self._prompt(skills=[("long-skill", LONG_DESC)])
        self.assertIn("- **long-skill**:", prompt)
        self.assertNotIn(LONG_DESC, prompt)
        # 截断后长度受限（含省略号）
        line = next(
            ln for ln in prompt.splitlines() if ln.startswith("- **long-skill**")
        )
        self.assertLessEqual(len(line), SKILL_DESC_MAX_CHARS + len("- **long-skill**: ") + 1)
        self.assertTrue(line.endswith("…"))

    def test_truncation_stops_at_sentence_end(self):
        """句末标点足够靠后时，切在句末（语义完整），不硬截。"""
        desc = ("A" * 80) + ". " + ("B" * 200)
        out = SkillLoader._truncate_desc(desc, 120)
        self.assertEqual(out, ("A" * 80) + ".")
        self.assertFalse(out.endswith("…"))

    def test_truncation_backs_off_to_word_boundary(self):
        """没有可用句末标点时，退回词边界，不切在单词中间（回归 "...review cod…"）。"""
        out = SkillLoader._truncate_desc(LONG_DESC, 40)
        self.assertTrue(out.endswith("…"))
        self.assertRegex(out, r"(trigger|alpha|beta|gamma)…$")

    def test_truncation_hard_cut_when_no_boundary_at_all(self):
        out = SkillLoader._truncate_desc("C" * 200, 50)
        self.assertEqual(out, ("C" * 50) + "…")

    def test_truncation_keeps_short_description_untouched(self):
        self.assertEqual(SkillLoader._truncate_desc("short one", 120), "short one")
        self.assertEqual(SkillLoader._truncate_desc("short one", 0), "short one")

    def test_list_skills_tool_still_returns_full_description(self):
        """静态段精简，但 list_skills 工具必须仍拿得到完整描述。"""
        with tempfile.TemporaryDirectory() as td:
            loader = _make_skill_loader(Path(td), [("long-skill", LONG_DESC)])
            self.assertIn(LONG_DESC, loader.list_skills())
            self.assertNotIn(LONG_DESC, loader.list_skills_compact())

    def test_compact_list_empty_when_no_skills(self):
        with tempfile.TemporaryDirectory() as td:
            loader = _make_skill_loader(Path(td), [])
            self.assertEqual(loader.list_skills_compact(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
