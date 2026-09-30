#!/usr/bin/env python3
"""目标模式**可见化**守护测试 —— 2026-09-30，docs/frontend/22 §6.6 / §7.6。

用户报的原始问题："我的目标模式执行和普通的模式没有区别，用户也感知不到每轮执行完
的偏差和结果是什么"。改造做了四件事，本文件逐一钉住：

1. **目标指令徽标**：`run_turn(goal_instruction=...)` 把"这条就是目标指令"落进
   jsonl 的 user 行（`goal={"kind":"instruction",...}`），前端据此挂徽标、回放同样可见。
2. **每轮检查结果进对话流**：Stop 七分支的裁决结果（action/round/reason/elapsed/
   tokens）**全部**落盘（原先只有 `block` 落、且那一条是给模型看的英文指令），
   并经 `goal_check` 信封实时上屏。
3. **不给模型添噪音**：非 block 的检查记录整条不进模型上下文（`_is_display_only_goal_record`
   + `_model_messages` 的过滤），`block`/`set`/`instruction` 一律保留 —— 白名单
   `MODEL_MSG_FIELDS` 只能剔**字段**、剔不了**整条消息**，这层过滤是必须的。
4. **两处顺序契约**（改造中最容易踩的两个坑）：
   - 终止态检查必须**排队**到 turn 收尾再落盘（当场 append 会让
     `_finalize_turn_usage` 找不到末行 assistant → 本轮 usage 永久丢失，
     并让 `run_turn` 的返回值变成检查记录）；
   - `_history_to_ui` 的 goal 分流必须放在 `<system-reminder>` 前缀判断**之前**。

⚠ 本文件会构造真实 `Agent`，因此需要 `OPENAI_API_KEY` / `OPENAI_BASE_URL`
（dummy 即可）在外部导出 —— 与仓库既有测试同一约定（见 test_goal_mode_axis.py）。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import dataclasses
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import ws_bridge  # noqa: E402
from agent_full_v2 import (  # noqa: E402
    _ZERO_USAGE,
    Agent,
    _is_display_only_goal_record,
)
from execution_mode import MODE_GOAL, MODE_NORMAL, MODE_PLAN  # noqa: E402
from goal import GoalEvaluation  # noqa: E402
from paths import default_scratch_paths  # noqa: E402
from session_manage import SessionManager  # noqa: E402

SYSTEM_PROMPT = "you are a test harness"
SID = "Sid0000001"
COND = "把 nav-tree 接口的任务数统计做完"


class _StubEvaluator:
    """可控评估器：按预设序列返回 `GoalEvaluation`（或抛异常）。"""

    def __init__(self, results):
        self.results = list(results)
        self.llm_client = None
        self.model = "stub"

    def evaluate(self, condition, messages):  # noqa: ARG002
        if not self.results:
            return GoalEvaluation(ok=False, reason="stub: 结果用尽")
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _AgentTest(unittest.TestCase):
    def setUp(self):
        lg = logging.getLogger("aigent")
        saved = list(lg.handlers)
        lg.handlers = [logging.NullHandler()]
        self.addCleanup(lambda: setattr(lg, "handlers", saved))

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()

    def _make_agent(self) -> Agent:
        ws_dir = self.root / "ws"
        ws_dir.mkdir(exist_ok=True)
        ws = dataclasses.replace(
            default_scratch_paths(), id="testws",
            data_root=self.root / "data", workdir=ws_dir, bash_cwd=ws_dir)
        self.ws = ws

        agent = Agent(silent=True, workspace=ws)
        agent.session_manager = SessionManager(
            self.root / "data" / ".chathistory", SYSTEM_PROMPT)
        sm = agent.session_manager
        sm.get_session_file(SID).touch()
        sm.ensure_index_entry(SID)
        agent.session_id = SID
        agent.session_file = sm.get_session_file(SID)
        # 事件出口：本文件只关心"发了什么"，不接 WS
        self.sink_calls = []
        agent.execution_mode_sink = (
            lambda kind, payload: self.sink_calls.append((kind, dict(payload))))
        # stream_sink：_finalize_turn_usage 会 emit usage_stats，给个哑的
        agent.stream_sink = mock.Mock()
        return agent

    def _rows(self, agent) -> list[dict]:
        text = agent.session_file.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def _goal(self, agent, condition: str = COND) -> Agent:
        self.assertEqual(agent.set_execution_mode(MODE_GOAL, condition), "")
        self.sink_calls.clear()
        return agent


# ══════════════════════════════════════════════════════════════════
#  一、目标指令徽标（jsonl 落盘 + 回放透传）
# ══════════════════════════════════════════════════════════════════

class TestGoalInstructionBadge(_AgentTest):
    def test_goal_set_message_carries_set_marker(self):
        """`[Goal set]` 旁挂 `goal.kind="set"`：回放时渲染成「目标已设定」卡片，
        而不是一条 `[Goal set] Condition: …` 的怪气泡。**正文一字不改**（模型照读）。"""
        a = self._goal(self._make_agent())
        last = self._rows(a)[-1]
        self.assertEqual(last["role"], "user")
        self.assertTrue(last["content"].startswith("[Goal set]"))
        self.assertEqual(last["goal"], {"kind": "set", "condition": COND})

    def test_run_turn_marks_the_goal_instruction_message(self):
        """`goal_instruction` 非 None → 该条 user 行带 `kind="instruction"`。"""
        a = self._make_agent()
        a.agent_loop = lambda: a.history_messages.append(
            {"role": "assistant", "content": "好的", "reasoning_content": ""})
        a.run_turn("把 nav-tree 接口的任务数统计做完", goal_instruction=COND)
        rows = self._rows(a)
        first = rows[0]
        self.assertEqual(first["role"], "user")
        self.assertEqual(first["goal"], {"kind": "instruction", "condition": COND})

    def test_run_turn_without_goal_instruction_adds_no_field(self):
        """无目标的消息**连字段都不多一个**（改造前的 jsonl 形状逐字节一致）。"""
        a = self._make_agent()
        a.agent_loop = lambda: a.history_messages.append(
            {"role": "assistant", "content": "好的", "reasoning_content": ""})
        a.run_turn("普通消息")
        self.assertNotIn("goal", self._rows(a)[0])

    def test_history_to_ui_keeps_instruction_marker(self):
        ui = ws_bridge._history_to_ui([
            {"role": "user", "content": "做吧", "created_at": "2026-09-30T10:00:00",
             "goal": {"kind": "instruction", "condition": COND}},
        ])
        self.assertEqual(len(ui), 1)
        self.assertEqual(ui[0]["role"], "user")
        self.assertEqual(ui[0]["content"], "做吧")
        self.assertEqual(ui[0]["goal"]["kind"], "instruction")

    def test_history_to_ui_without_goal_adds_no_field(self):
        ui = ws_bridge._history_to_ui([{"role": "user", "content": "普通消息"}])
        self.assertNotIn("goal", ui[0])

    def test_old_session_rows_are_untouched(self):
        """存量 jsonl（无 `goal`）零迁移：不报错、不产生幽灵卡片。"""
        rows = [
            {"role": "user", "content": "老会话消息"},
            {"role": "assistant", "content": "老答复"},
        ]
        ui = ws_bridge._history_to_ui(rows)
        self.assertEqual([m["role"] for m in ui], ["user", "assistant"])
        self.assertTrue(all("goal" not in m for m in ui))


# ══════════════════════════════════════════════════════════════════
#  二、每轮检查结果：七分支全部产出快照
# ══════════════════════════════════════════════════════════════════

class TestGoalCheckSnapshot(_AgentTest):
    def _decide(self, a, evaluator, *, background_running=False, tokens=1000):
        a.goal_controller.evaluator = evaluator
        return a.goal_controller.evaluate_after_turn(
            a.history_messages, background_running=background_running,
            current_tokens=tokens)

    def test_no_goal_produces_no_check(self):
        a = self._make_agent()
        d = a.goal_controller.evaluate_after_turn(a.history_messages)
        self.assertEqual(d.action, "allow")
        self.assertIsNone(a.goal_controller.last_check)

    def test_block_snapshot_has_action_round_reason(self):
        a = self._goal(self._make_agent())
        d = self._decide(a, _StubEvaluator([GoalEvaluation(ok=False, reason="缺证据")]))
        self.assertEqual(d.action, "block")
        chk = a.goal_controller.last_check
        self.assertEqual(chk["action"], "block")
        self.assertEqual(chk["round"], 1)
        self.assertEqual(chk["reason"], "缺证据")
        self.assertEqual(chk["condition"], COND)
        self.assertIn("elapsed", chk)
        self.assertIn("tokens", chk)

    def test_achieved_snapshot_taken_before_active_is_cleared(self):
        """`achieved` 会把 active 置 None —— 快照必须在清空前取，否则算不出
        condition / elapsed / tokens（这是改造中最容易写反的一处）。"""
        a = self._goal(self._make_agent())
        d = self._decide(a, _StubEvaluator([GoalEvaluation(ok=True, reason="pytest 全绿")]))
        self.assertEqual(d.action, "achieved")
        self.assertIsNone(a.goal_controller.active)          # 目标已清
        chk = a.goal_controller.last_check
        self.assertEqual(chk["action"], "achieved")
        self.assertEqual(chk["condition"], COND)             # ← 清空后仍拿得到
        self.assertEqual(chk["reason"], "pytest 全绿")
        self.assertEqual(chk["round"], 1)

    def test_failed_snapshot(self):
        a = self._goal(self._make_agent())
        d = self._decide(a, _StubEvaluator(
            [GoalEvaluation(ok=False, reason="条件自相矛盾", impossible=True)]))
        self.assertEqual(d.action, "failed")
        self.assertEqual(a.goal_controller.last_check["action"], "failed")

    def test_error_snapshot(self):
        a = self._goal(self._make_agent())
        d = self._decide(a, _StubEvaluator([RuntimeError("boom")]))
        self.assertEqual(d.action, "error")
        self.assertEqual(a.goal_controller.last_check["action"], "error")
        self.assertIn("boom", a.goal_controller.last_check["reason"])

    def test_defer_snapshot(self):
        a = self._goal(self._make_agent())
        d = self._decide(a, _StubEvaluator([]), background_running=True)
        self.assertEqual(d.action, "defer")
        self.assertEqual(a.goal_controller.last_check["action"], "defer")

    def test_limit_snapshot(self):
        a = self._goal(self._make_agent())
        a.goal_controller.block_cap = 1
        ev = _StubEvaluator([GoalEvaluation(ok=False, reason="还差 A"),
                             GoalEvaluation(ok=False, reason="还差 B")])
        self.assertEqual(self._decide(a, ev).action, "block")
        d = self._decide(a, ev)
        self.assertEqual(d.action, "limit")
        self.assertEqual(a.goal_controller.last_check["action"], "limit")
        self.assertEqual(a.goal_controller.last_check["round"], 2)

    def test_new_goal_clears_previous_check(self):
        """换目标时上一次的裁决结果必须清掉（否则界面上会显示一个属于旧目标的结论）。"""
        a = self._goal(self._make_agent())
        self._decide(a, _StubEvaluator([GoalEvaluation(ok=False, reason="x")]))
        self.assertIsNotNone(a.goal_controller.last_check)
        a.set_execution_mode(MODE_NORMAL)
        self.assertIsNone(a.goal_controller.last_check)


# ══════════════════════════════════════════════════════════════════
#  三、落盘与上行：goal_check 信封 + jsonl + meta 轮次
# ══════════════════════════════════════════════════════════════════

class TestGoalCheckDelivery(_AgentTest):
    def _check(self, a, action="block", **kw):
        a.goal_controller.evaluator = _StubEvaluator([kw.pop("evaluation")])
        d = a.goal_controller.evaluate_after_turn(
            a.history_messages, current_tokens=kw.pop("tokens", 0))
        self.assertEqual(d.action, action)
        return d

    def test_event_envelope_shape(self):
        a = self._goal(self._make_agent())
        d = self._check(a, "block", evaluation=GoalEvaluation(ok=False, reason="缺证据"))
        a._note_goal_check(d)
        kinds = [k for k, _ in self.sink_calls]
        self.assertIn("goal_check", kinds)
        payload = dict(self.sink_calls[kinds.index("goal_check")][1])
        self.assertEqual(payload["session_id"], SID)
        self.assertEqual(payload["kind"], "check")
        self.assertEqual(payload["action"], "block")
        self.assertEqual(payload["round"], 1)
        self.assertEqual(payload["reason"], "缺证据")
        self.assertEqual(payload["condition"], COND)
        for key in ("elapsed", "tokens", "at"):
            self.assertIn(key, payload)

    def test_all_six_actions_are_delivered(self):
        """六种终结/暂缓态各发一条 —— 改造前它们**只 print 到终端**，用户看不见。"""
        cases = {
            "achieved": GoalEvaluation(ok=True, reason="done"),
            "failed": GoalEvaluation(ok=False, reason="no", impossible=True),
            "error": RuntimeError("boom"),
            "defer": None,  # 用 background_running 走 defer
            "limit": GoalEvaluation(ok=False, reason="still"),
        }
        for action, ev in cases.items():
            with self.subTest(action=action):
                a = self._goal(self._make_agent())
                self.sink_calls.clear()
                if action == "limit":
                    a.goal_controller.block_cap = 0
                if ev is None:
                    a.goal_controller.evaluator = _StubEvaluator([])
                    d = a.goal_controller.evaluate_after_turn(
                        a.history_messages, background_running=True)
                else:
                    a.goal_controller.evaluator = _StubEvaluator([ev])
                    d = a.goal_controller.evaluate_after_turn(a.history_messages)
                self.assertEqual(d.action, action)
                a._note_goal_check(d)
                kinds = [k for k, _ in self.sink_calls]
                self.assertIn("goal_check", kinds)

    def test_queued_check_is_written_at_flush_with_empty_content(self):
        """终止态检查**空正文**落盘（评估器读不到 = 零污染），且由 flush 统一写。"""
        a = self._goal(self._make_agent())
        d = self._check(a, "achieved", evaluation=GoalEvaluation(ok=True, reason="done"))
        a._note_goal_check(d)                       # inline=False → 入队
        self.assertTrue(a._pending_goal_checks)
        before = len(self._rows(a))
        self.assertEqual(len(self._rows(a)), before)  # 未落盘
        a._flush_goal_checks()
        rows = self._rows(a)
        self.assertEqual(len(rows), before + 1)
        last = rows[-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"], "")
        self.assertEqual(last["goal"]["kind"], "check")
        self.assertEqual(last["goal"]["action"], "achieved")
        self.assertEqual(last["goal"]["reason"], "done")
        self.assertFalse(a._pending_goal_checks)

    def test_flush_is_idempotent(self):
        a = self._goal(self._make_agent())
        d = self._check(a, "achieved", evaluation=GoalEvaluation(ok=True, reason="done"))
        a._note_goal_check(d)
        a._flush_goal_checks()
        n = len(self._rows(a))
        a._flush_goal_checks()
        self.assertEqual(len(self._rows(a)), n)

    def test_check_updates_goal_round_in_meta(self):
        """轮次回写 meta —— 常驻目标条靠它在重连/切会话后显示正确的「第 N 轮」。"""
        a = self._goal(self._make_agent())
        d = self._check(a, "block", evaluation=GoalEvaluation(ok=False, reason="缺证据"))
        a._note_goal_check(d)
        self.assertEqual(a.session_manager.load_meta(SID).get("goal_round"), 1)


# ══════════════════════════════════════════════════════════════════
#  四、模型上下文纯净：非 block 的检查记录整条不进
# ══════════════════════════════════════════════════════════════════

class TestModelProjection(_AgentTest):
    def test_display_only_predicate(self):
        cases = [
            ({"role": "user", "content": "", "goal": {"kind": "check", "action": "achieved"}}, True),
            ({"role": "user", "content": "", "goal": {"kind": "check", "action": "failed"}}, True),
            ({"role": "user", "content": "", "goal": {"kind": "check", "action": "limit"}}, True),
            ({"role": "user", "content": "", "goal": {"kind": "check", "action": "error"}}, True),
            ({"role": "user", "content": "", "goal": {"kind": "check", "action": "defer"}}, True),
            # block 是 goal 回环的**唯一驱动力**，必须保留
            ({"role": "user", "content": "[Goal still active]", "goal": {"kind": "check", "action": "block"}}, False),
            ({"role": "user", "content": "[Goal set]", "goal": {"kind": "set"}}, False),
            ({"role": "user", "content": "做吧", "goal": {"kind": "instruction"}}, False),
            ({"role": "user", "content": "普通消息"}, False),
            ({"role": "assistant", "content": "x"}, False),
        ]
        for msg, expected in cases:
            with self.subTest(msg=msg):
                self.assertEqual(_is_display_only_goal_record(msg), expected)

    def test_model_messages_filters_terminal_checks_only(self):
        a = self._make_agent()
        a.history_messages = [
            {"role": "user", "content": "做吧",
             "goal": {"kind": "instruction", "condition": COND}},
            {"role": "user", "content": "[Goal set]\nCondition: x",
             "goal": {"kind": "set", "condition": COND}},
            {"role": "user", "content": "[Goal still active]\nEvaluator: 缺证据",
             "goal": {"kind": "check", "action": "block", "round": 1}},
            {"role": "user", "content": "",
             "goal": {"kind": "check", "action": "achieved", "round": 2}},
        ]
        out = a._model_messages()
        self.assertEqual(len(out), 3)                       # 只剔掉 achieved 那条
        self.assertEqual([m["content"] for m in out],
                         ["做吧", "[Goal set]\nCondition: x",
                          "[Goal still active]\nEvaluator: 缺证据"])

    def test_goal_field_never_leaks_into_model_request(self):
        """`goal` 不在 MODEL_MSG_FIELDS 白名单 → 挂多少元数据都漏不出去。"""
        a = self._make_agent()
        a.history_messages = [
            {"role": "user", "content": "做吧",
             "goal": {"kind": "instruction", "condition": COND}},
            {"role": "user", "content": "[Goal still active]",
             "goal": {"kind": "check", "action": "block", "round": 1}},
        ]
        for m in a._model_messages():
            self.assertNotIn("goal", m)


# ══════════════════════════════════════════════════════════════════
#  五、回放分流：check / set → goal_check 卡片，不进用户气泡
# ══════════════════════════════════════════════════════════════════

class TestHistoryRouting(unittest.TestCase):
    def test_check_becomes_card(self):
        ui = ws_bridge._history_to_ui([
            {"role": "user", "content": "", "created_at": "2026-09-30T10:05:00",
             "goal": {"kind": "check", "action": "block", "round": 3,
                      "reason": "缺证据", "condition": COND,
                      "elapsed": 95, "tokens": 45210, "at": "2026-09-30T10:05:00"}},
        ])
        self.assertEqual(len(ui), 1)
        self.assertEqual(ui[0]["role"], "goal_check")
        self.assertEqual(ui[0]["created_at"], "2026-09-30T10:05:00")
        self.assertEqual(ui[0]["goal"]["action"], "block")
        # 卡片不该带用户气泡的字段
        self.assertNotIn("attachments", ui[0])
        self.assertNotIn("refs", ui[0])

    def test_set_becomes_card_not_user_bubble(self):
        ui = ws_bridge._history_to_ui([
            {"role": "user", "content": "[Goal set]\nCondition: x\nWork toward...",
             "goal": {"kind": "set", "condition": COND}},
        ])
        self.assertEqual(len(ui), 1)
        self.assertEqual(ui[0]["role"], "goal_check")
        self.assertEqual(ui[0]["goal"]["kind"], "set")
        # 关键：不能再出现一条 "[Goal set] Condition: …" 的用户气泡
        self.assertNotIn(
            "[Goal set]", " ".join(m.get("content", "") for m in ui))

    def test_system_reminder_still_filtered_with_goal_present(self):
        """分流放在 `<system-reminder>` 前缀判断之前，但注入消息仍然不可见。"""
        ui = ws_bridge._history_to_ui([
            {"role": "user", "content": "<system-reminder>\n<env>x</env>\n</system-reminder>"},
            {"role": "user", "content": "", "goal": {"kind": "check", "action": "defer"}},
        ])
        self.assertEqual([m["role"] for m in ui], ["goal_check"])

    def test_mixed_history_keeps_order(self):
        ui = ws_bridge._history_to_ui([
            {"role": "user", "content": "做吧",
             "goal": {"kind": "instruction", "condition": COND}},
            {"role": "assistant", "content": "好"},
            {"role": "user", "content": "", "goal": {"kind": "check", "action": "block"}},
            {"role": "assistant", "content": "继续"},
            {"role": "user", "content": "", "goal": {"kind": "check", "action": "achieved"}},
        ])
        self.assertEqual([m["role"] for m in ui],
                         ["user", "assistant", "goal_check", "assistant", "goal_check"])


# ══════════════════════════════════════════════════════════════════
#  六、session_manage：读写往返 + meta 新字段
# ══════════════════════════════════════════════════════════════════

class TestSessionManageGoalFields(_AgentTest):
    def _sm(self) -> SessionManager:
        sm = SessionManager(self.root / "data" / ".chathistory", SYSTEM_PROMPT)
        sm.get_session_file(SID).touch()
        sm.ensure_index_entry(SID)
        return sm

    def test_jsonl_round_trip_preserves_goal(self):
        sm = self._sm()
        f = sm.get_session_file(SID)
        marker = {"kind": "check", "action": "block", "round": 2,
                  "reason": "缺证据", "condition": COND}
        sm.append_message_to_session(f, {"role": "user", "content": "", "goal": marker})
        loaded = sm.load_session_history(f)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["role"], "user")
        self.assertEqual(loaded[0]["goal"], marker)

    def test_user_row_without_goal_has_no_field(self):
        sm = self._sm()
        f = sm.get_session_file(SID)
        sm.append_message_to_session(f, {"role": "user", "content": "普通消息"})
        row = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
        self.assertNotIn("goal", row)

    def test_unknown_role_still_falls_back_to_user(self):
        """路线①的关键前提：**不新增 role** —— 否则 load_session_history 的 else
        兜底会把未知 role 静默强转成 user（消息内容变了却看不出来）。"""
        sm = self._sm()
        f = sm.get_session_file(SID)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": "goal_check", "content": ""}) + "\n")
        loaded = sm.load_session_history(f)
        self.assertEqual(loaded[0]["role"], "user")

    def test_set_session_execution_partial_update(self):
        sm = self._sm()
        sm.set_session_execution(SID, execution_mode=MODE_GOAL,
                                 goal_condition=COND, goal_round=0,
                                 goal_started_at=100.0)
        meta = sm.load_meta(SID)
        self.assertEqual(meta["execution_mode"], MODE_GOAL)
        self.assertEqual(meta["goal_round"], 0)
        self.assertEqual(meta["goal_started_at"], 100.0)
        # 只改轮次 → 其余不动（_UNSET 语义）
        sm.set_session_execution(SID, goal_round=7)
        meta = sm.load_meta(SID)
        self.assertEqual(meta["goal_round"], 7)
        self.assertEqual(meta["goal_started_at"], 100.0)
        self.assertEqual(meta["goal_condition"], COND)
        # 显式 None = 写入空（区别于省略）
        sm.set_session_execution(SID, goal_round=None, goal_started_at=None)
        meta = sm.load_meta(SID)
        self.assertIsNone(meta["goal_round"])
        self.assertIsNone(meta["goal_started_at"])
        self.assertEqual(meta["goal_condition"], COND)

    def test_list_sessions_carries_goal_metrics(self):
        """这两个字段必须随 `sessions` 列表下发 —— 它是断线重连 / 整页重载后恢复
        常驻目标条「第 N 轮」的**唯一**通道（session_history 只在切会话时发）。"""
        sm = self._sm()
        sm.set_session_execution(SID, execution_mode=MODE_GOAL,
                                 goal_condition=COND, goal_round=5,
                                 goal_started_at=1234.0)
        entry = next(x for x in sm.list_sessions("active") if x["id"] == SID)
        self.assertEqual(entry["goal_condition"], COND)
        self.assertEqual(entry["goal_round"], 5)
        self.assertEqual(entry["goal_started_at"], 1234.0)

    def test_list_sessions_defaults_for_legacy(self):
        sm = self._sm()
        entry = next(x for x in sm.list_sessions("active") if x["id"] == SID)
        self.assertIsNone(entry["goal_round"])
        self.assertIsNone(entry["goal_started_at"])
        self.assertEqual(entry["execution_mode"], "normal")

    def test_clear_session_resets_goal_metrics(self):
        """清空会话必须连**运行期指标**一起归零，否则会留下一条"第 7 轮"的幽灵目标条。"""
        sm = self._sm()
        f = sm.get_session_file(SID)
        sm.append_message_to_session(f, {"role": "user", "content": "hi"})
        sm.set_session_execution(SID, execution_mode=MODE_GOAL,
                                 goal_condition=COND, goal_round=7,
                                 goal_started_at=99.0, goal_tokens_at_start=10)
        sm.clear_session(f)
        meta = sm.load_meta(SID)
        self.assertIsNone(meta["execution_mode"])
        self.assertIsNone(meta["goal_condition"])
        self.assertIsNone(meta["goal_round"])
        self.assertIsNone(meta["goal_started_at"])
        self.assertIsNone(meta["goal_tokens_at_start"])


# ══════════════════════════════════════════════════════════════════
#  七、恢复连续性 + 尾部顺序契约
# ══════════════════════════════════════════════════════════════════

class TestRestoreContinuity(_AgentTest):
    def test_rebuild_keeps_round_and_started_at(self):
        """`GoalController.restore()` 按设计把轮次/时长归零（"恢复时无合理延续"）——
        但常驻目标条要显示「第 N 轮」、检查卡要显示"已运行多久"，归零会让每次
        切会话都像新设了一次目标。故由 meta 的投影字段覆盖回去。"""
        a = self._goal(self._make_agent())
        a.goal_controller.active.iterations = 6
        a.goal_controller.active.set_at = 1000.0
        a.goal_controller.active.tokens_at_start = 4242
        a._persist_execution()

        rebuilt = a._rebuild_goal_controller(COND, a.session_manager.load_meta(SID))
        self.assertIsNotNone(rebuilt.active)
        self.assertEqual(rebuilt.active.iterations, 6)
        self.assertEqual(rebuilt.active.set_at, 1000.0)
        self.assertEqual(rebuilt.active.tokens_at_start, 4242)

    def test_rebuild_falls_back_to_zero_for_legacy_meta(self):
        a = self._goal(self._make_agent())
        rebuilt = a._rebuild_goal_controller(COND, {})
        self.assertEqual(rebuilt.active.iterations, 0)

    def test_execution_state_exposes_goal_metrics(self):
        a = self._goal(self._make_agent())
        a.goal_controller.active.iterations = 3
        a.goal_controller.active.set_at = 2000.0
        st = a.execution_state()
        self.assertEqual(st["goal_round"], 3)
        self.assertEqual(st["goal_started_at"], 2000.0)

    def test_execution_state_goal_metrics_none_when_inactive(self):
        a = self._make_agent()
        st = a.execution_state()
        self.assertIsNone(st["goal_round"])
        self.assertIsNone(st["goal_started_at"])
        self.assertEqual(st["mode"], MODE_NORMAL)


class TestTurnTailOrder(_AgentTest):
    """**改造中最容易踩的坑**：终止态检查若当场 append，末行就不是 assistant ——
    `_finalize_turn_usage`（`append_usage_to_last_assistant`）会静默失败，本轮
    usage/model_info **永久丢失**；同时 `run_turn` 的返回值会变成检查记录的正文。"""

    def _run(self, agent) -> str:
        def fake_loop():
            agent._turn_usage["total_tokens"] = 123
            agent._turn_usage["prompt_tokens"] = 100
            agent.history_messages.append(
                {"role": "assistant", "content": "答复正文",
                 "reasoning_content": "", "tool_calls": []})
            agent.session_manager.append_message_to_session(
                agent.session_file, agent.history_messages[-1])
            # 模拟 Stop 边界的终止态：入队一个检查（不当场落盘）
            agent._pending_goal_checks.append(
                {"condition": COND, "round": 2, "elapsed": 5, "tokens": 10,
                 "at": "2026-09-30T10:05:00", "action": "achieved", "reason": "done"})
        with mock.patch.object(agent, "agent_loop", fake_loop):
            return agent.run_turn("做点事")

    def test_return_value_is_assistant_text_not_check_record(self):
        a = self._make_agent()
        self.assertEqual(self._run(a), "答复正文")

    def test_usage_lands_on_assistant_row_and_check_is_last(self):
        a = self._make_agent()
        self._run(a)
        rows = self._rows(a)
        self.assertEqual(rows[-1]["role"], "user")            # 检查记录在最后
        self.assertEqual(rows[-1]["goal"]["action"], "achieved")
        usage_row = next(r for r in rows if r["role"] == "assistant")
        self.assertIn("usage", usage_row)                     # ← 本轮 usage 没丢
        self.assertEqual(usage_row["usage"]["total_tokens"], 123)

    def test_no_pending_checks_leaves_tail_unchanged(self):
        a = self._make_agent()

        def fake_loop():
            a.history_messages.append(
                {"role": "assistant", "content": "答复正文",
                 "reasoning_content": "", "tool_calls": []})
            a.session_manager.append_message_to_session(
                a.session_file, a.history_messages[-1])
        with mock.patch.object(a, "agent_loop", fake_loop):
            a.run_turn("做点事")
        rows = self._rows(a)
        self.assertEqual(rows[-1]["role"], "assistant")
        self.assertEqual(len(rows), 2)                        # 无额外记录


if __name__ == "__main__":
    unittest.main()
