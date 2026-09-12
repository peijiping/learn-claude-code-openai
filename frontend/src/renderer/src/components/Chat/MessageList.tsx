import { useEffect, useRef } from 'react'
import { useAgentStore } from '@store/agentStore'
import MessageItem from './MessageItem'

/** 距容器底部多少像素内视为「贴底」（用户未主动上翻） */
const STICKY_THRESHOLD = 48

export default function MessageList(): JSX.Element {
  const messages = useAgentStore((s) => s.messages)
  const activeSession = useAgentStore((s) => s.activeSession)
  const containerRef = useRef<HTMLDivElement>(null)
  const stickyRef = useRef(true)
  const prevSessionRef = useRef(activeSession)

  const handleScroll = (): void => {
    const el = containerRef.current
    if (!el) return
    stickyRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICKY_THRESHOLD
  }

  useEffect(() => {
    // 切换会话（含首次加载）时重置为贴底跟随
    if (prevSessionRef.current !== activeSession) {
      prevSessionRef.current = activeSession
      stickyRef.current = true
    }
    const el = containerRef.current
    if (el && stickyRef.current) {
      // 直接赋值滚动位置：即时吸底，避免 smooth 动画被流式输出高频触发而上下跳动
      el.scrollTop = el.scrollHeight
    }
  }, [messages, activeSession])

  return (
    <div className="msglist" ref={containerRef} onScroll={handleScroll}>
      {messages.map((m) => (
        <MessageItem key={m.id} msg={m} />
      ))}
    </div>
  )
}