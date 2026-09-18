#!/usr/bin/env python3
"""
session_manage.py - 会话管理模块

提供对话历史的持久化存储和管理功能：
- 会话文件的创建、加载、切换
- 消息的序列化和反序列化
- 支持多个独立会话

使用方式：
    from session_manage import SessionManager

    manager = SessionManager(chat_history_dir, system_prompt)
    session_id, session_file, messages = manager.init_session()
"""

import json
import os
import secrets
import string
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from context_compact import ContextCompact, DEFAULT_MAX_CONTEXT_TOKENS
import paths  # 运行期读 paths.TASKS_DIR（测试/CLI 会临时改写模块级值，不能静态捕获）
from paths import DEFAULT_PROJECT_SLUG, task_files_for_session
from logger import get_logger

# 统一日志（~/.aigent/logs/agent_日期.log）
log = get_logger("session")

# ── 会话短 id（2026-09-14：文件命名从 session_<N> 自增编号迁移到随机短 id）──
# 新会话文件名：session_<id>.jsonl（id = 10 字符 base62 随机串，如 Kx7mQ2vT8p）。
# 存量会话不迁移（新旧共存）：其 id 即原编号的字符串形式（"session_6" → "6"），
# 全链路统一用 str 类型的 session_id 寻址，存量文件天然兼容。
# 防歧义约定：**全数字 stem = 存量编号，混合字符 = 新 id** —— 生成时全数字重掷。
BASE62_CHARS = string.ascii_letters + string.digits
SESSION_ID_LEN = 10

# token 消耗统计的四字段（与 LLM usage 投影结构一致，会话级累计/轮级明细共用）
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")


def new_session_id() -> str:
    """生成 10 字符 base62 随机短 id；恰好全为数字则重掷。

    随机熵 59.5 bit（1 万会话碰撞率 ≈ 0.00002%）；调用方创建文件时再查重、
    撞了重新生成 → 工程上不可能重复。不用 uuid：36 字符文件名太长。
    """
    while True:
        sid = "".join(secrets.choice(BASE62_CHARS) for _ in range(SESSION_ID_LEN))
        if not sid.isdigit():
            return sid


# 会话 id 的**跨工作空间**唯一性守卫（多工作空间改造，2026-09-18）。
# 由桥层注入"该 id 是否已被任意工作空间占用"的查询（见 set_session_id_guard）。
_SESSION_ID_TAKEN: Optional[Callable[[str], bool]] = None


def set_session_id_guard(fn: Optional[Callable[[str], bool]]) -> None:
    """注入会话 id 的全局占用查询（None = 关闭）。

    为什么必须有：id 若只在**单个空间**的目录里查重，两个工作空间就可能各自
    生成同一个 `session_x`。而 session_id 是全链路路由键（前端按它分发事件与
    消息缓冲、桥层按它解析所属空间），一旦重号就是"事件进了别的会话 / 切会话
    切到别的空间"，且没有任何自愈路径。概率极低，但代价是数据错位级别的，
    所以宁可每次新建多 N 次 stat。
    """
    global _SESSION_ID_TAKEN
    _SESSION_ID_TAKEN = fn


def _id_taken_globally(sid: str) -> bool:
    fn = _SESSION_ID_TAKEN
    if fn is None:
        return False
    try:
        return bool(fn(sid))
    except Exception:  # noqa: BLE001 - 守卫本身不能阻断建会话
        return False


def _now_iso() -> str:
    """本地时间秒级 isoformat（单机桌面产品，无时区转换需求）。"""
    return datetime.now().isoformat(timespec="seconds")


class SessionManager:
    """会话管理器，负责对话历史的持久化和管理"""

    def __init__(self, chat_history_dir: Path, system_prompt: str,
                 session_prefix: str = "session_", subagent_store=None,
                 project_id: str = DEFAULT_PROJECT_SLUG, tasks_dir: Path | None = None):
        """
        初始化会话管理器

        Args:
            chat_history_dir: 会话历史存储目录
            system_prompt: 系统提示词
            session_prefix: 会话文件名前缀，默认 "session_"；
                            cron 调度器传入 "cron_" 以独立编号
            subagent_store: 可选的子智能体旁路记录存储（SubagentStore 实例）。
                            传入时（桌面端）：子智能体执行过程写到独立的
                            `session_N.subagents.jsonl`，主会话文件只保留标准
                            消息（并在加载时把历史遗留的 in-file 行一次性迁出）。
                            为 None 时（CLI 旧路径）：保持原行为。
            project_id: 本管理器服务的**工作空间 id**（多工作空间，2026-09-18）。
                        写进会话元数据的 `project` 字段，前端据此把会话挂到对应
                        空间节点下；缺省 "default"（CLI / 单空间行为不变）。
            tasks_dir: 该工作空间的任务目录（删会话/清空会话时级联清理用）。
                       缺省回落模块级 `paths.TASKS_DIR`（= default 空间）。
        """
        self.chat_history_dir = chat_history_dir
        self.system_prompt = system_prompt
        self.session_prefix = session_prefix
        self.subagent_store = subagent_store
        self.project_id = project_id
        self._tasks_dir = tasks_dir
        self.compact_manager = ContextCompact(
            transcript_dir=chat_history_dir.parent / ".transcripts",
            tool_results_dir=chat_history_dir.parent / ".task_outputs" / "tool-results",
        )
        self.chat_history_dir.mkdir(parents=True, exist_ok=True)
        # 索引读-改-写互斥锁：标题生成等后台线程与 UI 管理操作并发更新索引时，
        # 防止两个 RMW 交错导致丢更新（JSONL 原子替换只保证单次写不损坏）
        self._index_lock = threading.Lock()
        # 追加写互斥锁：主循环（agent_loop）与后台 sub_agent 完成线程都会
        # append 会话文件（role=subagent 行即时落盘），持锁保证写入不交错。
        self._append_lock = threading.Lock()
        # 子智能体执行记录（role=subagent 行）进程内缓存：key = 会话文件路径。
        # 由 load_session_history / append_subagent_to_session 维护，
        # save_session_history 重写后据此回写，保证 compact / 自愈重写不丢记录。
        self.subagent_rows: dict[Path, list[dict]] = {}

    def format_context_label(self, messages: list) -> str:
        """格式化当前上下文窗口显示信息。"""
        return self.compact_manager.format_context_label(messages)

    @property
    def tasks_dir(self) -> Path:
        """本会话所属工作空间的任务目录（元数据目录下的 `.tasks`）。

        多工作空间（2026-09-18）：由调用方（`Agent` / `SessionRuntime`）按
        `workspace.tasks_dir` 显式注入，**不从 `chat_history_dir.parent` 反推** ——
        反推要求"chat_history_dir 一定叫 .chathistory 且直接挂在元数据目录下"，
        这个约定在 CLI / 测试夹具（直接给一个裸目录当 chat_history_dir）下不成立，
        会算到隔壁目录去。未注入时回落模块级 `paths.TASKS_DIR`（= default 空间），
        与改造前完全一致。
        """
        if self._tasks_dir is not None:
            return self._tasks_dir
        return paths.TASKS_DIR

    def set_max_context(self, max_context: str | None) -> None:
        """设置会话级上下文窗口覆盖（如 "1M" / "128k"）。

        空串/None 时恢复默认窗口（DEFAULT_MAX_CONTEXT_TOKENS 兜底）。
        LLM 模型/窗口配置统一由 ~/.aigent/llmconfig.json 按模型元数据解析
        （SessionRuntime 每轮把解析结果传入），不再读全局 env
        MAX_CONTEXT_TOKENS（历史 bug：该值与所选模型真实窗口不符导致统计误用 1M）。
        同步影响 ContextCompact 的压缩阈值与前端展示的上下文上限。
        """
        if max_context and str(max_context).strip():
            parsed = self.compact_manager.parse_max_context_tokens(
                str(max_context).strip(), DEFAULT_MAX_CONTEXT_TOKENS
            )
            self.compact_manager.max_context_tokens = parsed
        else:
            self.compact_manager.max_context_tokens = DEFAULT_MAX_CONTEXT_TOKENS

    def context_stats_dict(self, messages: list, max_context: str | None = None) -> dict:
        """计算当前消息的上下文统计 dict（供前端 context_stats 事件）。

        max_context 传入时按该窗口计算（如切会话时按会话元数据解析出的
        所选模型窗口），**不改共享压缩器状态**——并发会话/多次切换互不污染；
        缺省沿用 compact_manager 当前窗口。
        """
        cm = self.compact_manager
        if max_context and str(max_context).strip():
            window = cm.parse_max_context_tokens(
                str(max_context).strip(), cm.max_context_tokens)
        else:
            window = cm.max_context_tokens
        used = cm.estimate_tokens(messages)
        used_percent = min(100.0, (used / window) * 100) if window else 0.0
        return {
            "used_tokens": used,
            "max_tokens": window,
            "used_percent": round(used_percent, 1),
            "max_label": cm.format_token_count(window),
        }
    def get_latest_session(self) -> tuple[Optional[str], Optional[Path]]:
        """
        获取「最近使用」的会话 id 和文件路径。

        排序键与 list_sessions 一致：元数据 updated_at（无则文件 mtime 兜底）
        取最新 —— 编号退役后不再有"最大编号"概念，最近使用即最新会话。

        Returns:
            (会话 id, 会话文件路径) 如果没有会话文件则返回 (None, None)
        """
        best_sid, best_file, best_key = None, None, 0.0
        for f in self._iter_session_files():
            sid = self._sid_from_stem(f.stem)
            if sid is None:
                continue
            key = self._session_sort_key(f)
            if key > best_key:
                best_sid, best_file, best_key = sid, f, key
        if best_sid is None or best_file is None:
            return None, None
        return best_sid, best_file

    def get_session_file(self, session_id: str) -> Path:
        """
        根据会话 id 获取会话文件路径（存量会话 id 为原编号字符串，如 "6"）

        Args:
            session_id: 会话 id（str）

        Returns:
            会话文件路径
        """
        return self.chat_history_dir / f"{self.session_prefix}{session_id}.jsonl"

    # ── 文件枚举与解析（session_N 自增编号 → 短 id 共存兼容） ────────

    def _iter_session_files(self) -> list[Path]:
        """枚举本前缀的会话主文件，排除旁路/备份/临时文件，按文件名稳定排序。"""
        files = []
        for f in self.chat_history_dir.glob(f"{self.session_prefix}*.jsonl"):
            # 排除旁路文件（session_N.subagents.jsonl 的 stem 含 "."）与
            # .jsonl.tmp 之外的派生文件；sidecar/备份的 stem 均带后缀段
            if "." in f.stem[len(self.session_prefix):]:
                continue
            files.append(f)
        return sorted(files, key=lambda p: p.name)

    def _sid_from_stem(self, stem: str) -> Optional[str]:
        """从文件 stem 解析会话 id："session_3"/"session_Kx7mQ2vT8p" → "3"/"Kx7mQ2vT8p"。

        空串视为非法（返回 None）；不做 int 转换 —— 存量编号以字符串形式即 id。
        """
        sid = stem[len(self.session_prefix):] if stem.startswith(self.session_prefix) else ""
        return sid or None

    def _session_sort_key(self, session_file: Path) -> float:
        """会话排序键：「最后修改时间」的 epoch 秒（float，保留亚秒精度）。

        取元数据 updated_at 与文件 mtime 的较大者：
        - meta 轨会话：append/重写路径会同步刷 updated_at（秒级 iso）；
        - index 轨存量会话不单独刷新 → mtime（亚秒精度）兜底；
        - 两者取 max 保证任何轨道下都语义正确，且同秒内的多次修改
          仍能被 mtime 的亚秒精度区分。
        """
        ts = 0.0
        sid = self._sid_from_stem(session_file.stem) or ""
        meta = self.load_meta(sid) if sid else None
        if meta is None:
            meta = self.load_index().get(session_file.name) or {}
        raw = str(meta.get("updated_at") or "")
        if raw:
            try:
                ts = datetime.fromisoformat(raw).timestamp()
            except ValueError:
                ts = 0.0
        try:
            mtime = session_file.stat().st_mtime
        except OSError:
            mtime = 0.0
        return max(ts, mtime)

    def load_session_history(self, session_file: Path) -> list:
        """
        从jsonl文件加载对话历史

        容错策略：
        - 正常情况：每行一个 JSON，按行解析。
        - 异常情况（曾因进程中断导致两个 JSON 拼在同一行）：
          用 raw_decode 把一行内的多个 JSON 全部解出来，跳过空白再继续。
        加载成功后，如果发现存在拼行的情况，会以正确格式重写整个文件，
        避免下次启动再次触发同一错误。

        Args:
            session_file: 会话文件路径

        Returns:
            消息列表
        """
        messages = []
        if not session_file.exists():
            return messages

        # 旁路存储模式：先把历史遗留的 in-file `role=subagent` 行迁到
        # `session_N.subagents.jsonl` 并净化主文件（幂等，首次迁移前留 .bak）。
        # 迁移后主文件只含标准消息 → 下面的块结构自愈不会再被子智能体行打断。
        if self.subagent_store is not None:
            try:
                self.subagent_store.migrate(session_file)
            except Exception as exc:  # noqa: BLE001 - 迁移失败不阻断加载
                log.warning("[子智能体记录迁移] 跳过（%s: %s）",
                            type(exc).__name__, exc)

        repaired = False  # 是否检测到拼行/坏行
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                content = f.read()

            decoder = json.JSONDecoder()
            idx = 0
            n = len(content)
            while idx < n:
                # 跳过行间空白字符
                while idx < n and content[idx] in " \t\r\n":
                    idx += 1
                if idx >= n:
                    break
                obj, end = decoder.raw_decode(content, idx)
                # 检查到下一个非空白字符之间是否缺少换行/空白（拼行）：
                # 从 end 开始跳空白，若一步都没跳（next_idx == end），
                # 且文件还没读完，说明上一个 JSON 紧贴下一个 JSON。
                next_idx = end
                while next_idx < n and content[next_idx] in " \t\r\n":
                    next_idx += 1
                if next_idx == end and next_idx < n:
                    repaired = True
                messages.append(obj)
                idx = next_idx
        except Exception as e:
            log.error("加载会话历史失败: %s", e)

        # 把 dict 形式的 row 转成 load_session_history 期望的消息结构
        # 重置该文件的 subagent 缓存：以本次磁盘内容为准重新填充
        # （避免 load 多次调用时把旧缓存再叠加一遍，导致重写回写出重复行）
        self.subagent_rows.pop(session_file, None)
        normalized = []
        for msg_data in messages:
            if not isinstance(msg_data, dict):
                continue
            msg_role = msg_data.get("role")
            content = msg_data.get("content", "")
            if msg_role == "system":
                normalized.append({"role": "system", "content": content})
            elif msg_role == "user":
                norm = {"role": "user", "content": content}
                # 消息记录时间（UI 展示元数据，不进模型上下文），老行缺省
                if msg_data.get("created_at"):
                    norm["created_at"] = msg_data["created_at"]
                normalized.append(norm)
            elif msg_role == "assistant":
                norm = {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": msg_data.get("reasoning_content", ""),
                    "tool_calls": msg_data.get("tool_calls", []),
                }
                if msg_data.get("created_at"):
                    norm["created_at"] = msg_data["created_at"]
                # usage 为 UI 展示元数据（轮级 token 消耗），不进模型上下文
                #（Agent 侧发送 LLM 前会做白名单投影剔除）
                if msg_data.get("usage"):
                    norm["usage"] = msg_data["usage"]
                # model_info 同为 UI 展示元数据（本轮模型快照 + 净切换 switch），
                # 与 usage 平级保留——否则切会话回放/compact 重写后 footer 模型
                # 与「模型已切换」提示会永久丢失（balance 数据丢失 bug）
                if msg_data.get("model_info"):
                    norm["model_info"] = msg_data["model_info"]
                # usage_session 为 turn 收尾时的会话级累计快照（回放恢复「本会话
                # 累计」footer 第二段）；与 usage/model_info 同为展示元数据，保留
                if msg_data.get("usage_session"):
                    norm["usage_session"] = msg_data["usage_session"]
                normalized.append(norm)
            elif msg_role == "tool":
                normalized.append({
                    "role": "tool",
                    "content": content,
                    "tool_call_id": msg_data.get("tool_call_id", ""),
                })
            elif msg_role == "subagent":
                # 子智能体执行记录：原样保留（只服务回放展示，不进模型上下文；
                # Agent 侧加载后统一过滤）。同时进缓存，供重写后回写。
                row = {
                    "role": "subagent",
                    "subagent_id": msg_data.get("subagent_id", ""),
                    "name": msg_data.get("name", ""),
                    "thinking": msg_data.get("thinking", ""),
                    "toolCalls": msg_data.get("toolCalls", []),
                }
                if msg_data.get("tool_call_id"):
                    row["tool_call_id"] = msg_data["tool_call_id"]
                normalized.append(row)
                self.subagent_rows.setdefault(session_file, []).append(row)
            else:
                # 兜底：未知 role 仍按 user 处理，避免丢消息
                normalized.append({"role": "user", "content": str(content)})
        messages = normalized

        # 修复旧数据：ai(tool_calls) 后面若跟的是 human 消息（旧格式脏数据），
        # 则将其转换为 ToolMessage，避免 OpenAI 报 400
        messages = self._fix_legacy_tool_call_messages(messages)

        # 清理孤儿 AIMessage：上次进程在保存 AIMessage 后、ToolMessage 落盘前
        # 崩溃 / 被中断，导致 tool_calls 没有匹配的 tool 响应。重新加载整段历史
        # 直接回传 OpenAI 会触发 400 invalid_request_error。
        messages, repairs = self._sanitize_orphan_tool_calls(messages)

        # 自愈：发现拼行/坏行，或发生了块结构修复时，把清理后的列表写回文件，
        # 避免每次启动都重复处理同一批问题数据。
        # 重写不可逆（会丢弃历史坏数据）→ 先留 .bak 快照。
        needs_rewrite = repaired or repairs > 0
        if needs_rewrite and messages:
            try:
                self._snapshot_before_rewrite(session_file)
                self.save_session_history(session_file, messages)
                if repaired:
                    log.warning("[会话修复] 检测到历史文件存在拼行，已自动重写为标准 JSONL")
                else:
                    log.warning("[会话修复] 已修复 %d 处块结构问题并写回历史文件", repairs)
            except Exception as e:
                log.error("[会话修复] 重写历史文件失败: %s", e)

        return messages

    def _fix_legacy_tool_call_messages(self, messages: list) -> list:
        """
        修复遗留的 tool_calls 消息格式问题。

        旧版本代码把工具结果存成了 HumanMessage，导致 OpenAI API 要求
        tool_calls 后必须跟 ToolMessage 的校验失败。此函数在加载历史时
        自动将这类脏数据转换为 role=tool 的消息。
        """
        fixed = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            fixed.append(msg)

            # 检查当前消息是否是带 tool_calls 的 assistant 消息
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_call_ids = {
                    tc["id"]
                    for tc in msg["tool_calls"]
                    if isinstance(tc, dict) and "id" in tc
                }
                # 查看下一条消息是否是 user 消息且包含工具结果
                if i + 1 < len(messages):
                    next_msg = messages[i + 1]
                    if next_msg.get("role") == "user" and isinstance(next_msg.get("content"), str):
                        # 尝试解析旧格式的工具结果
                        try:
                            results = json.loads(next_msg["content"])
                            if isinstance(results, list) and results and all(
                                isinstance(r, dict) and "tool_id" in r for r in results
                            ):
                                # 这是旧格式的工具结果，转换为 tool 消息
                                for r in results:
                                    tc_id = r.get("tool_id", "")
                                    if tc_id in tool_call_ids:
                                        fixed.append({
                                            "role": "tool",
                                            "content": json.dumps(r, ensure_ascii=False),
                                            "tool_call_id": tc_id,
                                        })
                                i += 1  # 跳过已处理的 user 消息
                        except (json.JSONDecodeError, TypeError):
                            pass
            i += 1

        return fixed

    def _sanitize_orphan_tool_calls(self, messages: list) -> tuple[list, int]:
        """
        保守修复 tool 块结构，**绝不静默删除整轮对话**。

        背景（历史数据丢失 bug）：带 tool_calls 的 assistant 消息若其后没有匹配
        tool 响应（进程在两者之间崩溃），直接回传 OpenAI 会触发 400：
            An assistant message with 'tool_calls' must be followed by tool
            messages responding to each 'tool_call_id'.
        旧实现丢弃该 assistant 及其后续 tool 消息；一旦块结构被子智能体记录行
        （role=subagent，曾与标准消息混写在同一 jsonl）打断，就会误判为孤儿并
        **删除整轮对话 + 原子重写文件**（不可逆）。

        新实现采取保守策略：
        1. `role=subagent` 记录行不参与、也不打断 tool 块连续性判定（扫描时跳过
           并暂存），它不属于模型消息；
        2. 缺失的 tool 响应用占位 tool 消息补齐，而不是丢弃 assistant ——
           宁可让模型看到一条"结果缺失"，也不让用户丢掉一整轮对话；
        3. 暂存的 subagent 行统一移到该块之后，避免生成
           `assistant → subagent → tool` 这种非法顺序（新数据走旁路文件后
           不会再产生，此处仅兜底旧数据）；
        4. 仅"孤立 tool 消息"（前面确无匹配 assistant.tool_calls）才丢弃 ——
           这是 OpenAI 明确拒绝的结构，且无独立可恢复的信息。

        Returns:
            (修复后的消息列表, 修复动作条数)
        """
        sanitized: list = []
        repairs = 0
        i = 0
        n = len(messages)
        while i < n:
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                expected_ids = [
                    tc["id"] for tc in msg["tool_calls"]
                    if isinstance(tc, dict) and tc.get("id")
                ]
                if not expected_ids:
                    sanitized.append(msg)
                    i += 1
                    continue
                expected = set(expected_ids)

                # 向后扫描本块的 tool 响应；跳过（并暂存）subagent 记录行，
                # 绝不允许它打断块连续性判定。
                j = i + 1
                found_ids: set[str] = set()
                block_tools: list = []
                subagent_rows: list = []
                while j < n:
                    role = messages[j].get("role")
                    if role == "tool":
                        block_tools.append(messages[j])
                        if messages[j].get("tool_call_id") in expected:
                            found_ids.add(messages[j].get("tool_call_id"))
                        j += 1
                        if found_ids == expected:
                            break
                        continue
                    if role == "subagent":
                        subagent_rows.append(messages[j])
                        j += 1
                        continue
                    break

                missing = expected - found_ids
                sanitized.append(msg)
                sanitized.extend(block_tools)
                if missing:
                    for tcid in expected_ids:
                        if tcid in missing:
                            sanitized.append({
                                "role": "tool",
                                "tool_call_id": tcid,
                                "content": "Error: missing tool result (recovered)",
                            })
                    repairs += 1
                    log.warning(
                        "[会话修复] 补齐缺失的工具响应 "
                        "（%s，占位写入而非丢弃该轮对话）", sorted(missing)
                    )
                if subagent_rows:
                    # 子智能体记录移到块后，恢复合法结构
                    sanitized.extend(subagent_rows)
                    repairs += 1
                    log.warning(
                        "[会话修复] 归位 %d 条子智能体记录行 "
                        "（移出 assistant/tool 块中间）", len(subagent_rows)
                    )
                i = j
                continue

            # 孤立 tool 消息：前面没有带匹配 tool_call_id 的 assistant(tool_calls)。
            # 直接回传 OpenAI 会触发 400，且无独立可恢复信息 → 丢弃。
            if msg.get("role") == "tool":
                prev = sanitized[-1] if sanitized else None
                valid_prev = (
                    prev is not None
                    and prev.get("role") == "assistant"
                    and any(
                        isinstance(tc, dict) and tc.get("id") == msg.get("tool_call_id")
                        for tc in (prev.get("tool_calls") or [])
                    )
                )
                if not valid_prev:
                    repairs += 1
                    log.warning(
                        "[会话修复] 丢弃孤儿 tool 消息 "
                        "（缺少匹配的 assistant.tool_calls，"
                        "tool_call_id=%r）", msg.get("tool_call_id")
                    )
                    i += 1
                    continue

            sanitized.append(msg)
            i += 1
        return sanitized, repairs

    def _snapshot_before_rewrite(self, session_file: Path) -> None:
        """自愈重写前留一份 `.bak` 快照（仅首次，不覆盖更早的备份）。

        自愈重写是"丢弃历史坏数据"的不可逆操作；留快照让误判可人工回滚。
        与旁路记录迁移使用同一快照名（谁先写谁生效，语义都是"最初形态快照"）。
        """
        backup = session_file.with_name(session_file.name + ".bak")
        if backup.exists() or not session_file.exists():
            return
        try:
            backup.write_bytes(session_file.read_bytes())
            log.warning("[会话修复] 已留备份快照 %s（重写前）", backup.name)
        except OSError as e:
            log.error("[会话修复] 备份失败（继续重写）: %s", e)

    def _message_to_json_row(self, message) -> dict:
        """将 OpenAI JSON 格式消息转换为 jsonl 行（与 load_session_history 读取结构保持一致）。"""
        role = message.get("role")
        if role == "system":
            return {"role": "system", "content": message.get("content", "")}
        elif role == "user":
            row = {"role": "user", "content": message.get("content", "")}
            # 消息记录时间（前端右下角展示）：新消息落盘时打点，
            # 重写（compact/自愈）时保留行内已有值，避免老行被误改时间
            row["created_at"] = message.get("created_at") or _now_iso()
            return row
        elif role == "assistant":
            row = {
                "role": "assistant",
                "content": message.get("content", ""),
                "reasoning_content": message.get("reasoning_content", ""),
                "tool_calls": message.get("tool_calls", []),
            }
            row["created_at"] = message.get("created_at") or _now_iso()
            # 轮级 token 消耗（UI 展示元数据），存在才写入
            if message.get("usage"):
                row["usage"] = message["usage"]
            # 本轮模型快照 + 净切换（model_info），与 usage 平级保留
            if message.get("model_info"):
                row["model_info"] = message["model_info"]
            # 会话级累计快照（回放恢复 footer 第二段），存在才写入
            if message.get("usage_session"):
                row["usage_session"] = message["usage_session"]
            return row
        elif role == "tool":
            return {
                "role": "tool",
                "content": message.get("content", ""),
                "tool_call_id": message.get("tool_call_id", ""),
            }
        elif role == "subagent":
            # 子智能体执行记录：独立行，只服务回放展示，不进模型上下文
            row = {
                "role": "subagent",
                "subagent_id": message.get("subagent_id", ""),
                "name": message.get("name", ""),
                "thinking": message.get("thinking", ""),
                "toolCalls": message.get("toolCalls", []),
            }
            if message.get("tool_call_id"):
                row["tool_call_id"] = message["tool_call_id"]
            return row
        else:
            return {"role": "unknown", "content": str(message.get("content", ""))}

    def _json_safe(self, value):
        """确保 LangChain 附加元数据可以稳定写入 jsonl。"""
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except TypeError:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def append_message_to_session(self, session_file: Path, message) -> None:
        """
        向会话文件追加一条消息

        Args:
            session_file: 会话文件路径
            message: 消息对象 (SystemMessage/HumanMessage/AIMessage/ToolMessage)
        """
        try:
            with self._append_lock:
                with open(session_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(self._message_to_json_row(message), ensure_ascii=False) + "\n")
            # 会话内容变化 → 刷新元数据 updated_at（「按最后修改时间排序」的数据源）
            self._touch_updated_at(session_file)
        except Exception as e:
            log.error("写入会话历史失败: %s", e)

    def append_usage_to_last_assistant(self, session_file: Path, usage: dict,
                                       model_info: dict | None = None,
                                       usage_session: dict | None = None) -> bool:
        """把轮级 token 消耗 + 模型快照写进会话文件**最后一条 assistant 行**（就地重写最后一行）。

        turn 收尾时调用：末条 assistant 消息行落盘在前（append_message_to_session），
        而整轮 usage 汇总（主循环全部调用 + 同步子智能体）要等 turn 结束才能确定，
        故用「读尾块定位最后一行 → truncate 行首 → 重写该行」补写 usage 字段；
        model_info（本轮使用的模型与参数快照）与之同一次重写补进 model_info 节点，
        与 usage 平级——两者概念独立（配置 vs 消耗），且都被 _model_messages
        白名单投影挡在 LLM 上下文之外。usage_session（turn 收尾时的会话级累计快照）
        同样补进该行——旧回放只见「本轮」段，切会话后「本会话累计」段缺失；持久化后
        回放 footer 第二段也能恢复（与实时 usage_stats 事件同构）。
        末行不是 assistant（停止/异常收尾在 tool/user 行截断）时跳过返回 False。

        持 _append_lock 与 append 互斥；调用方需同步内存态（history_messages[-1]）。
        """
        if not usage or not session_file.exists():
            return False
        try:
            with self._append_lock:
                # 从文件尾部反向找最后一个完整行（块读避免整文件加载）
                with open(session_file, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    remaining, block = size, 4096
                    tail = b""
                    while remaining > 0:
                        read_len = min(block, remaining)
                        remaining -= read_len
                        f.seek(remaining)
                        chunk = f.read(read_len)
                        tail = chunk + tail
                        if b"\n" in chunk:
                            break
                lines = tail.rstrip(b"\n").split(b"\n") if tail.strip() else []
                if not lines:
                    return False
                last = lines[-1]
                try:
                    obj = json.loads(last)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return False
                if not isinstance(obj, dict) or obj.get("role") != "assistant":
                    return False
                obj["usage"] = usage
                if model_info:
                    obj["model_info"] = model_info
                if usage_session:
                    obj["usage_session"] = usage_session
                new_line = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
                # 行首偏移 = 文件大小 - 末行字节长度 - 1（行尾换行符）
                line_start = size - len(last) - 1
                with open(session_file, "r+b") as f:
                    f.truncate(line_start)
                    f.seek(line_start)
                    f.write(new_line)
            return True
        except OSError as e:
            log.error("写入轮级 usage 失败: %s", e)
            return False

    def append_switch_to_last_assistant(self, session_file: Path, switch: dict) -> bool:
        """把一次空闲期模型切换写进文件**最后一条 assistant 行**的 `model_info.switch`。

        与 append_usage_to_last_assistant 体例一致：读尾块定位最后一行 → truncate 行首
        → 重写该行。切换发生在空闲期，末条 assistant 必为上一轮已完成答复；把 switch
        挂到它（而非下一轮答复）是「切换时最后一条 assistant 消息展示」的正确口径。
        末行不是 assistant（切换发生在尚无任何答复的空会话）时跳过返回 False。
        """
        if not switch or not session_file.exists():
            return False
        try:
            with self._append_lock:
                with open(session_file, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    remaining, block = size, 4096
                    tail = b""
                    while remaining > 0:
                        read_len = min(block, remaining)
                        remaining -= read_len
                        f.seek(remaining)
                        chunk = f.read(read_len)
                        tail = chunk + tail
                        if b"\n" in chunk:
                            break
                lines = tail.rstrip(b"\n").split(b"\n") if tail.strip() else []
                if not lines:
                    return False
                last = lines[-1]
                try:
                    obj = json.loads(last)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return False
                if not isinstance(obj, dict) or obj.get("role") != "assistant":
                    return False
                model_info = dict(obj.get("model_info") or {})
                model_info["switch"] = switch
                obj["model_info"] = model_info
                new_line = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
                line_start = size - len(last) - 1
                with open(session_file, "r+b") as f:
                    f.truncate(line_start)
                    f.seek(line_start)
                    f.write(new_line)
            return True
        except OSError as e:
            log.error("写入空闲期模型切换失败: %s", e)
            return False

    def append_subagent_to_session(self, session_file: Path, transcript: dict) -> None:
        """
        把子智能体执行过程写入会话记录（终态行）。

        **旁路存储模式（桌面端，注入 subagent_store）**：写到独立的
        `session_N.subagents.jsonl`，主会话文件只保留标准消息。这是
        「写入位置错误 → 整轮对话被自愈逻辑删除」（D1）的结构性修复：
        两类数据物理隔离，append 顺序不再是隐式契约。

        未注入 store（CLI 旧路径）：沿用 in-file `role=subagent` 行。

        transcript 结构：{subagent_id, name, thinking, toolCalls}，可选
        tool_call_id（发起方主智能体 tool_call 的 id，回放时据此把记录挂回
        对应 assistant 消息下）、text / error / duration_ms / started_at。
        """
        if self.subagent_store is not None:
            try:
                self.subagent_store.append(session_file, transcript)
                # 旁路记录也是会话活动 → 刷新 updated_at
                self._touch_updated_at(session_file)
            except Exception as e:
                log.error("写入子智能体执行记录失败: %s", e)
            return
        row = {
            "role": "subagent",
            "subagent_id": transcript.get("subagent_id", ""),
            "name": transcript.get("name", ""),
            "thinking": transcript.get("thinking", ""),
            "toolCalls": transcript.get("toolCalls", []),
        }
        if transcript.get("tool_call_id"):
            row["tool_call_id"] = transcript["tool_call_id"]
        try:
            with self._append_lock:
                with open(session_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                # 缓存与写盘在同一把锁内更新，避免与 save_session_history 重写竞争
                self.subagent_rows.setdefault(session_file, []).append(row)
        except Exception as e:
            log.error("写入子智能体执行记录失败: %s", e)

    def begin_subagent(self, session_file: Path, subagent_id: str,
                       tool_call_id: str = "", name: str = "",
                       prompt: str = "", source: str = "sync") -> None:
        """子智能体启动时的占位记录（仅旁路存储模式生效）。

        写一条 `status=running` 行：进程被强杀（无终态行）时历史里仍留有痕迹，
        卡片显示"运行中/已中断"；同时让实时回放能立刻看到卡片。
        """
        if self.subagent_store is None:
            return
        try:
            self.subagent_store.begin(
                session_file, subagent_id=subagent_id, tool_call_id=tool_call_id,
                name=name, prompt=prompt, source=source,
            )
        except Exception as e:
            log.error("写入子智能体启动占位记录失败: %s", e)

    def load_subagent_records(self, session_file: Path) -> list:
        """读取某会话的子智能体执行记录（供桌面端回放挂载）。

        旁路存储模式读 `session_N.subagents.jsonl`（按 subagent_id 取末条）；
        未注入 store 时回退读 in-file `role=subagent` 行（兼容旧路径）。
        """
        if self.subagent_store is not None:
            try:
                return self.subagent_store.load(session_file)
            except Exception as e:
                log.error("读取子智能体执行记录失败: %s", e)
                return []
        return [dict(r) for r in self._read_subagent_rows(session_file)]

    def _read_subagent_rows(self, session_file: Path) -> list:
        """
        读取会话文件中全部 role=subagent 行（缓存优先，缺失时读磁盘并回填缓存）。

        缓存与磁盘保持一致：load_session_history / append_subagent_to_session
        维护缓存，save_session_history 重写后同步缓存。
        """
        cached = self.subagent_rows.get(session_file)
        if cached is not None:
            return cached
        rows = []
        if session_file.exists():
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj, dict) and obj.get("role") == "subagent":
                            rows.append(obj)
            except OSError:
                return rows
        self.subagent_rows[session_file] = rows
        return rows

    def save_session_history(self, session_file: Path, messages: list) -> None:
        """
        原子重写完整会话历史，保证磁盘 jsonl 与内存 messages 一致。

        子智能体执行记录（role=subagent 行）不属于 messages（模型上下文）：
        重写前从缓存/旧文件提取，重写后原样回写（与 messages 中已有的
        按 subagent_id 去重），保证 compact / 自愈重写不丢子智能体记录。

        旁路存储模式下主文件本就不含 subagent 行（记录在
        `session_N.subagents.jsonl`），因此无需（也不得）回写 —— 压缩 /
        自愈重写天然与子智能体记录互不影响。
        """
        session_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = session_file.with_suffix(session_file.suffix + ".tmp")

        try:
            with self._append_lock:
                # 读缓存与写盘同锁：后台 sub_agent 完成线程可能在重写期间
                # append 新行，加锁避免读到一半被并发修改/写盘错过新行。
                old_subagent_rows = (
                    [] if self.subagent_store is not None
                    else self._read_subagent_rows(session_file)
                )
                with open(tmp_file, "w", encoding="utf-8") as f:
                    for message in messages:
                        f.write(json.dumps(self._message_to_json_row(message), ensure_ascii=False) + "\n")
                tmp_file.replace(session_file)
                # 重写后回写 subagent 行
                if old_subagent_rows:
                    covered = {
                        m.get("subagent_id") for m in messages
                        if isinstance(m, dict) and m.get("role") == "subagent"
                    }
                    with open(session_file, "a", encoding="utf-8") as f:
                        for row in old_subagent_rows:
                            if row.get("subagent_id") not in covered:
                                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            # 缓存与磁盘保持一致（重写后旧行已全部回写）
            self.subagent_rows[session_file] = old_subagent_rows
        except Exception as e:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
            log.error("重写会话历史失败: %s", e)
            raise
        # 重写也是会话内容变化 → 刷新元数据 updated_at（放 try/except 外，失败不影响主流程）
        self._touch_updated_at(session_file)

    def maybe_compact_context(
        self,
        history_messages: list,
        session_file: Path,
        manual: bool = False,
    ) -> None:
        """
        检查并按阈值执行上下文压缩。

        manual=True 用于 /compact：仍遵守触发阈值，未达阈值时只提示当前状态。
        """
        stats = self.compact_manager.context_stats(history_messages)
        if not manual and stats.used_percent < 95:
            return

        log.warning(
            "[上下文压缩] 正在检查上下文：当前 %s/%s tokens，剩余 %d%%",
            stats.used_tokens, stats.max_label, int(stats.remaining_percent)
        )
        self.compact_messages_if_needed(
            history_messages,
            session_file,
            force=False,
            announce=True,
        )

    def compact_messages_if_needed(self, messages: list, session_file: Path, force: bool = False, announce: bool = False):
        """
        执行上下文压缩，并在发生变化时同步更新内存和会话文件。
        """
        result = self.compact_manager.compact_if_needed(messages, force=force)
        if announce:
            self._print_compact_result(result, force=force)
        if result.changed:
            messages[:] = result.messages
            self.save_session_history(session_file, messages)
        return result

    def _print_compact_result(self, result, force: bool = False) -> None:
        """
        以黄色提示行打印 compact 后的结果摘要。

        - `result.before is None` 时直接返回（压缩未实际执行）。
        - 若 `result.changed` 为 False：打印“无需压缩”一行，区分是否因 force
          而给出不同原因。
        - 若发生压缩：根据 `result.operations` 拼接各阶段操作（落盘超大工具
          输出 / 裁掉中间消息 / 旧工具结果占位 / LLM 摘要替换 / reactive
          兜底），再附上压缩后的 token 用量与剩余比例。
        """
        before = result.before
        after = result.after
        if before is None:
            return

        if not result.changed:
            reason = "未达到 L4 摘要阈值（已跑 L1/L2/L3 内部检查均无需处理）" if not force else "没有可压缩的历史消息"
            log.warning(
                "[上下文压缩] %s：当前 %s/%s tokens，剩余 %d%%",
                reason, before.used_tokens, before.max_label,
                int(before.remaining_percent)
            )
            return

        ops = result.operations
        parts = []
        if ops.get("tool_results_persisted"):
            parts.append(f"落盘超大工具输出 {ops['tool_results_persisted']} 条")
        if ops.get("messages_snip_compacted"):
            parts.append(f"裁掉中间消息 {ops['messages_snip_compacted']} 条")
        if ops.get("tool_results_micro_compacted"):
            parts.append(f"占位旧工具结果 {ops['tool_results_micro_compacted']} 条")
        if ops.get("summary_messages_replaced"):
            parts.append(f"LLM 摘要替换 {ops['summary_messages_replaced']} 条")
        if ops.get("reactive_compact_triggered"):
            parts.append("触发 reactive 兜底压缩")
        summary = "；".join(parts) if parts else "已整理上下文"
        after_text = f"{after.used_tokens}/{after.max_label} tokens，剩余 {int(after.remaining_percent)}%" if after else "未知"
        log.warning("[上下文压缩完成] %s；压缩后 %s", summary, after_text)

    def _build_initial_messages(self) -> list:
        """
        构造新会话的初始消息。

        第一条为 SystemMessage；workspace 指令文件（CLAUDE.md / AGENT.md）
        已在 system_prompt 构造阶段拼入，不再单独注入 user 消息。
        """
        return [{"role": "system", "content": self.system_prompt}]

    def create_initialized_session(self) -> tuple[str, Path, list]:
        """
        创建新会话并写入完整初始消息。

        Returns:
            (新会话 id, 新会话文件路径, 初始消息列表)
        """
        new_sid, new_file = self.create_new_session()
        messages = self._build_initial_messages()
        for message in messages:
            self.append_message_to_session(new_file, message)
        return new_sid, new_file, messages

    def create_new_session(self) -> tuple[str, Path]:
        """
        创建新会话：随机短 id 命名（撞名重掷，工程上不可能重复）

        查重范围 = 本空间目录 **+ 全部工作空间**（`set_session_id_guard` 注入的
        全局守卫）：session_id 是全链路路由键，跨空间重号会导致事件/归属错位。

        Returns:
            (新会话 id, 新会话文件路径)
        """
        sid = new_session_id()
        new_file = self.get_session_file(sid)
        while new_file.exists() or _id_taken_globally(sid):  # 碰撞重试：防御性兜底
            sid = new_session_id()
            new_file = self.get_session_file(sid)
        new_file.touch()
        # 同步写入元数据条目（标题/创建时间/状态/项目归属）
        self.ensure_index_entry(sid)
        return sid, new_file

    def init_session(self) -> tuple[str, Path, list]:
        """
        初始化会话：加载最后一次对话或创建新对话

        Returns:
            (会话 id, 会话文件路径, 消息列表)
        """
        sid, session_file = self.get_latest_session()

        if session_file and session_file.exists():
            messages = self.load_session_history(session_file)
            if messages:
                log.info("已加载会话: session_%s.jsonl (%d 条消息)",
                         sid, len(messages))
                return sid, session_file, messages

        new_sid, new_file, messages = self.create_initialized_session()
        log.info("已创建新会话: session_%s.jsonl", new_sid)
        return new_sid, new_file, messages

    def switch_session(self, target_id: str) -> tuple[str, Path, list]:
        """
        切换到指定会话

        Args:
            target_id: 目标会话 id（存量会话为原编号字符串）

        Returns:
            (会话 id, 会话文件路径, 消息列表)

        Raises:
            FileNotFoundError: 会话文件不存在
        """
        target_file = self.get_session_file(target_id)
        if not target_file.exists():
            raise FileNotFoundError(f"会话 session_{target_id}.jsonl 不存在")

        messages = self.load_session_history(target_file)
        return target_id, target_file, messages

    # ═══════════════════════════════════════════════════════════
    #  会话元数据（独立 meta 文件 + index.jsonl 兜底轨道）
    #  与 session_<id>.jsonl 通过文件名关联；以文件名（含前缀）为键。
    #  新会话 id 为随机短 id；存量会话 id 为原编号字符串，两条轨道均兼容。
    # ═══════════════════════════════════════════════════════════

    @property
    def index_file(self) -> Path:
        """会话元数据索引文件（与 chat history 同目录）。"""
        return self.chat_history_dir / "index.jsonl"

    def meta_file(self, session_id: str) -> Path:
        """会话独立元数据路径：{chat_history_dir}/session_<id>.meta.json。"""
        return self.chat_history_dir / f"{self.session_prefix}{session_id}.meta.json"

    def load_meta(self, session_id: str) -> Optional[dict]:
        """读取单个会话的独立元数据；文件不存在返回 None。O(1)。"""
        p = self.meta_file(session_id)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def save_meta(self, meta: dict) -> None:
        """原子写单个 meta 文件（tmp + replace，复用 save_index 的写入模式）。O(1)。"""
        sid = str(meta.get("id") or meta.get("num") or "")
        meta_path = self.meta_file(sid)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = meta_path.with_suffix(meta_path.suffix + ".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            tmp_file.replace(meta_path)
        except Exception as e:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
            log.error("写入会话元数据失败: %s", e)
            raise

    def _new_entry(self, session_id: str, file_name: str) -> dict:
        """构造新会话的默认元数据条目。id 为字符串；存量条目的 num 字段读取时兼容。"""
        now = _now_iso()
        return {
            "id": session_id,
            "file": file_name,
            "title": None,
            "title_source": "none",
            "created_at": now,
            "updated_at": now,
            "status": "active",
            "trashed_at": None,
            "project": self.project_id,
            "model_id": None,
            "overrides": None,
            "unread": False,
        }

    def _meta_entry_for(self, session_file: Path) -> dict:
        """取某会话的元数据条目：优先独立 meta 文件，回退 index.jsonl（存量会话）。"""
        sid = self._sid_from_stem(session_file.stem)
        if sid:
            meta = self.load_meta(sid)
            if meta is not None:
                return meta
        return self.load_index().get(session_file.name) or {}

    def load_index(self) -> dict[str, dict]:
        """读取元数据索引：{jsonl 文件名: 元数据 dict}；坏行跳过。"""
        entries: dict[str, dict] = {}
        if not self.index_file.exists():
            return entries
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("file"):
                        entries[str(obj["file"])] = obj
        except OSError as e:
            log.error("读取会话元数据索引失败: %s", e)
        return entries

    def save_index(self, entries: dict[str, dict]) -> None:
        """原子重写元数据索引（tmp + replace，同 save_session_history 模式）。"""
        index = self.index_file
        index.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = index.with_suffix(index.suffix + ".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                for obj in entries.values():
                    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            tmp_file.replace(index)
        except Exception as e:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
            log.error("重写会话元数据索引失败: %s", e)
            raise

    def backfill_index(self) -> None:
        """对账索引：目录内所有 jsonl 缺条目的补录；索引中 jsonl 已不存在的剔除。

        - glob 全部 *.jsonl（含其他前缀，如 cron_），避免误删别家前缀的条目
        - 已有独立 meta 文件的会话（新方案）跳过，不写入 index.jsonl
        - 老会话补录：title=null、created_at 取文件 mtime、status=active
        - stem 解析走 _sid_from_stem（存量编号与短 id 共存兼容）
        """
        entries = self.load_index()
        changed = False
        existing: set[str] = set()
        metas: set[str] = set()
        for f in self.chat_history_dir.glob("*.jsonl"):
            if f.name == self.index_file.name:
                continue
            if "." in f.stem[len(self.session_prefix):] and f.stem.startswith(self.session_prefix):
                continue  # 旁路/派生文件（stem 含后缀段）不入索引
            sid = self._sid_from_stem(f.stem) if f.stem.startswith(self.session_prefix) else None
            if sid is None and not f.stem.startswith(self.session_prefix):
                # 其他前缀（如 cron_）仅作对账占位，不解析 id
                existing.add(f.name)
                continue
            if sid is None:
                continue
            existing.add(f.name)
            if self.meta_file(sid).exists():
                # 新方案会话：元数据独立维护，index.jsonl 不再承载；跳过并在清理时保留
                metas.add(f.name)
                continue
            if f.name in entries:
                continue
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
            except OSError:
                mtime = datetime.now()
            ts = mtime.isoformat(timespec="seconds")
            entries[f.name] = {
                "id": sid,
                "file": f.name,
                "title": None,
                "title_source": "none",
                "created_at": ts,
                "updated_at": ts,
                "status": "active",
                "trashed_at": None,
                "project": self.project_id,
                "unread": False,
            }
            changed = True
        for key in [k for k in entries if k not in existing and k not in metas]:
            entries.pop(key)
            changed = True
        if changed:
            self.save_index(entries)

    def ensure_index_entry(self, session_id: str) -> None:
        """新建会话时写入独立元数据文件（已存在则跳过）。O(1)。"""
        if self.load_meta(session_id):
            return
        key = self.get_session_file(session_id).name
        self.save_meta(self._new_entry(session_id, key))

    def _touch_updated_at(self, session_file: Path) -> None:
        """会话内容变化（追加/重写/清空）后刷新元数据的 updated_at。

        「按最后修改时间排序」的数据来源：
        - meta 轨会话：这里同步刷新 updated_at（每条消息一次 <1KB 小文件写，
          本地 SSD 无感）；
        - index 轨存量会话：不单独刷新（避免全量重写 index），排序时由
          _session_sort_key 用文件 mtime 兜底，语义等价。
        """
        sid = self._sid_from_stem(session_file.stem)
        if not sid:
            return
        meta = self.load_meta(sid)
        if meta is None:
            return
        meta["updated_at"] = _now_iso()
        try:
            self.save_meta(meta)
        except Exception:
            pass  # 刷新失败只影响排序精度，不阻断消息写入主流程

    def _update_entry(self, session_id: str, mutate) -> dict:
        """定位条目 → mutate(entry) → 刷新 updated_at → 原子写回。

        新方案会话（存在独立 meta 文件）直接读写单文件 O(1)；
        存量会话回退 index.jsonl 全量路径，作为兜底。

        Raises:
            FileNotFoundError: 会话 jsonl 不存在
        """
        session_file = self.get_session_file(session_id)
        if not session_file.exists():
            raise FileNotFoundError(f"会话 {session_file.name} 不存在")
        key = session_file.name
        with self._index_lock:
            if self.meta_file(session_id).exists():
                entry = self.load_meta(session_id) or self._new_entry(session_id, key)
                mutate(entry)
                entry["updated_at"] = _now_iso()
                self.save_meta(entry)
            else:
                # 存量会话：走 index.jsonl 全量路径
                entries = self.load_index()
                entry = entries.get(key)
                if entry is None:
                    try:
                        mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
                    except OSError:
                        mtime = datetime.now()
                    ts = mtime.isoformat(timespec="seconds")
                    entry = self._new_entry(session_id, key)
                    entry["created_at"] = entry["updated_at"] = ts
                    entries[key] = entry
                mutate(entry)
                entry["updated_at"] = _now_iso()
                self.save_index(entries)
        return entry

    def rename_session(self, session_id: str, title: str) -> dict:
        """重命名会话（title_source=user，自动生成不再覆盖）。"""
        title = title.strip()
        if not title:
            raise ValueError("标题不能为空")
        return self._update_entry(
            session_id, lambda e: e.update({"title": title[:60], "title_source": "user"})
        )

    def set_auto_title(self, session_id: str, title: str, source: str = "auto") -> None:
        """写入自动生成的标题；用户手动改名（title_source=user）不覆盖。"""
        def mutate(e):
            if e.get("title_source") == "user":
                return
            e.update({"title": title[:60], "title_source": source})
        try:
            self._update_entry(session_id, mutate)
        except FileNotFoundError:
            pass

    def set_unread(self, session_id: str, unread: bool = False) -> dict:
        """记录会话的未读/已读状态（写入元数据，兼容新 meta 文件/存量 index）。

        语义由前端驱动：会话完整结束且用户当前不在查看它 → unread=True；
        用户进入（切换/点击查看）该会话 → unread=False。跨窗口/重启持久化。
        """
        return self._update_entry(session_id, lambda e: e.update({"unread": bool(unread)}))

    def set_session_model(self, session_id: str, model_id: str | None = None,
                          overrides: dict | None = None) -> dict:
        """记录会话最后选择的模型与其参数（写入元数据，兼容新 meta 文件/存量 index）。

        model_id: 该会话绑定的模型 id；None 表示不修改该项。
        overrides: 按模型 id 分别保存的 UI 级参数覆盖
                   { [model_id]: {thinking_strength?, max_context_option?} }；None 表示不修改。
        """
        def mutate(e):
            if model_id is not None:
                e["model_id"] = model_id
            if overrides is not None:
                e["overrides"] = overrides
        return self._update_entry(session_id, mutate)

    def add_usage_totals(self, session_id: str, delta: dict,
                         count_turn: bool = True) -> None:
        """把一轮 token 消耗增量累进会话元数据 usage_totals（O(1) 原子写）。

        delta 为轮级 usage dict（USAGE_FIELDS 四字段）；count_turn=False 用于
        后台子智能体迟到完成的补记（只加量不计数，turns 已在该轮收尾时 +1）。
        """
        if not delta:
            return

        def mutate(e):
            totals = e.get("usage_totals") or {}
            for k in USAGE_FIELDS:
                totals[k] = int(totals.get(k) or 0) + int(delta.get(k) or 0)
            if count_turn:
                totals["turns"] = int(totals.get("turns") or 0) + 1
            e["usage_totals"] = totals

        try:
            self._update_entry(session_id, mutate)
        except FileNotFoundError:
            pass  # 会话文件已不存在（如被并发删除），统计丢失可接受

    def trash_session(self, session_id: str) -> dict:
        """软删除：标记 status=trashed，jsonl/todo 原样保留。"""
        return self._update_entry(
            session_id, lambda e: e.update({"status": "trashed", "trashed_at": _now_iso()})
        )

    def restore_session(self, session_id: str) -> dict:
        """从回收站还原：status=active，清空 trashed_at。"""
        return self._update_entry(
            session_id, lambda e: e.update({"status": "active", "trashed_at": None})
        )

    def delete_session_permanent(self, session_id: str) -> bool:
        """永久删除会话：jsonl + 任务板文件 + 子智能体旁路 + 元数据。

        新方案会话（存在独立 meta 文件）删除单文件 O(1)；
        存量会话回退移除 index.jsonl 中对应条目。
        """
        session_file = self.get_session_file(session_id)
        if not session_file.exists():
            return False
        try:
            session_file.unlink()
        except OSError as e:
            log.error("删除会话文件失败: %s", e)
            return False
        # 子智能体旁路记录与主文件同生共死（不残留、不串台）
        if self.subagent_store is not None:
            self.subagent_store.delete(session_file)
        # 任务板与 chat history 同生共死：删除本会话作用域下的全部 task 文件。
        # （原此处删除 todo 文件；todo 已于 2026-09-16 下线，改由 task 承接）
        # 单个删除失败不阻断会话删除 —— 与子智能体旁路文件的处理策略一致。
        for task_file in task_files_for_session(
            session_id, self.session_prefix, self.tasks_dir
        ):
            try:
                task_file.unlink()
            except OSError as e:
                log.error("删除任务文件失败 %s: %s", task_file.name, e)
        with self._index_lock:
            if self.meta_file(session_id).exists():
                try:
                    self.meta_file(session_id).unlink()
                except OSError as e:
                    log.error("删除会话元数据失败: %s", e)
            else:
                # 存量会话：从 index.jsonl 移除对应条目
                entries = self.load_index()
                entries.pop(session_file.name, None)
                self.save_index(entries)
        return True

    def list_sessions(self, status: str = "active") -> list[dict]:
        """
        列出会话（含元数据）

        Args:
            status: "active"（默认，任务树）或 "trashed"（回收站）

        Returns:
            [{id, title, title_source, status, created_at, updated_at,
              trashed_at, file, model_id, project, usage_totals, unread}, ...]
            按「最后修改时间」（元数据 updated_at，mtime 兜底）降序 —— 最近使用的在前
        """
        self.backfill_index()
        sessions = []
        for f in self._iter_session_files():
            sid = self._sid_from_stem(f.stem)
            if sid is None:
                continue
            # 优先独立 meta 文件（新方案会话），否则回退 index.jsonl（存量会话）
            meta = self._meta_entry_for(f)
            sessions.append({
                "id": sid,
                "title": meta.get("title"),
                "title_source": meta.get("title_source", "none"),
                "status": meta.get("status", "active"),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "trashed_at": meta.get("trashed_at"),
                "file": f.name,
                "model_id": meta.get("model_id"),
                # 悬停卡片展示用：所属项目（工作空间）与会话级 token 累计。
                # 存量 meta 缺 project 字段时兜底为**本管理器的空间 id**：
                # 老会话按目录归属，永远不会被错认成 default 空间的会话。
                "project": meta.get("project", self.project_id),
                "usage_totals": meta.get("usage_totals"),
                "unread": bool(meta.get("unread", False)),
            })
        sessions = [s for s in sessions if s.get("status") == status]
        # 排序键与会话列表展示解耦：list_sessions 内单独算 key（含 mtime 兜底），
        # 保证 index 轨存量会话的"追加消息"也能正确参与最近使用排序。
        sessions.sort(
            key=lambda s: self._session_sort_key(self.chat_history_dir / s["file"]),
            reverse=True,
        )
        return sessions

    def clear_session(self, session_file: Path) -> int:
        """
        清空指定会话的历史消息

        清空会话文件内容，只保留系统提示词

        Args:
            session_file: 会话文件路径

        Returns:
            被删除的消息数量
        """
        if not session_file.exists():
            return 0

        # 加载当前会话，获取系统提示词
        messages = self.load_session_history(session_file)
        deleted_count = len(messages)

        # 清空文件并重新写入初始消息
        try:
            with open(session_file, "w", encoding="utf-8") as f:
                pass

            for message in self._build_initial_messages():
                self.append_message_to_session(session_file, message)

            # 子智能体执行记录随会话内容一起清空（缓存同步失效 + 旁路文件删除）
            self.subagent_rows.pop(session_file, None)
            if self.subagent_store is not None:
                self.subagent_store.clear(session_file)

            # 任务板随会话内容同生共死：删除本会话作用域下的全部 task 文件。
            # 注意必须放在这一层（而不是 Agent.clear_session）：ws_bridge 的
            # session_clear 分支是**直接调 sm.clear_session** 的，不经过 Agent。
            sid = self._sid_from_stem(session_file.stem)
            if sid:
                for task_file in task_files_for_session(
                    sid, self.session_prefix, self.tasks_dir
                ):
                    try:
                        task_file.unlink()
                    except OSError as e:
                        log.error("删除任务文件失败 %s: %s", task_file.name, e)

            # token 累计统计随会话内容同生共死：元数据 usage_totals 一并清零
            if sid:
                try:
                    self._update_entry(sid, lambda e: e.pop("usage_totals", None))
                except FileNotFoundError:
                    pass

            return max(0, deleted_count - 1)  # 减去保留的系统提示词
        except Exception as e:
            log.error("清空会话失败: %s", e)
            return 0
