import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Icon } from '@components/common/Icon'
import type { Message, SubAgentMsg, ToolCallMsg } from '@store/agentStore'

function ToolCallBar({ tool }: { tool: ToolCallMsg }): JSX.Element {
  return (
    <div className={`toolbar-call ${tool.status}`}>
      <span className="toolbar-icon">
        <Icon name="gear" size={13} />
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

function ThinkingBox({ text, open }: { text: string; open: boolean }): JSX.Element {
  const [expanded, setExpanded] = useState(open)
  if (!text) return <></>
  return (
    <div className="thinking-box">
      <button className="thinking-toggle" onClick={() => setExpanded((v) => !v)}>
        <Icon name="chevronRight" size={12} className={expanded ? 'rot' : ''} />
        <Icon name="brain" size={13} />
        <span>思考过程</span>
      </button>
      {expanded && <details open className="thinking-content">{text}</details>}
    </div>
  )
}

/** 子智能体执行块：机器人头图标标识，思考过程与工具执行折叠在块下（可展开/收起） */
function SubAgentBlock({ block }: { block: SubAgentMsg }): JSX.Element {
  const [expanded, setExpanded] = useState(true)
  return (
    <div className="subagent-box">
      <button className="subagent-header" onClick={() => setExpanded((v) => !v)}>
        <span className="subagent-icon">
          <Icon name="bot" size={14} />
        </span>
        <span className="subagent-name">{block.name || '子智能体'}</span>
        {block.streaming ? (
          <span className="toolbar-status spinner" />
        ) : (
          <span className="toolbar-status done">
            <Icon name="check" size={12} />
          </span>
        )}
        <Icon name="chevronRight" size={12} className={expanded ? 'rot' : ''} />
      </button>
      {expanded && (
        <div className="subagent-content">
          <ThinkingBox text={block.thinking} open={false} />
          {block.toolCalls.map((t) => (
            <ToolCallBar key={t.id} tool={t} />
          ))}
        </div>
      )}
    </div>
  )
}

export default function MessageItem({ msg }: { msg: Message }): JSX.Element {
  if (msg.role === 'user') {
    return (
      <div className="msg-row user">
        <div className="msg-bubble user">{msg.content}</div>
      </div>
    )
  }

  const usage = msg.usage && msg.usage.total_tokens ? (
    <span className="msg-usage">
      {msg.usage.prompt_tokens}→{msg.usage.completion_tokens} tokens
    </span>
  ) : null

  return (
    <div className="msg-row assistant">
      <div className="assistant-avatar">
        <Icon name="terminal" size={15} />
      </div>
      <div className="assistant-body">
        <ThinkingBox text={msg.thinking} open={false} />
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
  )
}