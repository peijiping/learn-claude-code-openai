"""子智能体执行过程「持久化 + 回放」离线回归测试。

运行方式（无需额外依赖，pytest 未安装也能跑；装了 pytest 同样可收集）::

    cd /Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main
    .venv/bin/python -m unittest discover -s tests -v

设计依据：`docs/frontend/08-子智能体执行过程持久化与回放改造方案.md`

覆盖内容（全部离线，不调 LLM、不触碰任何真实会话目录）：
  1. 旁路写入           —— 记录写进 session_N.subagents.jsonl，主 jsonl 不被污染
  2. 两段式覆盖         —— 占位行 + 终态行 → 取末条（done）
  3. 崩溃留痕           —— 只有占位行 → running（卡片显示"已中断"）
  4. 回放挂载           —— 按 tool_call_id 挂到"发起 sub_agent 的那条 assistant"
  5. D1 数据丢失回归    —— interleaved 旧文件加载后整轮对话不丢
  6. 工具状态配对       —— 并行 N 个工具调用全部闭合（回归 6/7 running）
  7. 会话生命周期       —— clear / delete 同步处理旁路文件；trash/restore 不丢
  8. 迁移幂等           —— 旧 in-file 行迁出后重复执行不重复、留 .bak
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from hooks import HookSystem  # noqa: E402
from session_manage import SessionManager  # noqa: E402
from streaming_client import CallbackSink, StreamEvent  # noqa: E402
import subagent  # noqa: E402
from subagent import SubAgent  # noqa: E402
from subagent_store import SubagentStore  # noqa: E402


def _load_history_to_ui():
    """从 ws_bridge 抽取 `_text_of` / `_attach_subagent` / `_history_to_ui`。

    直接 `import ws_bridge` 会执行模块顶层的服务自举（load_config + Agent 构造），
    测试里不合适，因此只取源码片段执行。
    """
    src = (AGENTS_DIR / "ws_bridge.py").read_text(encoding="utf-8")
    seg = src[src.index("def _text_of("):src.index("async def handle(ws):")]
    ns: dict = {}
    exec(compile(seg, "ws_bridge_hist", "exec"), ns)  # noqa: S102 - 测试内自用
    return ns["_history_to_ui"]


history_to_ui = _load_history_to_ui()


def _build_transcript(*args, **kwargs):
    """借实例方法（self 不参与计算）构造 transcript。"""
    return SubAgent._build_transcript(None, *args, **kwargs)


class SidecarTestCase(unittest.TestCase):
    """公共夹具：临时目录 + 不注入 store 的 SessionManager（CLI 旧路径对照）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.sm = SessionManager(self.dir, "sys")
        self.sm_side = SessionManager(self.dir, "sys", subagent_store=SubagentStore(self.dir))

    def tearDown(self):
        self._tmp.cleanup()

    # ── 辅助 ────────────────────────────────────────────────
    def write_session(self, name, *objects):
        f = self.dir / name
        f.write_text(
            "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objects),
            encoding="utf-8",
        )
        return f

    def roles(self, path, in_sidecar=False):
        target = SubagentStore.sidecar_path(path) if in_sidecar else path
        if not target.exists():
            return []
        return [
            json.loads(line).get("role")
            for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def assistant_with_tool_call(tcid="call_A", name="sub_agent"):
        return {
            "role": "assistant", "content": "", "reasoning_content": "",
            "tool_calls": [{"id": tcid, "type": "function",
                            "function": {"name": name, "arguments": "{}"}}],
        }

    def subagent_history(self, tcid="call_A"):
        """session_6 形态：glob → 派发 sub_agent → 后台占位 tool → 最终总结。"""
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "帮我看看我的pdf内容有哪些？用子智能体看看"},
            {"role": "assistant", "content": "", "reasoning_content": "先列文件",
             "tool_calls": [{"id": "call_glob", "type": "function",
                             "function": {"name": "run_glob", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_glob", "content": "DRG.pdf"},
            self.assistant_with_tool_call(tcid),
            {"role": "tool", "tool_call_id": tcid, "content": "[Background task bg_0001 started]"},
            {"role": "assistant", "content": "我已派子智能体在后台读取", "tool_calls": []},
        ]


# ── 1. 旁路写入 / 2. 两段式 / 3. 崩溃留痕 ─────────────────────
class TestSidecarWrite(SidecarTestCase):

    def test_append_writes_sidecar_only(self):
        f = self.write_session("session_1.jsonl", {"role": "system", "content": "s"})
        self.sm_side.append_subagent_to_session(f, {
            "subagent_id": "sub_1", "tool_call_id": "call_A", "name": "读pdf",
            "thinking": "思考", "toolCalls": [{"tool_id": "c1", "name": "bash",
                                             "args": "{}", "status": "done"}],
            "error": "", "text": "摘要", "duration_ms": 1234,
        })
        self.assertEqual(self.roles(f), ["system"], "主 jsonl 必须保持纯标准消息")
        records = self.sm_side.load_subagent_records(f)
        self.assertEqual([r["subagent_id"] for r in records], ["sub_1"])
        self.assertEqual(records[0]["status"], "done")
        self.assertEqual(records[0]["duration_ms"], 1234)

    def test_two_phase_takes_last_row(self):
        f = self.write_session("session_2.jsonl", {"role": "system", "content": "s"})
        self.sm_side.begin_subagent(f, "sub_2", tool_call_id="call_B",
                                    name="任务", prompt="做点事")
        records = self.sm_side.load_subagent_records(f)
        self.assertEqual(records[0]["status"], "running", "只有占位行时应为 running（崩溃留痕）")
        self.sm_side.append_subagent_to_session(f, {
            "subagent_id": "sub_2", "tool_call_id": "call_B", "name": "任务",
            "thinking": "t", "toolCalls": [], "error": "", "text": "结果",
            "duration_ms": 800,
        })
        records = self.sm_side.load_subagent_records(f)
        self.assertEqual(len(records), 1, "同一 subagent_id 只保留末条")
        self.assertEqual(records[0]["status"], "done")
        self.assertEqual(records[0]["text"], "结果")

    def test_error_status_from_transcript(self):
        f = self.write_session("session_3.jsonl", {"role": "system", "content": "s"})
        self.sm_side.append_subagent_to_session(f, {
            "subagent_id": "sub_3", "name": "失败任务", "thinking": "",
            "toolCalls": [], "error": "API 调用失败",
        })
        record = self.sm_side.load_subagent_records(f)[0]
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["error"], "API 调用失败")


# ── 4. 回放挂载 ──────────────────────────────────────────────
class TestHistoryReplay(SidecarTestCase):

    def test_mounts_on_origin_assistant(self):
        history = self.subagent_history()
        records = [{
            "subagent_id": "sub_fd", "tool_call_id": "call_A", "name": "读pdf",
            "thinking": "思考", "status": "done", "duration_ms": 159000, "error": "",
            "toolCalls": [{"tool_id": "c1", "name": "run_read_pdf", "args": "{}", "status": "done"}],
        }]
        ui = history_to_ui(history, records)
        # system/tool 被跳过 → user, assistant(glob), assistant(sub_agent), assistant(总结)
        self.assertEqual(len(ui), 4)
        self.assertNotIn("subagents", ui[1], "glob 那条不该被子智能体卡片污染")
        subs = ui[2].get("subagents")
        self.assertIsNotNone(subs, "卡片必须挂在发起 sub_agent 的 assistant 下")
        self.assertEqual(subs[0]["id"], "sub_fd")
        self.assertEqual(subs[0]["status"], "done")
        self.assertEqual(subs[0]["durationMs"], 159000)

    def test_legacy_infile_row_still_renders(self):
        """未迁移的旧数据（in-file 行）仍能回放展示。"""
        history = self.subagent_history() + [{
            "role": "subagent", "subagent_id": "sub_legacy", "tool_call_id": "call_A",
            "name": "旧记录", "thinking": "t", "toolCalls": [],
        }]
        ui = history_to_ui(history, [])
        self.assertEqual(ui[2]["subagents"][0]["id"], "sub_legacy")

    def test_sidecar_wins_over_legacy_duplicate(self):
        history = self.subagent_history() + [{
            "role": "subagent", "subagent_id": "sub_dup", "tool_call_id": "call_A",
            "name": "旧名", "thinking": "old", "toolCalls": [],
        }]
        ui = history_to_ui(history, [{
            "subagent_id": "sub_dup", "tool_call_id": "call_A", "name": "新名",
            "thinking": "new", "status": "done", "duration_ms": 5, "error": "",
            "toolCalls": [],
        }])
        subs = ui[2]["subagents"]
        self.assertEqual(len(subs), 1, "同一 subagent_id 不应重复出卡")
        self.assertEqual(subs[0]["name"], "新名", "旁路记录优先")


# ── 5. D1 回归：绝不静默删除整轮对话 ──────────────────────────
class TestSanitizeRegression(SidecarTestCase):

    def test_interleaved_subagent_row_does_not_drop_turn(self):
        f = self.write_session(
            "session_9.jsonl",
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "用子智能体看pdf"},
            self.assistant_with_tool_call("call_A"),
            {"role": "subagent", "subagent_id": "sub_x", "tool_call_id": "call_A",
             "name": "读pdf", "thinking": "思考", "toolCalls": []},
            {"role": "tool", "tool_call_id": "call_A", "content": "子智能体摘要"},
            {"role": "assistant", "content": "最终回答", "tool_calls": []},
        )
        messages = self.sm.load_session_history(f)  # 不注入 store（CLI 旧路径）
        roles = [m.get("role") for m in messages]
        self.assertEqual(len(messages), 6, f"整轮 6 条消息一条都不能丢，实际 {roles}")
        self.assertEqual(roles.index("tool") < roles.index("subagent"), True,
                         "subagent 行应被归位到 tool 块之后")

    def test_missing_tool_result_is_backfilled_not_dropped(self):
        f = self.write_session(
            "session_8.jsonl",
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_X", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "assistant", "content": "下一轮"},
        )
        messages = self.sm.load_session_history(f)
        self.assertEqual(len(messages), 5, "缺 tool 响应时应补占位，而不是丢弃该轮")
        placeholder = messages[3]
        self.assertEqual(placeholder["role"], "tool")
        self.assertEqual(placeholder["tool_call_id"], "call_X")
        self.assertIn("recovered", placeholder["content"])

    def test_orphan_tool_without_owner_is_dropped(self):
        f = self.write_session(
            "session_7.jsonl",
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "tool", "tool_call_id": "call_nowhere", "content": "孤儿"},
            {"role": "assistant", "content": "ok"},
        )
        messages = self.sm.load_session_history(f)
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant"],
                         "无归属的孤儿 tool 消息（OpenAI 拒收）应被丢弃")


# ── 6. 工具状态配对 ──────────────────────────────────────────
class TestToolStatusPairing(unittest.TestCase):

    def test_parallel_tool_calls_all_closed(self):
        count = 7
        ids = [f"call_{i}" for i in range(count)]
        events = [StreamEvent(type="thinking_delta", text="思考")]
        for i, tid in enumerate(ids):
            events.append(StreamEvent(type="tool_call_start", tool_id=tid,
                                      tool_name="run_read_pdf", args='{"p":'))
            events.append(StreamEvent(type="tool_call_delta", tool_id=tid, args=f'"{i}.pdf"}}'))
        for tid in reversed(ids):  # 完成事件乱序到达
            i = ids.index(tid)
            events.append(StreamEvent(type="tool_call", tool_id=tid,
                                      tool_name="run_read_pdf",
                                      args=f'{{"p":"{i}.pdf"}}'))

        t = _build_transcript("sub_p", "读pdf", events)
        self.assertEqual([x["status"] for x in t["toolCalls"]], ["done"] * count,
                         "并行工具调用必须全部闭合（旧实现残留 6/7 running）")
        self.assertEqual([x["args"] for x in t["toolCalls"]],
                         [f'{{"p":"{i}.pdf"}}' for i in range(count)],
                         "参数必须按 tool_id 各归各位，不得串台")

    def test_tool_call_without_start_is_appended(self):
        t = _build_transcript("sub_p", "n", [
            StreamEvent(type="tool_call", tool_id="call_Z", tool_name="bash", args="{}"),
        ])
        self.assertEqual([(x["tool_id"], x["status"]) for x in t["toolCalls"]],
                         [("call_Z", "done")])

    def test_transcript_carries_anchor_and_status(self):
        t = _build_transcript("sub_p", "n", [], prompt="p", tool_call_id="call_A",
                              started_at="2026-09-11T09:31:02", duration_ms=1200,
                              text="摘要")
        self.assertEqual(t["tool_call_id"], "call_A")
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["duration_ms"], 1200)
        self.assertEqual(t["started_at"], "2026-09-11T09:31:02")
        t_err = _build_transcript("sub_e", "n", [], error="boom")
        self.assertEqual(t_err["status"], "error")
        self.assertEqual(t_err["text"], "", "失败时不应把错误文本当作正文快照")


# ── 7. 生命周期 ──────────────────────────────────────────────
class TestLifecycle(SidecarTestCase):

    def _seed(self, num=10):
        f = self.write_session(f"session_{num}.jsonl", {"role": "system", "content": "s"})
        self.sm_side.append_subagent_to_session(f, {
            "subagent_id": "sub_x", "tool_call_id": "call_A", "name": "任务",
            "thinking": "t", "toolCalls": [], "error": "",
        })
        return f

    def test_clear_removes_sidecar(self):
        f = self._seed(10)
        self.sm_side.clear_session(f)
        self.assertEqual(self.sm_side.load_subagent_records(f), [])
        self.assertFalse(SubagentStore.sidecar_path(f).exists())

    def test_delete_permanent_removes_sidecar(self):
        f = self._seed(11)
        self.sm_side.ensure_index_entry(11)
        self.assertTrue(self.sm_side.delete_session_permanent(11))
        self.assertFalse(SubagentStore.sidecar_path(f).exists())

    def test_trash_and_restore_keep_sidecar(self):
        f = self._seed(12)
        self.sm_side.ensure_index_entry(12)
        self.sm_side.trash_session(12)
        self.sm_side.restore_session(12)
        self.assertEqual([r["subagent_id"] for r in self.sm_side.load_subagent_records(f)],
                         ["sub_x"])

    def test_compact_rewrite_does_not_touch_sidecar(self):
        """save_session_history（compact / 自愈重写）不得回写 subagent 行，也不丢旁路记录。"""
        f = self._seed(13)
        self.sm_side.save_session_history(f, [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hi", "tool_calls": []},
        ])
        self.assertEqual(self.roles(f), ["system", "user"])
        self.assertEqual([r["subagent_id"] for r in self.sm_side.load_subagent_records(f)],
                         ["sub_x"])


# ── 8. 迁移 ──────────────────────────────────────────────────
class TestMigration(SidecarTestCase):

    def _legacy_session(self, name="session_20.jsonl"):
        return self.write_session(
            name,
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            self.assistant_with_tool_call("call_A"),
            {"role": "subagent", "subagent_id": "sub_old", "tool_call_id": "call_A",
             "name": "旧记录", "thinking": "t",
             "toolCalls": [{"name": "bash", "args": "{}", "status": "running"}]},
            {"role": "tool", "tool_call_id": "call_A", "content": "结果"},
            {"role": "assistant", "content": "总结", "tool_calls": []},
        )

    def test_migration_is_idempotent_and_backs_up(self):
        f = self._legacy_session()
        first = self.sm_side.subagent_store.migrate(f)
        self.assertEqual(first, 1)
        self.assertNotIn("subagent", self.roles(f), "主文件应被净化为纯标准消息")
        self.assertTrue((self.dir / "session_20.jsonl.bak").exists(), "迁移前应留 .bak 快照")
        records = self.sm_side.load_subagent_records(f)
        self.assertEqual([(r["subagent_id"], r["tool_call_id"]) for r in records],
                         [("sub_old", "call_A")])
        self.assertEqual(records[0]["toolCalls"][0]["status"], "done",
                         "旧行残留的 running（旧配对 bug 产物）应在迁移时归一化为 done")
        self.assertEqual(self.sm_side.subagent_store.migrate(f), 0, "重复迁移应为空操作")
        self.assertEqual(len(self.sm_side.load_subagent_records(f)), 1, "不得重复迁出")

    def test_load_migrates_then_replays(self):
        f = self._legacy_session("session_21.jsonl")
        history = self.sm_side.load_session_history(f)
        self.assertNotIn("subagent", [m.get("role") for m in history])
        ui = history_to_ui(history, self.sm_side.load_subagent_records(f))
        self.assertEqual(ui[1]["subagents"][0]["id"], "sub_old")


# ── 9. spawn_subagent 返回值 / 异常收束（回归 TypeError 崩溃） ──
def _offline_subagent():
    """构造一个不联网、不读配置的 SubAgent（LLM 走桩，由用例逐个替换）。"""
    sub = SubAgent.__new__(SubAgent)
    sub.base_tools = []
    sub.tool_handlers = {}
    sub.tool_registry = None
    sub.sinks = []
    sub.hook_system = HookSystem()
    sub.hook_system.register_default_hooks()
    sub.sub_llm_client = None
    sub.model = "offline-stub"
    return sub


class StubMessage:
    """最小可用的流式聚合消息（只要 model_dump / tool_calls / content）。"""

    def __init__(self, content=None, tool_calls=None, dump_raises=False):
        self.content = content
        self.reasoning_content = None
        self.tool_calls = tool_calls or None
        self._dump_raises = dump_raises

    def model_dump(self):
        if self._dump_raises:
            raise RuntimeError("model_dump 内部炸了")
        return {"role": "assistant", "content": self.content,
                "reasoning_content": None, "tool_calls": None}


class TestSpawnSubagentContract(unittest.TestCase):
    """回归 `sub_msg, _finish, _usage = streamed_create(...)` 变量遮蔽 bug。

    该写法把局部 `_finish()` 闭包覆盖成 finish_reason 字符串，子智能体跑完
    第一次 `_finish(...)` 即抛 `TypeError: 'str' object is not callable`：
    session_1 实测表现为 bg followup crashed + 旁路记录永久停在 running。
    """

    def setUp(self):
        self._orig = subagent.streamed_create

    def tearDown(self):
        subagent.streamed_create = self._orig

    def _stub(self, message, finish_reason="stop"):
        """让 streamed_create 返回预设的 (message, finish_reason, usage)。"""
        def fake(llm, sinks=None, should_stop=None, **kwargs):
            return message, finish_reason, {}

        subagent.streamed_create = fake

    def test_plain_answer_returns_summary_and_transcript(self):
        """回归点：模型直接给正文（无 tool_calls）时不再抛 TypeError。"""
        self._stub(StubMessage(content="### 结果\n全部读完"), "stop")
        summary, transcript = _offline_subagent().spawn_subagent(
            "读目录", tool_call_id="call_1")
        self.assertEqual(summary, "### 结果\n全部读完")
        self.assertEqual(transcript["status"], "done")
        self.assertEqual(transcript["error"], "")
        self.assertEqual(transcript["text"], summary, "终态正文用于旁路落盘")
        self.assertEqual(transcript["tool_call_id"], "call_1")
        self.assertIsInstance(transcript["duration_ms"], int)

    def test_unexpected_exception_becomes_error_transcript(self):
        """任何未预期异常都必须收束成 error transcript 返回，绝不逃逸。

        逃逸会导致：主回合崩溃 + 终态记录写不出（卡片永久转圈）。
        """
        self._stub(StubMessage(content="x", dump_raises=True), "stop")
        summary, transcript = _offline_subagent().spawn_subagent(
            "读目录", tool_call_id="call_2")
        self.assertIn("子智能体执行异常", summary)
        self.assertIn("RuntimeError", summary)
        self.assertEqual(transcript["status"], "error")
        self.assertIn("RuntimeError", transcript["error"])
        self.assertEqual(transcript["text"], "", "失败时不留正文，交给 error 字段")

    def test_api_failure_keeps_error_status(self):
        """streamed_create 抛异常（模型侧失败）走原有 error 分支。"""
        def boom(*a, **kw):
            raise ConnectionError("网络断了")

        subagent.streamed_create = boom
        summary, transcript = _offline_subagent().spawn_subagent("读目录")
        self.assertIn("子智能体 API 调用失败", summary)
        self.assertEqual(transcript["status"], "error")
        self.assertIn("ConnectionError", transcript["error"])

    def test_end_event_emitted_on_both_paths(self):
        """正常与异常路径都要发 sub_agent_end（前端收折叠/停转圈）。"""
        for stub in (StubMessage(content="ok"),
                     StubMessage(content="x", dump_raises=True)):
            self._stub(stub, "stop")
            sub = _offline_subagent()
            seen = []
            sub.sinks = [CallbackSink(seen.append)]
            sub.spawn_subagent("任务", tool_call_id="call_3")
            self.assertIn("sub_agent_end", [e.type for e in seen])


if __name__ == "__main__":
    unittest.main(verbosity=2)
