import { useEffect, useRef } from 'react'
import { useAgentStore } from '@store/agentStore'
import MessageItem from './MessageItem'
import ApprovalCard from './ApprovalCard'

/** 距容器底部多少像素内视为「贴底」（用户未主动上翻） */
const STICKY_THRESHOLD = 48

export default function MessageList(): JSX.Element {
  const messages = useAgentStore((s) => s.messages)
  const activeSession = useAgentStore((s) => s.activeSession)
  /** 未被任何工具条配对的在途审批卡片（2026-09-22 权限管控）：正常情况审批卡
   *  由 MessageItem 按 toolCallId 锚定到触发的工具条下；这里只兜底
   *  "锚点丢失"（流式时序错位 / 工具行尚未建出）—— 审批是必须现在做决定的
   *  交互，宁可位置不对也不能不显示。 */
  const orphanApprovals = useAgentStore((s) => {
    const sid = s.activeSession
    if (!sid) return []
    const table = s.approvalBySession[sid] ?? {}
    return Object.values(table).filter(
      (a) =>
        !a.toolCallId ||
        !s.messages.some(
          (m) =>
            m.toolCalls.some((t) => t.id === a.toolCallId) ||
            m.subagents.some((x) => x.toolCalls.some((t) => t.id === a.toolCallId))
        )
    )
  })
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
  }, [messages, activeSession, orphanApprovals])

  return (
    // 两层结构：外层 .msgscroll 是**占满对话区全宽**的滚动视口（滚轮落在内容列两侧的
    // 空白处同样能滚动），内层 .msglist 只负责 940px 上限 + 居中 + 内边距。
    // 滚动位置 / 贴底判定一律以 .msgscroll 为准（containerRef 挂在外层）。
    <div className="msgscroll" ref={containerRef} onScroll={handleScroll}>
      <div className="msglist">
        {messages.map((m) => (
          <MessageItem key={m.id} msg={m} />
        ))}
        {/* 在途审批兜底卡片：贴在消息流末尾（见 orphanApprovals 注释） */}
        {orphanApprovals.map((a) => (
          <ApprovalCard key={a.requestId} approval={a} />
        ))}
      </div>
    </div>
  )
}