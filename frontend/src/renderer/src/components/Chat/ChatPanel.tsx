import { useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import MessageList from './MessageList'
import InputBox from './InputBox'
import TaskBoard from './TaskBoard'

/** 中央聊天面板：空态品牌 / 消息流 + 输入区 */
export default function ChatPanel(): JSX.Element {
  const messages = useAgentStore((s) => s.messages)
  const send = useAgentStore((s) => s.send)
  const [draft, setDraft] = useState('')

  const doSend = (): void => {
    if (!draft.trim()) return
    send(draft)
    setDraft('')
  }

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
        <InputBox value={draft} onChange={setDraft} onSend={doSend} />
      </div>
    </main>
  )
}