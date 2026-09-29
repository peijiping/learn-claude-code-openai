#!/usr/bin/env python3
"""目标模式（goal）与执行模式轴的整体守护测试 —— 2026-09-25，docs/frontend/22。

**铁律**：goal 的唯一真相恒为 `goal_controller.active`；`execution_mode == "goal"`
只是它的**投影**。本文件钉住这条铁律以及几处最容易踩坑的接线：

1. **薄委托**：进入/退出只转发既有 `set_goal` / `clear_goal`，评估与 Stop 七分支
   判定一行不改（`limit` / `error` 目标仍激活 → 模式保持 goal）。
2. **跨模式直接切换**（2026-09-27 改）：plan ↔ goal 之间**不再要求"先关闭再切换"**，
   切过去即放弃被让位那一方（目标 / plan 状态）；**plans/ 下的文书文件保留**。
3. **恢复喂料**：meta 的 `goal_condition` 经既有 `GoalController.restore()` 重建；
   本会话 meta 无目标而内存仍有 active → 必须重置（否则 A 会话的目标被 B 继承，
   这是 switch_session 不重建控制器的既有隐性缺陷）。
4. **两态对称注入**：plan 退出时必须补一条"撤销提醒"—— 注入是 append 并落盘，
   不撤销的话模型读到的最新指令仍是"禁止写操作"，表现为"批准了却拒绝干活"。
5. **单一投递路径**：`execution_mode_changed` 只经 `execution_mode_sink` 发出，
   Stop 边界（工作线程）与点 tag（事件循环）共用它。

⚠ 本文件会构造真实 `Agent`，因此需要 `OPENAI_API_KEY` / `OPENAI_BASE_URL`
（dummy 即可）在外部导出 —— 与仓库既有测试同一约定。会话与元数据全部落在
临时目录：`~/.aigent` 不可重定向（`config.AIGENT_HOME` 是模块常量），故这里
显式把 workspace 指向临时路径，并把 `aigent` 日志静音，保证零污染。

入口：`.venv/bin/python -m unittest discover -s tests`（仓库根运行）
"""

import dataclasses
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from agent_full_v2 import EXEC_MODE_TAG, Agent  # noqa: E402
from execution_mode import (  # noqa: E402
    MODE_GOAL,
    MODE_NORMAL,
    MODE_PLAN,
    PLAN_STATUS_READY,
)
from goal import GoalError  # noqa: E402
from paths import default_scratch_paths  # noqa: E402
from session_manage import SessionManager  # noqa: E402

SYSTEM_PROMPT = "you are a test harness"
SID = "Sid0000001"


class _AgentTest(unittest.TestCase):
    def setUp(self):
        # 真实 ~/.aigent/logs 不可重定向（AIGENT_HOME 是模块常量）→ 静音本进程的
        # aigent 日志处理器，避免构造 Agent 时往用户日志里写行。
        lg = logging.getLogger("aigent")
        saved = list(lg.handlers)
        lg.handlers = [logging.NullHandler()]
        self.addCleanup(lambda: setattr(lg, "handlers", saved))

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()

    def _make_agent(self) -> Agent:
        """构造一个绑定了临时 workspace 与会话的 Agent（不跑 LLM）。"""
        ws_dir = self.root / "ws"
        ws_dir.mkdir(exist_ok=True)
        ws = dataclasses.replace(
            default_scratch_paths(), id="testws",
            data_root=self.root / "data", workdir=ws_dir, bash_cwd=ws_dir)
        self.ws = ws

        agent = Agent(silent=True, workspace=ws)
        # 手动绑定会话（不走 init_session/switch_session：那两条路径会额外做
        # system prompt 重建与子智能体 store 接线，与本组断言无关）。
        agent.session_manager = SessionManager(
            self.root / "data" / ".chathistory", SYSTEM_PROMPT)
        sm = agent.session_manager
        sm.get_session_file(SID).touch()
        sm.ensure_index_entry(SID)
        agent.session_id = SID
        agent.session_file = sm.get_session_file(SID)

        self.sink_calls = []
        agent.execution_mode_sink = (
            lambda kind, payload: self.sink_calls.append((kind, dict(payload))))
        return agent


# ══════════════════════════════════════════════════════════════════
#  一、薄委托与互斥
# ══════════════════════════════════════════════════════════════════

class TestSetExecutionMode(_AgentTest):
    def test_enter_plan(self):
        a = self._make_agent()
        self.assertEqual(a.set_execution_mode(MODE_PLAN), "")
        self.assertEqual(a.execution_state()["mode"], MODE_PLAN)
        self.assertIsNone(a.goal_controller.active)

    def test_enter_goal_forwards_to_existing_set_goal(self):
        a = self._make_agent()
        self.assertEqual(a.set_execution_mode(MODE_GOAL, "跑通全部单测"), "")
        self.assertEqual(a.execution_state()["mode"], MODE_GOAL)
        self.assertIsNotNone(a.goal_controller.active)
        self.assertEqual(a.goal_controller.active.condition, "跑通全部单测")
        # 与既有 API 同源：直接读 goal_controller 也一致
        self.assertIn("跑通全部单测", a.goal_status())

    def test_goal_adds_a_set_message_for_the_model(self):
        """桌面端用户是点胶囊填条件，未必再发消息 → 必须显式告知模型。"""
        a = self._make_agent()
        a.set_execution_mode(MODE_GOAL, "条件 X")
        joined = "\n".join(str(m.get("content")) for m in a.history_messages)
        self.assertIn("[Goal set]", joined)
        self.assertIn("条件 X", joined)

    def test_exit_to_normal_clears_goal(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_GOAL, "条件 X")
        self.assertEqual(a.set_execution_mode(MODE_NORMAL), "")
        self.assertEqual(a.execution_state()["mode"], MODE_NORMAL)
        self.assertIsNone(a.goal_controller.active)
        self.assertIsNone(a.execution_state()["goal_condition"])

    def test_exit_to_normal_clears_plan(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_PLAN)
        a.execution_gate.mark_plan_ready("/tmp/a.md")
        self.assertEqual(a.set_execution_mode(MODE_NORMAL), "")
        self.assertEqual(a.execution_state()["mode"], MODE_NORMAL)
        self.assertIsNone(a.execution_state()["plan_status"])
        self.assertIsNone(a.execution_state()["plan_path"])

    def test_cross_mode_switch_is_direct(self):
        """跨模式直接切换（2026-09-27）：不再要求"先关闭再切换"。

        切过去 = 用户显式放弃被让位那一方：goal → plan 清目标；plan → goal 清 plan
        状态。两向都以空串返回（成功），模式投影与真源同步更新 —— 任一时刻都不会
        出现"两个模式并存"。
        """
        a = self._make_agent()
        a.set_execution_mode(MODE_GOAL, "条件 X")
        self.assertEqual(a.set_execution_mode(MODE_PLAN), "")   # 不再是拒绝文案
        self.assertEqual(a.execution_state()["mode"], MODE_PLAN)
        self.assertIsNone(a.goal_controller.active)             # 目标随切换退出

        b = self._make_agent()
        b.set_execution_mode(MODE_PLAN)
        plan_file = self.root / "plan.md"
        plan_file.write_text("# 计划\n", encoding="utf-8")
        b.execution_gate.mark_plan_ready(str(plan_file))
        self.assertEqual(b.set_execution_mode(MODE_GOAL, "条件 Y"), "")
        self.assertEqual(b.execution_state()["mode"], MODE_GOAL)
        self.assertIsNone(b.execution_state()["plan_status"])
        self.assertIsNone(b.execution_state()["plan_path"])
        self.assertIsNotNone(b.goal_controller.active)
        self.assertTrue(plan_file.exists())   # 文书文件保留在磁盘上（不删）

    def test_idempotent_same_mode(self):
        a = self._make_agent()
        self.assertEqual(a.set_execution_mode(MODE_PLAN), "")
        a.execution_gate.mark_plan_ready("/tmp/a.md")
        self.assertEqual(a.set_execution_mode(MODE_PLAN), "")
        # 幂等：重复进入**不重置** plan_status（否则卡片会突然回到"待产出"）
        self.assertEqual(a.execution_state()["plan_status"], PLAN_STATUS_READY)

    def test_unknown_mode_rejected(self):
        a = self._make_agent()
        self.assertTrue(a.set_execution_mode("bogus"))
        self.assertEqual(a.execution_state()["mode"], MODE_NORMAL)

    def test_goal_error_propagates(self):
        a = self._make_agent()
        with self.assertRaises(GoalError):
            a.set_execution_mode(MODE_GOAL, "")
        self.assertEqual(a.execution_state()["mode"], MODE_NORMAL)


# ══════════════════════════════════════════════════════════════════
#  二、快照的铁律校准与落盘
# ══════════════════════════════════════════════════════════════════

class TestExecutionState(_AgentTest):
    def test_goal_without_active_falls_back_to_normal(self):
        """mode 说 goal 而 active 已空（进程被杀 / meta 不一致）→ 以 active 为准。"""
        a = self._make_agent()
        a.execution_gate.set_mode(MODE_GOAL)
        self.assertIsNone(a.goal_controller.active)
        self.assertEqual(a.execution_state()["mode"], MODE_NORMAL)

    def test_persisted_to_meta(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_GOAL, "条件 X")
        meta = a.session_manager.load_meta(SID)
        self.assertEqual(meta["execution_mode"], MODE_GOAL)
        self.assertEqual(meta["goal_condition"], "条件 X")

    def test_approve_plan_persists_approved(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_PLAN)
        a.execution_gate.mark_plan_ready("/tmp/a.md")
        self.assertEqual(a.approve_plan(), "")
        meta = a.session_manager.load_meta(SID)
        self.assertEqual(meta["plan_status"], "approved")
        self.assertEqual(meta["execution_mode"], MODE_NORMAL)

    def test_approve_without_ready_plan_is_rejected(self):
        a = self._make_agent()
        self.assertTrue(a.approve_plan())
        a.set_execution_mode(MODE_PLAN)
        self.assertTrue(a.approve_plan())   # 进了 plan 但还没产出文书


# ══════════════════════════════════════════════════════════════════
#  三、恢复（喂料 + 跨会话泄漏修复）
# ══════════════════════════════════════════════════════════════════

class TestRestoreExecutionState(_AgentTest):
    def test_restore_goal_from_meta(self):
        a = self._make_agent()
        a.session_manager.set_session_execution(
            SID, execution_mode=MODE_GOAL, goal_condition="恢复条件")

        b = self._make_agent()          # 新 Agent，同一会话文件
        b._restore_execution_state()
        self.assertEqual(b.execution_state()["mode"], MODE_GOAL)
        self.assertIsNotNone(b.goal_controller.active)
        self.assertEqual(b.goal_controller.active.condition, "恢复条件")
        self.assertEqual(b.goal_controller.consecutive_blocks, 0)

    def test_restore_plan_from_meta(self):
        a = self._make_agent()
        a.session_manager.set_session_execution(
            SID, execution_mode=MODE_PLAN, plan_status=PLAN_STATUS_READY)
        b = self._make_agent()
        b._restore_execution_state()
        self.assertEqual(b.execution_state()["mode"], MODE_PLAN)
        self.assertEqual(b.execution_state()["plan_status"], PLAN_STATUS_READY)

    def test_preselect_plan_without_artifact_restores_plan_mode(self):
        """无会话时预选的「计划模式」必须能落到首轮（2026-09-27，§2.5）。

        预选路径写进 meta 的形状是 `execution_mode=plan, plan_status=None`
        （定稿时还没有文书），与上一条（READY）不同 —— 这里钉住"**没有
        plan_status 也必须进 plan 模式**"，并顺带断言该状态下写操作确实被拦
        （否则"预选计划模式"等于没选）。"""
        a = self._make_agent()
        a.session_manager.set_session_execution(
            SID, execution_mode=MODE_PLAN, plan_status=None, goal_condition=None)
        meta = a.session_manager.load_meta(SID)
        self.assertEqual(meta.get("execution_mode"), MODE_PLAN)
        self.assertIsNone(meta.get("plan_status"))
        b = self._make_agent()
        b._restore_execution_state()
        self.assertEqual(b.execution_state()["mode"], MODE_PLAN)
        self.assertIsNone(b.execution_state()["plan_status"])
        self.assertTrue(b.execution_gate.plan_blocks_writes())

    def test_approved_plan_does_not_restore_plan_mode(self):
        a = self._make_agent()
        a.session_manager.set_session_execution(
            SID, execution_mode=MODE_PLAN, plan_status="approved")
        b = self._make_agent()
        b._restore_execution_state()
        self.assertEqual(b.execution_state()["mode"], MODE_NORMAL)

    def test_stale_in_memory_goal_is_reset(self):
        """本会话 meta 无目标，而内存还挂着上一个会话的目标 → 必须重置。"""
        a = self._make_agent()
        a.set_execution_mode(MODE_GOAL, "A 会话的条件")
        # 模拟"切到一个 meta 里没有目标的会话"
        a.session_manager.set_session_execution(
            SID, execution_mode=None, goal_condition=None)
        a._restore_execution_state()
        self.assertIsNone(a.goal_controller.active)
        self.assertEqual(a.execution_state()["mode"], MODE_NORMAL)

    def test_blank_condition_does_not_resurrect_goal(self):
        a = self._make_agent()
        a.session_manager.set_session_execution(
            SID, execution_mode=MODE_GOAL, goal_condition="   ")
        b = self._make_agent()
        b._restore_execution_state()
        self.assertIsNone(b.goal_controller.active)
        self.assertEqual(b.execution_state()["mode"], MODE_NORMAL)


# ══════════════════════════════════════════════════════════════════
#  三之二、预选执行模式的交接（2026-09-27，docs/frontend/22 §2.5）
# ══════════════════════════════════════════════════════════════════

class TestPreselectHandoff(_AgentTest):
    """无会话时预选的执行模式 → 新会话**首轮即生效**。

    `ws_bridge` 的 `chat` 新建分支在派发首轮之前写两样东西（顺序即代码顺序）：
      ① `set_session_execution(execution_mode=..., plan_status=None, goal_condition=...)`
      ② goal 额外补一条 `[Goal set]` 消息（预选路径没走 `set_execution_mode`，
         `_restore_execution_state` 也不补，不补模型就不知道目标是什么）

    这组用例按 `ws_bridge` 的**同一步骤**写，然后走**真实的 `switch_session`**
    （= `SessionRuntime.build_agent()` 在首轮前做的事），断言首轮上下文里确实
    既有模式、也有目标条件。**改动 `_restore_execution_state` 或去掉 ② 都会红。**
    """

    def _preselect(self, agent: Agent, mode: str, condition: str = "") -> None:
        sm = agent.session_manager
        sm.set_session_execution(
            SID, execution_mode=mode, plan_status=None,
            goal_condition=(condition if mode == MODE_GOAL else None))
        if mode == MODE_GOAL:
            sm.append_message_to_session(sm.get_session_file(SID), {
                "role": "user",
                "content": (
                    "[Goal set]\n"
                    f"Condition: {condition}\n"
                    "Work toward this condition; the session will be evaluated "
                    "when you stop."
                ),
            })

    def _first_turn_agent(self) -> Agent:
        """全新 Agent + 真实 switch_session（首轮前的构建路径）。

        真实构建走 `init_session`/`switch_session` 的完整尾巴（system prompt 重建、
        记忆索引、任务板同步…），这些会往工作空间的元数据目录写；临时 workspace
        里对应目录先建出来（真机上由 `WorkspacePaths` 的初始化负责）。
        """
        for d in (self.ws.memory_dir, self.ws.tasks_dir):
            d.mkdir(parents=True, exist_ok=True)
        agent = Agent(silent=True, workspace=self.ws)
        agent.switch_session(SID)
        return agent

    def test_preselect_goal_is_visible_on_first_turn(self):
        a = self._make_agent()
        self._preselect(a, MODE_GOAL, "跑通全部单测")

        b = self._first_turn_agent()
        self.assertEqual(b.execution_state()["mode"], MODE_GOAL)
        self.assertIsNotNone(b.goal_controller.active)
        self.assertEqual(b.goal_controller.active.condition, "跑通全部单测")
        joined = "\n".join(str(m.get("content")) for m in b.history_messages)
        self.assertIn("[Goal set]", joined)
        self.assertIn("跑通全部单测", joined)

    def test_preselect_plan_blocks_writes_on_first_turn(self):
        a = self._make_agent()
        self._preselect(a, MODE_PLAN)

        b = self._first_turn_agent()
        self.assertEqual(b.execution_state()["mode"], MODE_PLAN)
        self.assertIsNone(b.execution_state()["plan_status"])
        self.assertTrue(b.execution_gate.plan_blocks_writes())


# ══════════════════════════════════════════════════════════════════
#  四、两态对称注入
# ══════════════════════════════════════════════════════════════════

class TestSyncExecutionMode(_AgentTest):
    def _injected(self, agent) -> list:
        return [m for m in agent.history_messages
                if EXEC_MODE_TAG in str(m.get("content"))]

    def test_no_injection_in_plain_normal_session(self):
        a = self._make_agent()
        a._sync_execution_mode()
        self.assertEqual(self._injected(a), [])
        self.assertEqual(a._last_injection_revision(EXEC_MODE_TAG), "")

    def test_plan_active_then_exited(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_PLAN)
        a._sync_execution_mode()
        self.assertEqual(a._last_injection_revision(EXEC_MODE_TAG), "plan-active")
        self.assertIn("计划模式", self._injected(a)[-1]["content"])

        # 同态重复 → 零开销、不刷屏
        a._sync_execution_mode()
        self.assertEqual(len(self._injected(a)), 1)

        # 退出 plan（批准）→ **必须**补一条撤销提醒
        a.execution_gate.mark_plan_ready("/tmp/a.md")
        a.approve_plan()
        a._sync_execution_mode()
        self.assertEqual(a._last_injection_revision(EXEC_MODE_TAG), "plan-exited")
        last = self._injected(a)[-1]["content"]
        self.assertIn("写操作已恢复", last)
        self.assertIn("<system-reminder>", last)

    def test_plan_to_goal_injects_goal_variant(self):
        """plan → goal 直接切换：撤销提醒必须换成"目标模式"版（2026-09-27）。

        照抄回落到 normal 的那版（末句"请按已批准的计划文书开始执行"）会让模型去找
        一份**已被作废**的计划 —— 这条路径上它通常根本没被批准。指纹仍记 `plan-exited`
        （语义态），文案随当前模式变。
        """
        a = self._make_agent()
        a.set_execution_mode(MODE_PLAN)
        a._sync_execution_mode()
        self.assertEqual(a._last_injection_revision(EXEC_MODE_TAG), "plan-active")

        a.set_execution_mode(MODE_GOAL, "条件 Z")
        a._sync_execution_mode()
        self.assertEqual(a._last_injection_revision(EXEC_MODE_TAG), "plan-exited")
        last = self._injected(a)[-1]["content"]
        self.assertIn("目标模式", last)
        self.assertIn("写操作已恢复", last)
        self.assertNotIn("已批准的计划文书", last)

    def test_injection_is_persisted(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_PLAN)
        a._sync_execution_mode()
        raw = a.session_file.read_text(encoding="utf-8")
        self.assertIn("plan-active", raw)

    def test_goal_does_not_inject_plan_reminders(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_GOAL, "条件 X")
        a._sync_execution_mode()
        self.assertEqual(self._injected(a), [])


# ══════════════════════════════════════════════════════════════════
#  五、Stop 边界终止与单一投递路径
# ══════════════════════════════════════════════════════════════════

class TestGoalTerminationAndSink(_AgentTest):
    def test_on_goal_terminated_falls_back_and_clears(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_GOAL, "条件 X")
        a.clear_goal()                 # Stop 边界里控制器**先**清 active
        a._on_goal_terminated()
        self.assertEqual(a.execution_state()["mode"], MODE_NORMAL)
        self.assertIsNone(a.execution_state()["goal_condition"])
        self.assertIsNone(a.session_manager.load_meta(SID)["goal_condition"])

    def test_limit_error_keep_goal_mode(self):
        """`limit` / `error` 时目标仍激活 → 模式**保持** goal（既有语义，勿动）。"""
        a = self._make_agent()
        a.set_execution_mode(MODE_GOAL, "条件 X")
        self.assertIsNotNone(a.goal_controller.active)
        self.assertEqual(a.execution_state()["mode"], MODE_GOAL)

    def test_sink_receives_single_changed_envelope(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_PLAN)
        kinds = [k for k, _ in self.sink_calls]
        self.assertEqual(kinds, ["execution_mode_changed"])
        payload = self.sink_calls[0][1]
        self.assertEqual(set(payload.keys()),
                         {"session_id", "mode", "plan_status",
                          "plan_path", "goal_condition"})
        self.assertEqual(payload["session_id"], SID)
        self.assertEqual(payload["mode"], MODE_PLAN)

    def test_same_mode_emits_nothing(self):
        a = self._make_agent()
        a.set_execution_mode(MODE_PLAN)
        self.sink_calls.clear()
        a.set_execution_mode(MODE_PLAN)      # 幂等 → 不广播
        self.assertEqual(self.sink_calls, [])

    def test_no_sink_is_silent(self):
        """未接线（CLI / 单测）→ 只落盘不推送，行为与改造前一致。"""
        a = self._make_agent()
        a.execution_mode_sink = None
        self.assertEqual(a.set_execution_mode(MODE_PLAN), "")   # 不抛


if __name__ == "__main__":
    unittest.main()
