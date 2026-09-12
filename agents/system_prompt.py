"""主智能体系统提示词

把 SYSTEM prompt 从 agent_full_v2.py 抽出来，让主循环代码保持简洁。

分层原则（按「变化频率」从低到高排列，最大化前缀缓存命中率）：
- L0 冻结段：身份 / 输出格式 / 上下文保护规则、工具使用策略、技能列表。
  永不变化（除非改代码或装/删技能），所有会话逐字节相同 → 可跨会话共享前缀缓存。
- L1 冷段：workspace 指令文件（AGENTS.md 等）。改文件才变，故排在静态段最末尾。
- L2 热段：记忆索引**不在这里**。它每轮都可能变，由
  `agent_full_v2._sync_memory_index()` 在指纹变化时以**尾部追加**的方式注入
  （尾部追加不破坏已缓存前缀；改动 system message 会让整个前缀失效）。

缓存前提：本链路是 OpenAI SDK（chat.completions.create）+ OpenAI 兼容端点，
使用**自动前缀缓存**（按最长公共前缀逐字节命中），**没有** Anthropic 的 cache_control 参数。
因此这里没有「静态/动态边界标记」——那对当前链路不产生任何缓存效果。
"""

import hashlib
import os
from pathlib import Path

from paths import WORKDIR, SKILLS_DIR
from tools import ToolRegistry
from skills import SkillLoader


# workspace 指令文件候选名（按优先级），任一存在即加载。不递归子目录。
DEFAULT_WORKSPACE_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "AGENT.md")

# 技能列表在静态段中的单条描述最大字符数（超出截断为 …）。
# 完整描述仍可通过 list_skills 工具按需获取。
SKILL_DESC_MAX_CHARS = int(os.environ.get("SKILL_DESC_MAX_CHARS") or 120)


class SystemPromptBuilder:
    """组装主智能体 system prompt。

    依赖（tools / skills / workdir）在初始化时注入，
    调用方只关心 build_system_prompt() 即可拿到完整 prompt 字符串。
    实例方法即使不使用 self，也保持与其他 Manager 一致的类风格。
    """

    def __init__(
        self,
        workdir: Path = WORKDIR,
        skills: SkillLoader = None,
        tools: ToolRegistry = None,        # ToolRegistry 实例（Agent 传入）
        workspace_instruction_files: tuple[str, ...] = None,
    ):
        self.workdir = workdir
        self.tools = tools
        self.skills = skills if skills else SkillLoader(SKILLS_DIR)
        self.workspace_instruction_files = tuple(workspace_instruction_files) \
            if workspace_instruction_files else DEFAULT_WORKSPACE_FILES
        

    def _get_workspace_instructions(self) -> str:
        """
        读取**工作空间**（self.workdir）下的指令文件（AGENTS.md / CLAUDE.md / AGENT.md），
        拼成独立的 system 段（静态段 L1，排在 sections 最末尾）。
        不递归子目录；多个文件同时存在则**全部加载并拼接**。

        为什么是 workdir 而不是仓库根：
        本产物是**通用助手** —— 用户给它一个工作空间，规则就写在那个工作空间里
        （`WorkSpace/task1/` 下的 `CLAUDE.md` / `AGENT.md` 正是这类文件）。而本仓库根的
        `AGENTS.md` 是**给开发这个项目的编码助手看的**（改代码的约定），不是给终端用户的
        助手看的；何况工具操作本就沙盒在 workdir 内，注入仓库根的规则等于让模型背一堆
        它根本读不到的代码规范。

        多文件并存是设计内行为：task1 的 `AGENT.md` 明确写着"应参考本文件和 `CLAUDE.md`
        中的 workspace 规则；若两者有重复，优先遵循更具体、更贴近当前任务的说明"。
        """
        sections: list[str] = []
        for filename in self.workspace_instruction_files:
            instruction_file = self.workdir / filename
            if not instruction_file.is_file():
                continue
            try:
                content = instruction_file.read_text(encoding="utf-8")
            except Exception as e:
                print(f"读取 workspace 指令文件失败: {instruction_file}: {e}")
                continue
            sections.append(f"以下是工作区根目录下的 {filename} 文件内容：\n{content}\n")
        return "\n\n".join(sections)

    def get_workspace_instructions(self) -> str:
        """公开入口：当前 workspace 指令文件内容（空串 = 无指令文件）。

        供 `agent_full_v2._sync_project_rules()` 在指令文件于会话期间被改动时，
        把最新全文追加到消息尾部（方案 C）。
        """
        return self._get_workspace_instructions()

    @property
    def workspace_revision(self) -> str:
        """当前 workspace 指令内容的指纹（**每次读取都重新读盘**，不缓存）。

        空内容也有稳定指纹（`sha1("")`），因此"本来就没有指令文件"与
        "指令文件刚被删掉"能被区分开 —— 后者的指纹会与 system prompt 里那份不同，
        从而触发一次"已移除"的尾部注入。
        """
        body = self._get_workspace_instructions()
        return hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]

    def _get_identity(self) -> str:
        """L0 冻结段：身份定义 + 工作方式 + 输出格式 + 上下文保护规则（始终加载）。

        定位是**通用 AI 助手**，不限于编程：给定一个工作空间，写作 / 科研 / 编码 /
        数据分析 / 资料整理等任务都要能做。大模型本身是通用的，助手也应通用，
        因此这里不预设任务类型，只给判断方法与边界。

        不包含 workspace 指令文件内容（属 L1 冷段，见 _get_workspace_instructions），
        也不包含记忆索引（属 L2 热段，由 agent_full_v2 尾部注入）。
        """
        return f"""你是一个通用型 AI 助手：给定工作空间，写作 / 科研 / 编码 / 数据分析 / 资料整理等任务都要能做 —— 不预设任务类型。

# 工作空间
工作空间是 {self.workdir}，**所有文件操作仅限该空间内**（读写、检索、执行命令都以此为边界）。

# 工作方式
- 先判断任务性质（写作 / 研究 / 编码 / 分析 / 检索…）再选方法，不要一律按软件工程任务处理
- 关键信息不足（目标、交付形态、约束）时先问清，不要凭猜测长时间执行
- 内部动作（读文件、检索、整理、分析）放手做；外部动作（发邮件、发布内容、推送代码）先确认
- 如实汇报：没做成的、跳过的、有风险的都要说

# 回复输出格式（Markdown）
回复渲染在桌面客户端的 Markdown 界面中，**必须用 Markdown**：
- 代码 / 命令 / 路径用行内代码或代码块（代码块标注语言）；多字段、方案对比、参数说明优先用表格
- 引用文件位置用 `[文件名](file:///绝对路径#L行号)` 链接
- 段落之间留空行；不要用 HTML 标签排版

# 上下文保护规则（最高优先级）
- **禁止**对二进制文件（PDF、图片、压缩包）用 strings / cat / hexdump
- **禁止**一次读超过 500 行，用 limit 或 | head 控制
- **禁止**单次工具输出超过 5000 字符进上下文，用 | head -100 / | tail 控制
- 读 PDF **必须**用 run_read_pdf 工具
"""

    def _get_skills(self) -> str:
        """L0 冻结段：技能机制说明 + 精简技能列表（名字 + 一行描述）。

        完整描述不放在这里 —— 部分技能的 frontmatter description 是数百字的触发词清单，
        每轮都占着缓存前缀（缓存命中是打折不是免费）。完整用途由模型调用
        `list_skills` 工具按需获取；技能正文由 `load_skill` 以 tool_result 按需加载。

        无技能时返回空串，让 build_system_prompt 的「空段整体跳过」真正生效。
        """
        listing = self.skills.list_skills_compact(max_desc_chars=SKILL_DESC_MAX_CHARS)
        if not listing:
            return ""
        return (
            "# 技能（Skills）\n"
            "按需加载：匹配当前任务时用 `load_skill(name)` 取正文；需要完整用途说明用 `list_skills`。\n"
            f"{listing}\n"
        )

    def _get_worktree_block(self) -> str:
        """Worktree 子段：**仅在 workspace 是 git 仓库时注入**（否则返回空串）。

        理由：worktree 是 git 专属能力，而本助手定位是通用助手（写作 / 科研 / 编码 / 分析）；
        对非 git 工作空间它纯属噪音（约 1KB）。而它的默认结论本就是"不用"，所以不注入也不损失
        能力 —— 真需要时模型仍能从每轮下发的工具 schema 里看到 `create_worktree`。
        """
        if not (self.workdir / ".git").exists():
            return ""
        return (
            "## Worktree（git 隔离工作区 · 默认不用）\n"
            "\n"
            "`create_worktree(name)` 建独立分支 `wt/<name>` 的隔离副本，并软链 `.venv` / `.env`。\n"
            "**默认**：常规改动直接在当前工作空间做，**不要**为普通任务创建 worktree。\n"
            "**仅命中任一才用**：① 大改动且不想污染主工作区 git 状态；② 多 agent 需在不同分支并行开发测试；"
            "③ 高风险改造需整体回滚；④ 需长期留独立交付物 review。\n"
            "\n"
            "用法：`create_worktree(name)` → `sub_agent(workdir=name, ...)` / `spawn_teammate(worktree=name, ...)` "
            "派进去作业 → 完成后 `remove_worktree(name)`（`discard_changes=true` 会丢弃未提交改动，慎用）。\n"
            "不要跨 worktree 做文件操作。\n"
            "\n"
        )

    def _get_tools(self) -> str:
        """L0 冻结段：工具使用策略 + 待办/任务。

        **不枚举工具名**：工具 schema 由 API 的 tools= 参数每轮下发
        （agent_loop → build_agent_tools()）。此处再列一遍既冗余、又会与实际下发的
        集合漂移 —— 这里曾硬编码 default_agent_tools(28)，而团队模式下实际下发的是
        main_agent_tools(34)，导致 prompt 里的清单与模型真正拿到的工具不一致。
        """
        if self.tools is None:
            raise RuntimeError("SystemPromptBuilder 需要传入 ToolRegistry 实例")
        worktree_block = self._get_worktree_block()
        return f"""# 工具使用策略
工具清单由 API 每轮下发（含动态连接的 MCP 工具），本节只说明何时用、怎么用。

> 默认用 sub_agent 分发子任务。输入 `/teams` 进入团队模式后，会多出 spawn_teammate / send_message /
> check_inbox / request_shutdown / request_plan / review_plan 6 个团队工具（schema 由 API 下发）。

## sub_agent（子智能体）
**强制使用**（主对话不得直接执行）：读 ≥3 文件 / 读 PDF / 工具调用 ≥5 步 / 探索代码库。
- 默认不含 todo/task 工具（只由主智能体维护）
- 只读场景设 `allowed_tools=["bash","run_read","run_read_pdf"]`（**名字必须与 API 下发的完全一致**，写错会拿不到该工具）
- 无依赖想省时间 → `parallel=true` 并发；有依赖 → `parallel=false` 串行
- 想拿 ID 后回头查 → `run_in_background=true`（立即返回 bg_id；不参与并行/串行桶，永远独立后台化）

{worktree_block}# 待办与任务（两套并存，按任务特征自选）
轻量 **TodoWrite** 与重型 **Task 全家桶**（create / list / get / claim / complete）可共存。

## L1：TodoWrite（单响应内的轻量进度）
适用：步骤 ≤7、本响应内完成、不派 subagent、不需跨子任务共享。
规范：动手前列全（pending）→ 开做标 in_progress（同时仅 1 个）→ 完成立刻标 completed → 换计划用 fresh_start 整体替换 → 收尾调一次 render。

## L2：Task 全家桶（会话内看板，支持依赖）
**满足任一即用 L2**：步骤 >7 / 要派 subagent / 多 agent（或队友）共享同一份清单 / 任务间有依赖（创建时声明 blockedBy，被阻塞任务须等依赖完成才能认领）。
规范：派 subagent 前先拆好任务 → 让 subagent 认领 → 完成后回填状态 → 主对话收尾汇总。

**任务板只活在当前会话**，不承担跨会话续接：跨会话的"干到哪、下一步"一律用 `write_memory` 落盘（`project` 类），不要依赖任务板，也不要直接改记忆文件。
"""

    def _get_memory_rules(self) -> str:
        """L0 冻结段：记忆机制说明（**不含记忆索引**）。

        索引属 L2 热段 —— 它每轮都可能因 write_memory / forget_memory 而变化，
        放进 system prompt 会让整段前缀随会话失效（且跨会话无法共享缓存）。
        改由 agent_full_v2._sync_memory_index() 在指纹变化时以**尾部追加**注入。
        """
        return """# 记忆系统（memory）
跨会话记忆经 `write_memory` / `forget_memory` 即时落盘（不做 LLM 事后抽取）。
**何时必须调用**：用户表达偏好 / 纠正做法 / 肯定方案 / 透露项目事实或约束 / 提到外部资源 / 明确要求"记住"。
**四类**：user=角色偏好习惯；feedback=工作方式指导；project=目标与架构决策；reference=外部资源。
**当前索引**：以对话上下文中的 `<memory_index>` 块为准；看不到即暂无记忆。
"""

    def build_system_prompt(self) -> str:
        """组装并返回完整的 system prompt（纯静态，无动态段）。

        分层与字节级稳定性保证：
        1. section 顺序用 list-of-tuples 写死，跨进程稳定。顺序即「变化频率」
           从低到高：identity → tools → skills → memory 规则 → workspace。
           workspace 指令（AGENTS.md）在开发期改动最频繁，故排在静态段最末尾。
        2. 空 section 整体跳过，不输出占位符。
        3. 全部为静态内容 → 同一项目的不同会话产出逐字节相同，
           可跨会话共享前缀缓存（这是把记忆索引移出 system prompt 的主要收益）。
        4. 记忆索引不在本函数内（L2 热段），见 agent_full_v2._sync_memory_index()。
        """
        # 1. 顺序写死：list-of-tuples 而非 dict
        sections = [
            ("identity",  self._get_identity()),                # L0
            ("tools",     self._get_tools()),                   # L0
            ("skills",    self._get_skills()),                  # L0
            ("memory",    self._get_memory_rules()),            # L0（仅机制说明，无索引）
            ("workspace", self._get_workspace_instructions()),  # L1（静态段最末尾）
        ]
        # 2. 过滤空段（无技能 / 找不到 workspace 指令文件时整段消失）
        parts = [f"{v}\n" for _, v in sections if v]
        return "\n".join(parts)

    


