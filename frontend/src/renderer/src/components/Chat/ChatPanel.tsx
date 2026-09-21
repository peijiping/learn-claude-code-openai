import { useEffect, useState } from 'react'
import { hasSendableContent, isSendableAttachment, useAgentStore } from '@store/agentStore'
import MessageList from './MessageList'
import InputBox from './InputBox'
import TaskBoard from './TaskBoard'
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

  const doSend = (): void => {
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
          clearSignal={clearSignal}
          attachments={draftAttachments}
          onStagePaths={(paths) => void stageAttachments(paths)}
          onRemoveAttachment={removeDraftAttachment}
          onOpenAttachment={(p) => void window.agent.openInFinder(p)}
        />
      </div>
    </main>
  )
}
