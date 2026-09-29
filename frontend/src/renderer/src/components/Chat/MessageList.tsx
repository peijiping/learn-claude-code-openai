import { useEffect, useMemo, useRef } from 'react'
import { useAgentStore } from '@store/agentStore'
import MessageItem from './MessageItem'
import ApprovalCard from './ApprovalCard'
import PlanCard from './PlanCard'

/** 距容器底部多少像素内视为「贴底」（用户未主动上翻） */
const STICKY_THRESHOLD = 48

interface MessageListProps {
  /** 计划卡片「继续修改」的动作（由 `ChatPanel` 承担：聚焦输入框 + 提示）。
   *  刻意**不在这里**直接去 focus DOM —— 输入区归 `InputBox` 管，跨组件摸它的
   *  contenteditable 会把"谁拥有焦点"这件事变成两处。 */
  onRevisePlan: () => void
}

export default function MessageList({ onRevisePlan }: MessageListProps): JSX.Element {
  const messages = useAgentStore((s) => s.messages)
  const activeSession = useAgentStore((s) => s.activeSession)
  /** 在途审批表（稳定引用）：zustand v5 的 useStore 直接跑在
   *  useSyncExternalStore 上，**selector 返回值就是 getSnapshot** —— 每次
   *  调用都新建数组/对象会触发 "getSnapshot should be cached" 无限重渲染
   *  （Maximum update depth exceeded，切会话即崩，2026-09-22 修复）。因此
   *  这里只取 store 里的对象引用（缺条目时 undefined，同样稳定），派生
   *  计算放 useMemo。 */
  const approvalTable = useAgentStore((s) =>
    s.activeSession ? s.approvalBySession[s.activeSession] : undefined
  )
  /** 未被任何工具条配对的在途审批卡片（2026-09-22 权限管控）：正常情况审批卡
   *  由 MessageItem 按 toolCallId 锚定到触发的工具条下；这里只兜底
   *  "锚点丢失"（流式时序错位 / 工具行尚未建出）—— 审批是必须现在做决定的
   *  交互，宁可位置不对也不能不显示。 */
  const orphanApprovals = useMemo(
    () =>
      Object.values(approvalTable ?? {}).filter(
        (a) =>
          !a.toolCallId ||
          !messages.some(
            (m) =>
              m.toolCalls.some((t) => t.id === a.toolCallId) ||
              m.subagents.some((x) => x.toolCalls.some((t) => t.id === a.toolCallId))
          )
      ),
    [approvalTable, messages]
  )
  /** 执行模式相关（2026-09-25，docs/frontend/22）：胶囊 tag 选中的会话若已有计划
   *  卡片状态，就在消息流**末尾**渲染固定块。`undefined` = 本会话无计划（多数情况），
   *  selector 直接取对象引用（缺条目时 undefined，引用稳定）—— 派生计算放 `useMemo`。 */
  const plan = useAgentStore((s) => (s.activeSession ? s.planBySession[s.activeSession] : undefined))
  const approvePlan = useAgentStore((s) => s.approvePlan)
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
  }, [messages, activeSession, orphanApprovals, plan])

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
        {/* 计划卡片：同为**消息流末尾固定块**（docs/frontend/22 §6.3）。
            为什么不做消息级锚定：事件不带 message_id、Message 类型无 plan 字段，
            而 plan_status + plan_path 就足以还原卡片 → 实时与回放天然一致。 */}
        {plan && <PlanCard plan={plan} onApprove={approvePlan} onRevise={onRevisePlan} />}
      </div>
    </div>
  )
}