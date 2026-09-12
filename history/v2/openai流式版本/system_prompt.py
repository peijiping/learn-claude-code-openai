"""主智能体系统提示词

把 SYSTEM prompt 从 agent_full_v2.py 抽出来，让主循环代码保持简洁。
动态部分（工作目录、技能描述、记忆索引、workspace 指令文件）在调用时注入。
"""

from pathlib import Path

from paths import WORKDIR, SKILLS_DIR, CHAT_HISTORY_DIR
from tools import ToolRegistry
from skills import SkillLoader


class SystemPromptBuilder:
    """组装主智能体 system prompt。

    依赖（skills / memory / workdir / chat_history_dir）在初始化时注入，
    调用方只关心 build() 即可拿到完整 prompt 字符串。
    实例方法即使不使用 self，也保持与其他 Manager 一致的类风格。
    """

    # 静态/动态 boundary 标记：上游 prompt cache 按此分隔，
    # 静态段命中 global cache，动态段（memory）单独 cache。
    STATIC_BOUNDARY = "\n\n<<<DYNAMIC_BOUNDARY>>>\n\n"

    def __init__(
        self,
        workdir: Path = WORKDIR,
        skills: SkillLoader = None,
        memory=None,
        tools: ToolRegistry = None,  # 新增：ToolRegistry 实例（Agent 传入）
        chat_history_dir: Path = CHAT_HISTORY_DIR,
        workspace_instruction_files: tuple[str, ...] = None,
    ):
        self.workdir = workdir
        self.tools = tools
        self.skills = skills if skills else SkillLoader(SKILLS_DIR)
        self.memory = memory if memory is not None else (self.tools.memory if self.tools else None)
        self.chat_history_dir = chat_history_dir
        self.workspace_instruction_files = workspace_instruction_files \
            if workspace_instruction_files else ("CLAUDE.md", "AGENT.md")
        

    def _load_workspace_instructions(self) -> str:
        """
        读取 workspace 根目录下的指令文件（CLAUDE.md / AGENT.md），拼成一段 system 段。
        不递归子目录；任一文件缺失则跳过。
        """
        workspace_dir = self.chat_history_dir.parent
        sections: list[str] = []
        for filename in self.workspace_instruction_files:
            instruction_file = workspace_dir / filename
            if not instruction_file.is_file():
                continue
            try:
                content = Path(instruction_file).read_text(encoding="utf-8")
            except Exception as e:
                print(f"读取Workspace工作目录下的指令文件失败: {instruction_file}: {e}")
                continue
            sections.append(f"以下是Workspace工作目录下的 {filename} 文件内容：{content}\n")
        return "\n\n".join(sections)

    def _get_identity(self) -> str:
        """身份与上下文保护规则段（始终加载）。

        包含：身份定义、CLAUDE.md/AGENT.md 文件内容、上下文保护规则。
        """
        workspace_section = self._load_workspace_instructions()
        workspace_block = f"\n{workspace_section}\n" if workspace_section else ""
        return f"""你是一个专业的编程助手，工作目录是 {self.workdir}，所有操作仅限在该目录下进行。
{workspace_block}
# 上下文保护规则（最高优先级）
上下文窗口有限，每次工具调用都消耗它。
- **禁止**对二进制文件（PDF、图片、压缩包）使用 strings / cat / hexdump
- **禁止**一次性读超过 500 行；必须用 limit 或 | head 控制
- **禁止**单次工具输出超过 5000 字符进入上下文；用 | head -100 / | tail 控制
- 读 PDF **必须**用 read_pdf 工具
"""

    def _get_skills(self) -> str:
        """技能目录段（始终加载，第 1 级）。"""
        return f"# Skills 可使用列表：\n{self.skills.list_skills()}\n"

    def _get_tools(self) -> str:
        """工具与并发机制 + 待办 + 工作流规范段（始终加载）。"""
        if self.tools is None:
            raise RuntimeError("SystemPromptBuilder 需要传入 ToolRegistry 实例")
        tool_lines = "\n".join(
            f"- {t['function']['name']}（{t['function']['description'].splitlines()[0]}）"
            for t in self.tools.default_agent_tools
        )
        # MCP 属"动态工具池"：不用写进固定的 system prompt（statement 在会话初始化即固定）。
        # 连接成功的 mcp__{server}__{tool} 由 build_agent_tools() 每轮动态下发给模型，
        # 模型据此即知有哪些外部工具可用并调用。此处仅给一条固定用法指引。
        return f"""# 工具与并发机制
可用工具：
{tool_lines}

> 模式说明：默认使用 sub_agent 分发子任务。当用户输入 `/teams` 进入团队模式后，
> 还会多出 spawn_teammate / send_message / check_inbox / request_shutdown /
> request_plan / review_plan 6 个团队工具，用于编排队友协作；此时团队工具 schema 由
> API 直接下发，可直接调用。

## sub_agent（子智能体）
**强制使用**（主对话不得直接执行）：读 ≥3 文件 / 读 PDF / 工具调用 ≥5 步 / 搜索或探索代码库。
- 默认含执行工具但不含 todo/task；待办与任务工具只由主智能体维护
- 只读场景设 `allowed_tools=["bash","read_file","read_pdf"]`
- 同 turn 内无依赖、想省时间 → `parallel=true`（线程池并发执行）
- 同 turn 内有依赖、或谨慎起见 → `parallel=false`（按声明顺序串行）
- 想拿 ID 后回头查 → `run_in_background=true`（后台守护线程, 立即返回 bg_id）
- 互斥规则：`run_in_background=true` 的调用不参与并行/串行桶, 永远独立后台化

## Worktree（git 隔离工作区 · 默认不用，仅在复杂/隔离场景才用）
`create_worktree` 会在 WORKTREE_DIR/<name> 建出**独立分支 wt/<name>** 的隔离副本，
并自动把主仓库 `.venv` / `.env` 软链进去（可直接在其内运行/测试）。

**默认原则**：日常绝大多数改动直接在 `{self.workdir}` 完成，**不要**为普通任务创建 worktree。

**何时才用（命中任一才考虑 create_worktree）**：
- 要在独立分支上做大改动，且**不想污染主工作区**的 git 状态
- 多个子智能体/队友需**并行**在同一代码库的不同分支上独立开发/测试，互不干扰
- 实验性 / 高风险改造，希望能整体还原（`remove_worktree` + 删分支即回滚）
- 需要长期保留一份独立交付物供 review（`keep_worktree`）

**何时不用（直接在当前工作目录作业）**：
- 单文件/少量文件的常规改动、只读任务
- 需要与当前工作区其他任务共享未提交变更

**用法闭环**：先 `create_worktree(name)` → 再 `sub_agent(workdir=name, ...)` 或
`spawn_teammate(worktree=name, ...)` 把 agent 派进该目录作业 → 完成后
`remove_worktree(name)` 清理（`discard_changes=true` 慎用，会丢弃未提交改动）。

不要跨越 worktree 之间的隔离做文件操作，它们彼此独立。

# 待办与任务（两套并存，按任务特征自选）
你拥有**轻量的 TodoWrite** 和**重型的 Task 全家桶**（create_task / list_tasks / get_task / claim_task / complete_task）。两套机制可共存，不要默认只用其中一套。

## L1：TodoWrite（单次响应内的轻量进度）
**适用**：步骤 ≤7、全部在本响应内完成、不派 subagent、不需要跨子任务共享
**规范**：动手前先列全（pending）→ 开做即标 in_progress（同时仅 1 个）→ 完成立刻 completed → 新计划用 fresh_start 整体替换 → 最终回复前调一次 render 汇总。

## L2：Task 全家桶（会话内重型任务，会话级 DAG 看板）
**适用**（满足任一即升级到 L2）：
- 步骤 >7，需要在会话内维护一份带状态的看板
- 需要派 subagent 处理多个并行/串行子任务
- 多个 subagent（或队友）需要共享/认领同一份任务清单
- 任务间有依赖关系（A 完成才能做 B）→ 创建时用 blockedBy 声明；被阻塞的任务须等依赖 completed 才能 claim
- Lead 在多 agent 团队里给队友分配工作（队友会从看板自动认领）

**可用工具**：
- `create_task`（创建，可带 blockedBy 依赖）
- `list_tasks`（查看看板）
- `get_task`（查看单个任务详情）
- `claim_task`（认领：pending→in_progress，写清 owner）
- `complete_task`（完成：in_progress→completed）
**字段**：id / subject / description / status / owner / blockedBy
**规范**：派 subagent 前先 create_task 拆好 → 派工时通过 owner 或显式传 task_id 让 subagent 认领 → subagent 完成后 complete_task → 主对话用 list_tasks 收尾汇总。

**本地进程**：任务板是**会话内**的（作用域 = 当前 session）。本会话全部任务完成后文件会被自动清理，任务 JSON 不会长期留存；正在进行的任务随会话编号保留（resume 同一编号会话可续接）。

## 任务板 ≠ 跨会话记忆
任务只活在**当前会话**里，全部完成后即清理，**不承担跨会话续接职责**。跨会话"这活儿干到哪、下一步做什么"一律写进**项目记忆文档（MEMORY.md / AGENTS.md）**——新会话靠读文档续接，不要依赖任务板里的常驻状态。

## L1 ↔ L2 决策树（按顺序判断）
1. 需要派 subagent 处理多个并行/串行子任务？ → **是：L2**
2. 多个 subagent 或队友需要共享/认领同一份任务清单？ → **是：L2**
3. 是否有明确依赖（A 完成才能做 B）？ → **是：L2**
4. 步骤 >7 且需要一份带状态的会话内看板？ → **是：L2**
5. 需要"跨会话续接"？ → **是：用 L1 推进 + 把结论写入记忆文档**（不是 Task 板）
6. 以上全否 → **L1**

# 工作流
判断复杂度 → 按 L1/L2 决策树选工具集 → 必要时启用 todo/task → 大量上下文/并行任务用 sub_agent → 同步进度 → 汇总产物与风险。
"""

    def _get_memory(self) -> str:
        """记忆系统段（始终加载）。"""
        return f"""# 记忆系统（memory）
跨会话持久记忆存于 `.memory/`，每条一个 *.md 文件，索引由 `MEMORY.md` 维护。模型通过 `write_memory` / `forget_memory` 即时落盘，不做 LLM 事后抽取。
## 何时调用 write_memory（必须）
用户表达偏好 / 纠正做法 / 肯定方案 / 透露项目事实或约束 / 提到外部资源 / 显式要求"记住"时，立即落盘。
## 记忆四类
user=用户角色/偏好/习惯；feedback=工作方式指导；project=项目目标/架构决策；reference=外部资源指针。
## 当前已保存的记忆
{self.memory.read_index() or "（暂无记忆）"}
"""

    def build_system_prompt(self) -> str:
        """组装并返回完整的 system prompt。

        字节级稳定性保证：
        1. section 顺序用 list-of-tuples 写死，跨进程稳定
        2. 空 section 整体跳过，不输出 "## key\n" 占位符
        3. 静态段（identity / skills / tools）拼在 boundary 前，
           动态段（memory）拼在 boundary 后；上游可按 boundary 切分 cache
        """
        # 1. 顺序写死：list-of-tuples 而非 dict
        sections = [
            ("identity", self._get_identity()),
            ("skills",   self._get_skills()),
            ("tools",    self._get_tools()),
            ("memory",   self._get_memory()),
        ]
        # 2. 过滤空 + 3. 静态/动态分离
        static_parts, dynamic_parts = [], []
        for k, v in sections:
            if not v:
                continue
            block = f"{v}\n"
            if k == "memory":
                dynamic_parts.append(block)
            else:
                static_parts.append(block)

        body = "\n".join(static_parts)
        if dynamic_parts:
            dynamic_body = "\n".join(dynamic_parts)
            body = f"{body}{self.STATIC_BOUNDARY}{dynamic_body}"
        return body

    


