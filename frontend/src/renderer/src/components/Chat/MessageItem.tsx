import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Icon } from '@components/common/Icon'
import type { Message, SubAgentMsg, ToolCallMsg } from '@store/agentStore'
import MessageMenu from './MessageMenu'

function ToolCallBar({ tool }: { tool: ToolCallMsg }): JSX.Element {
  return (
    <div className={`toolbar-call ${tool.status}`}>
      <span className="toolbar-icon">
        <Icon name="terminal" size={13} />
      </span>
      <span className="toolbar-name">{tool.name || '(工具)'}</span>
      <span className="toolbar-args">({tool.args.slice(0, 120)}{tool.args.length > 120 ? '…' : ''})</span>
      {tool.status === 'running' ? (
        <span className="toolbar-status spinner" />
      ) : (
        <span className="toolbar-status done">
          <Icon name="check" size={12} />
        </span>
      )}
    </div>
  )
}

/** 思考过程折叠块：思考中（active=true）自动展开，用固定高度框实时展示流式思考输出；
 * 思考结束后自动折叠回标题行；点击标题可重新展开为同款固定高度可滚动框。 */
function ThinkingBox({ text, open, active }: { text: string; open: boolean; active?: boolean }): JSX.Element {
  const [expanded, setExpanded] = useState(open)
  const contentRef = useRef<HTMLDivElement>(null)
  // 流式输出期间固定高度框自动滚到底，保证最新思考内容可见
  useEffect(() => {
    if (active && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight
    }
  }, [text, active])
  const showContent = active || expanded
  if (!text) return <></>
  return (
    <div className="thinking-box">
      <button className="thinking-toggle" onClick={() => setExpanded((v) => !v)}>
        <Icon name="brain" size={13} />
        <span>深度思考</span>
        {active && <span className="toolbar-status spinner" />}
        <Icon name="chevronRight" size={12} className={active || expanded ? 'rot' : ''} />
      </button>
      {showContent && (
        <div className="thinking-content" ref={contentRef}>
          {text}
        </div>
      )}
    </div>
  )
}

/** 子智能体执行块：机器人头图标标识，思考过程与工具执行折叠在块下（可展开/收起）。
 * 状态：执行中（转圈）/ 已完成（对勾 + 耗时）/ 失败（红叉 + 原因）/ 已中断
 *（进程被强杀，仅剩启动占位记录）。子智能体返回给主智能体的正文不在此展示
 * ——那是给主智能体的结果，不是给用户的。 */
function SubAgentBlock({ block }: { block: SubAgentMsg }): JSX.Element {
  const [expanded, setExpanded] = useState(true)
  const state: 'running' | 'done' | 'error' | 'aborted' = block.error
    ? 'error'
    : block.status === 'aborted'
      ? 'aborted'
      : block.streaming || block.status === 'running'
        ? 'running'
        : block.status ?? 'done'
  const seconds =
    typeof block.durationMs === 'number' ? Math.max(1, Math.round(block.durationMs / 1000)) : null
  return (
    <div className={`subagent-box ${state}`}>
      <button className="subagent-header" onClick={() => setExpanded((v) => !v)}>
        <span className="subagent-icon">
          <Icon name="bot" size={14} />
        </span>
        <span className="subagent-name">{block.name || '子智能体'}</span>
        {state === 'running' && <span className="subagent-badge running">执行中</span>}
        {state === 'error' && <span className="subagent-badge error">失败</span>}
        {state === 'aborted' && <span className="subagent-badge aborted">已中断</span>}
        {seconds !== null && state !== 'running' && (
          <span className="subagent-duration">{seconds}s</span>
        )}
        <span className="subagent-status">
          {state === 'running' ? (
            <span className="toolbar-status spinner" />
          ) : state === 'error' ? (
            <Icon name="close" size={12} />
          ) : state === 'aborted' ? (
            <Icon name="stop" size={12} />
          ) : (
            <Icon name="check" size={12} />
          )}
        </span>
        <Icon name="chevronRight" size={12} className={expanded ? 'rot' : ''} />
      </button>
      {expanded && (
        <div className="subagent-content">
          {block.error && <div className="subagent-error">{block.error}</div>}
          <ThinkingBox text={block.thinking} open={false} active={block.thinkingActive} />
          {block.toolCalls.map((t) => (
            <ToolCallBar key={t.id} tool={t} />
          ))}
          {state === 'running' && !block.thinking && block.toolCalls.length === 0 && (
            <div className="subagent-empty">子智能体正在准备…</div>
          )}
        </div>
      )}
    </div>
  )
}

export default function MessageItem({ msg }: { msg: Message }): JSX.Element {
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null)

  /** 右键打开消息菜单 */
  const openMenu = (e: ReactMouseEvent): void => {
    e.preventDefault()
    setMenu({ x: e.clientX, y: e.clientY })
  }

  /** 复制消息正文（原始纯文本） */
  const copyContent = (): void => {
    void navigator.clipboard.writeText(msg.content || '')
  }

  if (msg.role === 'user') {
    return (
      <>
        <div className="msg-row user" onContextMenu={openMenu}>
          <div className="msg-bubble user">{msg.content}</div>
        </div>
        {menu && <MessageMenu x={menu.x} y={menu.y} onCopy={copyContent} onClose={() => setMenu(null)} />}
      </>
    )
  }

  const usage = msg.usage && msg.usage.total_tokens ? (
    <span className="msg-usage">
      {msg.usage.prompt_tokens}→{msg.usage.completion_tokens} tokens
    </span>
  ) : null

  return (
    <>
      <div className="msg-row assistant" onContextMenu={openMenu}>
        <div className="assistant-body">
          <ThinkingBox text={msg.thinking} open={false} active={msg.thinkingActive} />
          {msg.toolCalls.map((t) => (
            <ToolCallBar key={t.id} tool={t} />
          ))}
          {msg.subagents.map((s) => (
            <SubAgentBlock key={s.id} block={s} />
          ))}
          <div className="markdown-body">
            {msg.content ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
            ) : (
              msg.streaming && <span className="cursor" />
            )}
            {msg.streaming && msg.content && <span className="cursor" />}
          </div>
          {!msg.streaming && usage}
        </div>
      </div>
      {menu && <MessageMenu x={menu.x} y={menu.y} onCopy={copyContent} onClose={() => setMenu(null)} />}
    </>
  )
}