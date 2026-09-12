#!/usr/bin/env python3
"""
subagent.py - 通用型子智能体模块

提供独立的子智能体执行环境，用于在隔离的上下文中执行子任务。
子智能体默认拥有全部工具权限，通过 prompt 引导行为，而非通过类型限制。
支持通过 system_prompt 和 allowed_tools 参数自定义子智能体的能力和角色。
"""
import os
import json

from paths import WORKDIR
from hooks import HookSystem
from llm_manage import LLMClient
from streaming_client import FilterSink, streamed_create

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
    ) -> str:
        """
        执行一次子智能体任务。

        特点：
        1. 独立的上下文环境，不受父智能体上下文污染
        2. 默认拥有全部工具权限，通过 prompt 引导行为
        3. 可通过 system_prompt 自定义角色和行为约束
        4. 可通过 allowed_tools 限制可用工具范围
        5. 循环调用工具直到完成或达到安全限制（MAX_ITERATIONS 轮）
        6. 只返回最终的任务摘要，而非完整执行过程

        参数:
            prompt: 需要子智能体执行的任务描述
            system_prompt: 自定义系统提示，为 None 时使用 DEFAULT_SYSTEM_PROMPT
            allowed_tools: 允许使用的工具名称列表，为 None 时使用全部工具。
                           例如 ["bash", "read_file", "read_pdf"] 限制为只读工具集
            workdir: 可选，工作目录（Path 或 worktree 目录）。给定时本子任务的文件
                     操作工具（bash/read/write/edit/glob/pdf）以该目录为工作根，
                     系统提示追加工作目录提醒。

        返回:
            str: 任务执行结果的摘要文本，如果无结果则返回 "(no summary)"
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

        # 子智能体的工具调用事件上行给父级 sinks（CLI 的 stream_sink / 未来 UI）：
        # 转发 tool_call_start / tool_call_delta / tool_call（预测式 + 完成态），
        # 不转发 thinking/content（避免刷屏）；无父级 sinks 时仅内部聚合。
        sub_sinks = [FilterSink(self.sinks, types={"tool_call", "tool_call_start", "tool_call_delta"})] if self.sinks else None

        sub_msg = None
        for iteration in range(self.MAX_ITERATIONS):
            try:
                # 统一流式入口：内部聚合出完整消息，sub_msg 接口兼容 OpenAI message
                sub_msg, _finish, _usage = streamed_create(
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
                return error_msg
            sub_messages.append(sub_msg.model_dump())

            if not sub_msg.tool_calls:
                content = self._extract_content(sub_msg.model_dump())
                return content or "(no summary)"

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
                        try:
                            output = handler(**tool_args)
                        except Exception as e:
                            output = f"Error executing {tool_name}: {e}"
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
            return f"[达到最大轮次限制，返回最后一轮摘要]\n{content}"
        return "(no summary: 达到最大轮次限制且最后一轮无内容)"
