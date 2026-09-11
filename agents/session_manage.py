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
    session_num, session_file, messages = manager.init_session()
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from context_compact import ContextCompact, DEFAULT_MAX_CONTEXT_TOKENS
from paths import DEFAULT_PROJECT_SLUG, todo_file_for_session


def _now_iso() -> str:
    """本地时间秒级 isoformat（单机桌面产品，无时区转换需求）。"""
    return datetime.now().isoformat(timespec="seconds")


class SessionManager:
    """会话管理器，负责对话历史的持久化和管理"""

    def __init__(self, chat_history_dir: Path, system_prompt: str,
                 session_prefix: str = "session_", subagent_store=None):
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
        """
        self.chat_history_dir = chat_history_dir
        self.system_prompt = system_prompt
        self.session_prefix = session_prefix
        self.subagent_store = subagent_store
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

    def set_max_context(self, max_context: str | None) -> None:
        """设置会话级上下文窗口覆盖（如 "1M" / "128k"）。

        空串/None 时恢复为环境变量/默认值。同步影响 ContextCompact 的
        压缩阈值与前端展示的上下文上限。
        """
        if max_context and str(max_context).strip():
            parsed = self.compact_manager.parse_max_context_tokens(
                str(max_context).strip(), DEFAULT_MAX_CONTEXT_TOKENS
            )
            self.compact_manager.max_context_tokens = parsed
        else:
            self.compact_manager.max_context_tokens = self.compact_manager.parse_max_context_tokens(
                os.environ.get("MAX_CONTEXT_TOKENS"), DEFAULT_MAX_CONTEXT_TOKENS
            )

    def context_stats_dict(self, messages: list) -> dict:
        """计算当前消息的上下文统计 dict（供前端 context_stats 事件）。"""
        s = self.compact_manager.context_stats(messages)
        return {
            "used_tokens": s.used_tokens,
            "max_tokens": s.max_tokens,
            "used_percent": round(s.used_percent, 1),
            "max_label": s.max_label,
        }
    def get_latest_session(self) -> tuple[int, Optional[Path]]:
        """
        获取最新的会话编号和文件路径

        Returns:
            (会话编号, 会话文件路径) 如果没有会话文件则返回 (0, None)
        """
        session_files = list(self.chat_history_dir.glob(f"{self.session_prefix}*.jsonl"))
        if not session_files:
            return 0, None

        max_num = 0
        for f in session_files:
            try:
                num = int(f.stem.replace(self.session_prefix, ""))
                if num > max_num:
                    max_num = num
            except ValueError:
                continue

        if max_num == 0:
            return 0, None

        return max_num, self.chat_history_dir / f"{self.session_prefix}{max_num}.jsonl"

    def get_session_file(self, session_num: int) -> Path:
        """
        根据会话编号获取会话文件路径

        Args:
            session_num: 会话编号

        Returns:
            会话文件路径
        """
        return self.chat_history_dir / f"{self.session_prefix}{session_num}.jsonl"

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
                print(f"\033[33m[子智能体记录迁移] 跳过（{type(exc).__name__}: {exc}）\033[0m")

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
            print(f"加载会话历史失败: {e}")

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
                normalized.append({"role": "user", "content": content})
            elif msg_role == "assistant":
                normalized.append({
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": msg_data.get("reasoning_content", ""),
                    "tool_calls": msg_data.get("tool_calls", []),
                })
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
                    print("\033[33m[会话修复] 检测到历史文件存在拼行，已自动重写为标准 JSONL\033[0m")
                else:
                    print(f"\033[33m[会话修复] 已修复 {repairs} 处块结构问题并写回历史文件\033[0m")
            except Exception as e:
                print(f"\033[33m[会话修复] 重写历史文件失败: {e}\033[0m")

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
                    print(
                        f"\033[33m[会话修复] 补齐缺失的工具响应 "
                        f"（{sorted(missing)}，占位写入而非丢弃该轮对话）\033[0m"
                    )
                if subagent_rows:
                    # 子智能体记录移到块后，恢复合法结构
                    sanitized.extend(subagent_rows)
                    repairs += 1
                    print(
                        f"\033[33m[会话修复] 归位 {len(subagent_rows)} 条子智能体记录行 "
                        f"（移出 assistant/tool 块中间）\033[0m"
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
                    print(
                        f"\033[33m[会话修复] 丢弃孤儿 tool 消息 "
                        f"（缺少匹配的 assistant.tool_calls，"
                        f"tool_call_id={msg.get('tool_call_id')!r}）\033[0m"
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
            print(f"\033[33m[会话修复] 已留备份快照 {backup.name}（重写前）\033[0m")
        except OSError as e:
            print(f"\033[33m[会话修复] 备份失败（继续重写）: {e}\033[0m")

    def _message_to_json_row(self, message) -> dict:
        """将 OpenAI JSON 格式消息转换为 jsonl 行（与 load_session_history 读取结构保持一致）。"""
        role = message.get("role")
        if role == "system":
            return {"role": "system", "content": message.get("content", "")}
        elif role == "user":
            return {"role": "user", "content": message.get("content", "")}
        elif role == "assistant":
            return {
                "role": "assistant",
                "content": message.get("content", ""),
                "reasoning_content": message.get("reasoning_content", ""),
                "tool_calls": message.get("tool_calls", []),
            }
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
        except Exception as e:
            print(f"写入会话历史失败: {e}")

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
            except Exception as e:
                print(f"写入子智能体执行记录失败: {e}")
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
            print(f"写入子智能体执行记录失败: {e}")

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
            print(f"写入子智能体启动占位记录失败: {e}")

    def load_subagent_records(self, session_file: Path) -> list:
        """读取某会话的子智能体执行记录（供桌面端回放挂载）。

        旁路存储模式读 `session_N.subagents.jsonl`（按 subagent_id 取末条）；
        未注入 store 时回退读 in-file `role=subagent` 行（兼容旧路径）。
        """
        if self.subagent_store is not None:
            try:
                return self.subagent_store.load(session_file)
            except Exception as e:
                print(f"读取子智能体执行记录失败: {e}")
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
            print(f"重写会话历史失败: {e}")
            raise

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

        print(
            f"\033[33m[上下文压缩] 正在检查上下文：当前 {stats.used_tokens}/{stats.max_label} tokens，"
            f"剩余 {int(stats.remaining_percent)}%\033[0m"
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
            print(
                f"\033[33m[上下文压缩] {reason}：当前 {before.used_tokens}/{before.max_label} tokens，"
                f"剩余 {int(before.remaining_percent)}%\033[0m"
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
        print(f"\033[33m[上下文压缩完成] {summary}；压缩后 {after_text}\033[0m")

    def _build_initial_messages(self) -> list:
        """
        构造新会话的初始消息。

        第一条为 SystemMessage；workspace 指令文件（CLAUDE.md / AGENT.md）
        已在 system_prompt 构造阶段拼入，不再单独注入 user 消息。
        """
        return [{"role": "system", "content": self.system_prompt}]

    def create_initialized_session(self) -> tuple[int, Path, list]:
        """
        创建新会话并写入完整初始消息。

        Returns:
            (新会话编号, 新会话文件路径, 初始消息列表)
        """
        new_num, new_file = self.create_new_session()
        messages = self._build_initial_messages()
        for message in messages:
            self.append_message_to_session(new_file, message)
        return new_num, new_file, messages

    def create_new_session(self) -> tuple[int, Path]:
        """
        创建新会话

        Returns:
            (新会话编号, 新会话文件路径)
        """
        max_num, _ = self.get_latest_session()
        new_num = max_num + 1
        new_file = self.get_session_file(new_num)
        new_file.touch()
        # 同步写入元数据索引条目（标题/创建时间/状态/项目归属）
        self.ensure_index_entry(new_num)
        return new_num, new_file

    def init_session(self) -> tuple[int, Path, list]:
        """
        初始化会话：加载最后一次对话或创建新对话

        Returns:
            (会话编号, 会话文件路径, 消息列表)
        """
        max_num, session_file = self.get_latest_session()

        if session_file and session_file.exists():
            messages = self.load_session_history(session_file)
            if messages:
                print(f"已加载会话: session_{max_num}.jsonl ({len(messages)} 条消息)")
                return max_num, session_file, messages

        new_num, new_file, messages = self.create_initialized_session()
        print(f"已创建新会话: session_{new_num}.jsonl")
        return new_num, new_file, messages

    def switch_session(self, target_num: int) -> tuple[int, Path, list]:
        """
        切换到指定会话

        Args:
            target_num: 目标会话编号

        Returns:
            (会话编号, 会话文件路径, 消息列表)

        Raises:
            FileNotFoundError: 会话文件不存在
        """
        target_file = self.get_session_file(target_num)
        if not target_file.exists():
            raise FileNotFoundError(f"会话 session_{target_num}.jsonl 不存在")

        messages = self.load_session_history(target_file)
        return target_num, target_file, messages

    # ═══════════════════════════════════════════════════════════
    #  会话元数据（index.jsonl，每行一条会话元数据）
    #  与 session_<N>.jsonl 通过文件名关联；以文件名（含前缀）为键，
    #  避免 session_ / cron_ 前缀共用目录时编号冲突。
    # ═══════════════════════════════════════════════════════════

    @property
    def index_file(self) -> Path:
        """会话元数据索引文件（与 chat history 同目录）。"""
        return self.chat_history_dir / "index.jsonl"

    def meta_file(self, num: int) -> Path:
        """新会话独立元数据路径：{chat_history_dir}/session_{num}.meta.json。"""
        return self.chat_history_dir / f"{self.session_prefix}{num}.meta.json"

    def load_meta(self, num: int) -> Optional[dict]:
        """读取单个会话的独立元数据；文件不存在返回 None。O(1)。"""
        p = self.meta_file(num)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def save_meta(self, meta: dict) -> None:
        """原子写单个 meta 文件（tmp + replace，复用 save_index 的写入模式）。O(1)。"""
        meta_path = self.meta_file(int(meta.get("num", 0)))
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
            print(f"写入会话元数据失败: {e}")
            raise

    def _new_entry(self, num: int, file_name: str) -> dict:
        """构造新会话的默认元数据条目（与旧 ensure_index_entry 字段一致）。"""
        now = _now_iso()
        return {
            "num": num,
            "file": file_name,
            "title": None,
            "title_source": "none",
            "created_at": now,
            "updated_at": now,
            "status": "active",
            "trashed_at": None,
            "project": DEFAULT_PROJECT_SLUG,
            "model_id": None,
            "overrides": None,
        }

    def _num_from_stem(self, stem: str) -> Optional[int]:
        """从文件 stem 解析会话编号（"session_3"/"cron_12" → 3/12）。"""
        try:
            return int(stem.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            return None

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
            print(f"读取会话元数据索引失败: {e}")
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
            print(f"重写会话元数据索引失败: {e}")
            raise

    def backfill_index(self) -> None:
        """对账索引：目录内所有 jsonl 缺条目的补录；索引中 jsonl 已不存在的剔除。

        - glob 全部 *.jsonl（含其他前缀，如 cron_），避免误删别家前缀的条目
        - 已有独立 meta 文件的会话（新方案）跳过，不写入 index.jsonl
        - 老会话补录：title=null、created_at 取文件 mtime、status=active
        """
        entries = self.load_index()
        changed = False
        existing: set[str] = set()
        metas: set[str] = set()
        for f in self.chat_history_dir.glob("*.jsonl"):
            if f.name == self.index_file.name:
                continue
            num = self._num_from_stem(f.stem)
            if num is None:
                continue
            existing.add(f.name)
            if self.meta_file(num).exists():
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
                "num": num,
                "file": f.name,
                "title": None,
                "title_source": "none",
                "created_at": ts,
                "updated_at": ts,
                "status": "active",
                "trashed_at": None,
                "project": DEFAULT_PROJECT_SLUG,
            }
            changed = True
        for key in [k for k in entries if k not in existing and k not in metas]:
            entries.pop(key)
            changed = True
        if changed:
            self.save_index(entries)

    def ensure_index_entry(self, num: int) -> None:
        """新建会话时写入独立元数据文件（已存在则跳过）。O(1)。"""
        if self.load_meta(num):
            return
        key = self.get_session_file(num).name
        self.save_meta(self._new_entry(num, key))

    def _update_entry(self, num: int, mutate) -> dict:
        """定位条目 → mutate(entry) → 刷新 updated_at → 原子写回。

        新方案会话（存在独立 meta 文件）直接读写单文件 O(1)；
        存量会话回退 index.jsonl 全量路径，作为兜底。

        Raises:
            FileNotFoundError: 会话 jsonl 不存在
        """
        session_file = self.get_session_file(num)
        if not session_file.exists():
            raise FileNotFoundError(f"会话 {session_file.name} 不存在")
        key = session_file.name
        with self._index_lock:
            if self.meta_file(num).exists():
                entry = self.load_meta(num) or self._new_entry(num, key)
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
                    entry = self._new_entry(num, key)
                    entry["created_at"] = entry["updated_at"] = ts
                    entries[key] = entry
                mutate(entry)
                entry["updated_at"] = _now_iso()
                self.save_index(entries)
        return entry

    def rename_session(self, num: int, title: str) -> dict:
        """重命名会话（title_source=user，自动生成不再覆盖）。"""
        title = title.strip()
        if not title:
            raise ValueError("标题不能为空")
        return self._update_entry(
            num, lambda e: e.update({"title": title[:60], "title_source": "user"})
        )

    def set_auto_title(self, num: int, title: str, source: str = "auto") -> None:
        """写入自动生成的标题；用户手动改名（title_source=user）不覆盖。"""
        def mutate(e):
            if e.get("title_source") == "user":
                return
            e.update({"title": title[:60], "title_source": source})
        try:
            self._update_entry(num, mutate)
        except FileNotFoundError:
            pass

    def set_session_model(self, num: int, model_id: str | None = None,
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
        return self._update_entry(num, mutate)

    def trash_session(self, num: int) -> dict:
        """软删除：标记 status=trashed，jsonl/todo 原样保留。"""
        return self._update_entry(
            num, lambda e: e.update({"status": "trashed", "trashed_at": _now_iso()})
        )

    def restore_session(self, num: int) -> dict:
        """从回收站还原：status=active，清空 trashed_at。"""
        return self._update_entry(
            num, lambda e: e.update({"status": "active", "trashed_at": None})
        )

    def delete_session_permanent(self, num: int) -> bool:
        """永久删除会话：jsonl + 绑定的 todo 文件 + 元数据。

        新方案会话（存在独立 meta 文件）删除单文件 O(1)；
        存量会话回退移除 index.jsonl 中对应条目。
        """
        session_file = self.get_session_file(num)
        if not session_file.exists():
            return False
        try:
            session_file.unlink()
        except OSError as e:
            print(f"删除会话文件失败: {e}")
            return False
        # 子智能体旁路记录与主文件同生共死（不残留、不串台）
        if self.subagent_store is not None:
            self.subagent_store.delete(session_file)
        # todo 与 chat history 同生共死（tools.set_todo_manager 创建的路径）
        try:
            todo_file = todo_file_for_session(num)
            if todo_file.exists():
                todo_file.unlink()
        except OSError:
            pass
        with self._index_lock:
            if self.meta_file(num).exists():
                try:
                    self.meta_file(num).unlink()
                except OSError as e:
                    print(f"删除会话元数据失败: {e}")
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
            [{num, title, title_source, status, created_at, updated_at,
              trashed_at, message_count, file}, ...] 按 num 降序（新会话在前）
        """
        self.backfill_index()
        entries = self.load_index()
        sessions = []
        for f in self.chat_history_dir.glob(f"{self.session_prefix}*.jsonl"):
            num = self._num_from_stem(f.stem)
            if num is None:
                continue
            try:
                with open(f, "r", encoding="utf-8") as file:
                    msg_count = sum(1 for line in file if line.strip())
            except (ValueError, IOError):
                continue
            # 优先独立 meta 文件（新方案会话），否则回退 index.jsonl（存量会话）
            meta = self.load_meta(num) or entries.get(f.name) or {}
            sessions.append({
                "num": num,
                "title": meta.get("title"),
                "title_source": meta.get("title_source", "none"),
                "status": meta.get("status", "active"),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "trashed_at": meta.get("trashed_at"),
                "message_count": msg_count,
                "file": f.name,
                "model_id": meta.get("model_id"),
            })
        sessions = [s for s in sessions if s.get("status") == status]
        return sorted(sessions, key=lambda x: x["num"], reverse=True)

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

            return max(0, deleted_count - 1)  # 减去保留的系统提示词
        except Exception as e:
            print(f"清空会话失败: {e}")
            return 0
