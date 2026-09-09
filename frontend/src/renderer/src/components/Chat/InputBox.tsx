import { useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '@components/common/Icon'
import { useAgentStore, resolveModelMeta } from '@store/agentStore'

interface InputBoxProps {
  value: string
  onChange: (v: string) => void
  onSend: () => void
}

// 思考强度档位展示映射（与后端 PROVIDERS thinking_strengths 对齐）
const THINKING_LABELS: Record<string, string> = {
  low: '轻',
  high: '高',
  very_high: '极高'
}

/** 中央核心输入区：textarea + 工具栏 + 上下文条 */
export default function InputBox({ value, onChange, onSend }: InputBoxProps): JSX.Element {
  const isSending = useAgentStore((s) => s.isSending)
  const stop = useAgentStore((s) => s.stop)
  const llmConfig = useAgentStore((s) => s.llmConfig)
  const llmSaving = useAgentStore((s) => s.llmSaving)
  const setSessionModel = useAgentStore((s) => s.setSessionModel)
  const setSessionOverrides = useAgentStore((s) => s.setSessionOverrides)
  const sessionModelId = useAgentStore((s) => s.sessionModelId)
  const currentContextStats = useAgentStore((s) => s.currentContextStats)
  const overridesByModel = useAgentStore((s) => s.overridesByModel)
  const activeSession = useAgentStore((s) => s.activeSession)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const [modelOpen, setModelOpen] = useState(false)
  const [hoveredPanel, setHoveredPanel] = useState<{ id: string; x: number; y: number } | null>(null)
  const [ctxTooltip, setCtxTooltip] = useState(false)
  // 面板以 Portal 渲染在 body 顶层，离开菜单项会先触发 onMouseLeave，
  // 用短延迟给鼠标留出"跨过间隙进入面板"的时间，避免面板闪断。
  const hideTimer = useRef<number | null>(null)
  const clearHide = (): void => {
    if (hideTimer.current !== null) {
      window.clearTimeout(hideTimer.current)
      hideTimer.current = null
    }
  }
  const scheduleHide = (id: string): void => {
    clearHide()
    hideTimer.current = window.setTimeout(() => {
      hideTimer.current = null
      setHoveredPanel((v) => (v?.id === id ? null : v))
    }, 180)
  }
  // 悬浮坐标：优先在菜单项右侧弹出；右/下空间不足时智能翻转，保证面板完整显示在窗口内
  const resolvePanelPos = (r: DOMRect): { x: number; y: number } => {
    const W = 240
    const H = 110
    const margin = 8
    let x = r.right + margin
    if (x + W > window.innerWidth) x = Math.max(margin, r.left - margin - W)
    let y = r.top
    if (y + H > window.innerHeight) y = Math.max(margin, window.innerHeight - H - margin)
    return { x, y }
  }

  // 推导当前高亮的模型：会话绑定模型优先，其次全局 active_model_id（与后端 primary 推导一致）
  const enabled = (llmConfig?.models ?? []).filter((m) => m.enabled)
  const active =
    enabled.find((m) => m.id === sessionModelId) ??
    enabled.find((m) => m.id === llmConfig?.active_model_id) ??
    enabled[0] ??
    null

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

  // 上下文圆圈：仅当有选中会话时才显示；数据来自后端 context_stats 事件
  const stats = currentContextStats
  const hasSession = activeSession !== null
  const usedPct = stats ? stats.used_percent : 0
  const indicatorColor = usedPct >= 90 ? '#e5484d' : usedPct >= 70 ? '#f5a623' : '#2ea043'

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
                <div className="model-menu-mask" onClick={() => { setModelOpen(false); setHoveredPanel(null) }} />
                <div className="model-menu">
                  {enabled.map((m) => {
                    const meta = resolveModelMeta(llmConfig, m)
                    return (
                      // 用 div 而非 button：面板内的思考强度 chip / 开关也是 button，
                      // button 嵌套 button 非法会被浏览器修复，导致面板不弹出。
                      <div
                        key={m.id}
                        role="menuitem"
                        className={`model-menu-item ${m.id === active?.id ? 'active' : ''}`}
                        onMouseEnter={(e) => {
                          clearHide()
                          const r = e.currentTarget.getBoundingClientRect()
                          setHoveredPanel({ id: m.id, ...resolvePanelPos(r) })
                        }}
                        onMouseLeave={() => scheduleHide(m.id)}
                        onClick={() => {
                          clearHide()
                          setModelOpen(false)
                          setHoveredPanel(null)
                          if (m.id !== active?.id) setSessionModel(m.id)
                        }}
                      >
                        <span className={`model-dot ${m.provider === 'deepseek' ? 'dp' : 'sf'}`} />
                        <span className="model-menu-name">{m.display_name || m.id}</span>
                        {m.id === active?.id && <Icon name="check" size={13} />}
                      </div>
                    )
                  })}
                </div>
                {/* 使用 Portal 把悬浮配置面板渲染到 body 顶层，脱离 .model-menu 及其祖先的
                    overflow / transform / 裁剪限制，面板可完整显示、必要时浮出菜单甚至窗口边界外的可视区。
                    新会话（activeSession === null，即无会话）依然允许设置参数 → 只要求模型有元数据即可，不依赖有会话。 */}
                {hoveredPanel &&
                  (() => {
                    const m = enabled.find((mm) => mm.id === hoveredPanel.id)
                    if (!m) return null
                    const meta = resolveModelMeta(llmConfig, m)
                    return meta ? (
                      createPortal(
                        // 仅负责定位的包装层；面板视觉样式由内部 .model-hover-panel 提供
                        <div
                          style={{
                            position: 'fixed',
                            left: hoveredPanel.x,
                            top: hoveredPanel.y,
                            zIndex: 9999
                          }}
                          onClick={(e) => e.stopPropagation()}
                          onMouseEnter={clearHide}
                          onMouseLeave={() => scheduleHide(hoveredPanel.id)}
                        >
                          <ModelConfigPanel
                            meta={meta}
                            overrides={overridesByModel[m.id] ?? null}
                            onChange={(ov) => setSessionOverrides(ov, m.id)}
                          />
                        </div>,
                        document.body
                      )
                    ) : null
                  })()}
              </>
            )}
          </div>
          {/* 上下文使用量圆圈指示器：仅选中会话时显示，用自定义悬浮提示替代不可靠的原生 title */}
          {hasSession && stats && (
            <div
              className="context-indicator"
              onMouseEnter={() => setCtxTooltip(true)}
              onMouseLeave={() => setCtxTooltip(false)}
            >
              <svg viewBox="0 0 36 36" className="context-circle">
                <path
                  className="context-bg"
                  d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                  fill="none"
                  strokeWidth="3.2"
                />
                <path
                  className="context-progress"
                  d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                  fill="none"
                  strokeWidth="3.2"
                  stroke={indicatorColor}
                  strokeLinecap="round"
                  strokeDasharray={`${Math.max(0, Math.min(100, usedPct))} ${100 - Math.max(0, Math.min(100, usedPct))}`}
                />
              </svg>
              {ctxTooltip && (
                <div className="context-tooltip">
                  已用 {stats.used_tokens.toLocaleString()} / 总计 {stats.max_label} tokens（{Math.round(usedPct)}%）
                </div>
              )}
            </div>
          )}
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

interface ModelMetaData {
  max_context?: string
  max_context_extended?: string
  thinking_strengths?: string[]
  default_thinking?: string
}

/** 模型悬浮配置面板：思考强度档位选择 + 更大上下文开关（仅作用当前会话下一轮） */
function ModelConfigPanel(props: {
  meta: ModelMetaData
  overrides: { thinkingStrength?: string; maxContextOption?: 'standard' | 'extended' } | null
  onChange: (ov: { thinkingStrength?: string; maxContextOption?: 'standard' | 'extended' } | null) => void
}): JSX.Element {
  const { meta, overrides, onChange } = props
  const strengths = meta.thinking_strengths ?? ['high']
  const defaultStrength = meta.default_thinking ?? 'high'
  const strength = overrides?.thinkingStrength ?? defaultStrength
  const extOption = overrides?.maxContextOption
  const canExtend = !!meta.max_context_extended

  return (
    <div className="model-hover-panel" onClick={(e) => e.stopPropagation()}>
      <div className="mhp-section">
        <span className="mhp-title">思考强度</span>
        <div className="mhp-strengths">
          {strengths.map((s) => (
            <button
              key={s}
              type="button"
              className={`mhp-chip ${s === strength ? 'active' : ''}`}
              onClick={() => onChange({ ...(overrides ?? {}), thinkingStrength: s })}
            >
              {THINKING_LABELS[s] ?? s}
            </button>
          ))}
        </div>
      </div>
      {canExtend && (
        <div className="mhp-section">
          <span className="mhp-title">更大上下文</span>
          <button
            type="button"
            className={`mhp-switch ${extOption === 'extended' ? 'on' : ''}`}
            role="switch"
            aria-checked={extOption === 'extended'}
            onClick={() => onChange({ ...(overrides ?? {}), maxContextOption: extOption === 'extended' ? 'standard' : 'extended' })}
          >
            <span className="mhp-switch-knob" />
          </button>
          <span className="mhp-hint">
            {extOption === 'extended'
              ? `已扩展至 ${meta.max_context_extended}`
              : `标准 ${meta.max_context ?? '默认'}`}
          </span>
        </div>
      )}
    </div>
  )
}