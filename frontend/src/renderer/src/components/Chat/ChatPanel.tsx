import { useEffect, useState } from 'react'
import { hasSendableContent, isSendableAttachment, useAgentStore } from '@store/agentStore'
import MessageList from './MessageList'
import InputBox from './InputBox'
import TaskBoard from './TaskBoard'
import AskUserPanel from './AskUserPanel'
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

  // ── 右栏入口**不在本组件**（2026-09-23 二次调整）──────────────────
  // 开关图标已上移到窗口标题栏（`components/TitleBar/TitleBar.tsx`，与窗口标题
  // 「个人AI助手」同层），`⌘⇧E`/`⌘⇧G` 视图快捷键在 `RightPanel` 里（与 Esc 同属
  // "右栏的键盘面"）。聊天区顶栏（`.chat-toolbar`）随之删除 —— 它当初就是为承载
  // 这两枚按钮而存在的，按钮走了就只剩一条 40px 空栏（还会白占一条分隔线）。

  const doSend = (): void => {
    // 在途提问期间输入区已被隐藏（见 askOpen）—— 这里再兜一道：
    // 万一有残留焦点 / 快捷键把发送打进来，也绝不与作答面板抢答。
    if (askOpen) return
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
    // `chat--asking` = 输入区让位给作答面板（隐藏 composer，面板自身承担底部留白）
    <main className={`chat${askOpen ? ' chat--asking' : ''}`}>
      {messages.length === 0 ? (
        <div className="empty-state">
          <div className="brand-logo">&lt;/&gt;</div>
          <div className="brand-text">Anything for You</div>
        </div>
      ) : (
        <MessageList />
      )}

      {/* 任务面板：固定在输入框上方（有未完成任务组时才渲染） */}
      <TaskBoard />

      {/* 结构化提问作答面板（ask_user）：同样固定在输入框上方，紧贴输入框 ——
          它是"必须现在做决定"的交互，位置越靠近手边越好。有在途提问时才渲染。
          它出现时输入区**整块隐藏**（`chat--asking`），面板因此落在最底部 */}
      <AskUserPanel />

      <div className="composer-wrap">
        <InputBox
          value={draft}
          onChange={setDraft}
          onSend={doSend}
          clearSignal={clearSignal}
          suspended={askOpen}
          attachments={draftAttachments}
          onStagePaths={(paths) => void stageAttachments(paths)}
          onRemoveAttachment={removeDraftAttachment}
          onOpenAttachment={(p) => void window.agent.openInFinder(p)}
        />
      </div>
    </main>
  )
}
