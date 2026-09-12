#!/usr/bin/env python3
"""
subagent.py - 通用型子智能体模块

提供独立的子智能体执行环境，用于在隔离的上下文中执行子任务。
子智能体默认拥有全部工具权限，通过 prompt 引导行为，而非通过类型限制。
支持通过 system_prompt 和 allowed_tools 参数自定义子智能体的能力和角色。
"""
import os
import json
import time
import uuid
from datetime import datetime

from paths import WORKDIR
from hooks import HookSystem
from llm_manage import LLMClient
from logger import get_logger
from streaming_client import CallbackSink, FilterSink, StreamEvent, streamed_create

# 统一日志（~/.aigent/logs/agent_日期.log）：排查"前端状态断了"时，
# 对照后端工具执行打点与前端卡片更新时间即可定位断连窗口。
log = get_logger("subagent")

class SubAgent:
    """
    通用型子智能体。

    每次 run() 调用都会在隔离的消息上下文里独立循环，只把最终摘要回传给
    调用方。默认拥有全部工具权限，通过 system_prompt 引导行为，
    也可通过 allowed_tools 在单次调用时收窄工具集。
    """

    DEFAULT_SYSTEM_PROMPT = f"""你是一个通用型子智能体，工作目录是 {WORKDIR}。
        ## 核心规则
        1. **任务导向**：严格按照任务描述完成指定工作，不要发散
        2. **输出控制**：每次工具调用都要限制输出量。读取文件时使用 limit 参数，bash 命令用 | head 限制行数
        3. **PDF 读取**：必须使用 read_pdf 工具读取 PDF，不要使用 strings/cat 等命令
        4. **摘要优先**：你的输出是给主智能体看的，只返回关键发现和结果，不要返回原始数据
        5. **安全操作**：执行写入或删除操作前，确认目标路径在工作目录内
        6. **看板边界**：不要创建或更新任务看板；不要调用 task_create、task_create_many 或 task_update；任务看板由主智能体统一维护

        ## 输出格式
        完成任务后，用以下格式返回摘要：
        ### 结果
        [任务完成的关键结果]
        ### 要点
        - [关键发现或实现细节]
        ### 注意
        [需要注意的问题或后续工作]"""

    MAX_ITERATIONS = 100

    def __init__(self, base_tools: list, tool_handlers: dict, hook_system: HookSystem | None = None, tool_registry=None, sinks=None):
        self.base_tools = base_tools
        self.tool_handlers = tool_handlers
        # tool_registry：可选，注入 ToolRegistry 实例，用于在 workdir 场景下生成
        # scoped_handlers(cwd)（文件工具以 worktree 为工作根）。None 时退化为共用 handlers。
        self.tool_registry = tool_registry
        # sinks：父级事件 sink 列表（CLI 的 stream_sink / 未来 UI 的 WSSink）。
        # 子智能体只把工具类事件（start/delta/tool_call）转发给父级
        # （思考/内容不上行，避免刷屏）；None 时子智能体静默，仅内部聚合出完整消息。
        self.sinks = sinks
        # 未显式传入时,SubAgent 内部自实例化一次独立的 hook_system
        if hook_system is None:
            hook_system = HookSystem()
            hook_system.register_default_hooks()
        self.hook_system = hook_system
        self.sub_llm_client = LLMClient().llm
        self.model = os.environ.get("OPENAI_MODEL_ID", "")

    def set_llm(self, llm_client, model: str) -> None:
        """配置热切换：就地重建子智能体的 LLM 绑定（无需重建实例）。"""
        self.sub_llm_client = llm_client
        self.model = model

    def _emit_sub_agent(self, ev_type: str, subagent_id: str, text: str = "",
                        tool_id: str = "", tool_name: str = "", args: str = "") -> None:
        """直接向父级 sinks 发子智能体生命周期事件（start/end，不经过 FilterSink 的类型过滤）。

        tool_id：**发起本次子任务的主智能体 tool_call_id**（不是子智能体内部工具的
        id）。前端据此把子智能体卡片挂到"发起 sub_agent 的那条 assistant 消息"下，
        避免实时挂载点与回放挂载点不一致（切会话后卡片位置跳变）。

        tool_name / args：子智能体**内部工具执行**生命周期事件（tool_exec_start /
        tool_exec_end）携带，前端把卡片里对应的工具行从"流式聚合完成"拨回
        "执行中"，执行结束后再闭合——否则工具执行阶段（往往最耗时）卡片零更新，
        用户看到的是"执行状态断了"，直到主智能体续轮才"回来"（2026-09-12 修复）。
        """
        if not self.sinks:
            return
        ev = StreamEvent(type=ev_type, text=text, subagent_id=subagent_id,
                         tool_id=tool_id, tool_name=tool_name, args=args)
        for s in self.sinks:
            s.emit(ev)

    @staticmethod
    def _extract_content(response) -> str:
        """
        从 LLM 响应中提取文本内容。

        同时兼容 dict（model_dump() 的产物）和 Pydantic 对象两种入参。
        当 content 为空时回退到 reasoning_content，确保 thinking 模式下
        即使被 max_tokens 截断也能拿到可读内容，避免误判为 "(no summary)"。
        """
        if isinstance(response, dict):
            content = response.get("content") or ""
            reasoning = response.get("reasoning_content") or ""
        else:
            content = getattr(response, "content", "") or ""
            reasoning = getattr(response, "reasoning_content", "") or ""

        # content 是 list（如 OpenAI 多模态）时，拼接 text 块
        if isinstance(content, list):
            content = "".join(
                (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", ""))
                for b in content
            )

        if content:
            return str(content)
        return str(reasoning)

    def spawn_subagent(
        self,
        prompt: str,
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        workdir=None,
        tool_call_id: str = "",
    ) -> tuple[str, dict]:
        """
        执行一次子智能体任务。

        特点：
        1. 独立的上下文环境，不受父智能体上下文污染
        2. 默认拥有全部工具权限，通过 prompt 引导行为
        3. 可通过 system_prompt 自定义角色和行为约束
        4. 可通过 allowed_tools 限制可用工具范围
        5. 循环调用工具直到完成或达到安全限制（MAX_ITERATIONS 轮）
        6. 只把最终的任务摘要返回给父智能体，执行过程（思考/工具）以
           transcript 形式返回，由父智能体决定是否持久化展示（不进父上下文）

        参数:
            prompt: 需要子智能体执行的任务描述
            system_prompt: 自定义系统提示，为 None 时使用 DEFAULT_SYSTEM_PROMPT
            allowed_tools: 允许使用的工具名称列表，为 None 时使用全部工具。
                           例如 ["bash", "read_file", "read_pdf"] 限制为只读工具集
            workdir: 可选，工作目录（Path 或 worktree 目录）。给定时本子任务的文件
                     操作工具（bash/read/write/edit/glob/pdf）以该目录为工作根，
                     系统提示追加工作目录提醒。
            tool_call_id: 发起本次子任务的主智能体 tool_call id。会写进 transcript
                     并随 sub_agent_start 事件上行，供前端/回放把执行记录挂到
                     "发起 sub_agent 的那条 assistant 消息"下（唯一锚点规则）。

        返回:
            (str, dict): (任务执行结果的摘要文本, 子智能体执行过程 transcript)。
                         transcript 结构：
                         {subagent_id, tool_call_id, name, status, prompt, thinking,
                          text, toolCalls, error, started_at, duration_ms}；
                         失败时 status=error、error 非空、thinking/toolCalls 可能为空。
        """
        if allowed_tools is not None:
            sub_tools = [t for t in self.base_tools if t.get("function").get("name") in allowed_tools]
        else:
            sub_tools = self.base_tools

        if workdir is not None and self.tool_registry is not None:
            # worktree 场景：文件工具以 workdir 为工作根，其余工具复用共用 handlers
            sub_handlers = self.tool_registry.scoped_handlers(workdir)
            if system_prompt is None:
                sub_system = (self.DEFAULT_SYSTEM_PROMPT
                              + f"\n\n<system-reminder>你的工作目录（所有文件操作根）是：{workdir}</system-reminder>")
            else:
                sub_system = system_prompt
        else:
            sub_handlers = self.tool_handlers
            sub_system = system_prompt or self.DEFAULT_SYSTEM_PROMPT

        sub_messages = [{"role": "system", "content": sub_system}]
        sub_messages.append({"role": "user", "content": prompt})

        tools_label = f"{len(sub_tools)} tools" if allowed_tools else "all child tools"
        print(f"\033[2;91m  [subagent] 开始执行任务 ({tools_label}): {prompt[:80]}...\033[0m")

        # 子智能体执行过程上行给父级 sinks（CLI 的 stream_sink / 桌面端 WSSink）：
        # 转发 thinking_delta（思考过程）+ tool_call_start/delta/tool_call（预测式 + 完成态），
        # 全部打上本次子任务 id，供前端把内容折叠到对应子智能体块下。
        # **不转发 content_delta**：子智能体的答复正文是给主智能体的（最终由主智能体
        # 复述进正文），不需要流式给用户看，也不进卡片（避免与主正文重复）。
        # 工具执行生命周期（tool_exec_start/end）不走这里 —— 它们由下方执行循环经
        # _emit_sub_agent 直达父级 sinks（事件自带 subagent_id，无需 FilterSink 打标）。
        # 同时用 CallbackSink 旁路收集同一批流式事件，作为 transcript 的持久化依据。
        subagent_id = f"sub_{uuid.uuid4().hex[:8]}"
        log.info("[subagent] %s 开始执行任务 (%s, tool_call_id=%s): %r",
                 subagent_id, tools_label, tool_call_id or "-", prompt[:80])
        collected: list[StreamEvent] = []
        _started_at = datetime.now().isoformat(timespec="seconds")
        _t0 = time.monotonic()
        self._emit_sub_agent("sub_agent_start", subagent_id, prompt[:120],
                             tool_id=tool_call_id)
        sub_sinks = [FilterSink([*(self.sinks or []), CallbackSink(collected.append)],
                                types={"thinking_delta", "tool_call", "tool_call_start", "tool_call_delta"},
                                subagent_id=subagent_id)]

        # transcript 名称取任务 prompt 前 80 字，便于回放时辨认
        name = (prompt[:80] + "…") if len(prompt) > 80 else (prompt or "子智能体")

        def _finish(summary: str, err: str = "") -> tuple[str, dict]:
            """收束为 (回传主智能体的摘要, transcript)：统一补齐状态/正文/耗时。"""
            return summary, self._build_transcript(
                subagent_id, name, collected,
                error=err,
                text="" if err else summary,
                prompt=prompt,
                tool_call_id=tool_call_id,
                started_at=_started_at,
                duration_ms=int((time.monotonic() - _t0) * 1000),
            )

        sub_msg = None
        try:
            for iteration in range(self.MAX_ITERATIONS):
                try:
                    # 统一流式入口：内部聚合出完整消息，sub_msg 接口兼容 OpenAI message。
                    # 注意：streamed_create 返回 (message, finish_reason, usage)，
                    # 第二个值必须叫 finish_reason —— 早期误写成 `_finish`，把上面
                    # 定义的 _finish() 闭包覆盖成了字符串，导致后续 `_finish(...)`
                    # 抛 "TypeError: 'str' object is not callable"（子智能体跑完即崩）。
                    sub_msg, finish_reason, _usage = streamed_create(
                        self.sub_llm_client,
                        sinks=sub_sinks,
                        model=self.model,
                        messages=sub_messages,
                        tools=sub_tools,
                        max_tokens=int(os.environ.get("SUBAGENT_MAX_TOKENS") or 8000),
                        temperature=0.5,
                        reasoning_effort="high", #思考强度，DeepSeek只有 high、max 两个选项
                        extra_body={"thinking":{"type":"enabled"}} #思考模式开关，值范围 disabled、enabled，默认 enabled
                    )
                except Exception as e:
                    error_msg = f"子智能体 API 调用失败 (第 {iteration + 1} 轮): {type(e).__name__}: {e}"
                    print(f"  [subagent] {error_msg}")
                    return _finish(error_msg, err=error_msg)
                sub_messages.append(sub_msg.model_dump())

                if not sub_msg.tool_calls:
                    content = self._extract_content(sub_msg.model_dump())
                    return _finish(content or "(no summary)")

                for tool_call in sub_msg.tool_calls:
                    tool_id = tool_call.id
                    tool_name = tool_call.function.name
                    # OpenAI SDK 返回的 function.arguments 是 JSON 字符串,需解析为 dict 才能 ** 解包
                    raw_args = tool_call.function.arguments
                    tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

                    if tool_name:
                        # hooks: PreToolUse
                        blocked = self.hook_system.trigger("PreToolUse", tool_call)
                        if blocked:
                            sub_messages.append({"role": "tool", "tool_use_id": tool_id,
                                                 "content": str(blocked)})
                            continue
                        handler = sub_handlers.get(tool_name)
                        if handler:
                            # 工具执行生命周期上行（修复"执行阶段卡片零更新"）：
                            # 流式聚合完成时 tool_call 事件已把该工具标成 done，
                            # 但真正的执行此刻才开始（往往最耗时）——不把状态拨回
                            # "执行中"，前端卡片就会在整个执行窗口内完全冻结，
                            # 用户看到"子智能体与前端的状态断了"，直到主智能体
                            # 续轮输出时才"回来"（2026-09-12 实测复现）。
                            self._emit_sub_agent("tool_exec_start", subagent_id,
                                                 tool_id=tool_id, tool_name=tool_name)
                            _exec_t0 = time.monotonic()
                            try:
                                output = handler(**tool_args)
                            except Exception as e:
                                output = f"Error executing {tool_name}: {e}"
                            self._emit_sub_agent("tool_exec_end", subagent_id,
                                                 tool_id=tool_id, tool_name=tool_name)
                            log.info("[subagent] %s 工具 %s 执行完成 (%.2fs)",
                                     subagent_id, tool_name, time.monotonic() - _exec_t0)
                            # hooks: PostToolUse
                            self.hook_system.trigger("PostToolUse", tool_call, output)
                        else:
                            output = f"Unknown tool: {tool_name}"
                        result = {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": str(output),
                        }
                    else:
                        result = {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": "Error: tool call missing name",
                        }
                    sub_messages.append(result)

                # print(f"  [subagent] 第 {iteration + 1} 轮，执行了 {len(sub_msg.tool_calls)} 个工具调用")

            # 达到最大轮次，尝试从最后一轮响应中提取内容返回
            content = self._extract_content(sub_msg.model_dump()) if sub_msg else ""
            if content:
                return _finish(f"[达到最大轮次限制，返回最后一轮摘要]\n{content}")
            return _finish("(no summary: 达到最大轮次限制且最后一轮无内容)")
        except Exception as e:
            # 采集完整性兜底：子智能体内部任何**未预期**异常都必须自己收束成
            # error transcript 正常返回，绝不让异常逃逸出本方法。逃逸的后果
            # 是三重的：(1) 主智能体这一轮 turn 直接崩掉（工具结果拿不到）；
            # (2) 调用方拿不到 transcript → 旁路记录永远停在 `running` 占位行，
            #     前端卡片永久转圈；(3) 后台路径下表现为 "bg followup crashed"。
            # 注意：这里只兜 BaseException 之外的普通异常，ESC/KeyboardInterrupt
            # 仍按正常中断语义向上传播。
            error_msg = (f"子智能体执行异常 ({type(e).__name__}): {e}")
            print(f"  [subagent] {error_msg}")
            return _finish(error_msg, err=error_msg)
        finally:
            # 无论正常完成还是异常返回，都通知前端子智能体执行结束（收折叠态/停转圈）
            self._emit_sub_agent("sub_agent_end", subagent_id, tool_id=tool_call_id)
            # 完成打点（含耗时）：排查"前端状态断了"时对照后端是否真的结束
            _elapsed = time.monotonic() - _t0
            print(f"  [subagent] 结束 ({_elapsed:.1f}s): {(name or prompt)[:50]}")
            log.info("[subagent] %s 结束 (%.1fs): %r", subagent_id, _elapsed, (name or prompt)[:50])

    def _build_transcript(self, subagent_id: str, name: str, events: list,
                          error: str = "", text: str = "", prompt: str = "",
                          tool_call_id: str = "", started_at: str = "",
                          duration_ms: int | None = None) -> dict:
        """把旁路收集的子智能体事件聚合为可持久化的 transcript（旁路记录一条）。

        字段与 `subagent_store` 的记录结构、前端 SubAgentMsg 的可视字段一致。

        toolCalls 聚合规则（修工具状态配对 bug）：
        - `tool_call_start(tool_id)` → 新建条目（status=running）
        - `tool_call_delta(tool_id)` → 追加该条目的 args
        - `tool_call(tool_id)`       → 按 **tool_id** 定位并置 done

        旧实现按 `tool_calls[-1]`（最后一条）闭合：一次响应里模型并行发起 N 个
        工具调用时，先来 N 个 start（追加 N 条），流结束后一次性来 N 个
        tool_call（每次只标记最后一条）→ 前 N-1 条永久停在 running
        （实测 session_6 为 6/7 running，卡片一直转圈）。
        """
        thinking = "".join(e.text for e in events if e.type == "thinking_delta")
        tool_calls: list[dict] = []
        by_id: dict[str, dict] = {}
        for ev in events:
            if ev.type == "tool_call_start":
                item = {
                    "tool_id": ev.tool_id or "",
                    "name": ev.tool_name or "",
                    "args": ev.args or "",
                    "status": "running",
                }
                tool_calls.append(item)
                if item["tool_id"]:
                    by_id[item["tool_id"]] = item
            elif ev.type == "tool_call_delta":
                item = by_id.get(ev.tool_id or "")
                if item is not None:
                    item["args"] += ev.args or ""
            elif ev.type == "tool_call":
                item = by_id.get(ev.tool_id or "")
                if item is None:
                    # 兜底：没有对应的 start 事件（如中途接入）→ 补一条完整记录
                    item = {
                        "tool_id": ev.tool_id or "",
                        "name": ev.tool_name or "",
                        "args": ev.args or "",
                        "status": "done",
                    }
                    tool_calls.append(item)
                    if item["tool_id"]:
                        by_id[item["tool_id"]] = item
                    continue
                if ev.args:
                    item["args"] = ev.args
                if ev.tool_name:
                    item["name"] = ev.tool_name
                item["status"] = "done"
        return {
            "subagent_id": subagent_id,
            "tool_call_id": tool_call_id,
            "name": name,
            "status": "error" if error else "done",
            "prompt": prompt,
            "thinking": thinking,
            "text": text,
            "toolCalls": tool_calls,
            "error": error,
            "started_at": started_at,
            "duration_ms": duration_ms,
        }
