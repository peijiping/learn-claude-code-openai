import { useEffect, useMemo, useState } from 'react'
import { Icon } from '@components/common/Icon'
import type { LlmAdvanced, LlmCapabilities, LlmConnectionModel, LlmProvider } from '@protocols/agentProtocol'
import {
  CAP_INPUT,
  CAP_OUTPUT,
  CONTEXT_IN_OPTIONS,
  CONTEXT_OUT_OPTIONS,
  DEFAULT_CAPS,
  detectPresetModel,
  formatTokens,
  genId,
  hasAdvancedValue,
  numOk,
  tokensOf
} from './llmShared'

interface AddModelModalProps {
  /** 编辑已有模型时传入（保留其 id / 启用状态） */
  initial?: LlmConnectionModel | null
  /** 所属连接的预置目录条目（用于「自动识别」预填能力与上下文） */
  providerPreset?: LlmProvider | null
  saving?: boolean
  /** 编辑态：该模型是否已是默认模型（active_model_id） */
  isDefault?: boolean
  onCancel: () => void
  onSubmit: (model: LlmConnectionModel) => void
  /** 编辑态提供删除入口 */
  onDelete?: (id: string) => void
  /** 编辑态提供「设为默认模型」入口（写入全局 active_model_id 草稿） */
  onSetDefault?: () => void
}

/** 「添加模型 / 模型参数」弹窗：模型 ID + 参数（上下文 / 输出上限）+ 模型能力 + 来源 + 进阶参数。
 * 对应 Reasonix「添加模型」面板；模型 ID 支持自由输入（厂商更新模型后无需等预置目录）。 */
export default function AddModelModal({
  initial = null,
  providerPreset = null,
  saving = false,
  isDefault = false,
  onCancel,
  onSubmit,
  onDelete,
  onSetDefault
}: AddModelModalProps): JSX.Element {
  const editing = !!initial
  const [modelId, setModelId] = useState(initial?.model ?? '')
  const [displayName, setDisplayName] = useState(initial?.display_name ?? '')
  const [contextIn, setContextIn] = useState(initial?.context_in ?? '')
  const [contextOut, setContextOut] = useState(initial?.context_out ?? '')
  const [caps, setCaps] = useState<LlmCapabilities>(initial?.capabilities ?? { ...DEFAULT_CAPS })
  const [source, setSource] = useState<'auto' | 'manual'>(initial?.capability_source ?? 'auto')
  const [advanced, setAdvanced] = useState<LlmAdvanced>(initial?.advanced ?? {})
  const [advOpen, setAdvOpen] = useState(hasAdvancedValue(initial?.advanced))
  const [confirmDelete, setConfirmDelete] = useState(false)

  // 自动识别出的预置元数据（随模型 ID 变化）
  const preset = useMemo(() => detectPresetModel(providerPreset, modelId), [providerPreset, modelId])

  // 「自动识别」态下：模型 ID 命中预置目录即回填能力与继承窗口，避免用户手填
  useEffect(() => {
    if (source !== 'auto' || !preset) return
    setCaps(preset.capabilities ?? { ...DEFAULT_CAPS })
    setDisplayName((cur) => cur || preset.display_name || preset.id)
  }, [preset, source])

  /** 继承来源（自动识别 / 预置目录）的窗口值，用于「继承 · 1,000,000」提示 */
  const inheritedIn = tokensOf(preset?.max_context_extended || preset?.max_context) ?? tokensOf(initial?.max_context)
  const inheritedOut = tokensOf(initial?.context_out)

  const toggleCap = (group: 'input' | 'output', key: string): void => {
    setSource('manual')
    setCaps((cur) => {
      const set = new Set(cur[group] ?? [])
      if (set.has(key)) set.delete(key)
      else set.add(key)
      // 输入侧至少保留一项，避免「无输入能力」的非法声明
      const list = Array.from(set)
      if (group === 'input' && list.length === 0) list.push('text')
      if (group === 'output' && list.length === 0) list.push('text')
      return { ...cur, [group]: list }
    })
  }

  const restoreAuto = (): void => {
    setSource('auto')
    setCaps(preset?.capabilities ?? { ...DEFAULT_CAPS })
    setContextIn('')
  }

  const setAdv = (patch: Partial<LlmAdvanced>): void => setAdvanced((cur) => ({ ...cur, ...patch }))

  const valid =
    !!modelId.trim() &&
    numOk(advanced.temperature, 0, 2) &&
    numOk(advanced.top_p, 0, 1) &&
    numOk(advanced.top_k, 1, 100) &&
    ((advanced.tool_rounds ?? '').trim() === '' ||
      (Number(advanced.tool_rounds) > 0 && Number.isInteger(Number(advanced.tool_rounds))))

  const submit = (): void => {
    if (!valid) return
    const id = modelId.trim()
    onSubmit({
      id: initial?.id ?? genId('m_'),
      model: id,
      display_name: displayName.trim() || id,
      enabled: initial?.enabled ?? true,
      tags: preset?.tags ?? initial?.tags ?? [],
      context_in: contextIn,
      context_out: contextOut,
      capabilities: caps,
      capability_source: source,
      max_context: preset?.max_context ?? initial?.max_context,
      max_context_extended: preset?.max_context_extended ?? initial?.max_context_extended,
      thinking_strengths: preset?.thinking_strengths ?? initial?.thinking_strengths,
      default_thinking: preset?.default_thinking ?? initial?.default_thinking,
      ...(hasAdvancedValue(advanced) ? { advanced } : {})
    })
  }

  return (
    <div className="addmodel-mask" onClick={() => !saving && onCancel()}>
      <div className="addmodel" onClick={(e) => e.stopPropagation()}>
        <div className="addmodel-head">
          <span className="addmodel-title">{editing ? '模型参数' : '添加模型'}</span>
          <button className="icon-btn" onClick={onCancel} disabled={saving}>
            <Icon name="close" size={15} />
          </button>
        </div>

        <div className="addmodel-body">
          <div className="form-field">
            <label className="form-label">模型 ID</label>
            <input
              className="input"
              autoFocus
              placeholder="deepseek-v4-flash"
              value={modelId}
              onChange={(e) => {
                setModelId(e.target.value)
                if (source === 'auto') setCaps({ ...DEFAULT_CAPS })
              }}
            />
            <p className="form-hint">
              可自由输入任意模型 ID——厂商更新模型后直接填新 ID 即可，无需等待预置目录更新。
            </p>
          </div>

          <div className="ammodal-grid">
            {/* 左：参数 */}
            <div className="ammodal-col">
              <div className="ammodal-col-title">参数</div>

              <div className="form-field">
                <div className="ammodal-row-head">
                  <label className="form-label">上下文（tokens）</label>
                  <button
                    type="button"
                    className="icon-btn"
                    title="恢复继承"
                    onClick={() => setContextIn('')}
                  >
                    <Icon name="refresh" size={13} />
                  </button>
                </div>
                <select
                  className="input"
                  value={contextIn}
                  onChange={(e) => setContextIn(e.target.value)}
                >
                  <option value="">继承</option>
                  {CONTEXT_IN_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
                <p className="form-hint">
                  继承 · {formatTokens(inheritedIn) || '未声明'}
                </p>
              </div>

              <div className="form-field">
                <div className="ammodal-row-head">
                  <label className="form-label">输出上限（Token）</label>
                  <button
                    type="button"
                    className="icon-btn"
                    title="恢复继承"
                    onClick={() => setContextOut('')}
                  >
                    <Icon name="refresh" size={13} />
                  </button>
                </div>
                <select
                  className="input"
                  value={contextOut}
                  onChange={(e) => setContextOut(e.target.value)}
                >
                  <option value="">继承</option>
                  {CONTEXT_OUT_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
                <p className="form-hint">
                  留空继承（{formatTokens(inheritedOut) || '未声明'}）；-1 表示不发送可选输出限制，仍受模型实际限制。
                </p>
              </div>

              <div className="form-field">
                <label className="form-label">显示名称</label>
                <input
                  className="input"
                  placeholder="留空则沿用模型 ID"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                />
              </div>
            </div>

            {/* 右：模型能力 + 来源 */}
            <div className="ammodal-col ammodal-col-right">
              <div className="ammodal-col-title">模型能力</div>

              <div className="form-field">
                <label className="form-label cap-label">输入</label>
                <div className="cap-grid">
                  {CAP_INPUT.map((c) => (
                    <button
                      key={c.key}
                      type="button"
                      className={`cap-chip ${caps.input?.includes(c.key) ? 'on' : ''}`}
                      onClick={() => toggleCap('input', c.key)}
                    >
                      <span className="cap-box">{caps.input?.includes(c.key) ? '✓' : ''}</span>
                      {c.label}
                      {c.key === 'text' && <Icon name="edit" size={11} />}
                    </button>
                  ))}
                </div>
                <p className="form-hint">暂不支持直接输入视频或 PDF。</p>
              </div>

              <div className="form-field">
                <label className="form-label cap-label">输出</label>
                <div className="cap-grid">
                  {CAP_OUTPUT.map((c) => (
                    <button
                      key={c.key}
                      type="button"
                      className={`cap-chip ${caps.output?.includes(c.key) ? 'on' : ''}`}
                      onClick={() => toggleCap('output', c.key)}
                    >
                      <span className="cap-box">{caps.output?.includes(c.key) ? '✓' : ''}</span>
                      {c.label}
                      {c.key === 'text' && <Icon name="edit" size={11} />}
                    </button>
                  ))}
                </div>
              </div>

              <div className="form-field">
                <div className="ammodal-source">
                  <span className="form-label">来源</span>
                  <span className={`ammodal-source-tag ${source}`}>
                    {source === 'auto' ? '自动识别' : '手动覆盖'}
                  </span>
                </div>
                <button
                  type="button"
                  className="btn btn-sm ammodal-restore"
                  onClick={restoreAuto}
                  disabled={source === 'auto'}
                >
                  恢复自动识别
                </button>
                <p className="form-hint">
                  仅覆盖能力声明，不会改变模型的实际上下文与参数行为。
                </p>
              </div>
            </div>
          </div>

          {/* 进阶参数（旧的采样/思考项，折叠收纳，通常不用改） */}
          <div className="form-field adv">
            <button type="button" className="ammodal-adv-toggle" onClick={() => setAdvOpen((v) => !v)}>
              进阶参数（通常不用改）
              {hasAdvancedValue(advanced) && <span className="adv-dot" title="已配置自定义参数" />}
              <span className={`select-caret ${advOpen ? 'open' : ''}`}>▾</span>
            </button>
            {advOpen && (
              <div className="adv-body">
                <div className="adv-group">
                  <div className="adv-group-title">工具调用轮数</div>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    placeholder="留空则使用默认值（100）"
                    value={advanced.tool_rounds ?? ''}
                    onChange={(e) => setAdv({ tool_rounds: e.target.value })}
                  />
                </div>
                <div className="adv-group">
                  <div className="adv-group-title">
                    思考模式 <span className="adv-opt">（可选）</span>
                  </div>
                  <div className="adv-radio-row">
                    <label className="adv-radio">
                      <input
                        type="radio"
                        name="adv-thinking"
                        checked={advanced.thinking === 'default'}
                        onChange={() => setAdv({ thinking: 'default' })}
                      />{' '}
                      跟随模型默认配置
                    </label>
                    <label className="adv-radio">
                      <input
                        type="radio"
                        name="adv-thinking"
                        checked={advanced.thinking === 'enabled'}
                        onChange={() => setAdv({ thinking: 'enabled' })}
                      />{' '}
                      开启
                    </label>
                    <label className="adv-radio">
                      <input
                        type="radio"
                        name="adv-thinking"
                        checked={advanced.thinking === 'disabled'}
                        onChange={() => setAdv({ thinking: 'disabled' })}
                      />{' '}
                      关闭
                    </label>
                    {advanced.thinking && (
                      <button type="button" className="adv-clear" onClick={() => setAdv({ thinking: '' })}>
                        清除
                      </button>
                    )}
                  </div>
                </div>
                <div className="adv-group">
                  <div className="adv-group-title">采样参数</div>
                  <div className="adv-sample-row">
                    <span className="adv-row-label">Temperature</span>
                    <input
                      className="input"
                      placeholder="留空使用最佳配置，或输入 0 ~ 2 之间的数值"
                      value={advanced.temperature ?? ''}
                      onChange={(e) => setAdv({ temperature: e.target.value })}
                    />
                  </div>
                  <div className="adv-sample-row">
                    <span className="adv-row-label">Top P</span>
                    <input
                      className="input"
                      placeholder="留空使用最佳配置，或输入 0 ~ 1 之间的数值"
                      value={advanced.top_p ?? ''}
                      onChange={(e) => setAdv({ top_p: e.target.value })}
                    />
                  </div>
                  <div className="adv-sample-row">
                    <span className="adv-row-label">Top K</span>
                    <input
                      className="input"
                      placeholder="留空使用最佳配置，或输入 1 ~ 100 之间的数值"
                      value={advanced.top_k ?? ''}
                      onChange={(e) => setAdv({ top_k: e.target.value })}
                    />
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="addmodel-foot">
          {editing && onDelete && (
            <button
              className={`btn ammodal-del ${confirmDelete ? 'confirm' : ''}`}
              disabled={saving}
              onClick={() => {
                if (!confirmDelete) {
                  setConfirmDelete(true)
                  return
                }
                onDelete(initial!.id)
              }}
            >
              {confirmDelete ? '确认删除？' : '删除模型'}
            </button>
          )}
          {editing && onSetDefault && (
            <button
              className="btn ammodal-default"
              disabled={saving || isDefault || !initial?.enabled}
              title={initial?.enabled ? undefined : '模型未启用，不能设为默认模型'}
              onClick={onSetDefault}
            >
              {isDefault ? '默认模型 ✓' : '设为默认模型'}
            </button>
          )}
          <span className="addmodel-foot-hint">保存连接后生效</span>
          <button className="btn" onClick={onCancel} disabled={saving}>
            取消
          </button>
          <button className="btn btn-primary" disabled={!valid || saving} onClick={submit}>
            {saving ? '保存中…' : editing ? '保存' : '添加到列表'}
          </button>
        </div>
      </div>
    </div>
  )
}
