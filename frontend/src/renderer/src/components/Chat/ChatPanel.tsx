import { useEffect, useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import MessageList from './MessageList'
import InputBox from './InputBox'
import TaskBoard from './TaskBoard'

/** 中央聊天面板：空态品牌 / 消息流 + 输入区 */
export default function ChatPanel(): JSX.Element {
  const messages = useAgentStore((s) => s.messages)
  const send = useAgentStore((s) => s.send)
  const draftAttachments = useAgentStore((s) => s.draftAttachments)
  const stageAttachments = useAgentStore((s) => s.stageAttachments)
  const removeDraftAttachment = useAgentStore((s) => s.removeDraftAttachment)
  const [draft, setDraft] = useState('')

  const doSend = (): void => {
    const ready = draftAttachments.filter((a) => a.status === 'ready')
    // 正文与附件都为空的发送没有意义；**只有附件不打字也必须能发**
    if (!draft.trim() && ready.length === 0) return
    send(draft, ready)
    setDraft('')
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
    <main className="chat">
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

      <div className="composer-wrap">
        <InputBox
          value={draft}
          onChange={setDraft}
          onSend={doSend}
          attachments={draftAttachments}
          onStagePaths={(paths) => void stageAttachments(paths)}
          onRemoveAttachment={removeDraftAttachment}
          onOpenAttachment={(p) => void window.agent.openInFinder(p)}
        />
      </div>
    </main>
  )
}
