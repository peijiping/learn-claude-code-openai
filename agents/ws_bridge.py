"""
ws_bridge.py - 桌面端桥层（新增，不修改 agent_full_v2.py）
把 Agent 变成 WS service：命令进、事件出（走 WSSink 的 JSON 行协议）。

协议见 docs/frontend/03-前后端通信协议.md。Electron 主进程拉起本脚本，
连 ws://127.0.0.1:<AGENT_WS_PORT>（默认 8765）。

与后端铁律一致：只新增这一个薄层，agent_full_v2.py 及以下零改动。
"""
import asyncio
import json
import os
import re
import threading
from typing import Optional

import websockets
from openai import OpenAI

from agent_full_v2 import Agent
from config import load as load_config
from llm_config import get_config, load_llm_config, save_config
from paths import CHAT_HISTORY_DIR
from session_manage import SessionManager
from session_runtime import SessionRuntimeRegistry

# 启动即自举配置（Electron spawn 的 cwd 为仓库根，config.py 按 cwd 解析项目级配置）
load_config()
# 存在 llmconfig.json 则加载大模型配置映射进 env（文件缺失时不影响启动）
load_llm_config()

PORT = int(os.environ.get("AGENT_WS_PORT", "8765"))

# 全局 Agent 仅用于：大模型配置热切换（reload_llm_bindings）、会话标题生成、
# 以及 goal/tasks/skills 等查询；"跑对话"不再走它——并发会话各自持有一个
# 独立 Agent（见 session_runtime.SessionRuntime），事件按 session_num 路由。
# 惰性会话：启动不建会话（避免每次打开窗口都多一个空 jsonl），
# 会话号由 ws_bridge 在事件循环内确定性分配。
agent = Agent(silent=True)


def _envelope(kind: str, payload: dict) -> str:
    return json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False)


def _session_meta(item: dict) -> dict:
    """session_manager.list_sessions() 的条目已是元数据 dict，直接透传。"""
    return dict(item)


# ── 会话标题生成（独立 daemon 线程，先于主对话请求发出） ──────────────

TITLE_SYSTEM_PROMPT = (
    "你是会话标题生成器。根据用户的首条消息生成一个不超过16个字的简短标题，"
    "概括用户意图。直接输出标题文本：不要引号、不要句号、不要任何解释。"
)

# 标题请求专用短超时：独立小客户端，不与主对话共用连接池/超时/重试策略。
# 若服务端串行排队，标题请求也要在 TITLE_TIMEOUT 秒内出结果或降级兜底，
# 绝不悬挂到主 turn 结束（主客户端 timeout=1200s + 3 次重试，绝不复用）。
TITLE_TIMEOUT = 30
# max_tokens 必须给足：推理模型（如 deepseek-v4-flash）的思考过程也计入
# completion 预算，预算太小会被 reasoning_tokens 吃光导致 content 为空/
# 只挤出单字。1000 对"思考 + 16 字标题"足够，成本可忽略。
TITLE_MAX_TOKENS = 1000

# 同一会话的标题线程去重（clear 后重发首条消息等场景），防止并发重复写索引
_title_threads_lock = threading.Lock()
_title_inflight: set = set()


def _generate_session_title(first_user_text: str) -> Optional[str]:
    """用独立短超时客户端发一次极小的非流式请求生成标题；任何异常返回 None（调用方降级）。"""
    text = (first_user_text or "").strip()
    if not text:
        return None
    try:
        # 独立客户端：只复用主客户端的鉴权/地址配置，连接池、超时、重试全部独立
        client = OpenAI(
            api_key=agent.llm_client.api_key,
            base_url=str(agent.llm_client.base_url),
            timeout=TITLE_TIMEOUT,
            max_retries=0,
        )
        resp = client.chat.completions.create(
            model=agent.model,
            messages=[
                {"role": "system", "content": TITLE_SYSTEM_PROMPT},
                {"role": "user", "content": text[:2000]},
            ],
            max_tokens=TITLE_MAX_TOKENS,
            temperature=0.3,
        )
        title = (resp.choices[0].message.content or "").strip().strip('"“”').strip()
        title = re.sub(r"\s+", " ", title)
        title = title.rstrip("。，,．.！!？?；;：:、")
        # 合法性校验：单字/空串拒绝（如推理模型预算被吃光只挤出"写"），走降级兜底
        if len(title) < 2:
            return None
        return title[:24]
    except Exception:
        return None


def _fallback_title(first_user_text: str) -> Optional[str]:
    """标题请求失败/不合法时的兜底：按标点切分取首个语义片段。

    例："帮我写一个简单的python程序，越简单越好…" → "帮我写一个简单的python程序"，
    而不是盲目截断 20 字（可能在词中间断开或只剩半句话）。
    """
    text = re.sub(r"\s+", " ", (first_user_text or "").strip())
    if not text:
        return None
    first_clause = re.split(r"[，,。．.！!？?；;：:、\n]", text, maxsplit=1)[0].strip()
    if len(first_clause) < 4:  # 首个标点出现太早，整句兜底
        first_clause = text
    return first_clause[:16] or None


def _title_worker(loop, ws, session_num: int, first_user_text: str) -> None:
    """标题线程主体：生成 → 写索引 → 回发会话列表。任何异常静默吞掉，绝不影响主对话。"""
    try:
        title = _generate_session_title(first_user_text)
        source = "auto"
        if not title:
            title = _fallback_title(first_user_text)
            source = "trunc"
        if title:
            sm = _ensure_session_manager()
            sm.set_auto_title(session_num, title, source)
            # 从工作线程安全地把"刷新会话列表"调度回事件循环
            asyncio.run_coroutine_threadsafe(_reply_sessions(ws), loop)
    except Exception:
        pass
    finally:
        with _title_threads_lock:
            _title_inflight.discard(session_num)


def _start_title_thread(ws, session_num: int, first_user_text: str) -> None:
    """收到首条消息立即启动独立 daemon 标题线程。

    必须在 run_turn 线程提交之前调用：标题请求先于主对话请求到达服务端，
    即使服务端串行排队（本地模型/单并发代理），标题也能最先被处理。
    线程完全独立于会话的 run_turn/agent_loop 生命周期，turn 中途完成即回发。
    """
    loop = asyncio.get_running_loop()
    with _title_threads_lock:
        if session_num in _title_inflight:
            return  # 同会话已有标题线程在跑，跳过
        _title_inflight.add(session_num)
    threading.Thread(
        target=_title_worker,
        args=(loop, ws, session_num, first_user_text),
        name=f"session-title-{session_num}",
        daemon=True,
    ).start()


def _has_real_user_turn(messages: list) -> bool:
    """历史中是否已有真实用户消息（排除 <system-reminder> 系统注入）。"""
    for m in messages:
        if m.get("role") != "user":
            continue
        if _text_of(m.get("content")).startswith("<system-reminder>"):
            continue
        return True
    return False


def _ensure_session_manager():
    """惰性会话下 session_manager 可能为 None（尚未 init/switch），
    列会话等只读操作前先兜底构建（构建后 init_session 也会复用）。"""
    if agent.session_manager is None:
        agent.session_manager = SessionManager(
            CHAT_HISTORY_DIR, agent.system_prompt.build_system_prompt(),
            session_prefix=agent.session_prefix,
        )
    return agent.session_manager


def _text_of(content) -> str:
    """历史消息 content 兼容转换：str 直接返回，list（多模态 blocks）拼接 text。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


def _history_to_ui(messages: list) -> list[dict]:
    """session 历史 → 前端可渲染消息列表。

    - 跳过 system / tool 消息（前者无展示价值，后者已聚合进 assistant 工具条）
    - 跳过系统注入的 user 消息（<system-reminder> 开头的 todo reminder 等）
    - assistant 保留 reasoning_content → thinking、tool_calls → 工具条
    """
    ui = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            content = _text_of(m.get("content"))
            if content.startswith("<system-reminder>"):
                continue
            ui.append({"role": "user", "content": content})
        elif role == "assistant":
            ui.append({
                "role": "assistant",
                "content": _text_of(m.get("content")),
                "thinking": m.get("reasoning_content") or "",
                "toolCalls": [
                    {
                        "name": (tc.get("function") or {}).get("name", ""),
                        "args": (tc.get("function") or {}).get("arguments", ""),
                    }
                    for tc in (m.get("tool_calls") or [])
                ],
            })
    return ui


async def handle(ws):
    loop = asyncio.get_running_loop()
    line_q: asyncio.Queue = asyncio.Queue()

    # deliver(kind, payload)：把信封从任意线程（run_turn 工作线程 / 本协程）投递回
    # 事件循环队列，writer 协程逐条 flush 到前端。必须经 loop.call_soon_threadsafe，
    # 否则 asyncio.Queue 的 put_nowait 不会唤醒事件循环，增量会积压到 turn 结束才
    # 一次性刷出（流式失效）。
    def deliver(kind: str, payload: dict) -> None:
        loop.call_soon_threadsafe(line_q.put_nowait, _envelope(kind, payload))

    async def reply_sessions() -> None:
        await _reply_sessions(ws)

    # 并发会话运行时注册表：每个会话独立 Agent + 会话级事件路由 + 独立停止。
    # load_meta 供每会话按元数据独立绑定模型。
    registry = SessionRuntimeRegistry(
        deliver, reply_sessions,
        load_meta=lambda num: (_ensure_session_manager().load_meta(num)),
    )

    async def writer():
        while True:
            line = await line_q.get()
            await ws.send(line)

    writer_task = asyncio.create_task(writer())
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(_envelope("error", {"msg": f"bad json: {raw}"}))
                continue
            kind = msg.get("kind")
            payload = msg.get("payload") or {}

            if kind == "chat":
                # 并发会话：每个会话由注册表里的 SessionRuntime 独立跑 run_turn，
                # 事件按 session_num 路由到前端对应缓冲。本循环不做 await run_turn，
                # 派发后立即继续读命令 → 任意会话可后台执行、切换不断流。
                sm = _ensure_session_manager()
                num = payload.get("num")
                text = payload.get("text", "")
                # 全新会话（前端无激活会话 / 未带 num）：事件循环内确定性领号 +
                # 写入初始 system 消息（create_new_session 读文件取 max，必须在这个
                # 单线程事件循环里执行，避免并发线程 race 到同一编号）。
                if num is None:
                    new_num, new_file = sm.create_new_session()
                    for m in sm._build_initial_messages():
                        sm.append_message_to_session(new_file, m)
                    num = new_num
                    await ws.send(_envelope("session", {"num": num, "message_count": 0}))
                    # 新建会话首批：把前端选择的模型持久化进该会话元数据。
                    #（参数覆盖由前端在收到 session 信封后按 UI 形状 map 写入，此处只记模型；
                    #  chat 透传的 overrides 是已换算的单轮 resolved 形状，不宜直接落元数据。）
                    await asyncio.to_thread(
                        sm.set_session_model, new_num,
                        model_id=payload.get("model_id"),
                    )
                    await _reply_sessions(ws)
                rt = registry.get_or_create(num)
                if rt.busy:
                    # 同会话并发 turn 拒绝：避免两线程同时写同一会话 jsonl
                    await ws.send(_envelope("error", {
                        "msg": f"该会话 (session_{num}) 正在执行，请先用停止按钮结束后再发送",
                    }))
                    continue
                # 标题：全新会话，或该会话此前从无真实 user 消息（旧会话首轮）
                history = await asyncio.to_thread(
                    sm.load_session_history, sm.get_session_file(num)
                )
                if not _has_real_user_turn(history):
                    _start_title_thread(ws, num, text)
                # 会话级请求覆盖：思考强度 / 更大上下文（本轮生效，内存态，不写配置）。
                # 前端下拉悬浮面板改动后随 chat 命令带上来。
                ov = payload.get("overrides") or {}
                reasoning_effort = ov.get("thinking_strength") or None
                max_context_raw = ov.get("max_context") or None
                # 前端发送的是叠加态（standard/extended 二选一），这里已由前端换算成
                # 具体窗口字符串；若前端仅传开关位则回落到 None（走全局）。跳过空串。
                max_context = str(max_context_raw) if max_context_raw else None
                # 后台线程跑 turn；事件循环继续处理其它命令（切换 / 其它会话 / stop）
                asyncio.create_task(
                    rt.start_turn(text, reasoning_effort=reasoning_effort,
                                  max_context=max_context)
                )

            elif kind == "stop":
                # 仅停止当前显示会话正在执行的那一轮，其它会话不受影响
                num = int(payload.get("num", 0))
                rt = registry.get(num)
                if rt is not None:
                    rt.request_stop()

            elif kind == "session_switch":
                sm = _ensure_session_manager()
                num = int(payload.get("num", 0))
                # 运行中的会话不读磁盘回放：turn 在途时 jsonl 可能处于
                # "assistant(tool_calls) 已落盘、tool 响应未落盘" 的中间态，
                # load_session_history 的孤儿清理会把它当坏数据重写文件，
                # 截断在途消息。前端对运行中会话本就以实时缓冲为准
                # （session_history 的 hasLive 守卫），此处回放空消息即可。
                if registry.is_busy(num):
                    # 消息回放跳过（以实时缓冲为准），但模型与参数仍按元数据恢复，
                    # 保证切到运行中会话时其参数覆盖也能正确加载。
                    meta = (await asyncio.to_thread(sm.load_meta, num)) or {}
                    await ws.send(_envelope("session_history", {
                        "num": num, "messages": [],
                        "model_id": meta.get("model_id"),
                        "overrides": meta.get("overrides") or {},
                    }))
                    await _reply_sessions(ws)
                    continue
                try:
                    _, _, history = await asyncio.to_thread(sm.switch_session, num)
                except FileNotFoundError:
                    await ws.send(_envelope("error", {"msg": f"session {num} not found"}))
                else:
                    # 切换只是"按号读取该会话历史回放"（并刷新列表），
                    # 不改变任何运行中会话的执行状态 → 切换不断流。
                    # 顺带读取该会话记录的模型与参数，供前端按元数据恢复选中。
                    meta = (await asyncio.to_thread(sm.load_meta, num)) or {}
                    await ws.send(_envelope("session_history", {
                        "num": num,
                        "messages": _history_to_ui(history),
                        "model_id": meta.get("model_id"),
                        "overrides": meta.get("overrides") or {},
                    }))
                    # 切换会话后推送该会话的上下文统计（供前端圆圈指示器按会话展示）
                    try:
                        stats = await asyncio.to_thread(sm.context_stats_dict, history)
                        await ws.send(_envelope("context_stats", {"num": num, **stats}))
                    except Exception:
                        pass
                    await _reply_sessions(ws)

            elif kind == "session_model":
                # 记录会话最后选择的模型 + 参数到会话元数据（会话级独立绑定）；
                # 无 num（新建任务预设态）由 chat 首条统一持久化，此处仅处理已建会话。
                sm = _ensure_session_manager()
                num = int(payload.get("num", 0) or 0)
                if num <= 0:
                    continue
                if not sm.get_session_file(num).exists():
                    await ws.send(_envelope("error", {"msg": f"session {num} not found"}))
                    continue
                model_id = payload.get("model_id")
                overrides = payload.get("overrides") or None
                await asyncio.to_thread(
                    sm.set_session_model, num,
                    model_id=model_id if model_id is not None else None,
                    overrides=overrides if isinstance(overrides, dict) else None,
                )
                await ws.send(_envelope("session_model", {
                    "num": num, "model_id": model_id, "overrides": overrides,
                }))
                await _reply_sessions(ws)

            elif kind == "session_clear":
                sm = _ensure_session_manager()
                num = int(payload.get("num", 0) or 0)
                if num <= 0:
                    await ws.send(_envelope("error", {"msg": "当前无激活会话"}))
                    continue
                if registry.is_busy(num):
                    await ws.send(_envelope("error", {
                        "msg": f"该会话 (session_{num}) 正在执行，暂不能清空",
                    }))
                    continue
                if not sm.get_session_file(num).exists():
                    await ws.send(_envelope("error", {"msg": f"session {num} not found"}))
                    continue
                await asyncio.to_thread(sm.clear_session, sm.get_session_file(num))
                await ws.send(_envelope("session", {"num": num, "message_count": 0}))
                await _reply_sessions(ws)

            elif kind == "sessions_list":
                await _reply_sessions(ws)

            elif kind == "session_rename":
                sm = _ensure_session_manager()
                try:
                    await asyncio.to_thread(
                        sm.rename_session,
                        int(payload.get("num", 0)), str(payload.get("title", "")),
                    )
                except FileNotFoundError:
                    await ws.send(_envelope("error", {"msg": f"session {payload.get('num')} not found"}))
                except ValueError as exc:
                    await ws.send(_envelope("error", {"msg": f"重命名失败：{exc}"}))
                else:
                    await _reply_sessions(ws)

            elif kind == "session_trash":
                sm = _ensure_session_manager()
                num = int(payload.get("num", 0))
                # 运行中的会话禁止进回收站（后台还在写文件 / 流式输出）
                if registry.is_busy(num):
                    await ws.send(_envelope("error", {
                        "msg": f"该会话 (session_{num}) 正在执行，请先停止后再删除",
                    }))
                    continue
                try:
                    await asyncio.to_thread(sm.trash_session, num)
                except (FileNotFoundError, ValueError):
                    await ws.send(_envelope("error", {"msg": f"session {num} not found"}))
                else:
                    registry.remove(num)
                    await _reply_sessions(ws)

            elif kind == "session_restore":
                sm = _ensure_session_manager()
                try:
                    await asyncio.to_thread(sm.restore_session, int(payload.get("num", 0)))
                except (FileNotFoundError, ValueError):
                    await ws.send(_envelope("error", {"msg": f"session {payload.get('num')} not found"}))
                else:
                    await _reply_sessions(ws)

            elif kind == "session_delete":
                # 批量永久删除：逐条执行，单条失败不断整批；
                # 运行中的会话拒绝删除（后台还在写文件）
                sm = _ensure_session_manager()
                nums = payload.get("nums") or []
                deleted, failed = [], []
                for n in nums:
                    try:
                        num = int(n)
                    except (TypeError, ValueError):
                        continue
                    if registry.is_busy(num):
                        failed.append(num)
                        continue
                    try:
                        ok = await asyncio.to_thread(sm.delete_session_permanent, num)
                    except (TypeError, ValueError):
                        continue
                    if ok:
                        deleted.append(num)
                        registry.remove(num)
                    else:
                        failed.append(num)
                # 结果回发后不再全量广播 sessions：前端以 deleted[] 本地增量移除，
                # 避免删除完成后重建整个会话列表（逐个重数 message_count）造成的刷新延迟。
                await ws.send(_envelope("session_delete_result", {
                    "deleted": deleted, "failed": failed,
                }))

            elif kind == "trash_list":
                sm = _ensure_session_manager()
                items = await asyncio.to_thread(sm.list_sessions, "trashed")
                await ws.send(_envelope("sessions_trashed", {
                    "sessions": [_session_meta(i) for i in items],
                }))

            elif kind == "goal_status":
                text = await asyncio.to_thread(agent.goal_status)
                await ws.send(_envelope("goal_status", {"text": text}))

            elif kind == "tasks":
                # 惰性会话下可能尚未绑定 todo manager，无激活会话时给占位文本
                if agent.session_num is None:
                    await ws.send(_envelope("tasks", {"text": "(当前会话暂无待办)"}))
                    continue
                text = await asyncio.to_thread(agent.show_tasks)
                if not text.strip():
                    text = "(当前会话暂无待办)"
                await ws.send(_envelope("tasks", {"text": text}))

            elif kind == "skills":
                text = await asyncio.to_thread(agent.skills.list_skills)
                await ws.send(_envelope("skills", {"text": text}))

            elif kind == "llm_config_get":
                await ws.send(_envelope("llm_config", {"config": get_config()}))

            elif kind == "llm_config_save":
                config = payload.get("config") or {}
                try:
                    saved = await asyncio.to_thread(save_config, config)
                    # 重新映射进 env 并就地热切换 LLM 绑定，立即生效（无需重启）
                    await asyncio.to_thread(load_llm_config)
                    result = await asyncio.to_thread(agent.reload_llm_bindings)
                except ValueError as exc:
                    await ws.send(_envelope("error", {"msg": f"保存失败：{exc}"}))
                else:
                    # 同步所有已构造的运行时会话 Agent 的新绑定（并发后台会话也立即生效）
                    await asyncio.to_thread(registry.reload_llm_bindings)
                    ok = result.get("applied", False)
                    await ws.send(_envelope("llm_config", {
                        "config": get_config(),
                        "applied": ok,
                        "msg": (f"模型配置已生效（{result.get('primary')}）"
                                if ok else result.get("reason", "未生效")),
                    }))

            else:
                await ws.send(_envelope("error", {"msg": f"unknown kind: {kind}"}))
    finally:
        writer_task.cancel()


async def _reply_sessions(ws):
    sm = await asyncio.to_thread(_ensure_session_manager)
    items = await asyncio.to_thread(sm.list_sessions)
    sessions = [_session_meta(i) for i in items]
    await ws.send(_envelope("sessions", {"sessions": sessions}))


async def main():
    async with websockets.serve(handle, "127.0.0.1", PORT):
        print(f"[ws_bridge] WS server listening on 127.0.0.1:{PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())