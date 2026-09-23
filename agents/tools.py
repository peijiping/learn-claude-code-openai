#!/usr/bin/env python3

"""
tools.py - 工具注册中心（ToolRegistry）

合并自原 tool_base.py + tools.py，全部工具能力收拢为一个类：

- 基础工具方法（run_bash / run_read / run_write ...）→ 实例方法
- 工具定义（base_tools / tools / main_agent_tools）→ 懒加载属性
- 工具处理器（handlers）→ 懒加载属性，方法名到调用方的统一映射
- 统一执行入口 execute(name, **args)
- 依赖注入（skills / memory / task_manager / bus）+ holder 模式（background）

路径常量统一从 paths.py 导入，不再在此模块内声明（见 AGENTS.md 路径规则）。

⚠️ 不再提供全局单例 TOOL_REGISTRY。
由调用方（Agent 等）显式实例化 ToolRegistry()，保证多实例隔离。
每个 Agent 实例拥有独立的 ToolRegistry / background holder / task_manager。
（todo holder 已于 2026-09-16 随 todo 工具下线停用，定义保留但不再被引用。）
"""

import os
from pathlib import Path
import subprocess
import glob as g

from paths import (
    WORKDIR,
    SKILLS_DIR,
    INBOX_DIR,
    TODO_DIR,
    MEMORY_DIR,
    todo_file_for_session,
)
from skills import SkillLoader
from todo_manager import TodoManager
from task_manager import TaskManager
from message_bus import MessageBus, VALID_MSG_TYPES
from memories import MemoryStore
from logger import get_logger
import sandbox as sandbox_mod
# 工具读图与读文档（2026-09-21）：中性图片块的构造在 attachments 里定义（展开侧
# 也在那），本模块只负责"读盘 + 判类型 + 造块"。**不引入循环依赖**：attachments
# 只依赖标准库 + paths/config/doc_convert + logger，不 import tools。
# 文档转换走 doc_convert（叶子模块）：PDF 的「文本层 + 页图」与 Office 的文本抽取
# 都由它提供，附件通道与工具通道共用同一段代码。
import doc_convert
from attachments import (
    IMAGE_EXTS,
    build_tool_image_result,
    build_tool_images_result,
    doc_max_images,
    doc_max_pages,
    doc_page_image_min_text,
    image_max_edge,
    sniff_image_mime,
    text_max_chars,
)

# 统一日志：run_bash 等工具层的异常兜底打点（见 run_bash 的 except 分支）
log = get_logger("tools")


# ── 团队工具名集合（s17 自主智能体）────────────────────────────────
# 默认（子智能体）模式下这些工具不暴露给 LLM；仅 `/teams` 进入团队模式
# 时才出现在喂给 LLM 的工具集里（见 default_agent_tools / main_agent_tools）。
TEAM_TOOL_NAMES = {
    "spawn_teammate", "send_message", "check_inbox",
    "request_shutdown", "request_plan", "review_plan",
}


class ToolRegistry:
    """
    统一管理所有工具：方法、定义、处理器与执行入口。

    设计要点：
    - 基础工具（bash / 文件读写 / glob）是实例方法，供本注册中心与
      其他模块（如 teammate_manager）直接调用；
    - handlers 暴露「工具名 → 可调用对象」的映射，兼容 agent 循环与
      SubAgent 以 dict 方式按名查找处理器；
    - base_tools / tools / main_agent_tools 是喂给 LLM 的 JSON Schema 定义，
      区分粒度：base=子智能体可用；tools=主智能体全部；main=主智能体+sub_agent；
    - holder 模式：background / todo 管理器由外部在运行期注入，避免反向依赖。
    """

    def __init__(
        self,
        skills: SkillLoader | None = None,
        memory: MemoryStore | None = None,
        task_manager: TaskManager | None = None,
        bus: MessageBus | None = None,
        cron_scheduler=None,
        teammate_manager=None,
        workdir: Path | None = None,
        bash_cwd: Path | None = None,
    ):
        # 工作根（图省事也叫"沙箱根"）：文件工具（run_read/run_write/run_edit/
        # run_glob）相对路径的基准，也是 `safe_path` 的越界判定基准。
        # 多工作空间下每个 Agent 传自己空间的 `workdir`（= 用户选定的真实目录）；
        # 不传时沿用遗留 WORKDIR（default 空间），行为一字不变。
        self.workdir = Path(workdir) if workdir is not None else WORKDIR
        # run_bash 的**缺省**工作目录。None = 进程 cwd（历史行为）。
        # 为什么和 workdir 分开（2026-09-18）：default 空间历史上 bash 就跑在
        # 进程 cwd（Electron 拉起后端时 = 应用/仓库目录），文件工具跑在 WORKDIR，
        # 两者本就不同；把 default 的 bash 一并挪到 WORKDIR 会改变既有会话里
        # 相对路径命令的落点（风险远大于收益）。**自定义工作空间**则两者都落在
        # 选定目录（Agent 构造时显式传 `bash_cwd=ws.workdir`），语义自洽。
        self.bash_cwd = Path(bash_cwd) if bash_cwd is not None else None

        # ── 依赖注入：默认惰性构造，允许外部传入自定义实例 ──
        self.skills = skills if skills is not None else SkillLoader(SKILLS_DIR)
        self.memory = memory if memory is not None else MemoryStore(MEMORY_DIR)
        self.task_manager = task_manager if task_manager is not None else TaskManager()
        self.bus = bus if bus is not None else MessageBus(INBOX_DIR)

        # ── holder 模式：运行期注入，避免 tools.py 反向依赖 agent_full_v2 ──
        self._background_manager = None
        self._todo_manager = None
        self._cron_scheduler = cron_scheduler  # cron 调度器（holder）
        self._teammate_manager = teammate_manager  # 团队成员管理器（holder，s17）
        self._worktree_manager = None  # worktree 管理器（holder，s18）
        self._mcp_manager = None  # MCP 管理器（holder，s19）
        self._workflow_manager = None  # 工作流运行时管理器（holder，s16）
        # 交互提问 broker（holder，2026-09-21）：由 SessionRuntime 在 build_agent 时
        # 按会话注入（InteractionBroker 需要 session_id 与 deliver，属会话级状态）。
        # 未注入时 ask_user 返回明确的 Error 文本（绝不做阻塞式 input() 兜底）。
        self._interaction_broker = None
        # 额外目录集（holder，2026-09-22 权限管控）：与 PermissionGate 共享同一
        # set 对象（见 set_extra_dirs），safe_path 兜底层用它放行工作根之外的
        # 预授权目录。gate 接线前为空集，行为与改造前一致。
        self._extra_dirs: set = set()

        # ── 懒加载缓存 ──
        self._handlers_cache = None
        self._base_tools_cache = None
        self._tools_cache = None
        self._main_agent_tools_cache = None
        self._default_agent_tools_cache = None

    # ═══════════════════════════════════════════════════════════
    #  holder 模式：background（运行期注入）
    #  todo holder（_todo_manager / set_todo_manager / get_todo_manager）已于
    #  2026-09-16 随 todo 工具下线停用 —— 定义保留以便回滚，但**不应新增引用**。
    # ═══════════════════════════════════════════════════════════

    def set_background_manager(self, bm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 BackgroundManager 实例。"""
        self._background_manager = bm

    def get_background_manager(self):
        """获取 BackgroundManager 实例；未初始化时抛错，提示调用方先 set_background_manager。"""
        mgr = self._background_manager
        if mgr is None:
            raise RuntimeError(
                "BackgroundManager 未初始化。请先调用 set_background_manager(...)。"
            )
        return mgr

    def set_teammate_manager(self, tm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 TeammateManager 实例（s17）。"""
        self._teammate_manager = tm

    def get_teammate_manager(self):
        """获取 TeammateManager 实例；未初始化时抛错，提示调用方先 set_teammate_manager。"""
        mgr = self._teammate_manager
        if mgr is None:
            raise RuntimeError(
                "TeammateManager 未初始化。请先调用 set_teammate_manager(...)。"
            )
        return mgr

    def set_worktree_manager(self, wm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 WorktreeManager 实例（s18）。"""
        self._worktree_manager = wm

    def get_worktree_manager(self):
        """获取 WorktreeManager 实例；未初始化时抛错，提示调用方先 set_worktree_manager。"""
        mgr = self._worktree_manager
        if mgr is None:
            raise RuntimeError(
                "WorktreeManager 未初始化。请先调用 set_worktree_manager(...)。"
            )
        return mgr

    def set_mcp_manager(self, mm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 MCPManager 实例（s19）。"""
        self._mcp_manager = mm

    def get_mcp_manager(self):
        """获取 MCPManager 实例；未初始化时抛错，提示调用方先 set_mcp_manager。"""
        mgr = self._mcp_manager
        if mgr is None:
            raise RuntimeError(
                "MCPManager 未初始化。请先调用 set_mcp_manager(...)。"
            )
        return mgr

    def set_workflow_manager(self, wm) -> None:
        """由 agent_full_v2.py 在启动时调用一次，挂上 WorkflowManager 实例（s16）。"""
        self._workflow_manager = wm

    def get_workflow_manager(self):
        """获取 WorkflowManager 实例；未初始化时抛错，提示调用方先 set_workflow_manager。"""
        mgr = self._workflow_manager
        if mgr is None:
            raise RuntimeError(
                "WorkflowManager 未初始化。请先调用 set_workflow_manager(...)。"
            )
        return mgr

    def set_interaction_broker(self, broker) -> None:
        """由 SessionRuntime.build_agent 调用一次，挂上本会话的 InteractionBroker。

        holder 而非构造参数：ToolRegistry 是 Agent 级对象，而交互提问是**会话级**
        能力（需要 session_id 与事件投递出口），且要在 Agent 构造之后才拿得到。
        """
        self._interaction_broker = broker

    def set_extra_dirs(self, dirs: set) -> None:
        """注入额外目录集（PermissionGate 共享对象，2026-09-22 权限管控）。

        ⚠️ **共享同一个 set 对象**（非拷贝）：gate 在会话恢复 / 审批「会话内允许」/
        「允许一次」时动态增删，safe_path 的兜底判定即时同步，无二次同步代码。

        内容 = WORKTREE_DIR（应用自管沙箱）∪ 全局 additional_dirs（permissions.json）
        ∪ 会话批准目录（session_allows 的 path 类记忆，见 permission.py §3.3）。
        gate 未接线时为空集 —— safe_path 行为与改造前一致（只认 workdir）。
        """
        if isinstance(dirs, set):
            self._extra_dirs = dirs

    def get_interaction_broker(self):
        """获取 InteractionBroker；未注入返回 None（**不抛错**）。

        与 background/teammate 的 holder 不同，这里刻意不抛 RuntimeError：
        `ask_user` 在无前端场景（子智能体、cron、headless）属于"能力不可用"，
        应当由 handler 返回一句可读的 Error 文本给模型，而不是炸掉整轮。
        getattr 兜底是为了兼容测试里 `ToolRegistry.__new__()` 手搭的桩。
        """
        return getattr(self, "_interaction_broker", None)

    def set_todo_manager(self, session_id: str) -> "TodoManager":
        """
        切换 TodoManager 到指定会话 id 对应的 todo 文件。

        调用时机：
        - 启动时 init_session 后
        - /newsession、/switchsession <id>、/clearsession 后

        每次调用都会重新构造 TodoManager（构造时即从磁盘 load），
        这样上一个会话的内存状态与新会话完全隔离。
        """
        TODO_DIR.mkdir(parents=True, exist_ok=True)
        todo_file = todo_file_for_session(session_id)
        self._todo_manager = TodoManager(todo_file)
        return self._todo_manager

    def get_todo_manager(self) -> "TodoManager":
        """
        获取当前会话的 TodoManager。

        未初始化（set_todo_manager 从未被调用）时抛错，提示调用方先去初始化。
        """
        mgr = self._todo_manager
        if mgr is None:
            raise RuntimeError(
                "TodoManager 未初始化。请先调用 set_todo_manager(session_id) "
                "或在启动后使用 init_session。"
            )
        return mgr

    # ═══════════════════════════════════════════════════════════
    #  基础工具实现（原 tool_base.py 的函数 → 实例方法）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _is_binary_content(text: str) -> bool:
        """检测输出是否包含大量二进制垃圾数据"""
        if not text or len(text) < 100:
            return False
        sample = text[:2000]
        printable = sum(1 for c in sample if c.isprintable() or c in '\n\r\t')
        if (len(sample) - printable) / len(sample) > 0.3:
            return True
        binary_patterns = [
            'endobj', 'endstream', '/FontDescriptor', '/CIDToGIDMap',
            '/Type /Font', '/Subtype /CIDFont', '/BaseFont /',
            '0 obj<<', '/FontFile2', '/ToUnicode',
        ]
        pattern_hits = sum(1 for p in binary_patterns if p in sample)
        if pattern_hits >= 2:
            return True
        return False

    @staticmethod
    def _smart_truncate(text: str, max_chars: int = 10000) -> str:
        """智能截断：保留首尾，中间用省略标记替代"""
        if len(text) <= max_chars:
            return text
        head_size = max_chars // 2
        tail_size = max_chars // 4
        head = text[:head_size]
        tail = text[-tail_size:]
        return f"{head}\n\n... [输出已截断，共 {len(text)} 字符，保留首 {head_size} + 尾 {tail_size} 字符] ...\n\n{tail}"

    def safe_path(self, p: str, base: Path | None = None) -> Path:
        """
        验证路径是否在指定工作根内，防止路径遍历攻击
        安全机制：
        - 将相对路径与工作根（base，默认本实例的 workdir）拼接后转换为绝对路径
        - 检查最终路径是否仍然在 base 内
        - 如果路径逃逸到 base 之外，抛出 ValueError
        参数：
            p: 相对路径字符串
            base: 可选，工作根目录（worktree 场景传入 worktree 路径）；
                  None 时用 **本实例的 `self.workdir`**
        返回：
            验证通过后的绝对路径(Path对象)
        异常：
            ValueError: 当路径试图逃逸到工作根之外时抛出
                         例如：p = "../../etc/passwd" 会被拒绝
        说明（2026-09-18）：
            原为 `@staticmethod` + 模块级 `WORKDIR`。多工作空间下每个 Agent 的沙箱根
            不同（= 该空间选定的真实目录），故改为实例方法读 `self.workdir`。
            `base` 覆盖语义不变（worktree / 子智能体 scoped workdir 仍走它）。
        兜底层扩展（2026-09-22 权限管控，见 docs/frontend/17 §3.3）：
            判定层（PreToolUse 的 PermissionGate）先跑 —— 目标路径不在有效目录集内
            会触发审批，批准后把父目录写入 `_extra_dirs`（与 gate 共享的 set）。
            这里是**第二道兜底**：路径逃逸 base 后再查 `_extra_dirs`（全局
            additional_dirs ∪ 会话批准目录 ∪ WORKTREE_DIR），命中则放行 ——
            保证审批放行的单次执行不会在工具层被二次拦下；仍不在任何有效目录
            才抛 ValueError（防绕过 hook 的调用路径）。
        """
        # 拼接工作根和输入路径，并解析为绝对路径
        # .resolve() 会解析符号链接并返回绝对路径
        base = base or self.workdir
        path = (base / p).resolve()

        # is_relative_to() 检查 path 是否在 base 的子目录中
        if not path.is_relative_to(base):
            # 逃逸 base → 查共享额外目录集（gate 维护；未接线时为空集 = 改造前行为）
            if not self._in_extra_dirs(path):
                raise ValueError(f"Path escapes workspace: {p}")

        return path

    def _in_extra_dirs(self, path: Path) -> bool:
        """路径是否落在额外目录集内（safe_path 兜底；逐目录 is_relative_to）。"""
        for d in self._extra_dirs:
            try:
                if path.is_relative_to(d):
                    return True
            except (OSError, ValueError):
                continue
        return False

    def run_bash(self, command: str, base: Path | None = None) -> str:
        """
        执行shell命令并返回结果
        安全特性：
        - 危险命令门控已上收到 PermissionGate（PreToolUse 先于本方法执行，
          2026-09-22 权限管控，见 docs/frontend/17）：内置硬拒绝/危险清单、
          自定义规则、会话内允许记忆、审批流均在判定层完成 —— 工具层不再
          保留重复黑名单，避免两处清单漂移。
        - 超时保护：命令执行超过120秒会自动终止
        - 输出截断：结果最多返回50000字符，防止内存溢出
        - 沙盒隔离（2026-09-22，见 sandbox.py / docs/frontend/19）：开关开启且
          平台后端可用时，命令经 sandbox-exec（macOS）/ bwrap（Linux）执行 ——
          写被限制在工作区∪额外目录∪临时目录内、网络默认断开、敏感目录不可读。
          这是执行层的"绝对墙"，与判定层（PermissionGate）互补；full_access
          模式下同样生效。后端不可用则裸跑 + 一次性 warning（诚实降级）。
        参数：
            command: 要执行的shell命令字符串
            base: 可选，命令的工作目录（worktree / 子智能体 scoped 场景传入）；
                  None 时用本实例的 `bash_cwd`，再退到进程 cwd
        返回：
            命令成功：返回标准输出+标准错误的合并内容（最多50000字符）
            命令失败：返回格式 "Error: command failed with return code X\\n错误信息"
            超时：返回 "Error: Timeout (120s)"
            危险命令：在判定层被拦截（回填 "Error: Permission denied ..."），
            不会执行到这里
        """
        cwd = base or self.bash_cwd or os.getcwd()
        backend = sandbox_mod.get_backend()

        def _bare_run():
            return subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                # 显式 utf-8 + errors="replace"：**绝不能**用默认的严格解码。
                # 2026-09-18 事故根因：模型执行 `sed -n '1,40p' x.md | head -c 4200`，
                # head -c 按**字节**切割，把中文字符切成半个，尾部落下不完整 UTF-8
                # 序列 → text=True（strict）在解码 stdout 时抛 UnicodeDecodeError。
                # 这类命令是合法用法，不能让它炸掉整轮对话，故用 U+FFFD 替换非法字节。
                encoding="utf-8",
                errors="replace",
                timeout=120
            )

        try:
            if backend is not None:
                # 可写集 = workdir ∪ 有效 cwd（bash_cwd / worktree base 可能不在
                # workdir 内，不加入会误拦工作目录内的写入）∪ 会话批准额外目录
                try:
                    r = backend.run(
                        command,
                        cwd=Path(cwd),
                        workdir=self.workdir,
                        extra_writable=[Path(cwd), *self._extra_dirs],
                        timeout=120,
                    )
                except RuntimeError as e:
                    # 模板被改坏（缺必需占位符）：降级裸跑 + error log，
                    # 提示用户在设置页恢复默认模板。绝不因模板问题打死本轮。
                    log.error("沙盒模板损坏，降级裸跑: %s", e)
                    r = _bare_run()
            else:
                r = _bare_run()
            out = (r.stdout + r.stderr).strip()
            if self._is_binary_content(out):
                return "Error: 输出包含大量二进制数据，请使用专用工具（如 pymupdf 读取 PDF）而非 strings/cat/hexdump 等原始命令。"
            if r.returncode != 0:
                msg = self._smart_truncate(out, 50000)
                # 沙盒拦截特征：给模型一条可行动的提示（走配置/模板调整，而非反复重试）
                if sandbox_mod.looks_blocked_by_sandbox(r.stderr or ""):
                    msg += ("\n（沙盒拦截：命令可能尝试写工作区外文件、访问网络或读取"
                            "受保护目录；如确有需要，请让用户在设置→沙盒中调整沙盒配置）")
                return f"Error: 命令执行失败，返回码 {r.returncode}\n{msg}"
            return self._smart_truncate(out, 50000) if out else "(command executed successfully, no output)"
        except subprocess.TimeoutExpired:
            # 命令执行超时（超过120秒）
            return "Error: Timeout (120s)"
        except Exception as e:
            # 兜底：任何意外异常都降级成一条工具结果返回给模型，绝不向上抛。
            # 工具层的契约是"返回字符串"，一旦让异常穿透 agent_loop，本轮的
            # tool_result 就缺一条（历史留下孤儿 tool_call），整轮对话被当场打死。
            log.error(
                "run_bash 异常: %s: %s | command=%r", type(e).__name__, e, command[:200],
                exc_info=True,
            )
            return f"Error: {type(e).__name__}: {e}"

    def run_read(self, path: str, limit: int | None = None,
                 max_pages: int | None = None, base: Path | None = None):
        """读取文件内容 —— **读取文件的唯一入口**，按类型自动分派。

        | 输入 | 行为 |
        | --- | --- |
        | 文本 / 代码 | 返回文本（`limit` 限行数） |
        | 图片 | 返回**中性图片块**（下一跳才编码成像素） |
        | PDF | 返回**文本层 + 页图**（含图表/扫描页才渲染，见 `doc_convert`） |
        | docx / xlsx / pptx | 返回文本 + 表格结构（无视觉版式，末尾如实声明） |
        | 目录 | 报错并指路（`run_glob` / `ls`） |

        **为什么要合并成一个名字（2026-09-21）**：此前是 `run_read` /
        `run_read_pdf` / `view_image` 三个工具，"选错工具"是模型最常见的一类失败 ——
        读 PDF 用了 `run_read` 拿到一句解码错误、读图片用了 `run_read` 拿到二进制
        乱码，每次都要多花一轮。真实 Claude Code 同样是一个 Read 工具按类型分派，
        模型没有选错的机会。合并后**提示词里不再需要任何格式→工具的映射表**。

        判类型**魔数优先**（`sniff_image_mime`）、扩展名兜底：被改名成 `.txt` 的
        图片也能正确走进图片分支（改造前它会报"二进制无法解码"）。

        参数：
            path: 文件路径（相对路径按 base / workdir 解析）
            limit: 文本分支最多读多少行；Office 分支当作字符上限
            max_pages: PDF 最多渲染多少张**页图**（默认 `doc_max_pages()`）
            base: 可选，工作根目录（worktree / 子智能体 scoped 场景传入）
        返回：
            文本分支 → str；图片 / PDF 分支 → 中性图片块 dict —— 引擎按**形状**
            识别（`is_tool_image_result`），**不会**把它 str() 掉。任何失败都收束为
            `"Error: ..."` 字符串，**绝不抛异常**（异常穿透会打死整轮 agent_loop）。
        """
        try:
            fp = self.safe_path(path, base)
        except Exception as e:
            return f"Error: {e}"
        try:
            if fp.is_dir():
                return (f"Error: {path} 是目录，run_read 只读文件。"
                        f"查看目录内容请用 run_glob，或用 bash 的 ls。")
            if not fp.exists():
                return f"Error: File not found: {path}"
            mime = sniff_image_mime(fp)
            if mime:
                return self._read_image(path, mime=mime, base=base)
            ext = fp.suffix.lower()
            if ext == ".pdf":
                return self._read_pdf(fp, path, max_pages=max_pages, base=base)
            if ext in doc_convert.OFFICE_EXTS:
                return self._read_office(fp, path, limit)
            return self._read_text(fp, path, limit)
        except Exception as e:  # noqa: BLE001 - 工具层契约：绝不向上抛
            log.error("run_read 异常: %s: %s | path=%r", type(e).__name__, e,
                      str(path)[:200], exc_info=True)
            return f"Error: {type(e).__name__}: {e}"

    def _read_text(self, fp: Path, display: str, limit: int | None) -> str:
        """文本分支：读全文 + 按行截断。

        图片在这里**已经不可能出现**（`run_read` 用魔数先判走了），所以解码失败
        就真的是"未知二进制"，提示面对准它，不再指向某个工具名。
        """
        try:
            lines = fp.read_text().splitlines()
        except UnicodeDecodeError:
            return (f"Error: {display} 不是文本文件（二进制内容无法解码）。"
                    f"可用 bash：`file \"{display}\"` 确认它的真实格式。")
        if isinstance(limit, int) and limit > 0 and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)

    def _read_office(self, fp: Path, display: str, limit: int | None) -> str:
        """Office 分支：走统一转换层抽「文本 + 表格结构」。

        图表、图片、版式**拿不到**（需要 LibreOffice，已被否决），转换层会在正文
        末尾如实声明。库缺失 / 文件损坏由 `convert_office` 抛异常，交给 `run_read`
        的外层兜底 —— 与本模块"不做静默降级"的既有约定一致。
        """
        cap = limit if isinstance(limit, int) and limit > 0 else text_max_chars()
        out = doc_convert.convert_office(fp, fp.suffix.lower(), limit=cap)
        body = str(out.get("markdown") or "")
        return body if body.strip() else doc_convert.empty_note(fp.suffix.lower())

    def _read_image(self, path: str, mime: str = "",
                    base: Path | None = None):
        """图片分支：把磁盘上的图片**读进上下文**，让模型用自己的视觉能力查看。

        ⚠️ 本方法**不调用任何模型**，也不生成任何文字描述。它只做三件事：
        `safe_path` 校验 → 魔数确认真是图片 → 返回一个**中性图片块**（只有
        路径与 mime，**没有字节**）。真正的"看图"发生在下一跳请求里 ——
        agent_loop 把该块聚合成一条合成 user 消息，`_model_messages` 在发送
        边界把它编码成 `image_url`，同一个模型那时才看到像素。

        为什么不做"转述"：让另一个模型描述图片再回填，等于让主模型拿着有损的
        二手信息作答（子模型不知道用户到底想问什么，图表数值/UI 对齐/报错行号
        必然丢），而且同一张图付两次钱。

        参数：
            path: 图片文件路径（`run_read` 已用魔数判过类型，这里再兜一次）
            mime: 可选，调用方已探好的 mime（省一次读盘）
        返回：
            成功：`{"type":"tool_image","text":...,"images":[{...}]}` 中性块
            失败：`"Error: ..."` 字符串（工具层契约：绝不向上抛）
        """
        try:
            fp = self.safe_path(path, base)
        except Exception as e:
            return f"Error: {e}"
        try:
            if not fp.is_file():
                return f"Error: File not found: {path}"
        except OSError as e:
            return f"Error: {type(e).__name__}: {e}"
        resolved_mime = mime or sniff_image_mime(fp)
        if not resolved_mime:
            exts = "/".join(ext.lstrip(".") for ext in IMAGE_EXTS)
            return (f"Error: 不是可识别的图片文件：{path}。可识别的图片格式为"
                    f"（{exts}）。纯文本或代码直接传路径即可，PDF 与 Office"
                    f"文档也走同一个 run_read。")
        try:
            return build_tool_image_result(fp, mime=resolved_mime)
        except Exception as e:  # noqa: BLE001 - 工具层契约：绝不向上抛
            log.error("构造图片块失败: %s: %s", type(e).__name__, e,
                      exc_info=True)
            return f"Error: {type(e).__name__}: {e}"

    def _read_pdf(self, fp: Path, display: str, *, max_pages: int | None = None,
                  base: Path | None = None):
        """PDF 分支：**文本层 + 页图**一起交给模型（2026-09-21 重做）。

        与附件通道**同源**（都走 `doc_convert.convert_pdf`：按「文本层是否足以
        代表这一页」决定渲不渲页图），差别只有落盘位置 —— 附件落在
        `.attachments/<sid>/`，这里落在**工作空间内**的
        `<workdir>/.aigent/pages/<key>/`（理由见 `doc_convert.tool_cache_dir`）。

        改造前这个分支只抽文本层（`fitz.get_text`），含图表的页与扫描件等于
        什么都没给 —— 用户"@ 了一个带图表的 PDF，模型却答不出图表内容"就是
        这么来的。现在图表的视觉真相随页图一起进上下文。

        参数：
            max_pages: 最多渲染多少张页图（默认 `doc_max_pages()`）。注意它约束的
                       是**页图**，文本层永远全量 —— 文本是无视觉模型兜底的主通道。
        返回：
            有页图 → 中性图片块 dict；无页图 → 纯文本 str；失败 → `"Error: ..."`。
        """
        try:
            import fitz  # noqa: F401 - 只为早失败；真正的转换在 doc_convert 里
        except ImportError:
            return "Error: pymupdf 未安装。请运行: python3 -m pip install pymupdf"

        page_budget = (max_pages if isinstance(max_pages, int) and max_pages > 0
                       else doc_max_pages())
        max_edge = image_max_edge()
        image_budget = doc_max_images()
        min_text = doc_page_image_min_text()
        workdir = base or self.workdir
        cache_dir = doc_convert.tool_cache_dir(
            workdir, fp, max_edge=max_edge, max_pages=page_budget,
            max_images=image_budget, min_text=min_text)
        if cache_dir is None:
            # 只在"连路径都算不出来"时发生（workdir 为空 / 不可解析）。
            # **目录不可写不算**：那种情况由 convert_pdf 内部转为纯文本模式，
            # 正文照常拿到，只是没有页图。
            return (f"Error: 无法解析页图缓存目录（workdir 为空或不可解析）。"
                    f"可用 bash 直接读该 PDF。")

        out = doc_convert.convert_pdf(
            fp, cache_dir, "doc", max_edge=max_edge, max_pages=page_budget,
            max_images=image_budget, min_text=min_text)
        pages = int(out.get("pages") or 0)
        assets = [a for a in (out.get("images") or []) if isinstance(a, dict)]
        body = doc_convert.humanize_anchors(str(out.get("markdown") or "")).strip()
        if not body and not assets:
            return (f"[PDF: {display}] 共 {pages} 页，未提取到任何文本或可渲染内容"
                    f"（可能为空白 / 加密 / 损坏）。")

        head = f"PDF: {display}，共 {pages} 页。"
        if assets:
            page_list = "、".join(str(a.get("page")) for a in assets)
            head += (f"随附页图 {len(assets)} 张（第 {page_list} 页），"
                     f"正文中 `[第 N 页为图像，随附]` 处就是它。")
        else:
            head += ("本次未随附页图：该 PDF 的文本层足以代表各页内容，"
                     "或页图渲染被跳过（见下方说明）。")
        # warnings 是"诚实失败"通道（无文本层已按图发送 / 页图目录不可写 /
        # 只渲染了前 N 页…）—— 必须让模型看到，否则它会以为已尽收眼底。
        tail = [f"（{w}）" for w in (out.get("warnings") or []) if str(w).strip()]
        text = "\n".join([head, body, *tail]).strip()[:text_max_chars()]
        if not assets:
            return text
        images = [{
            "path": str(a.get("path") or ""),
            "name": f"{fp.name} 第 {a.get('page')} 页",
            "mime": "image/jpeg",
            "page": a.get("page"),
        } for a in assets]
        return build_tool_images_result(images, text=text, source=fp.name)

    def run_write(self, path: str, content: str, base: Path | None = None) -> str:
        """
        写入内容到文件
        功能特性：
        - 使用 safe_path 进行安全路径验证
        - 自动创建父目录：如果父目录不存在会递归创建
        - 覆盖写入：目标文件已存在会被覆盖
        - 返回写入字节数，便于验证
        参数：
            path: 要写入的文件路径（相对路径）
            content: 要写入的内容字符串
            base: 可选，工作根目录（worktree / 子智能体 scoped 场景传入）；
                  None 时用本实例的 `self.workdir`
        返回：
            成功：格式 "Wrote {字节数} bytes to {路径}"
            失败：格式 "Error: {异常信息}"
        """
        try:
            fp = self.safe_path(path, base)
            # 自动创建父目录
            # parents=True: 递归创建所有不存在的父目录
            # exist_ok=True: 如果目录已存在不报错
            fp.parent.mkdir(parents=True, exist_ok=True)
            # 写入内容（覆盖模式）
            fp.write_text(content)
            return f"已写入： {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_edit(self, path: str, old_text: str, new_text: str, base: Path | None = None) -> str:
        """
        替换文件中的指定文本
        功能特性：
        - 使用 safe_path 进行安全路径验证
        - 精确替换：只替换第一处匹配（使用 count=1）
        - 先检查再写入：验证old_text存在后才执行替换
        - 原子性保证：读取和写入之间可能存在竞态条件
        参数：
            path: 要编辑的文件路径（相对路径）
            old_text: 要被替换的原文本（必须是完整的连续字符串）
            new_text: 替换后的新文本
            base: 可选，工作根目录（worktree / 子智能体 scoped 场景传入）；
                  None 时用本实例的 `self.workdir`
        返回：
            成功：格式 "Edited {路径}"
            失败（文本未找到）：格式 "Error: Text not found in {路径}"
            失败（其他）：格式 "Error: {异常信息}"
        """
        try:
            fp = self.safe_path(path, base)
            # 读取文件全部内容
            content = fp.read_text()
            # 检查要替换的文本是否存在于文件中
            if old_text not in content:
                return f"Error: Text not found in {path}"
            # 执行替换：只替换第一处匹配
            # replace(old_text, new_text, 1) 中的 1 表示只替换一次
            fp.write_text(content.replace(old_text, new_text, 1))
            return f"已编辑： {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_glob(self, pattern: str, base: Path | None = None) -> str:
        """
        使用 glob 模块搜索匹配的文件路径
        功能特性：
        - 使用 safe_path 进行安全路径验证
        - 仅返回相对于工作目录的路径
        参数：
            pattern: 要匹配的文件路径模式（支持 glob 模式）
            base: 可选，工作根目录（worktree / 子智能体 scoped 场景传入）；
                  None 时用本实例的 `self.workdir`
        返回：
            成功：匹配的文件路径列表（每个路径占一行）
            失败：格式 "Error: {异常信息}"
        """
        base = base or self.workdir
        try:
            results = []
            for match in g.glob(pattern, root_dir=base):
                if (base / match).resolve().is_relative_to(base):
                    results.append(match)
            return "\n".join(results) if results else "(no matches)"
        except Exception as e:
            return f"Error: {e}"

    # ═══════════════════════════════════════════════════════════
    #  工具处理器映射（工具名 → 可调用对象）
    # ═══════════════════════════════════════════════════════════

    def _build_handlers(self) -> dict:
        """建立工具名称到调用入口的映射（懒构建、可缓存）。

        当大模型返回工具调用请求时，agent 循环 / SubAgent 按工具名
        从这里取出处理器执行。holder 型依赖（background）在
        调用时才 get，保证运行期注入后依然拿到同一实例。
        """
        return {
            "bash":        lambda **kw: self.run_bash(kw["command"]),
            # 读文件只有一个入口（2026-09-21）：文本 / 图片 / PDF / Office 由
            # `run_read` 内部按类型分派（魔数优先）。返回值可能是 str，也可能是
            # **中性图片块 dict**（读图片、读带页图的 PDF）—— 引擎侧按形状识别
            # （`is_tool_image_result`）并装配成合成 user 消息，**不会**把它
            # str()/json.dumps() 掉。全仓仅此一个工具会返回非 str。
            "run_read":    lambda **kw: self.run_read(
                kw["path"], kw.get("limit"), kw.get("max_pages")),
            "run_write":   lambda **kw: self.run_write(kw["path"], kw["content"]),
            "run_edit":    lambda **kw: self.run_edit(kw["path"], kw["old_text"], kw["new_text"]),
            "run_glob":    lambda **kw: self.run_glob(kw["pattern"]),
            # ── todo 已下线（2026-09-16）────────────────────────────────
            # 原 TodoWrite：单列表、整表替换语义 update(items, fresh_start)。
            # 下线原因：与 task 看板功能高度重合；且「每轮整表覆盖写」会冲掉
            # 并发修改、容易漏项（Claude Code 已走过同一条路并删掉该工具）。
            # 能力由 create_task / claim_task / complete_task 承接。
            # 回滚方式：取消下一行注释，并恢复 _tools_cache 里的 "todo" 定义。
            # "todo":      lambda **kw: self.get_todo_manager().update(kw["items"], kw.get("fresh_start", False)),
            "load_skill":  lambda **kw: self.skills.load_skill(kw["name"]),
            "list_skills": lambda **kw: self.skills.list_skills(),
            "write_memory":   lambda **kw: self.memory.write(kw["name"], kw["type"], kw["description"], kw["body"]),
            "forget_memory":  lambda **kw: self.memory.forget(kw["name"]),
            "create_task": lambda **kw: self.task_manager.run_create_task(
                subject=kw["subject"],
                description=kw.get("description", ""),
                blockedBy=kw.get("blockedBy"),
                parent_id=kw.get("parent_id"),
            ),
            "list_tasks": lambda **kw: self.task_manager.run_list_tasks(),
            "get_task": lambda **kw: self.task_manager.run_get_task(kw["task_id"]),
            "claim_task": lambda **kw: self.task_manager.run_claim_task(kw["task_id"]),
            "complete_task": lambda **kw: self.task_manager.run_complete_task(
                kw["task_id"], kw.get("result", "")
            ),
            # 2026-09-18 新增：残留/写错的任务要能**就地**修或删。
            # 缺这两个出口时，模型只能另建"修正依赖版"新任务，
            # 旧任务永久留在板上 → 面板永远停在「执行中」（事故见 task_manager 模块头）
            "update_task": lambda **kw: self.task_manager.run_update_task(
                kw["task_id"],
                subject=kw.get("subject"),
                description=kw.get("description"),
                blockedBy=kw.get("blockedBy"),
                result=kw.get("result"),
            ),
            "delete_task": lambda **kw: self.task_manager.run_delete_task(kw["task_id"]),
            # check_background：仅查询语义，不消费结果；可重复调用。
            # agent_full_v2.py 在每个 turn 开头以及 turn 内每轮 tool 执行后，
            # 会自动把已完成任务以 <task_notification> 注入上下文（消费语义），
            # 本工具是"主动查询"补充，用于模型想看还未被消费的任务当前状态。
            "check_background": lambda **kw: self.get_background_manager().check(kw.get("task_id")),
            # cron 工具（s14）：调度器为 None 时返回占位错误
            "schedule_cron": lambda **kw: (
                self._cron_scheduler.run_schedule_cron(
                    kw["cron"], kw["prompt"],
                    kw.get("recurring", True), kw.get("durable", True)
                ) if self._cron_scheduler else "Error: Cron scheduler not available"
            ),
            "list_crons": lambda **kw: (
                self._cron_scheduler.run_list_crons()
                if self._cron_scheduler else "Error: Cron scheduler not available"
            ),
            "cancel_cron": lambda **kw: (
                self._cron_scheduler.run_cancel_cron(kw["job_id"])
                if self._cron_scheduler else "Error: Cron scheduler not available"
            ),
            # ── 团队成员工具（s17 自主智能体）──
            # teammate_manager 为 None 时返回占位错误（与 cron 工具一致的 holder 语义）
            "spawn_teammate": lambda **kw: (
                self.get_teammate_manager().spawn_teammate(
                    kw["name"], kw["role"], kw["prompt"]
                )
            ),
            "send_message": lambda **kw: (
                self.get_teammate_manager().send_message(
                    kw["to"], kw["content"])
            ),
            "check_inbox": lambda **kw: (
                self.get_teammate_manager().check_inbox()
            ),
            "request_shutdown": lambda **kw: (
                self.get_teammate_manager().request_shutdown(kw["teammate"])
            ),
            "request_plan": lambda **kw: (
                self.get_teammate_manager().request_plan(
                    kw["teammate"], kw["task"])
            ),
            "review_plan": lambda **kw: (
                self.get_teammate_manager().review_plan(
                    kw["request_id"], kw["approve"], kw.get("feedback", ""))
            ),
            # ── worktree 工具（s18）──
            # worktree_manager 为 None 时抛 RuntimeError（与 teammate holder 语义一致）
            "create_worktree": lambda **kw: self.get_worktree_manager().create(
                kw["name"]),
            "list_worktrees": lambda **kw: self.get_worktree_manager().list_all(),
            "remove_worktree": lambda **kw: self.get_worktree_manager().remove(
                kw["name"], kw.get("discard_changes", False)),
            "keep_worktree": lambda **kw: self.get_worktree_manager().keep(
                kw["name"]),
            # ── MCP 工具（s19）──
            # mcp_manager 为 None 时返回占位错误（与 cron 工具一致的 holder 语义）
            "connect_mcp": lambda **kw: (
                self._mcp_manager.connect(kw["name"])
                if self._mcp_manager else "Error: MCP not available"
            ),
            "list_mcp": lambda **kw: (
                "Available MCP servers:\n" + self._mcp_manager.catalog_text()
                if self._mcp_manager else "Error: MCP not available"
            ),
            # ── 工作流工具（s16）──
            # workflow_manager 为 None 时返回占位错误（与 cron/mcp holder 语义一致）。
            # run_sync 内部把 WorkflowInputError 转成 "Error: ..." 文本回给模型。
            "run_workflow": lambda **kw: (
                self.get_workflow_manager().run_sync(
                    kw["name"], kw.get("args"), kw.get("resume_from_run_id"))
            ),
            # ── 交互提问（2026-09-21）──
            # `_tool_call_id` / `_stop_event` 由 agent_full_v2 的 `_make_executor`
            # 注入（只有这一个工具需要它们：前者给前端锚定小结块，后者让等待
            # 可被停止唤醒）。模型侧 schema 里**没有**这两个字段。
            "ask_user": lambda **kw: self._run_ask_user(kw),
        }

    # ── ask_user：向用户提出结构化选择题并阻塞等待作答 ──────────────
    def _run_ask_user(self, kw: dict) -> str:
        """ask_user 处理器：透传给本会话的 InteractionBroker。

        工具层铁律：**永远返回字符串、绝不向上抛异常**。
        broker 未注入（子智能体 / cron / headless / 后端未接线）时返回明确
        Error 文本，让模型改为"在回复里直接提问并结束回合"，而不是挂死。
        """
        broker = self.get_interaction_broker()
        if broker is None:
            return (
                "Error: ask_user 当前不可用（没有可交互的前端会话；"
                "子智能体/后台任务不支持向用户提问）。"
                "请改为在回复中直接向用户提问，并结束本回合。"
            )
        try:
            return broker.ask(
                questions=kw.get("questions") or [],
                tool_call_id=str(kw.get("_tool_call_id") or ""),
                stop_event=kw.get("_stop_event"),
            )
        except Exception as e:  # noqa: BLE001 - 工具层绝不向上抛
            log.error("ask_user 处理器异常: %s: %s", type(e).__name__, e, exc_info=True)
            return f"Error: ask_user 执行失败: {type(e).__name__}: {e}"

    @property
    def handlers(self) -> dict:
        """工具名 → 处理器的映射（懒构建 + 缓存）。"""
        if self._handlers_cache is None:
            self._handlers_cache = self._build_handlers()
        return self._handlers_cache

    def scoped_handlers(self, cwd: Path) -> dict:
        """返回 handler 的浅拷贝，仅文件类工具改用 `base=cwd` 调用。

        供子智能体 / 队友注入工作目录（worktree）使用，让文件操作落在
        指定 cwd（如 WORKTREE_DIR/<name>）内。其余工具（任务/技能/记忆/
        团队等）复用共享 handlers，不改动 lead 的 handlers 本体。
        """
        scoped = self.handlers.copy()
        scoped["bash"] = lambda **kw: self.run_bash(kw["command"], base=cwd)
        scoped["run_read"] = lambda **kw: self.run_read(
            kw["path"], kw.get("limit"), kw.get("max_pages"), base=cwd)
        scoped["run_write"] = lambda **kw: self.run_write(
            kw["path"], kw["content"], base=cwd)
        scoped["run_edit"] = lambda **kw: self.run_edit(
            kw["path"], kw["old_text"], kw["new_text"], base=cwd)
        scoped["run_glob"] = lambda **kw: self.run_glob(kw["pattern"], base=cwd)
        return scoped

    # ═══════════════════════════════════════════════════════════
    #  工具定义（初始化时传给大模型，告诉它有哪些工具可用）
    # ═══════════════════════════════════════════════════════════

    @property
    def base_tools(self) -> list:
        """基础工具，主要是子智能体可用的工具。

        工具定义遵循 OpenAI SDK 格式：每个工具用 type="function" 包装，
        参数定义在 function.parameters 下。
        """
        if self._base_tools_cache is None:
            self._base_tools_cache = [
                {
                    "type": "function",
                    "function": {
                        "name": "bash", "description": "执行 shell 命令。",
                        "parameters": {"type": "object", "properties": {
                            "command": {"type": "string"},
                            "run_in_background": {"type": "boolean", "default": False,
                                "description": "True 时把命令丢到后台线程异步执行，立即返回任务 ID；"
                                               "不传则按启发式（install/build/test 等关键词）兜底判断。"},
                            "parallel": {"type": "boolean", "default": False,
                                "description": "True 时与同次响应中其他独立工具调用并行执行。"
                                               "只对无副作用、无共享状态的命令声明（如多个独立查询、多个独立 ls/wc/grep）。"
                                               "链式命令（cd && make）、修改状态的命令（rm/mv/install/build/test）不要传 True。"}
                        }, "required": ["command"]}
                    }
                },
                {
                    # 读文件**唯一入口**（2026-09-21）。描述措辞直接决定模型会不会用
                    # 它、会不会绕路先试 strings/cat/hexdump，所以三件事必须说清：
                    #   ① 文本 / 图片 / PDF / Office **都走它**（模型不必按格式换工具）
                    #   ② 图片与 PDF 页图给的是**像素本身**，不是文字描述
                    #   ③ PDF 同时给出文本层与页图（图表、扫描件的视觉真相在那）
                    "type": "function",
                    "function": {
                        "name": "run_read",
                        "description": "读取文件内容。**文本、图片、PDF、Word/Excel/PPT 都用这一个工具**，"
                                       "它会按文件类型自动处理：文本/代码返回原文；"
                                       "图片返回**图片本身**（你直接看到像素，而不是一段文字描述）；"
                                       "PDF 返回**每页文本 + 含图表或扫描页的整页图**；"
                                       "docx/xlsx/pptx 返回文本与表格内容（图表与版式不在其中，会明确说明）。"
                                       "读 PDF、图片、Office 文档时**不要**改用 bash 的 cat/strings/hexdump —— "
                                       "那些命令拿不到正确内容，只会白花一轮。",
                        "parameters": {"type": "object", "properties": {
                            "path": {"type": "string", "description": "文件路径"},
                            "limit": {"type": "integer", "description": "可选：文本文件最多读多少行；Office 文档当作字符上限"},
                            "max_pages": {"type": "integer", "description": "可选：PDF 最多附带多少张整页图（默认 20）。页图是看清图表与扫描件的唯一途径"},
                            "parallel": {"type": "boolean", "default": False,
                                "description": "True 时与同次响应中其他独立文件读取并行执行。多个互不依赖的 read 一起发可提速。"}
                        }, "required": ["path"]}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_write", "description": "将内容写入文件。",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_edit", "description": "替换文件中指定的文本内容。",
                        "parameters": {"type": "object", "properties": {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"}
                        }, "required": ["path", "old_text", "new_text"]}
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "run_glob", "description": "使用 glob 模式匹配文件路径。",
                        "parameters": {"type": "object", "properties": {
                            "pattern": {"type": "string", "description": "要匹配的文件路径模式"},
                            "parallel": {"type": "boolean", "default": False,
                                "description": "True 时与同次响应中其他独立 glob 搜索并行执行。多个互不依赖的 pattern 一起发可提速。"}
                        }, "required": ["pattern"]}
                    }
                },
                # 原「view_image」工具定义已下线（2026-09-21）：读图片并入 run_read
                # —— 模型不会再"该用 A 却用了 B"。图片依旧是**像素本身**进上下文
                # （通道没变，见 docs/frontend/14 与 15），只是入口不再单独暴露。
            ]
        return self._base_tools_cache

    @property
    def tools(self) -> list:
        """主智能体全部工具 = 基础工具 + 待办/技能/记忆/任务/后台查询。"""
        if self._tools_cache is None:
            self._tools_cache = [
                *self.base_tools,
                # ── "todo" 工具定义已下线（2026-09-16）────────────────────
                # 下线理由见 _build_handlers 内同处注释。保留原文便于审阅/回滚：
                # {"type": "function", "function": {
                #     "name": "todo",
                #     "description": "更新当前会话的待办列表。整体替换语义：传入完整的 items 数组即可。对复杂任务建议在动手前先调用一次（把计划铺开），执行中逐步把对应项标记为 in_progress / completed。fresh_start=True 表示开始新计划——会先丢弃当前列表里所有已完成的任务，适合在同一会话内切换到下一个独立任务时使用。",
                #     "parameters": {"type": "object", "properties": {
                #         "items": {"type": "array", "description": "完整的待办事项列表。", "items": {"type": "object", "properties": {
                #             "id": {"type": "string", "description": "任务标识，可省略，省略时按数组下标生成。"},
                #             "text": {"type": "string", "description": "任务内容（必填）。"},
                #             "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "任务状态；同一时刻只能有 1 个 in_progress。"},
                #         }, "required": ["text", "status"]}},
                #         "fresh_start": {"type": "boolean", "default": False, "description": "True 时表示开始新计划——先清掉当前列表里所有已完成的任务，再用 items 替换整个列表。"},
                #     }, "required": ["items"]}
                # }},
                {"type": "function", "function": {
                    "name": "load_skill", "description": "加载指定名称的专业技能（skill）知识。",
                    "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "要加载的专业技能（skill）名称"}}, "required": ["name"]}
                }},
                {"type": "function", "function": {
                    "name": "list_skills", "description": "获取当前所有可用技能（skill）的名称和简短描述列表，用于了解当前会话支持哪些技能。",
                    "parameters": {"type": "object", "properties": {
                        "parallel": {"type": "boolean", "default": False,
                            "description": "True 时与同次响应中其他独立查询并行执行。"}
                    }}
                }},
                # ── [改动 2] 新增：记忆工具 ──────────────────────────────
                {"type": "function", "function": {
                    "name": "write_memory",
                    "description": "Save a piece of information to persistent memory. "
                                   "Use when the user states a preference, corrects you, "
                                   "approves an approach, reveals a project fact, or asks you to remember something. "
                                   "The memory will be available in future sessions.",
                    "parameters": {"type": "object", "properties": {
                        "name": {"type": "string", "description": "Short kebab-case identifier, e.g. 'user-preference-tabs'"},
                        "type": {"type": "string",
                                  "enum": ["user", "feedback", "project", "reference"],
                                  "description": "user=preference/habit, feedback=guidance/correction, project=fact/decision, reference=external pointer"},
                        "description": {"type": "string", "description": "One-line summary shown in MEMORY.md index"},
                        "body": {"type": "string", "description": "Full detail in markdown. Include context and rationale."}
                    }, "required": ["name", "type", "description", "body"]}
                }},
                {"type": "function", "function": {
                    "name": "forget_memory",
                    "description": "Delete a memory by its name or filename. "
                                   "Use when the user contradicts a saved memory or asks you to forget something.",
                    "parameters": {"type": "object", "properties": {
                        "name": {"type": "string", "description": "Name of the memory to delete (slug or filename)"}
                    }, "required": ["name"]}
                }},
                # ── [改动 3] 新增：任务管理工具 ──────────────────────────────
                {"type": "function", "function": {
                    "name": "create_task",
                    "description": "创建一个任务。可选 blockedBy 声明依赖（依赖未完成时无法认领），"
                                   "可选 parent_id 拆成子树（最多 3 层）。同一批计划的任务会自动归为一组。",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "subject": {"type": "string",
                                                   "description": "简短标题，用于任务面板列表展示"},
                                       "description": {"type": "string",
                                                       "description": "详细说明，建议包含验收标准"},
                                       "blockedBy": {"type": "array",
                                                     "items": {"type": "string"},
                                                     "description": "依赖的任务 ID：须等这些任务全部 completed 后才能认领本任务。"
                                                                    "**必须是从 create_task / list_tasks 返回里复制的真实 id**"
                                                                    "（形如 t_<时间戳>_<随机数>）；写序号或不存在的 id 会被直接拒绝创建。"
                                                                    "若要依赖同批新建的前序任务：先建它、拿到 id 后再建本任务"},
                                       "parent_id": {"type": "string",
                                                     "description": "父任务 ID（可选）。用于把大任务拆成子项，最多 3 层"}},
                                   "required": ["subject"]}
                }},
                {"type": "function", "function": {
                    "name": "list_tasks",
                    "description": "List all tasks with status, owner, and dependencies.",
                    "parameters": {"type": "object", "properties": {
                        "parallel": {"type": "boolean", "default": False,
                            "description": "True 时与同次响应中其他独立查询并行执行。"}
                    },
                    "required": []}
                }},
                {"type": "function", "function": {
                    "name": "get_task",
                    "description": "Get full details of a specific task by ID.",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "task_id": {"type": "string"},
                                       "parallel": {"type": "boolean", "default": False,
                                           "description": "True 时与同次响应中其他独立查询并行执行。多个 get_task 一起发可提速。"}
                                   },
                                   "required": ["task_id"]}
                }},
                {"type": "function", "function": {
                    "name": "claim_task",
                    "description": "Claim a pending task. Sets owner, changes status to in_progress.",
                    "parameters": {"type": "object",
                                   "properties": {"task_id": {"type": "string"}},
                                   "required": ["task_id"]}
                }},
                {"type": "function", "function": {
                    "name": "complete_task",
                    "description": "完成一个 in_progress 任务，并返回因此解锁的下游任务。",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "task_id": {"type": "string"},
                                       "result": {"type": "string",
                                                  "description": "可选完成摘要（一句话说明这条做了什么），会显示在任务面板上"}},
                                   "required": ["task_id"]}
                }},
                # ── 2026-09-18 新增：残留任务的**就地**修正 / 删除出口 ──────────
                {"type": "function", "function": {
                    "name": "update_task",
                    "description": "就地修正一条任务（改 blockedBy / subject / description / result）。"
                                   "任务写错了、依赖填错了、或已不再照原计划做时用本工具，"
                                   "**不要另建一条\"修正版\"新任务** —— 旧任务会永久留在面板上，"
                                   "让整组永远回不到「全部完成」。"
                                   "status 与 parent_id 不可改：状态只走 claim_task / complete_task，"
                                   "层级本期不支持移动。",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "task_id": {"type": "string"},
                                       "subject": {"type": "string",
                                                   "description": "新的简短标题（不传则不改）"},
                                       "description": {"type": "string",
                                                       "description": "新的详细说明（不传则不改）"},
                                       "blockedBy": {"type": "array",
                                                     "items": {"type": "string"},
                                                     "description": "新的依赖列表，整体替换；传 [] 清空依赖。"
                                                                    "必须是真实存在的 task id（不存在的会被拒绝），且不能成环"},
                                       "result": {"type": "string",
                                                  "description": "完成摘要（不传则不改）"}},
                                   "required": ["task_id"]}
                }},
                {"type": "function", "function": {
                    "name": "delete_task",
                    "description": "删除一条任务（用于清掉不再需要的残留项）。删除后其它任务对它的依赖引用会被自动移除，"
                                   "避免留下悬空依赖。有子任务时会被拒绝 —— 先删子任务。"
                                   "已做完的活不要删：用 complete_task 留痕。",
                    "parameters": {"type": "object",
                                   "properties": {"task_id": {"type": "string"}},
                                   "required": ["task_id"]}
                }},
                {"type": "function", "function": {
                    "name": "check_background",
                    "description": (
                        "查询后台任务状态。仅查询语义，不消费结果，可重复调用。\n"
                        "- 不传 task_id：列出所有后台任务的当前状态（running / completed / notified），已完成或已通知的任务会附带结果预览。\n"
                        "- 传 task_id：返回该任务的详细状态；若已完成或已通知则返回完整结果。\n\n"
                        "适用场景：\n"
                        "1. 用户问后台任务执行得怎么样了时，主动查而不是再起一个新任务。\n"
                        "2. 后台 sub_agent 已派发但还没收到 task_notification 时，确认是否还在跑。\n"
                        "3. task_notification 已注入但 <summary> 截断不够用时，再用 task_id 取完整结果。\n"
                        "4. 排查后台任务是否失败（status=error 也会出现在列表里）。\n\n"
                        "注意：本工具只查询、不消费结果。优先用主循环自动注入的 task_notification 里的 <full_output>；"
                        "如需重复取完整结果再调用本工具的 task_id 参数。"
                    ),
                    "parameters": {"type": "object",
                                   "properties": {
                                       "task_id": {"type": "string",
                                                   "description": "可选，指定任务 ID（如 bg_0001）。不传则列出所有后台任务。"},
                                       "parallel": {"type": "boolean", "default": False,
                                           "description": "True 时与同次响应中其他独立查询并行执行。"}
                                   }}
                }},
                # ── cron 定时任务工具（s14）──────────────────────────────
                {"type": "function", "function": {
                    "name": "schedule_cron",
                    "description": "Schedule a cron job. cron is 5-field: min hour dom month dow.",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "cron": {"type": "string",
                                                "description": "5-field cron expression"},
                                       "prompt": {"type": "string",
                                                  "description": "Message to inject when fired"},
                                       "recurring": {"type": "boolean",
                                                     "description": "True=recurring, False=one-shot"},
                                       "durable": {"type": "boolean",
                                                   "description": "True=persist to disk"}},
                                   "required": ["cron", "prompt"]}
                }},
                {"type": "function", "function": {
                    "name": "list_crons",
                    "description": "List all registered cron jobs.",
                    "parameters": {"type": "object", "properties": {
                        "parallel": {"type": "boolean", "default": False,
                            "description": "True 时与同次响应中其他独立查询并行执行。"}
                    },
                    "required": []}
                }},
                {"type": "function", "function": {
                    "name": "cancel_cron",
                    "description": "Cancel a cron job by ID.",
                    "parameters": {"type": "object",
                                   "properties": {"job_id": {"type": "string"}},
                                   "required": ["job_id"]}
                }},
                # 团队成员工具由 _team_tool_defs() 提供，避免与 tools 内联重复
                *self._team_tool_defs(),
                # worktree 管理工具（s18）：仅 Lead 创建/删除，两种模式都可见
                *self._worktree_tool_defs(),
                # MCP 发现工具（s19）：connect_mcp（name 枚举可用服务器）+ list_mcp（查目录），
                # 连上后其 mcp__* 工具由 build_agent_tools() 动态追加（见 _mcp_tool_defs）
                *self._mcp_discovery_tool_defs(),
                # 工作流工具（s16）：按名运行已保存的编排脚本（计划即代码）
                {"type": "function", "function": {
                    "name": "run_workflow",
                    "description": "Run a saved workflow by name. A workflow is a "
                                   "pre-registered deterministic orchestration script "
                                   "(multi-subagent plan as code): it spawns focused "
                                   "single-step subagents, with journal-based resume "
                                   "support. Pass input in args; pass resume_from_run_id "
                                   "to resume a previous run (unchanged steps replay "
                                   "from cache). Available names are limited to the "
                                   "host registry (e.g. 'review-changes': review "
                                   "changed code across dimensions and adversarially "
                                   "verify each finding).",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "name": {"type": "string",
                                           "description": "注册表中工作流的名字，如 'review-changes'"},
                                       "args": {"type": "object",
                                           "description": "工作流入参（可选）。review-changes 的入参："
                                                          "changes（变更代码文本）、budget（token 上限，null 不限）"},
                                       "resume_from_run_id": {"type": "string",
                                           "description": "可选，上次运行的 runId（wf_* 格式）。"
                                                          "传入后续跑：未变化的步骤直接命中缓存重放。"}},
                                   "required": ["name"]}
                }},
                # ── 交互提问（2026-09-21）──────────────────────────────
                # 只进主智能体工具集（tools），**绝不进 base_tools**：
                # 子智能体只有单向事件上行、没有下行应答通道，常跑在后台 daemon
                # 线程里 —— 拿到它就会调用到一个永远无人应答的接口并挂住线程。
                self._ask_user_tool_def(),
            ]
        return self._tools_cache

    # ── 团队工具定义（s17自主智能体）──仅团队模式下喂给 LLM ─────────
    def _team_tool_defs(self) -> list:
        """6 个团队工具定义（spawn/send_message/check_inbox/协议工具）。

        供 `tools` 属性展开使用；默认（非团队）模式下会被 default_agent_tools
        整体剔除，不暴露给 LLM，从而省掉这部分的 schema token 开销。
        """
        return [
            {"type": "function", "function": {
                "name": "spawn_teammate",
                "description": "Spawn an autonomous teammate agent in a "
                               "background thread. Teammate runs a "
                               "WORK→IDLE loop: lists and claims tasks "
                               "from the board, checks inbox, then shuts "
                               "down. Use with send_message / check_inbox "
                               "/ request_shutdown / request_plan / "
                               "review_plan.",
                "parameters": {"type": "object",
                               "properties": {
                                   "name": {"type": "string"},
                                   "role": {"type": "string"},
                                   "prompt": {"type": "string"},
                                   "worktree": {"type": "string",
                                       "description": "可选，已创建 worktree 的名称。给定时队友在 WORKTREE_DIR/<worktree> 内作业（文件操作根指向该目录）。"}},
                               "required": ["name", "role", "prompt"]}
            }},
            {"type": "function", "function": {
                "name": "send_message",
                "description": "Send a message to a teammate.",
                "parameters": {"type": "object",
                               "properties": {
                                   "to": {"type": "string"},
                                   "content": {"type": "string"}},
                               "required": ["to", "content"]}
            }},
            {"type": "function", "function": {
                "name": "check_inbox",
                "description": "Check Lead's inbox for teammate messages "
                               "and protocol responses (plan/shutdown). "
                               "Routes protocol responses to their "
                               "original requests.",
                "parameters": {"type": "object", "properties": {},
                               "required": []}
            }},
            # 协议工具（队友管理）
            {"type": "function", "function": {
                "name": "request_shutdown",
                "description": "Request a teammate to shut down "
                               "gracefully.",
                "parameters": {"type": "object",
                               "properties": {"teammate": {"type": "string"}},
                               "required": ["teammate"]}
            }},
            {"type": "function", "function": {
                "name": "request_plan",
                "description": "Ask a teammate to submit a plan for "
                               "review.",
                "parameters": {"type": "object",
                               "properties": {
                                   "teammate": {"type": "string"},
                                   "task": {"type": "string"}},
                               "required": ["teammate", "task"]}
            }},
            {"type": "function", "function": {
                "name": "review_plan",
                "description": "Approve or reject a submitted plan by "
                               "request_id.",
                "parameters": {"type": "object",
                               "properties": {
                                   "request_id": {"type": "string"},
                                   "approve": {"type": "boolean"},
                                   "feedback": {"type": "string"}},
                               "required": ["request_id", "approve"]}
            }},
        ]

    # ── worktree 管理工具定义（s18）── Lead 侧创建/删除/保留 ────────
    def _worktree_tool_defs(self) -> list:
        """4 个 worktree 管理工具定义（create/list/remove/keep）。

        供 `tools` 属性展开使用（default/main 两种模式都可见）。子智能体/队友
        只作为 cwd 的"消费者"（workdir/worktree 参数），不暴露这些管理工具。
        """
        return [
            {"type": "function", "function": {
                "name": "create_worktree",
                "description": "创建带独立分支的隔离 git worktree（分支 wt/<name>，位于 WORKTREE_DIR/<name>）。"
                               "创建时自动检测仓库技术栈并软链对应运行时（如 .venv、node_modules、target 等），"
                               "供 agent 在其内部运行/测试。"
                               "仅创建工作区，不绑定任务；随后用 sub_agent(workdir=...) 或 spawn_teammate(worktree=...) 把 agent 派进去作业。",
                "parameters": {"type": "object",
                               "properties": {"name": {"type": "string",
                                   "description": "worktree 名称（字母/数字/点/下划线/连字符，1-64 字符）"},
                                   "stack": {"type": "string",
                                       "description": "可选，强制指定技术栈标签（不传时自动检测）。"
                                                       "常见值：python、node/typescript、go、rust、java-maven、"
                                                       "java-gradle、c-cpp、php、ruby、dotnet、dart-flutter、terraform。"}},
                               "required": ["name"]}
            }},
            {"type": "function", "function": {
                "name": "list_worktrees",
                "description": "列出所有已创建的 worktree（名称/分支/路径/供给概览）。",
                "parameters": {"type": "object", "properties": {},
                               "required": []}
            }},
            {"type": "function", "function": {
                "name": "remove_worktree",
                "description": "移除指定 worktree 及其专属分支。默认拒绝移除有未提交改动或未推送提交的 worktree；"
                               "确需丢弃时传 discard_changes=true 强制移除。",
                "parameters": {"type": "object",
                               "properties": {
                                   "name": {"type": "string"},
                                   "discard_changes": {"type": "boolean",
                                       "description": "True 时忽略未提交/未推送检查，强制移除。"}},
                               "required": ["name"]}
            }},
            {"type": "function", "function": {
                "name": "keep_worktree",
                "description": "保留指定 worktree 供人工复核（不删除分支）。",
                "parameters": {"type": "object",
                               "properties": {"name": {"type": "string"}},
                               "required": ["name"]}
            }},
        ]

    # ── MCP 发现工具定义（s19）──────────────────────────────
    def _mcp_discovery_tool_defs(self) -> list:
        """MCP 「发现」工具定义：connect_mcp（name 枚举可用服务器）+ list_mcp（查目录）。

        痛点：仅靠 connect_mcp 时，LLM 不知道存在哪些服务器、能力藏在哪个 MCP 里，
        只能靠“猜名字”瞎试。这里动态读 MCPManager 的可连接目录（available_servers /
        catalog_text），让 LLM 在调用前就知道「有哪些服务器、各自提供哪些工具」，
        从而作出是否 connect、连哪个的决定。

        仍**不预加载**各工具的完整参数 schema——连接成功后才由 build_agent_tools() 动态
        追加 mcp__{server}__{tool}，保持 s19「动态工具池、省 token」的设计。
        manager 未注入时 name 枚举降级为空、目录文本提示未配置。
        """
        mgr = self._mcp_manager
        enum = list(mgr.available_servers()) if mgr else []
        catalog_txt = mgr.catalog_text() if mgr else ""

        connect_def = {"type": "function", "function": {
            "name": "connect_mcp",
            "description": (
                "Reconnect a configured MCP server that is currently disconnected or "
                "failed to connect (configured servers are auto-loaded at startup). "
                "After connecting, its tools become available as mcp__{server}__{tool} "
                "in subsequent rounds (dynamic tool pool). "
                "Choose the server whose exposed capability matches what you need.\n"
                "Available servers:" + ("\n" + catalog_txt if catalog_txt else " none")
            ),
            "parameters": {"type": "object",
                           "properties": {"name": {"type": "string",
                               "enum": enum,
                               "description": "MCP server name to connect."}},
                           "required": ["name"]}
        }}

        list_def = {"type": "function", "function": {
            "name": "list_mcp",
            "description": "List available MCP servers and the tools each exposes. "
                           "Use this to check whether a capability lives in an MCP "
                           "server before calling connect_mcp.",
            "parameters": {"type": "object", "properties": {},
                           "required": []}
        }}

        return [connect_def, list_def]

    def _mcp_tool_defs(self) -> list:
        """动态组装当前已连接 MCP 服务器的工具定义（OpenAI 格式）。

        每轮现场组装（不缓存），保证 connect_mcp 后 LLM 立即可见新工具。
        MCPManager 未注入时返回空列表。
        """
        if self._mcp_manager is None:
            return []
        return self._mcp_manager.assemble_tools()

    def build_agent_tools(self, team_mode: bool = False) -> list:
        """组装喂给 LLM 的工具集 = 基础（团队/默认）+ 已连接 MCP 工具。

        与 default_agent_tools / main_agent_tools 的差异：这里是**动态方法**，
        每次调用都把已连接 MCP 服务器的工具合并进去，实现 s19 的"动态工具池"。
        主循环 agent_loop 每轮调用它，connect_mcp 后下一轮即可看到新工具。
        """
        base = self.main_agent_tools if team_mode else self.default_agent_tools
        mcp_defs = self._mcp_tool_defs()
        if not mcp_defs:
            return base
        return [*base, *mcp_defs]

    @property
    def main_agent_tools(self) -> list:
        """团队模式工具集 = 全部工具（含团队工具）+ sub_agent。

        仅在 `/teams` 进入团队模式时由主循环喂给 LLM。
        """
        if self._main_agent_tools_cache is None:
            self._main_agent_tools_cache = [
                *self.tools,
                self._sub_agent_tool_def(),
            ]
        return self._main_agent_tools_cache

    @property
    def default_agent_tools(self) -> list:
        """默认（子智能体）模式工具集 = 全部工具剔除团队工具 + sub_agent。

        缺少团队工具的 schema，省 token，并避免模型误触发代价高昂的团队协作。
        """
        if self._default_agent_tools_cache is None:
            non_team = [t for t in self.tools
                        if t["function"]["name"] not in TEAM_TOOL_NAMES]
            self._default_agent_tools_cache = [
                *non_team, self._sub_agent_tool_def(),
            ]
        return self._default_agent_tools_cache

    # ── sub_agent 工具定义（默认与团队模式共用）────────────────────
    def _sub_agent_tool_def(self) -> dict:
        """sub_agent 工具定义（分发子任务给通用型子智能体）。

        ⚠️ 本定义的措辞直接决定模型能否稳定产出工具调用，改动前注意两条硬约束
        （2026-09-14 事故复盘）：
        1. **schema 不得自相矛盾**：`required` 里列出的字段，description 里不能
           又禁止模型填。历史写法 `required=["prompt","parallel"]` + "已传
           run_in_background=true 时不要再传 parallel" 让模型在「批量任务必须
           后台」这个唯一高频场景下**每一次都必须违规**（实测模型确实只传
           prompt/description/allowed_tools/run_in_background，缺 parallel）。
           这种"必填但被禁止"的构造下模型偶尔会放弃工具调用、只回一句
           "我派一个子智能体去读"，本轮随即结束 —— 用户看到的就是"直接中断"。
        2. **示例里的工具名必须与 `base_tools` 完全一致**（bash / run_read /
           run_write / run_edit / run_glob）。写成 read_file / read_pdf 这类不
           存在的名字会污染 allowed_tools，子智能体直接拿不到那件工具。
           注意 `run_read` 是**读文件的唯一入口**（2026-09-21 起图片、PDF、
           Office 都由它分派），所以示例里**不该再出现 run_read_pdf / view_image**。
        """
        return {"type": "function", "function": {
            "name": "sub_agent",
            "description": "分发子任务给通用型子智能体。子智能体拥有独立上下文（不污染主对话），共享文件系统，只返回最终摘要。子智能体默认拥有执行工具权限，但不包含 task 系列工具；任务看板只由主智能体维护。当任务需要多步骤操作、读取多个文件、收集信息或可能产生大量工具调用时使用。\n\n⚠️ 决定派发就必须在**本轮同一条回复里立即发起本次工具调用**。只输出「我派一个子智能体去读」这类正文而不调用本工具，本轮会直接结束、子任务永远不会执行（实测事故：模型承诺派发但零工具调用 → turn 结束 → 用户侧表现为「直接中断、不往下执行」）。\n\n⚠️ 强制规则（必须遵守，违例会阻塞主循环浪费时间）：\n凡是「批量 / 全量 / 跨多个文件 / 跨整个目录 / 预计耗时 > 30 秒」的任务，**必须传 run_in_background=true** 丢到后台线程异步执行，立即返回任务 ID，结果通过后续轮次的 <task_notification> 收回。绝对不要同步等待这类任务完成。\n判断标准（命中任意一条就必须后台）：\n  - 涉及 ≥ 2 个文件 / 整个目录 / 全部 N 个 X\n  - prompt 含「全部 / 全量 / 批量 / 跑一遍 / 扫描 / 审计 / 构建 / 测试套件」等关键词\n  - 需要多步骤工具调用且总耗时可能 > 30 秒\n允许同步（不传 run_in_background）的场景：\n  - 单个文件的快速查询、单步工具调用\n  - 必须等前序结果才能继续的下一步操作\n\nparallel 只对**同步**子任务有意义：多个互不依赖的同步子任务设 parallel=true 可并发执行；不传按串行处理。run_in_background=true 时本字段无意义，可以完全不传（后台任务各自独立，不参与并行/串行分桶）。\n\n可通过 allowed_tools 限制子智能体的工具范围，例如只允许只读操作。**工具名必须与 API 下发的完全一致**（现有只读工具为 bash / run_read / run_write；名字写错会拿不到该工具）。\n\n示例：\n- sub_agent(prompt=\"读取 DRG_Docs 目录下全部 6 个 PDF 的标题和摘要\", run_in_background=true)  ← 批量全目录，必须后台\n- sub_agent(prompt=\"实现用户注册功能\", parallel=false)\n- sub_agent(prompt=\"分析当前代码架构并设计重构方案\", parallel=false)\n- sub_agent(prompt=\"只读方式搜索代码中的安全问题\", allowed_tools=[\"bash\",\"run_read\"], parallel=true)\n- sub_agent(prompt=\"跑全量测试并报告失败用例\", run_in_background=true)",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "给子智能体的任务描述，应具体说明要做什么"},
                    "description": {"type": "string", "description": "任务的简短描述，用于日志记录"},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "限制子智能体可用的工具名称列表。不设置则默认使用全部工具。例如 [\"bash\",\"run_read\"] 限制为只读工具集"},
                    "parallel": {"type": "boolean", "default": False,
                        "description": "仅对同步子任务有意义：True 表示与其他同步 sub_agent 并行执行，"
                                       "不传按串行处理。run_in_background=true 时无意义，不必传。"},
                    "run_in_background": {"type": "boolean", "default": False,
                        "description": "True 时把子任务丢到后台线程异步执行，立即返回后台任务 ID；"
                                       "结果通过 <task_notification> 在后续轮次通知。"
                                       "批量/全量/多文件任务必须传 True。"},
                    "workdir": {"type": "string",
                        "description": "可选，已创建 worktree 的名称。给定时子智能体的工作目录（所有文件操作根）为 WORKTREE_DIR/<workdir>，"
                                       "用于在隔离 worktree 内改代码并运行测试。需先 create_worktree 创建。"}
                },
                "required": ["prompt"]
            }
        }}

    # ── ask_user 工具定义 ────────────────────────────────────────
    def _ask_user_tool_def(self) -> dict:
        """ask_user 工具定义（向用户提出结构化选择题并等待作答）。

        ⚠️ 三条硬约束（改动前必读）：
        1. **本定义只进 `tools`（主智能体），绝不进 `base_tools`**。子智能体只有
           单向事件上行、没有下行应答通道，且常跑在后台 daemon 线程里 ——
           拿到它就会调用到一个永远无人应答的接口，把线程挂死。
        2. **schema 里不得出现 `parallel` / `run_in_background`**。`_execute_tool_call`
           与分桶逻辑都硬编码"ask_user 永远串行且独占"，若 schema 里给了这两个
           字段，就复刻了 sub_agent 那次"required 里必填、description 里又禁止"
           的自相矛盾事故（模型每次都必须违规）。
        3. **description 必须写明"禁止索取敏感信息"**。这是唯一一个能把模型输出
           直接引向用户的通道，凭证/隐私必须走别的路。
        """
        return {"type": "function", "function": {
            "name": "ask_user",
            "description": (
                "向用户提出 1–4 个结构化选择题，并**阻塞等待**用户在前端作答；"
                "答案会作为本工具的结果返回，你在**同一个回合内**据此继续。\n\n"
                "【何时该用】仅当你确实无法从用户消息、工作空间文件、记忆或既有约定"
                "中推断，且不同选择会导致**明显不同且代价高**的实现路径时。"
                "例如：技术栈/存储方案二选一、删除还是保留、范围与优先级取舍。\n\n"
                "【何时不该用】\n"
                "- 用户已给出答案，或能从文件/约定推断出来 —— 直接推断并说明即可；\n"
                "- 只是「确认我理解得对不对」这类客套确认 —— 直接干活并在回复里说明你的假设；\n"
                "- 一次想问超过 4 个问题 —— 拆成多轮，或先做能确定的部分；\n"
                "- 需要开放式的长文本回答 —— 那属于普通对话，不该用本工具。\n\n"
                "【禁止】绝不用它索取任何敏感信息：密码、API Key/令牌、身份证/银行卡号、"
                "私人联系方式、医疗隐私等。本工具**禁止用于收集凭据**。\n\n"
                "【重要限制】本工具会**阻塞当前回合**直到用户点提交/取消/停止；"
                "`run_in_background` / `parallel` 对它无效，它永远串行且独占执行"
                "（你无需也不应传这两个字段）。用户也可能直接在输入框打字发送 —— "
                "那会被当作本题的自由文本答案，此时你收到的结果形如"
                "「用户没有选择选项，而是直接回复了：…」。"
                "若当前没有可交互的前端（例如在子智能体/后台任务里），本工具会返回 "
                "Error 文本，请改用普通提问并结束回合。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "description": "1–4 个问题。前端**一题一屏、点「下一步」逐步作答**，"
                                       "最后一题点提交时一次性回传全部答案。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "本题的稳定标识（小写英文+下划线，批内唯一），"
                                                   "答案按它对号入座"},
                                "header": {
                                    "type": "string",
                                    "description": "不超过 12 字的短标签，用于分屏步骤条与结果小结的标题；"
                                                   "不要用整句"},
                                "question": {
                                    "type": "string",
                                    "description": "完整问题，一句话说清要用户决定什么"},
                                "multi_select": {
                                    "type": "boolean", "default": False,
                                    "description": "false=单选（点选项即选中）；true=多选（可勾多项）。"
                                                   "默认单选"},
                                "allow_custom": {
                                    "type": "boolean", "default": True,
                                    "description": "是否额外提供「其他」自由文本输入。默认 true"
                                                   "（用户可能有你没想到的答案）；"
                                                   "仅当你已穷举所有可能时才设为 false"},
                                "custom_label": {
                                    "type": "string",
                                    "description": "自定义输入项的显示文案，默认「其他」"},
                                "options": {
                                    "type": "array", "minItems": 2, "maxItems": 4,
                                    "description": "2–4 个候选。label 必须彼此可区分；"
                                                   "description 写清该选项的后果/代价"
                                                   "（用户看不懂的选项不如不提供）",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {
                                                "type": "string",
                                                "description": "选项短文案（建议不超过 16 字），中文优先"},
                                            "description": {
                                                "type": "string",
                                                "description": "一句话解释该选项的含义/影响，可省略"},
                                        },
                                        "required": ["label"],
                                    },
                                },
                            },
                            "required": ["id", "header", "question", "options"],
                        },
                    },
                },
                "required": ["questions"],
            },
        }}

    # ═══════════════════════════════════════════════════════════
    #  统一执行入口
    # ═══════════════════════════════════════════════════════════

    def resolve_handler(self, tool_name: str):
        """按工具名解析处理器：先查静态 handlers，未命中且为 mcp__* 时查 MCP 管理器。

        返回可调用对象或 None（未找到）。MCP 工具是动态发现的，不预先注册进静态
        handlers，故通过本方法在运行时委托给对应 MCP 客户端。
        """
        handler = self.handlers.get(tool_name)
        if handler is not None:
            return handler
        if tool_name.startswith("mcp__") and self._mcp_manager is not None:
            return self._mcp_manager.assemble_handlers().get(tool_name)
        return None

    def execute(self, tool_name: str, **tool_args):
        """按工具名执行一次工具调用；未知工具返回错误字符串。

        返回类型是 `str`，**唯一例外是 `run_read`** —— 读图片或读带页图的 PDF 时
        它返回一个中性图片块（`{"type":"tool_image",...}`，见 `_read_image` /
        `_read_pdf`）。工具层"永远返回东西、绝不抛异常"的契约不变：所有失败路径
        仍然返回 `"Error: ..."` 字符串，调用方按形状分流即可
        （`agent_full_v2._execute_tool_call`）。
        """
        handler = self.resolve_handler(tool_name)
        if handler is None:
            return f"Error: Unknown tool {tool_name}"
        return handler(**tool_args)
