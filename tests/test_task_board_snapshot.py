#!/usr/bin/env python3
"""任务面板快照与任务存储布局的守护测试（2026-09-16 两轮改造）。

运行方式::

    .venv/bin/python -m unittest discover -s tests -v

## 被守护的约定

0. **存储布局**：一个会话一个 JSON（`.tasks/<scope>.json`），文件内以组为 key；
   `group_id` 不落进任务体（靠文件 key 承载）；写盘原子且不留临时文件。
   改 `task_manager` 的读写路径时，本文件的 `TaskFileLayoutTests` 必红。
1. **零迁移**：`Task` 新增字段全部带默认值，缺字段的历史条目必须能直接加载
   （读盘走 `Task(**条目)`）。改动字段时若漏了默认值，本文件必红。
2. **组不变量**：同一会话同时最多只有一个未完成组（`_ensure_group_id`）——
   "多组 task 只显示最后执行中那组"与"回放不显示已结束组"两条需求靠它成立。
3. **`blocked` 是派生态**：由 `blockedBy` 实时算出，不落盘。
4. **中断恢复**：`release_stale_in_progress` 只归一 owner 为空/agent 的
   in_progress；队友持有的一律不动。
5. **清理边界**：`clear_scope` 在 scope 为空时必须拒绝执行（否则会误删旧全局看板）。
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

from task_manager import (  # noqa: E402
    AGENT_OWNER,
    LEGACY_GROUP_ID,
    MAX_TASK_DEPTH,
    Task,
    TaskManager,
    build_board,
    current_board,
    latest_board,
    load_scope_tasks,
    read_doc,
)

SCOPE = "session_testscope"


class _BoardTestCase(unittest.TestCase):
    """公共夹具：临时目录 + 已设 scope 的 TaskManager。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.tm = TaskManager(self.dir)
        self.tm.set_scope(SCOPE)

    def mk(self, subject, **kw):
        return self.tm._create_task(subject, **kw)

    def board(self):
        return current_board(SCOPE, self.dir)

    def latest(self):
        return latest_board(SCOPE, self.dir)

    def task_file(self, scope: str = SCOPE) -> Path:
        return self.dir / f"{scope}.json"

    def write_groups(self, groups: dict, scope: str = SCOPE) -> None:
        """手写一份任务文件（group_id → 条目列表），用于模拟历史/损坏数据。"""
        self.task_file(scope).write_text(json.dumps({
            "version": 1, "scope": scope, "updated_at": 0.0, "groups": groups,
        }, ensure_ascii=False), encoding="utf-8")

    def patch_raw(self, task_id: str, **fields) -> None:
        """就地改一条**落盘**条目的字段（模拟改造前落下的坏数据，如悬空依赖）。

        与 `write_groups` 的区别：只动目标条目，不覆盖整份文件 ——
        自愈类用例需要"正常任务 + 一条坏数据"同时在场。
        """
        doc = json.loads(self.task_file().read_text(encoding="utf-8"))
        for items in doc["groups"].values():
            for item in items:
                if item.get("id") == task_id:
                    item.update(fields)
        self.task_file().write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")


# ── 0. 存储布局契约 ───────────────────────────────────────────────

class TaskFileLayoutTests(_BoardTestCase):
    def test_one_file_per_scope_holding_all_groups(self):
        """三组活（每组做完再派下一组）→ 磁盘上仍只有一个文件、三个组 key。"""
        gids = []
        for i in range(3):
            t = self.mk(f"组{i}")
            gids.append(t.group_id)
            self.tm._claim_task(t.id)
            self.tm._complete_task(t.id)

        self.assertEqual(sorted(p.name for p in self.dir.glob("*.json")),
                         [f"{SCOPE}.json"])
        doc = read_doc(SCOPE, self.dir)
        self.assertEqual(sorted(doc["groups"]), sorted(gids))
        self.assertEqual(len(gids), len(set(gids)), "每组必须是独立 key")

    def test_group_id_is_not_persisted_in_task_body(self):
        """组归属由文件 key 承载 —— 任务体内不再冗余存一份 group_id。"""
        t = self.mk("A")

        doc = read_doc(SCOPE, self.dir)
        self.assertIn(t.group_id, doc["groups"])
        entry = doc["groups"][t.group_id][0]
        self.assertNotIn("group_id", entry)
        self.assertEqual(self.tm._load_task(t.id).group_id, t.group_id)

    def test_scope_maps_to_single_file(self):
        """scope ↔ 文件名口径（paths 与 task_manager 必须同源）。"""
        from paths import task_scope_file, task_scope_key

        self.assertEqual(task_scope_key(None), "_global")
        self.assertEqual(task_scope_file(None).name, "_global.json")
        self.assertEqual(task_scope_file(SCOPE).name, f"{SCOPE}.json")
        self.assertEqual(task_scope_file(SCOPE, self.dir), self.task_file())

    def test_scope_none_uses_global_sentinel_file(self):
        """未设 scope（旧全局看板）落到 _global.json，不与任何会话串台。"""
        other = TaskManager(self.dir)          # 刻意不 set_scope
        other._create_task("全局看板任务")

        self.assertTrue(self.task_file("_global").exists())
        self.assertFalse(self.task_file().exists())
        self.assertEqual(load_scope_tasks(None, self.dir)[0].subject, "全局看板任务")

    def test_writes_are_atomic_and_leave_no_temp_files(self):
        """临时文件 + os.replace：单文件承载整会话，绝不能留下半截文件。"""
        t = self.mk("A")
        self.tm._claim_task(t.id)
        self.tm._complete_task(t.id, "做完了")

        leftovers = [p.name for p in self.dir.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])
        # 文件必须始终是完整可解析的 JSON
        self.assertIsInstance(read_doc(SCOPE, self.dir)["groups"], dict)

    def test_task_ids_unique_and_scope_free(self):
        """id 不再编码 scope；同一会话内连续创建（同秒）也必须唯一。"""
        ids = [self.mk(f"任务{i}").id for i in range(20)]

        self.assertEqual(len(set(ids)), 20)
        self.assertTrue(all(i.startswith("t_") for i in ids), ids)


# ── 1. 零迁移：缺字段的历史条目必须能读 ────────────────────────────

class LegacyCompatibilityTests(_BoardTestCase):
    def test_entry_missing_new_fields_loads_with_defaults(self):
        """改造前落盘的条目只有 6 个字段；加载后新增字段必须取默认值。"""
        old = {
            "id": "t_1700000000_0001",
            "subject": "老任务",
            "description": "改造前创建",
            "status": "pending",
            "owner": None,
            "blockedBy": [],
        }
        self.write_groups({LEGACY_GROUP_ID: [old]})

        loaded = self.tm._load_task(old["id"])

        self.assertEqual(loaded.subject, "老任务")
        self.assertIsNone(loaded.parentId)
        self.assertEqual(loaded.depth, 0)
        self.assertEqual(loaded.orderIndex, 0)
        self.assertEqual(loaded.path, "")
        self.assertEqual(loaded.created_at, 0.0)
        self.assertEqual(loaded.result, "")
        # 组归属不再来自任务体，而是文件里的 key
        self.assertEqual(loaded.group_id, LEGACY_GROUP_ID)

    def test_legacy_tasks_grouped_under_sentinel(self):
        """哨兵组 g_legacy 下的任务仍算一个未完成组（老会话没做完的活）。"""
        self.write_groups({LEGACY_GROUP_ID: [{
            "id": "t_1700000000_0002",
            "subject": "老任务", "description": "", "status": "in_progress",
            "owner": AGENT_OWNER, "blockedBy": [],
        }]})

        board = self.board()

        self.assertIsNotNone(board)
        self.assertEqual(board["group_id"], LEGACY_GROUP_ID)
        self.assertEqual(board["status"], "running")

    def test_task_body_group_id_is_overridden_by_file_key(self):
        """任务体里残留的 group_id 一律以文件 key 为准（防双写不一致）。"""
        self.write_groups({"g_real": [{
            "id": "t_1700000000_0003", "subject": "错位", "description": "",
            "status": "pending", "owner": None, "blockedBy": [],
            "group_id": "g_stale",
        }]})

        self.assertEqual(self.tm._load_task("t_1700000000_0003").group_id, "g_real")

    def test_corrupt_file_yields_empty_list_not_crash(self):
        """整份文件损坏 → 空列表 + 面板 None，不能让读盘把上层炸掉。"""
        self.mk("正常任务")
        self.task_file().write_text("{ not json", encoding="utf-8")

        self.assertEqual(load_scope_tasks(SCOPE, self.dir), [])
        self.assertIsNone(self.board())

    def test_corrupt_entry_inside_group_is_skipped(self):
        """组内单条损坏只跳过该条，同组其它任务照常返回。"""
        self.mk("正常任务")
        doc = read_doc(SCOPE, self.dir)
        gid = next(iter(doc["groups"]))
        doc["groups"][gid].append({"id": "bad", "unknown_field": 1})
        self.task_file().write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        tasks = load_scope_tasks(SCOPE, self.dir)
        self.assertEqual([t.subject for t in tasks], ["正常任务"])

    def test_non_list_group_is_skipped(self):
        """组值不是数组（人工改坏）→ 跳过该组，不抛异常。"""
        self.write_groups({"g_bad": {"not": "a list"}})

        self.assertEqual(load_scope_tasks(SCOPE, self.dir), [])


# ── 2. 组不变量 ───────────────────────────────────────────────────

class GroupInvariantTests(_BoardTestCase):
    def test_new_tasks_join_the_unfinished_group(self):
        """未完成时新建任务必须归入同一组 —— 组不变量的唯一守卫点。"""
        a = self.mk("A")
        b = self.mk("B")

        self.assertEqual(a.group_id, b.group_id)

    def test_new_group_opens_only_after_previous_finished(self):
        a = self.mk("组1的唯一任务")
        self.tm._claim_task(a.id)
        self.tm._complete_task(a.id)

        b = self.mk("组2的任务")
        self.assertNotEqual(a.group_id, b.group_id)
        self.assertEqual(self.board()["group_id"], b.group_id)

    def test_current_board_returns_last_unfinished_group(self):
        """连续派三组活（每组做完再派下一组），current_board 只返回最后一组。"""
        for i in range(3):
            t1 = self.mk(f"组{i}-1")
            t2 = self.mk(f"组{i}-2")
            self.tm._claim_task(t1.id)
            self.tm._complete_task(t1.id)
            self.tm._claim_task(t2.id)
            self.tm._complete_task(t2.id)

        last1 = self.mk("最后一组-1")
        last2 = self.mk("最后一组-2")
        self.tm._claim_task(last1.id)

        board = self.board()
        self.assertEqual(board["group_id"], last1.group_id)
        self.assertEqual({t["id"] for t in board["tasks"]}, {last1.id, last2.id})

    def test_current_board_none_when_all_completed(self):
        """全部完成 → current_board 返回 None（回放时已结束的组不显示）。"""
        t = self.mk("唯一任务")
        self.tm._claim_task(t.id)
        self.tm._complete_task(t.id)

        self.assertIsNone(self.board())

    def test_latest_board_still_returns_done_group(self):
        """但 latest_board 必须仍给出 status=done 的那一版 ——
        否则前端在"最后一笔完成"时收不到终态快照，看不到「全部完成 N/N」。
        """
        t = self.mk("唯一任务")
        self.tm._claim_task(t.id)
        self.tm._complete_task(t.id)

        board = self.latest()
        self.assertIsNotNone(board)
        self.assertEqual(board["status"], "done")
        self.assertEqual(board["counts"]["completed"], 1)

    def test_latest_board_none_when_no_tasks(self):
        self.assertIsNone(self.latest())


# ── 3. blocked 是派生态 ───────────────────────────────────────────

class DerivedStatusTests(_BoardTestCase):
    def test_blocked_is_derived_not_persisted(self):
        dep = self.mk("依赖")
        downstream = self.mk("下游", blockedBy=[dep.id])

        board = self.board()
        row = next(t for t in board["tasks"] if t["id"] == downstream.id)

        self.assertEqual(row["derived_status"], "blocked")
        # 落盘的 status 仍是 pending —— blocked 不进文件，避免依赖状态双写
        self.assertEqual(self.tm._load_task(downstream.id).status, "pending")

    def test_blocked_clears_after_dependency_completes(self):
        dep = self.mk("依赖")
        downstream = self.mk("下游", blockedBy=[dep.id])
        self.tm._claim_task(dep.id)
        self.tm._complete_task(dep.id)

        board = self.board()
        row = next(t for t in board["tasks"] if t["id"] == downstream.id)

        self.assertEqual(row["derived_status"], "pending")

    def test_missing_dependency_counts_as_blocked_for_legacy_data(self):
        """**存量**数据里已有的悬空依赖 → 运行期仍视作阻塞（兜底，故意保留）。

        新数据不会再产生悬空依赖：`create_task` 直接拒绝（见 DependencyHygieneTests），
        这里手写文件模拟改造前落下的坏数据。
        """
        self.write_groups({LEGACY_GROUP_ID: [{
            "id": "t_1700000000_9001", "subject": "悬空依赖", "description": "",
            "status": "pending", "owner": None, "blockedBy": ["task_does_not_exist"],
        }]})
        row = next(r for r in self.board()["tasks"] if r["id"] == "t_1700000000_9001")
        self.assertEqual(row["derived_status"], "blocked")

    def test_counters(self):
        a = self.mk("A")
        b = self.mk("B", blockedBy=[a.id])
        c = self.mk("C")
        self.tm._claim_task(a.id)

        counts = self.board()["counts"]
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["in_progress"], 1)
        self.assertEqual(counts["pending"], 1)   # C
        self.assertEqual(counts["blocked"], 1)   # B
        self.assertEqual(counts["completed"], 0)


# ── 3b. 依赖卫生与残留清理（2026-09-18 事故修复）────────────────────

class DependencyHygieneTests(_BoardTestCase):
    """守护三件事：入口拒绝悬空依赖 / 残留可就地自愈 / 面板不再把"待办残留"当"执行中"。

    事故复盘（`session_F2xNqhpm0t`）：模型 `create_task(blockedBy=["1"])`
    写了个不存在的 id → 该任务被永久判定 blocked → 组永不闭合 →
    「会话已完成」而面板永久显示「执行中」，且当时没有 update/delete 可清理。
    下面每条用例对应这条链路的一环。
    """

    def test_create_rejects_dangling_dependency(self):
        with self.assertRaises(ValueError) as ctx:
            self.mk("依赖写错", blockedBy=["1"])
        self.assertIn("依赖 id 不存在", str(ctx.exception))
        self.assertEqual(load_scope_tasks(SCOPE, self.dir), [], "拒绝创建时不得落盘")

    def test_create_rejects_blank_dependency(self):
        with self.assertRaises(ValueError):
            self.mk("空依赖", blockedBy=["  "])

    def test_create_accepts_real_id_and_dedups(self):
        dep = self.mk("依赖")
        downstream = self.mk("下游", blockedBy=[dep.id, dep.id])
        self.assertEqual(downstream.blockedBy, [dep.id])

    def test_legacy_dangling_task_is_healed_by_update_task(self):
        """事故现场的自愈路径：update 改依赖 → claim → complete，无需另建新任务。"""
        residual = self.mk("残留任务")
        self.patch_raw(residual.id, blockedBy=["1"])

        blocked = self.tm.run_claim_task(residual.id)
        self.assertTrue(blocked.startswith("Blocked by: ['1']"), blocked)
        self.assertIn("悬空引用", blocked, "阻塞反馈必须点明出口，否则模型只会另建修正版")

        self.assertTrue(self.tm.run_update_task(residual.id, blockedBy=[])
                        .startswith("Updated"))
        self.assertTrue(self.tm.run_claim_task(residual.id).startswith("Claimed"))
        self.assertTrue(self.tm.run_complete_task(residual.id, "已修正依赖并完成")
                        .startswith("Completed"))
        self.assertIsNone(self.board(), "组闭合后 current_board 必须回到 None")

    def test_update_task_rejects_cycle(self):
        a = self.mk("A")
        b = self.mk("B", blockedBy=[a.id])
        out = self.tm.run_update_task(a.id, blockedBy=[b.id])
        self.assertTrue(out.startswith("Error:"), out)
        self.assertIn("成环", out)
        self.assertEqual(self.tm._load_task(a.id).blockedBy, [], "成环修改必须整体回滚")

    def test_update_task_rejects_dangling_and_unknown_id(self):
        t = self.mk("A")
        self.assertIn("依赖 id 不存在",
                      self.tm.run_update_task(t.id, blockedBy=["nope"]))
        self.assertIn("not found", self.tm.run_update_task("t_ghost", subject="x"))

    def test_update_task_cannot_bypass_state_machine(self):
        """只开放字段级修正：status 绕不过 claim → complete。"""
        t = self.mk("A")
        self.assertTrue(self.tm.run_update_task(t.id, subject="A'", result="备注")
                        .startswith("Updated"))
        loaded = self.tm._load_task(t.id)
        self.assertEqual(loaded.subject, "A'")
        self.assertEqual(loaded.result, "备注")
        self.assertEqual(loaded.status, "pending")

    def test_delete_task_closes_group_and_strips_reference(self):
        stale = self.mk("卡死残留")
        after = self.mk("引用它的任务", blockedBy=[stale.id])
        self.tm._claim_task(stale.id)

        out = self.tm.run_delete_task(stale.id)

        self.assertTrue(out.startswith("Deleted"), out)
        self.assertIn("引用它的任务", out)
        # 引用被剥离 → 引用方不再悬空，可以直接认领
        self.assertEqual(self.tm._load_task(after.id).blockedBy, [])
        self.assertTrue(self.tm.run_claim_task(after.id).startswith("Claimed"))
        self.tm.run_complete_task(after.id)
        self.assertIsNone(self.board())

    def test_delete_last_task_removes_empty_group(self):
        t = self.mk("唯一任务")
        self.tm.run_delete_task(t.id)
        doc = read_doc(SCOPE, self.dir)
        self.assertEqual(doc["groups"], {}, "删空后不得留空组（会闪出空气泡）")
        self.assertEqual(len(list(self.dir.glob("*.json"))), 1, "文件本身留给会话级清理")

    def test_delete_task_refuses_when_children_exist(self):
        parent = self.mk("父")
        self.mk("子", parent_id=parent.id)
        out = self.tm.run_delete_task(parent.id)
        self.assertTrue(out.startswith("Error:"), out)
        self.assertIn("子任务", out)

    def test_delete_unknown_id_is_readable(self):
        self.assertIn("not found", self.tm.run_delete_task("t_ghost"))

    def test_has_in_progress_separates_backlog_from_execution(self):
        """核心语义：**有活但没人跑 ≠ 执行中** —— 面板假「执行中」的根因就在这一位。"""
        dep = self.mk("A")
        downstream = self.mk("B", blockedBy=[dep.id])

        board = self.board()
        self.assertEqual(board["status"], "running")     # 组没干完
        self.assertFalse(board["has_in_progress"])       # 但没人真在跑

        self.tm._claim_task(dep.id)
        self.assertTrue(self.board()["has_in_progress"])

        self.tm._complete_task(dep.id)
        self.assertFalse(self.board()["has_in_progress"])
        self.tm._claim_task(downstream.id)
        self.tm._complete_task(downstream.id)

        done = self.latest()
        self.assertEqual(done["status"], "done")
        self.assertFalse(done["has_in_progress"])


# ── 4. 层级 ───────────────────────────────────────────────────────

class HierarchyTests(_BoardTestCase):
    def test_depth_and_order_derived_from_parent(self):
        root = self.mk("根")
        c1 = self.mk("子1", parent_id=root.id)
        c2 = self.mk("子2", parent_id=root.id)

        self.assertEqual(root.depth, 0)
        self.assertEqual(c1.depth, 1)
        self.assertEqual(c1.orderIndex, 0)
        self.assertEqual(c2.orderIndex, 1)
        self.assertIsNone(root.parentId)

    def test_max_depth_enforced(self):
        """超过 MAX_TASK_DEPTH 必须被拒绝（防面板缩进失控）。"""
        chain = [self.mk("L0")]
        for i in range(1, MAX_TASK_DEPTH + 1):
            chain.append(self.mk(f"L{i}", parent_id=chain[-1].id))

        msg = self.tm.run_create_task("太深了", parent_id=chain[-1].id)

        self.assertTrue(msg.startswith("Error:"), msg)
        self.assertIn("depth", msg)

    def test_unknown_parent_rejected(self):
        msg = self.tm.run_create_task("孤儿", parent_id="task_nope")
        self.assertTrue(msg.startswith("Error:"), msg)

    def test_child_total_derived(self):
        root = self.mk("根")
        self.mk("子1", parent_id=root.id)
        sub = self.mk("子2", parent_id=root.id)
        self.tm._claim_task(sub.id)
        self.tm._complete_task(sub.id)

        row = next(r for r in self.board()["tasks"] if r["id"] == root.id)
        self.assertEqual(row["child_total"], 2)
        self.assertEqual(row["child_completed"], 1)


# ── 5. 中断恢复 ───────────────────────────────────────────────────

class StaleInProgressTests(_BoardTestCase):
    def test_releases_agent_owned_in_progress(self):
        t = self.mk("被中断的任务")
        self.tm._claim_task(t.id)

        released = self.tm.release_stale_in_progress()

        self.assertEqual(released, 1)
        back = self.tm._load_task(t.id)
        self.assertEqual(back.status, "pending")
        self.assertIsNone(back.owner)
        self.assertIsNone(back.started_at)
        # 归一后必须能重新认领（这正是本方法存在的理由）
        self.assertTrue(self.tm._claim_task(t.id).startswith("Claimed"))

    def test_teammate_owned_in_progress_untouched(self):
        """队友（owner=<队友名>）可能真的还在跑，不能归一。"""
        t = self.mk("队友在做的任务")
        self.tm._claim_task(t.id, owner="agent-a")

        released = self.tm.release_stale_in_progress()

        self.assertEqual(released, 0)
        self.assertEqual(self.tm._load_task(t.id).status, "in_progress")

    def test_completed_and_pending_untouched(self):
        p = self.mk("待办")
        d = self.mk("已完成")
        self.tm._claim_task(d.id)
        self.tm._complete_task(d.id)

        self.assertEqual(self.tm.release_stale_in_progress(), 0)
        self.assertEqual(self.tm._load_task(p.id).status, "pending")
        self.assertEqual(self.tm._load_task(d.id).status, "completed")

    def test_stale_before_guard(self):
        """started_at 不早于 stale_before 的（刚认领的）不动。"""
        t = self.mk("刚认领")
        self.tm._claim_task(t.id)
        started = self.tm._load_task(t.id).started_at

        self.assertEqual(self.tm.release_stale_in_progress(stale_before=started - 1), 0)

    def test_board_emitted_after_release(self):
        seen = []
        self.tm.set_emitter(seen.append)
        t = self.mk("被中断")
        self.tm._claim_task(t.id)
        seen.clear()

        self.tm.release_stale_in_progress()

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["board"]["counts"]["pending"], 1)


# ── 6. 快照推送 ───────────────────────────────────────────────────

class EmitterTests(_BoardTestCase):
    def test_revision_is_monotonic(self):
        seen = []
        self.tm.set_emitter(seen.append)

        t = self.mk("A")
        self.tm._claim_task(t.id)
        self.tm._complete_task(t.id)

        revs = [p["board"]["revision"] for p in seen]
        self.assertEqual(len(revs), 3)
        self.assertEqual(revs, sorted(revs))
        self.assertLess(revs[0], revs[-1])

    def test_every_mutation_emits_a_snapshot(self):
        seen = []
        self.tm.set_emitter(seen.append)
        t = self.mk("A")
        self.tm._claim_task(t.id)
        self.tm._complete_task(t.id)

        # 每次都是整份快照（幂等替换，不是增量）——最后一份即终态
        last = seen[-1]["board"]
        self.assertEqual(last["status"], "done")
        self.assertIsInstance(last["tasks"], list)

    def test_emitter_none_is_silent(self):
        """CLI 场景不注入 emitter，能力不受影响。"""
        self.tm.set_emitter(None)
        t = self.mk("A")
        self.assertTrue(self.tm._claim_task(t.id).startswith("Claimed"))

    def test_emitter_exception_does_not_break_task(self):
        def boom(_payload):
            raise RuntimeError("推送炸了")
        self.tm.set_emitter(boom)

        t = self.mk("A")           # 不应抛异常
        self.assertTrue(self.tm._claim_task(t.id).startswith("Claimed"))


# ── 7. 生命周期 ───────────────────────────────────────────────────

class LifecycleTests(_BoardTestCase):
    def test_clear_scope_removes_files_and_emits_none(self):
        seen = []
        self.tm.set_emitter(seen.append)
        self.mk("A")
        self.mk("B")

        removed = self.tm.clear_scope()

        self.assertEqual(removed, 2)
        self.assertEqual(list(self.dir.glob("*.json")), [])
        self.assertIsNone(seen[-1]["board"])

    def test_clear_scope_refuses_without_scope(self):
        """scope 为空时拒绝 —— 否则会误删旧全局看板的全部任务。"""
        other = TaskManager(self.dir)     # 未 set_scope
        self.mk("会话内任务")

        self.assertEqual(other.clear_scope(), 0)
        self.assertEqual(len(list(self.dir.glob("*.json"))), 1)

    def test_clear_scope_only_touches_own_scope(self):
        self.mk("本会话")
        other_scope = TaskManager(self.dir)
        other_scope.set_scope("session_other")
        other_scope._create_task("别的会话")

        self.tm.clear_scope()

        remaining = [p.name for p in self.dir.glob("*.json")]
        self.assertEqual(len(remaining), 1)
        self.assertIn("session_other", remaining[0])

    def test_completed_tasks_are_not_auto_deleted(self):
        """_gc_scoped_tasks 已停用：全部完成也不许删文件（文件留到会话删除）。"""
        t = self.mk("唯一任务")
        self.tm._claim_task(t.id)
        self.tm._complete_task(t.id)

        self.assertEqual(len(list(self.dir.glob("*.json"))), 1)


# ── 8. 纯函数入口与实例路径一致 ────────────────────────────────────

class PureFunctionContractTests(_BoardTestCase):
    def test_module_level_and_instance_paths_agree(self):
        """重放（模块级，无 Agent）与实时（实例）必须产出同一份结构。"""
        t = self.mk("A")
        self.tm._claim_task(t.id)

        self.assertEqual(current_board(SCOPE, self.dir), self.board())
        self.assertEqual(latest_board(SCOPE, self.dir), self.latest())

    def test_build_board_marks_done_only_when_all_completed(self):
        a = self.mk("A")
        b = self.mk("B")
        self.tm._claim_task(a.id)
        self.tm._complete_task(a.id)
        tasks = load_scope_tasks(SCOPE, self.dir)

        self.assertEqual(build_board(tasks, b.group_id)["status"], "running")

    def test_board_has_all_contract_keys(self):
        self.mk("A")
        board = self.board()
        self.assertEqual(
            {"group_id", "revision", "status", "counts", "tasks", "has_in_progress"},
            set(board))
        self.assertEqual(
            {"total", "completed", "in_progress", "pending", "blocked"},
            set(board["counts"]))


class UnknownTaskIdTests(_BoardTestCase):
    """野 task_id（模型自己编的）必须变成可读反馈，而不是工具异常。"""

    def test_claim_unknown_id_returns_readable_error(self):
        self.assertEqual(self.tm.run_claim_task("t_does_not_exist"),
                         "Task t_does_not_exist not found")

    def test_complete_unknown_id_returns_readable_error(self):
        self.assertEqual(self.tm.run_complete_task("t_does_not_exist"),
                         "Task t_does_not_exist not found")

    def test_get_unknown_id_returns_readable_error(self):
        self.assertTrue(self.tm.run_get_task("t_does_not_exist").startswith("Error:"))

    def test_claim_blocked_reports_missing_dependency(self):
        """悬空依赖的阻塞反馈必须点名"悬空"+给出口（否则模型会另建修正版）。"""
        t = self.tm._create_task("残留")
        self.patch_raw(t.id, blockedBy=["t_ghost"])
        out = self.tm.run_claim_task(t.id)
        self.assertTrue(out.startswith("Blocked by: ['t_ghost']"), out)
        self.assertIn("悬空引用", out)
        self.assertIn("update_task", out)


class ConcurrencyTests(_BoardTestCase):
    """单文件承载整会话后最容易被写坏的一条：并发读-改-写必须不丢更新。"""

    def test_parallel_updates_on_distinct_tasks_do_not_lose_writes(self):
        import threading

        other = TaskManager(self.dir)          # 同一目录的第二个实例（模拟主/后台两条路径）
        other.set_scope(SCOPE)
        tasks = [self.mk(f"任务{i}") for i in range(20)]
        errors = []

        def work(t):
            try:
                other._claim_task(t.id)
                other._complete_task(t.id, "并发完成")
            except Exception as e:              # noqa: BLE001 - 记录后由断言统一暴露
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=work, args=(t,)) for t in tasks]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [])
        loaded = load_scope_tasks(SCOPE, self.dir)
        self.assertEqual(len(loaded), 20, "并发写丢了任务条目")
        self.assertTrue(all(t.status == "completed" for t in loaded))
        self.assertEqual(len([p for p in self.dir.glob("*.json")]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
