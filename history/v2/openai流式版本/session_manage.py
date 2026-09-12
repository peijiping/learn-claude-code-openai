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
from pathlib import Path
from typing import Optional

from context_compact import ContextCompact


class SessionManager:
    """会话管理器，负责对话历史的持久化和管理"""

    def __init__(self, chat_history_dir: Path, system_prompt: str,
                 session_prefix: str = "session_"):
        """
        初始化会话管理器

        Args:
            chat_history_dir: 会话历史存储目录
            system_prompt: 系统提示词
            session_prefix: 会话文件名前缀，默认 "session_"；
                            cron 调度器传入 "cron_" 以独立编号
        """
        self.chat_history_dir = chat_history_dir
        self.system_prompt = system_prompt
        self.session_prefix = session_prefix
        self.compact_manager = ContextCompact(
            transcript_dir=chat_history_dir.parent / ".transcripts",
            tool_results_dir=chat_history_dir.parent / ".task_outputs" / "tool-results",
        )
        self.chat_history_dir.mkdir(parents=True, exist_ok=True)

    def format_context_label(self, messages: list) -> str:
        """格式化当前上下文窗口显示信息。"""
        return self.compact_manager.format_context_label(messages)
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
        messages, orphan_drops = self._sanitize_orphan_tool_calls(messages)

        # 自愈：发现拼行/坏行，或丢弃了孤儿消息时，把清理后的列表写回文件，
        # 避免每次启动都重复剔除同一批干消息。
        needs_rewrite = repaired or orphan_drops > 0
        if needs_rewrite and messages:
            try:
                self.save_session_history(session_file, messages)
                if repaired:
                    print("\033[33m[会话修复] 检测到历史文件存在拼行，已自动重写为标准 JSONL\033[0m")
                else:
                    print(f"\033[33m[会话修复] 已丢弃 {orphan_drops} 条孤儿消息并写回历史文件\033[0m")
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
        清理孤儿 assistant 消息：带 tool_calls 但其后没有匹配 tool 消息的情况。

        当会话文件因进程崩溃 / Ctrl+C 在 assistant 消息落盘后、tool 消息落盘前被
        中断时，加载整段历史直接回传 OpenAI 会触发：
            BadRequestError: An assistant message with 'tool_calls' must be
            followed by tool messages responding to each 'tool_call_id'.
        本函数扫描消息列表，对每个带 tool_calls 的 assistant 消息，验证紧随其后
        的 tool 消息是否覆盖了全部 tool_call_id；缺失则丢弃该 assistant 消息
        以及它后面紧跟的任何错位 tool 消息。

        Returns:
            (清理后的消息列表, 丢弃的孤儿 assistant 消息条数)
        """
        sanitized = []
        drops = 0
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                expected_ids = {
                    tc["id"] for tc in msg["tool_calls"]
                    if isinstance(tc, dict) and "id" in tc
                }
                if not expected_ids:
                    sanitized.append(msg)
                    i += 1
                    continue

                j = i + 1
                found_ids: set[str] = set()
                while j < len(messages) and messages[j].get("role") == "tool":
                    if messages[j].get("tool_call_id") in expected_ids:
                        found_ids.add(messages[j].get("tool_call_id"))
                    j += 1
                    if found_ids == expected_ids:
                        break

                if found_ids == expected_ids:
                    sanitized.extend(messages[i:j])
                    i = j
                else:
                    missing = expected_ids - found_ids
                    dropped_tools = j - i - 1
                    drops += 1
                    print(
                        f"\033[33m[会话修复] 丢弃孤儿 assistant 消息 "
                        f"（缺失 tool 响应: {sorted(missing)}，"
                        f"丢弃错位 tool 消息: {dropped_tools} 条）\033[0m"
                    )
                    i = j
            else:
                sanitized.append(msg)
                i += 1
        return sanitized, drops

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
            with open(session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(self._message_to_json_row(message), ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"写入会话历史失败: {e}")

    def save_session_history(self, session_file: Path, messages: list) -> None:
        """
        原子重写完整会话历史，保证磁盘 jsonl 与内存 messages 一致。
        """
        session_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = session_file.with_suffix(session_file.suffix + ".tmp")

        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                for message in messages:
                    f.write(json.dumps(self._message_to_json_row(message), ensure_ascii=False) + "\n")
            tmp_file.replace(session_file)
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

    def list_sessions(self) -> list[tuple[int, Path, int]]:
        """
        列出所有会话

        Returns:
            [(会话编号, 会话文件路径, 消息数量), ...]
        """
        sessions = []
        session_files = list(self.chat_history_dir.glob(f"{self.session_prefix}*.jsonl"))

        for f in session_files:
            try:
                num = int(f.stem.replace(self.session_prefix, ""))
                with open(f, "r", encoding="utf-8") as file:
                    msg_count = sum(1 for line in file if line.strip())
                sessions.append((num, f, msg_count))
            except (ValueError, IOError):
                continue

        return sorted(sessions, key=lambda x: x[0])

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

            return max(0, deleted_count - 1)  # 减去保留的系统提示词
        except Exception as e:
            print(f"清空会话失败: {e}")
            return 0
