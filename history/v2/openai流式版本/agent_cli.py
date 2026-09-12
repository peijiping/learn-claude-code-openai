#!/usr/bin/env python3
"""agent_cli.py - 主智能体命令行交互入口

实例化 Agent 并驱动 REPL：input 循环 + 斜杠命令 + readline 中文输入配置。
启动时初始化 CronScheduler（s14 定时任务调度器），通过大模型对话创建/管理定时任务。

- 新推荐入口：python agent_cli.py
- 向后兼容：python agent_full_v2.py 内部延迟调用本模块的 main()

为 s14 定时任务（每任务独立会话）与未来 TUI 多会话预留的接缝在 Agent 类上：
  agent = Agent()
  agent.init_session(resume=False)   # 新会话
  agent.run_turn("[Scheduled] ...") # 非交互单轮
"""
import os
import re
from pathlib import Path

# 锁定 cwd 到本仓库根目录：保证 paths.py 的 `ROOT_DIR = Path.cwd()` 始终解析到正确位置。
# 必须放在所有 import 之前——agent_full_v2 → paths 的 import 链会在加载期立即执行
# paths.py 顶层的 `ensure_dirs()`，那个时点 cwd 还不对就会在错误位置建出 .memory/ .chathistory/ 等子目录。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if Path.cwd() != _PROJECT_ROOT:
    os.chdir(_PROJECT_ROOT)

from dotenv import load_dotenv
from agent_full_v2 import Agent
from cron_scheduler import CronScheduler
from goal import CLEAR_ALIASES, GoalError
from paths import CHAT_HISTORY_DIR, DURABLE_PATH

# readline 中文输入配置（从原 agent_full_v2.py 顶部迁移过来）
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass


def main() -> None:
    load_dotenv(override=True)

    # ── s14：启动 CronScheduler（定时任务调度器）──
    cron_scheduler = CronScheduler(CHAT_HISTORY_DIR, DURABLE_PATH)
    cron_scheduler.start()

    agent = Agent(cron_scheduler=cron_scheduler)
    agent.init_session()  # resume 最近会话或新建

    while True:
        try:
            label = agent.context_label()
            mode_tag = "|teams" if agent.team_mode else ""
            query = input(f"\033[36m[session_{agent.session_num} ({label}{mode_tag})] >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        cmd = query.strip().lower()
        if cmd == "/help":
            help_lines = [
                ("/help", "显示本帮助信息"),
                ("/q 或 /exit", "退出程序"),
                ("/newsession", "新建一个会话"),
                ("/switchsession N", "切换到第 N 个会话"),
                ("/clearsession", "清空当前会话的全部历史消息"),
                ("/tasks", "查看当前任务列表"),
                ("/compact", "压缩当前会话上下文"),
                ("/skills", "查看当前可用的技能"),
                ("/teams", "进入团队模式（s17 队友协作）"),
                ("/subagent", "退出团队模式，回到默认子智能体模式"),
                ("/goal [condition|clear]", "查看/设置/清除会话级目标（s17 目标循环）"),
            ]
            print("可用命令：")
            for name, desc in help_lines:
                print(f"  {name:<18} {desc}")
            continue
        if cmd in ("/q", "/exit", ""):
            break
        if cmd == "/newsession":
            num, _ = agent.new_session()
            print(f"\033[33m已创建新会话: session_{num}.jsonl\033[0m")
            continue
        if cmd.startswith("/switchsession "):
            try:
                target_num = int(cmd.split()[1])
                num, msg_count = agent.switch_session(target_num)
                print(f"\033[33m已切换到会话: session_{num}.jsonl ({msg_count} 条消息)\033[0m")
            except (ValueError, IndexError):
                print("\033[31m用法: /switchsession <数字>\033[0m")
            except FileNotFoundError as e:
                print(f"\033[31m{e}\033[0m")
            continue
        if cmd == "/clearsession":
            deleted = agent.clear_session()
            print(f"\033[33m已清空当前会话，删除了 {deleted} 条历史消息\033[0m")
            continue
        if cmd == "/tasks":
            print(agent.show_tasks())
            continue
        if cmd == "/compact":
            agent.compact()
            continue
        if cmd == "/skills":
            print(f"当前可用技能:\n{agent.show_skills()}")
            continue
        # /goal：目标循环（s17）的 REPL 入口，三种用法：
        #   /goal                → 打印当前目标状态（时长/评估次数/花费/最近判定）
        #   /goal clear|stop|... → 清除目标（CLEAR_ALIASES 同义别名，与教程一致）
        #   /goal <condition>    → 设置目标并立即以目标文字跑一轮（教程行为：
        #                          目标作为首条用户消息喂给 Worker，马上开始
        #                          朝目标干活；之后的回环由 agent_loop 的
        #                          停止边界自动驱动，无需用户反复输入）
        # 仅 set_goal 阶段捕获 GoalError（空目标/超长目标的用法错误）；
        # run_turn 内部的错误由 agent_loop 的 recovery / goal error 态处理。
        if cmd == "/goal" or cmd.startswith("/goal "):
            argument = query.strip()[5:].strip()
            if not argument:
                print(agent.goal_status())
                continue
            if argument.lower() in CLEAR_ALIASES:
                print(f"\033[33m{agent.clear_goal()}\033[0m")
                continue
            try:
                print(f"\033[33m{agent.set_goal(argument)}\033[0m")
            except GoalError as e:
                print(f"\033[31m{e}\033[0m")
                continue
            reply = agent.run_turn(argument)
            print(reply)
            print()
            continue
        # /teams 与 /subagent：只要在输入中被空格独立隔开（前后为空白或行界）即生效，
        # 无需单独成行。如 `帮我 /teams 一下`、`请 /subagent 吧` 均会触发。
        teams_m = re.search(r"(?<!\S)/teams(?!\S)", query)
        subagent_m = re.search(r"(?<!\S)/subagent(?!\S)", query)
        if subagent_m:
            # 退出团队模式（粘性），回到默认子智能体分发模式
            agent.team_mode = False
            print("\033[33m已退出团队模式，回到默认子智能体模式。\033[0m")
            continue
        if teams_m:
            # 进入团队模式（粘性）：把 6 个团队工具暴露给 LLM。
            # 若匹配位置之后还带了文本（如 `请 /teams 让队友 A 负责 X`），
            # 用该文本作为本轮输入立刻执行；否则只切换模式等待下一条。
            agent.team_mode = True
            rest = query[teams_m.end():].strip()
            if rest:
                reply = agent.run_turn(rest)
                print(reply)
                print()
            else:
                print(
                    "\033[33m已进入团队模式（可用 spawn_teammate / send_message / check_inbox /\n"
                    "  request_shutdown / request_plan / review_plan 编排队友）。\n"
                    "输入 /subagent 可退出团队模式，回到默认子智能体模式。\033[0m"
                )
            continue

        # 普通用户输入 → 跑一轮
        reply = agent.run_turn(query)
        print(reply)
        print()


if __name__ == "__main__":
    main()
