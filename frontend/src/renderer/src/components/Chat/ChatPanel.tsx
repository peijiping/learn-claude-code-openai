import { useEffect, useState } from 'react'
import { hasSendableContent, isSendableAttachment, useAgentStore } from '@store/agentStore'
import MessageList from './MessageList'
import InputBox from './InputBox'
import TaskBoard from './TaskBoard'
import GoalBar from './GoalBar'
import AskUserPanel from './AskUserPanel'
import PlanActionBar from './PlanActionBar'
import type { EditorSnapshot } from './editor/serializeDoc'

/** 空草稿（模块级常量：身份稳定，避免每次渲染都造新对象） */
const EMPTY_DRAFT: EditorSnapshot = { text: '', refs: [] }

/** 中央聊天面板：空态品牌 / 消息流 + 输入区 */
export default function ChatPanel(): JSX.Element {
  const messages = useAgentStore((s) => s.messages)
  const send = useAgentStore((s) => s.send)
  const draftAttachments = useAgentStore((s) => s.draftAttachments)
  const stageAttachments = useAgentStore((s) => s.stageAttachments)
  const removeDraftAttachment = useAgentStore((s) => s.removeDraftAttachment)
  // 正文 + 引用的快照。**编辑器内容不受这一份 state 控制** —— 它只用于发送判据
  // 与"发完清空"，回灌进编辑器会冲掉光标 / 打断拼音输入（见 InputBox 的 props 注释）。
  const [draft, setDraft] = useState<EditorSnapshot>(EMPTY_DRAFT)
  // 自增即"清空输入框"信号（清空走编辑器命令，不做受控同步）
  const [clearSignal, setClearSignal] = useState(0)
  /** 本会话是否有**在途提问**（ask_user 面板正在等作答）。
   *
   *  此时输入区整块让位：面板与输入框并存会同时给出两条作答路径 ——
   *  用户可能在输入框里把答案打一半、又去点选项，两边都像"已经答了"。
   *  判据与 AskUserPanel 的渲染条件同源（同一个 `interactionBySession` 条目），
   *  不做第二份推断：面板渲染出来的那一刻，输入框就收起。 */
  const askOpen = useAgentStore((s) => {
    if (!s.activeSession) return false
    const it = s.interactionBySession[s.activeSession]
    return !!it && it.questions.length > 0
  })
  /** 本会话是否有**在途审批**（PreToolUse 判定 ask，2026-09-22 权限管控）。
   *  与 askOpen 不同：审批**不让位输入区**（`.chat--asking` 只属于 ask 面板），
   *  只禁用发送按钮 —— 审批挂起时停止按钮必须可用（用户可能想直接停掉本轮），
   *  输入框也保持可打字（草稿不丢）。 */
  const approvalPending = useAgentStore((s) =>
    s.activeSession ? Object.keys(s.approvalBySession[s.activeSession] ?? {}).length > 0 : false
  )

  // ── 计划操作栏（2026-09-29 二次改版，docs/frontend/22 §7.3.1）──────────
  // 待批准（`ready`）期间，**操作选择框顶替输入框的位置**（`.chat--plan`）：
  // 用户拍板的形态是"操作与自由输入互斥"—— 两个入口并存会让人以为有两件事要做。
  // 已批准（`approved`）不占位：计划模式正在退出、改代码才是正题，输入框原样回来。
  const plan = useAgentStore((s) => (s.activeSession ? s.planBySession[s.activeSession] : undefined))
  const activeSession = useAgentStore((s) => s.activeSession)
  const approvePlan = useAgentStore((s) => s.approvePlan)
  const planPending = plan?.status === 'ready'
  /** 「确定」（四个选项任一）/「暂不执行先看方案」→ 操作栏收起、输入框回来
   *  （计划模式不受影响；动作照常往下执行）。
   *
   *  为什么这个状态必须住在 `ChatPanel` 而不是操作栏内部：收起后操作栏**要卸载**
   *  （输入框得让回来），组件内的 state 会随之消失。重新展开的入口是计划卡片的
   *  「选择操作」（`onPlanChoose`），它通过 `MessageList` 透传到卡片。
   *
   *  **复位判据是整条 `plan` 对象的身份**，不是 `path|status`：
   *  重新产出的一版是**同 path、同 status**（后端单份覆盖），用那两个字段判不出
   *  "新一版来了" —— 那样用户每轮修改都得去卡片上手动把操作栏请回来。
   *  身份变化只发生在 store **替换**该会话的桶时（`plan_ready` / 模式广播 /
   *  `plan_read` 回填 / 回放），而这些上游都有"无变化则返回原引用"的守卫，
   *  不会因为一次无关重渲染就把收起态吃掉。切会话即复位（同一条链）。 */
  const [planBarOff, setPlanBarOff] = useState(false)
  useEffect(() => {
    setPlanBarOff(false)
  }, [activeSession, plan])
  /** 本会话是否正在跑一轮（`running` / `background` 都算，判据见 store 的 `session_status`）。
   *
   *  ⚠️ **它必须参与 `planBarShown`**（2026-09-29 用户实测报的问题）：用户在本栏里选
   *  「继续修改」→ 提交 → 模型立刻起新一轮（`plan_ready` 也就在**这一轮之内**，写文件
   *  只是本轮的一个工具调用，模型随后还要输出收尾文本）—— 那段时间里若操作栏又冒出来，
   *  用户会在"智能体正按我的指令重做计划"的同时点下「开始执行」。后果不是"没反应"：
   *  后端 `rt.busy` 时 `approve_plan` **照样落盘 approved**、只是不起续跑（`ws_bridge`
   *  回了句 toast），于是壳被撤掉、用户以为已在执行，实际要再发一条消息 —— 两边状态都乱。
   *
   *  所以：**执行期一律不占输入区**（本栏连同卡片上的「选择操作」一起失效），等这一轮
   *  落地（`isSending` 回落）再弹。而"要不要弹"（`planBarOff` 收起态）仍由**计划对象
   *  身份**复位 —— 新一轮 `plan_ready` 已把 `planBarOff` 清掉，所以本轮一结束操作栏
   *  自然回来，用户不必再去卡片上手动请。 */
  const isSending = useAgentStore((s) => s.isSending)
  const planBarShown = planPending && !planBarOff && !isSending

  const doSend = (): void => {
    // 在途提问期间输入区已被隐藏（见 askOpen）—— 这里再兜一道：
    // 万一有残留焦点 / 快捷键把发送打进来，也绝不与作答面板抢答。
    if (askOpen) return
    // 操作栏占位期间输入框同样被隐藏（`.chat--plan`）—— 同款兜底。
    if (planBarShown) return
    // 在途审批期间发送按钮已禁用（InputBox.canSend）—— 这里同样兜一道
    //（Enter 键路径 / 状态竞争窗口），审批未结算前不放进新消息。
    if (approvalPending) return
    // 用共享判据而不是 `status === 'ready'`：degraded（分析不完整但可用）也必须随
    // payload 发出，否则扫描件会被静默丢掉（见 isSendableAttachment 的说明）。
    const ready = draftAttachments.filter(isSendableAttachment)
    // 正文 / 就绪附件 / 引用 **三者任一非空**即可发送；全空才拦下。
    // 判据只有一处（store 的 hasSendableContent），别在这里另写一份。
    if (!hasSendableContent(draft.text, ready, draft.refs)) return
    send(draft.text, ready, draft.refs)
    setDraft(EMPTY_DRAFT)
    setClearSignal((n) => n + 1)
  }

  /** 计划操作栏「继续修改 / 其他」：把用户写的文字**直接发出去**（`send(text, [], [])`）。
   *
   *  文字是在操作栏里主动写下的（点选项才出现输入框，还要再点「确定」），不存在
   *  "半截提示被当成用户意图"的风险。计划模式**保持不变**（不发 `session_exec_mode`），
   *  模型据此重新产出文书（覆盖同一份）。 */
  const handlePlanSendText = (text: string): void => {
    const t = text.trim()
    if (!t) return
    send(t, [], [])
  }

  // 全局兜底：任何落在 composer 之外的 dragover/drop 都必须 preventDefault。
  // 否则 Electron 会把它当成"导航到这个文件"→ 整个窗口被替换成文件内容（白屏事故）。
  // 这里只拦不处理：真正的投放逻辑在 InputBox 的 composer 上。
  useEffect(() => {
    const swallow = (e: DragEvent): void => e.preventDefault()
    window.addEventListener('dragover', swallow)
    window.addEventListener('drop', swallow)
    return () => {
      window.removeEventListener('dragover', swallow)
      window.removeEventListener('drop', swallow)
    }
  }, [])

  return (
    // `chat--asking` = 输入区**整块**让位给作答面板（连 `.composer-wrap` 一起隐藏，
    //   面板自身承担底部留白）；`chat--plan` = 只让位**输入框本身**（`.composer`），
    //   同一个 `.composer-wrap` 里换上计划操作栏 —— 位置不变、左右留白不变。
    //   两条让位互不冲突：ask 面板出现时整块让位优先，它结束时操作栏自然回来。
    <main className={`chat${askOpen ? ' chat--asking' : ''}${planBarShown ? ' chat--plan' : ''}`}>
      {messages.length === 0 ? (
        <div className="empty-state">
          <div className="brand-logo">&lt;/&gt;</div>
          <div className="brand-text">Anything for You</div>
        </div>
      ) : (
        <MessageList onPlanChoose={planBarShown ? undefined : () => setPlanBarOff(false)} />
      )}

      {/* 任务面板：固定在输入框上方（有未完成任务组时才渲染） */}
      <TaskBoard />

      {/* 常驻目标条（2026-09-30 目标可见化）：目标全程激活期间固定在输入区上方，
          一眼可见"还开着目标模式、目标是啥、跑到第几轮"。放在这里而不是
          `.composer-wrap` 内 —— 它不占输入框的位置，因此与 ask 整块让位 /
          计划操作栏让位互不干扰。无目标时组件自身 `return null`（零占位）。 */}
      <GoalBar />

      {/* 结构化提问作答面板（ask_user）：同样固定在输入框上方，紧贴输入框 ——
          它是"必须现在做决定"的交互，位置越靠近手边越好。有在途提问时才渲染。
          它出现时输入区**整块隐藏**（`chat--asking`），面板因此落在最底部 */}
      <AskUserPanel />

      <div className="composer-wrap">
        {/* 计划操作栏：占的是输入框的位置（同一父容器、同一套左右留白）——
            "选择框替换输入框"因此是布局事实，不是两处各画一遍的巧合。 */}
        {planBarShown && plan && (
          <PlanActionBar
            plan={plan}
            onApprove={approvePlan}
            onSendText={handlePlanSendText}
            onDismiss={() => setPlanBarOff(true)}
          />
        )}
        <InputBox
          value={draft}
          onChange={setDraft}
          onSend={doSend}
          clearSignal={clearSignal}
          // 让位期间只做一件事：把焦点收回（否则盲打进看不见的编辑器）
          suspended={askOpen || planBarShown}
          attachments={draftAttachments}
          onStagePaths={(paths) => void stageAttachments(paths)}
          onRemoveAttachment={removeDraftAttachment}
          onOpenAttachment={(p) => void window.agent.openInFinder(p)}
        />
      </div>
    </main>
  )
}
