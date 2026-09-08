import { useEffect, useRef, useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import type { LlmAdvanced, LlmConfig, LlmModel, LlmProvider, LlmProviderModel } from '@protocols/agentProtocol'
import { Icon } from '@components/common/Icon'

type ProviderKey = 'deepseek' | 'siliconflow'

const EMPTY_ADVANCED: LlmAdvanced = {}

interface Draft {
  id: string | null // null = 新增
  provider: ProviderKey
  model: string
  display_name: string
  base_url: string
  api_key: string
  enabled: boolean
  advanced: LlmAdvanced
}

function providerOptions(providers: Record<string, LlmProvider>): [ProviderKey, LlmProvider][] {
  const keys: ProviderKey[] = ['deepseek', 'siliconflow']
  return keys.filter((k) => providers[k]).map((k) => [k, providers[k]])
}

const PROVIDER_THEME: Record<string, string> = {
  deepseek: 'dp',
  siliconflow: 'sf'
}

/** 高级设置里是否有任一项被填写（决定是否落盘 advanced 字段） */
function hasAdvancedValue(adv: LlmAdvanced): boolean {
  return Object.values(adv).some((v) => typeof v === 'string' && v.trim() !== '')
}

/** 数值型高级项校验：留空合法；填了必须是 min~max 的数字 */
function numOk(v: string | undefined, min: number, max: number): boolean {
  const s = (v ?? '').trim()
  if (!s) return true
  const n = Number(s)
  return Number.isFinite(n) && n >= min && n <= max
}

/** 模型管理页：模型列表 + 添加/编辑/删除/启用开关 */
export default function ModelSettings(): JSX.Element {
  const llmConfig = useAgentStore((s) => s.llmConfig)
  const llmSaving = useAgentStore((s) => s.llmSaving)
  const saveLlConfig = useAgentStore((s) => s.saveLlConfig)

  const providers = llmConfig?.providers ?? {}
  const models = llmConfig?.models ?? []
  const activeId = llmConfig?.active_model_id ?? null

  const [editing, setEditing] = useState<Draft | null>(null)

  const persist = async (next: LlmConfig): Promise<void> => {
    await saveLlConfig({ active_model_id: next.active_model_id, models: next.models })
  }

  const toggle = (m: LlmModel): void => {
    persist({
      active_model_id: activeId,
      models: models.map((x) => (x.id === m.id ? { ...x, enabled: !x.enabled } : x))
    })
  }

  const remove = (m: LlmModel): void => {
    // 任何模型都可删除；删光后回到「未配置」态（后端 save_config 允许空 models）
    const next = models.filter((x) => x.id !== m.id)
    persist({ active_model_id: activeId === m.id ? null : activeId, models: next })
  }

  const openAdd = (): void => {
    const first = providerOptions(providers)[0]
    const provider = first ? first[0] : 'deepseek'
    const preset = first?.[1]?.models[0]
    setEditing({
      id: null,
      provider,
      model: preset?.id ?? '',
      display_name: preset?.display_name ?? '',
      base_url: first ? first[1].base_url : '',
      api_key: '',
      enabled: true,
      advanced: { ...EMPTY_ADVANCED }
    })
  }

  const openEdit = (m: LlmModel): void => {
    setEditing({
      id: m.id,
      provider: (m.provider as ProviderKey) || 'deepseek',
      model: m.model,
      display_name: m.display_name,
      base_url: m.base_url,
      api_key: '',
      enabled: m.enabled,
      advanced: { ...EMPTY_ADVANCED, ...(m.advanced ?? {}) }
    })
  }

  return (
    <div className="model-settings">
      <div className="model-header">
        <h3 className="model-title">模型管理</h3>
        <p className="model-desc">配置 API Key 添加更多可用模型，仅支持 OpenAI 兼容格式的 API；模型与高级设置保存后立即热生效。</p>
        <button className="btn btn-primary btn-sm" onClick={openAdd}>
          <Icon name="plus" size={14} /> 添加模型
        </button>
      </div>

      <table className="model-table">
        <thead>
          <tr>
            <th>模型</th>
            <th>服务商</th>
            <th className="model-ops">操作</th>
          </tr>
        </thead>
        <tbody>
          {models.length === 0 && (
            <tr>
              <td colSpan={3} className="model-empty">
                还没有配置模型，点击右上角「添加模型」开始。
              </td>
            </tr>
          )}
          {models.map((m) => {
            const isActive = m.id === activeId
            return (
              <tr key={m.id}>
                <td>
                  <div className="model-name-row">
                    <span className={`model-initial ${PROVIDER_THEME[m.provider] ?? ''}`}>
                      {(m.display_name || m.id).slice(0, 1).toUpperCase()}
                    </span>
                    <div className="model-name-cell">
                      <div className="model-name">
                        <span className="model-name-text" title={m.display_name || m.id}>
                          {m.display_name || m.id}
                        </span>
                        {isActive && <span className="model-active-tag">当前</span>}
                      </div>
                      <div className="model-model-id" title={m.model}>{m.model}</div>
                    </div>
                  </div>
                </td>
                <td>{providers[m.provider]?.name ?? m.provider}</td>
                <td className="model-ops">
                  <button className="icon-btn" title="编辑" onClick={() => openEdit(m)}>
                    <Icon name="gear" size={13} />
                  </button>
                  <button
                    className="icon-btn danger"
                    title="删除"
                    onClick={() => remove(m)}
                  >
                    <Icon name="trash" size={13} />
                  </button>
                  <button
                    className={`switch ${m.enabled ? 'on' : ''}`}
                    role="switch"
                    aria-checked={m.enabled}
                    title={m.enabled ? '点击停用' : '点击启用'}
                    onClick={() => toggle(m)}
                  >
                    <span className="switch-knob" />
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {llmSaving && <div className="model-saving">保存中…</div>}

      {editing && (
        <AddModelModal
          title={editing.id ? '编辑模型' : '通过服务商添加'}
          draft={editing}
          providers={providers}
          saving={llmSaving}
          onCancel={() => setEditing(null)}
          onSave={(next) => {
            const draft = next as Draft
            const existing = models.find((x) => x.id === draft.id)
            const adv = draft.advanced ?? {}
            const payload: LlmModel = {
              id: draft.id ?? `m_${Date.now().toString(36)}`,
              provider: draft.provider,
              display_name: draft.display_name || draft.model,
              model: draft.model,
              base_url: draft.base_url,
              // 编辑时留空 = 沿用原密钥（密钥只存后端，不回显）
              api_key: draft.api_key || existing?.api_key || '',
              enabled: draft.enabled,
              // 高级设置：任一项填了才落盘，全空则不写（后端按「未配置」走默认）
              ...(hasAdvancedValue(adv) ? { advanced: adv } : {})
            }
            const isNew = !draft.id
            const nextModels = isNew
              ? [...models, payload]
              : models.map((x) => (x.id === payload.id ? payload : x))
            const nextActive = isNew && models.length === 0 ? payload.id : activeId
            void persist({ active_model_id: nextActive, models: nextModels }).then(() =>
              setEditing(null)
            )
          }}
        />
      )}
    </div>
  )
}

/** 模型下拉：展示服务商预置模型（含 1M/图片 等标签），点击外部自动收起。
 * 编辑历史配置时若当前 model 不在候选里，置顶一条「自定义」项避免丢值。 */
function ModelSelect(props: {
  value: string
  options: LlmProviderModel[]
  onChange: (id: string) => void
}): JSX.Element {
  const { value, options, onChange } = props
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const list: LlmProviderModel[] = value && !options.some((o) => o.id === value)
    ? [{ id: value, display_name: value }, ...options]
    : options
  const current = list.find((o) => o.id === value)

  return (
    <div className="model-select-dd" ref={ref}>
      <button
        type="button"
        className={`input select-trigger ${open ? 'open' : ''}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={current ? '' : 'select-placeholder'}>{current?.id || '选择模型'}</span>
        <span className="select-caret">▾</span>
      </button>
      {open && (
        <div className="select-menu" role="listbox">
          {list.map((o) => (
            <button
              key={o.id}
              type="button"
              className={`select-option ${o.id === value ? 'active' : ''}`}
              onClick={() => {
                onChange(o.id)
                setOpen(false)
              }}
            >
              <span className="select-option-id">{o.id}</span>
              <span className="select-option-tags">
                {(o.tags ?? []).map((t) => (
                  <span key={t} className="model-tag">{t}</span>
                ))}
              </span>
              {o.id === value && <span className="select-check">✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** 高级设置（可折叠）：上下文窗口 / 工具调用轮数 / 图片输入 / 思考模式 / 采样参数。
 * 全部可选项：留空 = 使用程序默认；保存后写入 llmconfig.json 并在模型实例中生效。 */
function AdvancedSection(props: {
  value: LlmAdvanced
  onChange: (next: LlmAdvanced) => void
}): JSX.Element {
  const { value, onChange } = props
  const filled = hasAdvancedValue(value)
  const [open, setOpen] = useState(filled) // 编辑已配置过高级项的模型时默认展开
  const set = (patch: Partial<LlmAdvanced>): void => onChange({ ...value, ...patch })

  const ctxIn = ['128k', '256k', '512k', '1M']
  const ctxOut = ['4k', '16k', '32k', '128k']

  return (
    <div className="form-field adv">
      <button type="button" className="adv-toggle" onClick={() => setOpen((v) => !v)}>
        高级配置
        {filled && <span className="adv-dot" title="已配置自定义高级项" />}
        <span className={`select-caret ${open ? 'open' : ''}`}>▾</span>
      </button>

      {open && (
        <div className="adv-body">
          <div className="adv-group">
            <div className="adv-group-title">上下文窗口（Token）</div>
            <div className="adv-row">
              <span className="adv-row-label">输入</span>
              <input
                className="input"
                placeholder="留空则使用最佳默认值"
                value={value.context_in ?? ''}
                onChange={(e) => set({ context_in: e.target.value })}
              />
              <span className="quick-chips">
                {ctxIn.map((t) => (
                  <button
                    key={t}
                    type="button"
                    className={`chip ${(value.context_in ?? '').toLowerCase() === t ? 'active' : ''}`}
                    onClick={() => set({ context_in: (value.context_in ?? '').toLowerCase() === t ? '' : t })}
                  >
                    {t}
                  </button>
                ))}
              </span>
            </div>
            <div className="adv-row">
              <span className="adv-row-label">输出</span>
              <input
                className="input"
                placeholder="留空则使用最佳默认值"
                value={value.context_out ?? ''}
                onChange={(e) => set({ context_out: e.target.value })}
              />
              <span className="quick-chips">
                {ctxOut.map((t) => (
                  <button
                    key={t}
                    type="button"
                    className={`chip ${(value.context_out ?? '').toLowerCase() === t ? 'active' : ''}`}
                    onClick={() => set({ context_out: (value.context_out ?? '').toLowerCase() === t ? '' : t })}
                  >
                    {t}
                  </button>
                ))}
              </span>
            </div>
          </div>

          <div className="adv-group">
            <div className="adv-group-title">工具调用轮数</div>
            <input
              className="input"
              type="number"
              min={1}
              placeholder="留空则使用默认值（100）"
              value={value.tool_rounds ?? ''}
              onChange={(e) => set({ tool_rounds: e.target.value })}
            />
          </div>

          <div className="adv-group">
            <div className="adv-group-title">
              支持图片输入 <span className="adv-opt">（可选）</span>
            </div>
            <div className="adv-radio-row">
              <label className="adv-radio">
                <input
                  type="radio"
                  name="adv-image"
                  checked={value.image_input === 'yes'}
                  onChange={() => set({ image_input: 'yes' })}
                />{' '}
                支持
              </label>
              <label className="adv-radio">
                <input
                  type="radio"
                  name="adv-image"
                  checked={value.image_input === 'no'}
                  onChange={() => set({ image_input: 'no' })}
                />{' '}
                不支持
              </label>
              {value.image_input && (
                <button type="button" className="adv-clear" onClick={() => set({ image_input: '' })}>
                  清除
                </button>
              )}
            </div>
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
                  checked={value.thinking === 'default'}
                  onChange={() => set({ thinking: 'default' })}
                />{' '}
                跟随模型默认配置
              </label>
              <label className="adv-radio">
                <input
                  type="radio"
                  name="adv-thinking"
                  checked={value.thinking === 'enabled'}
                  onChange={() => set({ thinking: 'enabled' })}
                />{' '}
                开启
              </label>
              <label className="adv-radio">
                <input
                  type="radio"
                  name="adv-thinking"
                  checked={value.thinking === 'disabled'}
                  onChange={() => set({ thinking: 'disabled' })}
                />{' '}
                关闭
              </label>
              {value.thinking && (
                <button type="button" className="adv-clear" onClick={() => set({ thinking: '' })}>
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
                value={value.temperature ?? ''}
                onChange={(e) => set({ temperature: e.target.value })}
              />
            </div>
            <div className="adv-sample-row">
              <span className="adv-row-label">Top P</span>
              <input
                className="input"
                placeholder="留空使用最佳配置，或输入 0 ~ 1 之间的数值"
                value={value.top_p ?? ''}
                onChange={(e) => set({ top_p: e.target.value })}
              />
            </div>
            <div className="adv-sample-row">
              <span className="adv-row-label">Top K</span>
              <input
                className="input"
                placeholder="留空使用最佳配置，或输入 1 ~ 100 之间的数值"
                value={value.top_k ?? ''}
                onChange={(e) => set({ top_k: e.target.value })}
              />
            </div>
          </div>

          <p className="form-hint">
            高级配置全部为可选项，留空即使用程序默认；保存后写入 ~/.aigent/llmconfig.json 并加载进模型实例立即生效。
          </p>
        </div>
      )}
    </div>
  )
}

/** 添加 / 编辑模型的内嵌弹窗：先选服务商（预置网格）→ 下拉选模型 → 填表单（含可选高级配置） */
function AddModelModal(props: {
  title: string
  draft: Draft
  providers: Record<string, LlmProvider>
  saving: boolean
  onCancel: () => void
  onSave: (draft: Draft) => void
}): JSX.Element {
  const { title, draft, providers, saving, onCancel, onSave } = props
  const options = providerOptions(providers)
  const [form, setForm] = useState<Draft>(draft)
  const [showKey, setShowKey] = useState(false)

  const preset = providers[form.provider]
  const pickProvider = (k: ProviderKey): void => {
    const provider = providers[k]
    const p = provider.models[0]
    setForm((f) => ({
      ...f,
      provider: k,
      base_url: provider.base_url,
      model: p?.id ?? '',
      display_name: p?.display_name ?? ''
    }))
  }
  const pickModel = (id: string): void => {
    const p = providers[form.provider]?.models.find((m) => m.id === id)
    // 显示名称跟随所选模型 ID，避免「显示名与实际模型对不上」
    setForm((f) => ({
      ...f,
      model: id,
      display_name: p?.display_name ?? id,
      base_url: providers[form.provider]?.base_url ?? f.base_url
    }))
  }

  // 新增必须填 API Key；编辑时留空沿用原密钥
  const adv = form.advanced ?? {}
  const valid = !!form.provider && !!form.model.trim() && !!form.base_url.trim()
    && (form.api_key.trim() !== '' || !!draft.id)
    && numOk(adv.temperature, 0, 2)
    && numOk(adv.top_p, 0, 1)
    && numOk(adv.top_k, 1, 100)
    && ((adv.tool_rounds ?? '').trim() === '' || (Number(adv.tool_rounds) > 0 && Number.isInteger(Number(adv.tool_rounds))))

  return (
    <div className="addmodel-mask" onClick={() => !saving && onCancel()}>
      <div className="addmodel" onClick={(e) => e.stopPropagation()}>
        <div className="addmodel-head">
          <span className="addmodel-title">{title}</span>
          <button className="icon-btn" onClick={onCancel} disabled={saving}>
            <Icon name="close" size={15} />
          </button>
        </div>

        <div className="addmodel-body">
          <div className="form-field">
            <label className="form-label">
              服务商 <b className="req">*</b>
            </label>
            <div className="provider-grid">
              {options.map(([k, p]) => (
                <button
                  key={k}
                  className={`provider-cell ${form.provider === k ? 'active' : ''}`}
                  onClick={() => pickProvider(k)}
                >
                  <span className={`provider-dot ${PROVIDER_THEME[k] ?? ''}`} />
                  {p.name}
                </button>
              ))}
            </div>
          </div>

          <div className="form-field">
            <label className="form-label">
              模型 <b className="req">*</b>
            </label>
            <ModelSelect
              value={form.model}
              options={preset?.models ?? []}
              onChange={pickModel}
            />
          </div>

          <div className="form-field">
            <label className="form-label">显示名称</label>
            <input
              className="input"
              placeholder="留空则沿用模型 ID"
              value={form.display_name}
              onChange={(e) => setForm((f) => ({ ...f, display_name: e.target.value }))}
            />
          </div>

          <div className="form-field">
            <label className="form-label">
              API 密钥 <b className="req">*</b>
            </label>
            <div className="input-suffix">
              <input
                className="input"
                type={showKey ? 'text' : 'password'}
                placeholder="请输入 API Key"
                value={form.api_key}
                onChange={(e) => setForm((f) => ({ ...f, api_key: e.target.value }))}
              />
              <button
                className="suffix-btn"
                title={showKey ? '隐藏' : '显示'}
                onClick={() => setShowKey((v) => !v)}
              >
                <Icon name={showKey ? 'eyeSlash' : 'eye'} size={14} />
              </button>
              <a
                className="suffix-link"
                href={preset?.base_url}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
              >
                获取 API 密钥
              </a>
            </div>
            {!draft.id && (
              <p className="form-hint">密钥仅保存在本地 {`~/.aigent/llmconfig.json`}，保存后立即生效。</p>
            )}
          </div>

          <div className="form-field">
            <label className="form-label">接口地址</label>
            <input
              className="input"
              value={form.base_url}
              onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))}
            />
          </div>

          <AdvancedSection
            value={adv}
            onChange={(next) => setForm((f) => ({ ...f, advanced: next }))}
          />

          <div className="form-field row">
            <label className="form-label">启用模型</label>
            <button
              className={`switch ${form.enabled ? 'on' : ''}`}
              role="switch"
              aria-checked={form.enabled}
              onClick={() => setForm((f) => ({ ...f, enabled: !f.enabled }))}
            >
              <span className="switch-knob" />
            </button>
            <span className="form-hint">{form.enabled ? '已启用' : '已停用'}</span>
          </div>
        </div>

        <div className="addmodel-foot">
          <button className="btn" onClick={onCancel} disabled={saving}>
            取消
          </button>
          <button
            className="btn btn-primary"
            disabled={!valid || saving}
            onClick={() => onSave(form)}
          >
            {saving ? '保存中…' : draft.id ? '保存' : '添加模型'}
          </button>
        </div>
      </div>
    </div>
  )
}
