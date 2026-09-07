import { useRef, useState } from 'react'
import { Icon } from '@components/common/Icon'
import { useAgentStore } from '@store/agentStore'

interface InputBoxProps {
  value: string
  onChange: (v: string) => void
  onSend: () => void
}

/** 中央核心输入区：textarea + 工具栏 + 上下文条 */
export default function InputBox({ value, onChange, onSend }: InputBoxProps): JSX.Element {
  const isSending = useAgentStore((s) => s.isSending)
  const stop = useAgentStore((s) => s.stop)
  const llmConfig = useAgentStore((s) => s.llmConfig)
  const llmSaving = useAgentStore((s) => s.llmSaving)
  const setActiveModel = useAgentStore((s) => s.setActiveModel)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const [modelOpen, setModelOpen] = useState(false)

  // 从 llmconfig 推导当前激活模型（与后端 apply_to_env 的 primary 推导一致）
  const enabled = (llmConfig?.models ?? []).filter((m) => m.enabled)
  const active =
    enabled.find((m) => m.id === llmConfig?.active_model_id) ?? enabled[0] ?? null

  const autoGrow = (el: HTMLTextAreaElement): void => {
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (!isSending) onSend()
    }
  }

  return (
    <div className="composer">
      <textarea
        ref={taRef}
        className="composer-input"
        placeholder="有什么我可以帮你的吗？"
        value={value}
        rows={1}
        onChange={(e) => {
          onChange(e.target.value)
          autoGrow(e.target)
        }}
        onKeyDown={onKeyDown}
      />

      <div className="composer-toolbar">
        <div className="toolbar-left">
          <button className="tool-btn" title="添加内容">
            <Icon name="plus" size={16} />
          </button>
          <button className="tool-btn access">
            完全访问 <Icon name="chevronDown" size={12} />
          </button>
          <button className="tool-btn" title="附件（后续增量）">
            <Icon name="paperclip" size={16} />
          </button>
          <button className="tool-btn" title="图片（后续增量）">
            <Icon name="chart" size={16} />
          </button>
        </div>

        <div className="toolbar-right">
          <div className="model-select">
            <button
              className="tool-btn model"
              title="切换模型"
              disabled={enabled.length === 0 || llmSaving}
              onClick={() => setModelOpen((v) => !v)}
            >
              <span className="model-name">{llmSaving ? '切换中…' : active?.display_name ?? '未配置模型'}</span>
              <Icon name="chevronDown" size={12} />
            </button>
            {modelOpen && enabled.length > 0 && (
              <>
                <div className="model-menu-mask" onClick={() => setModelOpen(false)} />
                <div className="model-menu">
                  {enabled.map((m) => (
                    <button
                      key={m.id}
                      className={`model-menu-item ${m.id === active?.id ? 'active' : ''}`}
                      onClick={() => {
                        setModelOpen(false)
                        if (m.id !== active?.id) void setActiveModel(m.id)
                      }}
                    >
                      <span className={`model-dot ${m.provider === 'deepseek' ? 'dp' : 'sf'}`} />
                      <span className="model-menu-name">{m.display_name || m.id}</span>
                      {m.id === active?.id && <Icon name="check" size={13} />}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
          <button className="tool-btn" title="通知开关（占位）">
            <Icon name="bell" size={16} />
          </button>
          {isSending ? (
            <button className="send-btn stop" onClick={stop} title="停止">
              <Icon name="stop" size={15} />
              <span>停止</span>
            </button>
          ) : (
            <button className="send-btn" onClick={onSend} disabled={!value.trim()} title="发送">
              <Icon name="send" size={15} />
            </button>
          )}
        </div>
      </div>

      <div className="composer-context">
        <span className="ctx-chip">
          <Icon name="terminal" size={13} /> 本地 <Icon name="chevronDown" size={11} />
        </span>
        <span className="ctx-chip">
          <Icon name="folder" size={13} /> learn-claude-code-… <Icon name="chevronDown" size={11} />
        </span>
      </div>
    </div>
  )
}