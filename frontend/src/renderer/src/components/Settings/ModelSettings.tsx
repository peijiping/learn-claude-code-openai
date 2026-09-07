import { useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import type { LlmConfig, LlmModel, LlmProvider } from '@protocols/agentProtocol'
import { Icon } from '@components/common/Icon'

type ProviderKey = 'deepseek' | 'siliconflow'

interface Draft {
  id: string | null // null = 新增
  provider: ProviderKey
  model: string
  display_name: string
  base_url: string
  api_key: string
  enabled: boolean
}

function providerOptions(providers: Record<string, LlmProvider>): [ProviderKey, LlmProvider][] {
  const keys: ProviderKey[] = ['deepseek', 'siliconflow']
  return keys.filter((k) => providers[k]).map((k) => [k, providers[k]])
}

const PROVIDER_THEME: Record<string, string> = {
  deepseek: 'dp',
  siliconflow: 'sf'
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
    if (models.length <= 1) return // 后端要求至少保留一个模型
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
      enabled: true
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
      enabled: m.enabled
    })
  }

  return (
    <div className="model-settings">
      <div className="model-header">
        <h3 className="model-title">模型管理</h3>
        <p className="model-desc">配置 API Key 添加更多可用模型，预置模型默认使用稳定版本。</p>
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
                    <div>
                      <div className="model-name">
                        {m.display_name || m.id}
                        {isActive && <span className="model-active-tag">当前</span>}
                      </div>
                      <div className="model-model-id">{m.model}</div>
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
                    disabled={models.length <= 1}
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
            const payload: LlmModel = {
              id: draft.id ?? `m_${Date.now().toString(36)}`,
              provider: draft.provider,
              display_name: draft.display_name || draft.model,
              model: draft.model,
              base_url: draft.base_url,
              // 编辑时留空 = 沿用原密钥（密钥只存后端，不回显）
              api_key: draft.api_key || existing?.api_key || '',
              enabled: draft.enabled
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

/** 添加 / 编辑模型的内嵌弹窗：先选服务商（预置网格）→ 填表单 */
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
  const pickModelPreset = (id: string): void => {
    const p = providers[form.provider]?.models.find((m) => m.id === id)
    setForm((f) => ({
      ...f,
      model: id,
      display_name: p?.display_name ?? id,
      base_url: providers[form.provider]?.base_url ?? f.base_url
    }))
  }

  // 新增必须填 API Key；编辑时留空沿用原密钥
  const valid = !!form.provider && !!form.model.trim() && !!form.base_url.trim()
    && (form.api_key.trim() !== '' || !!draft.id)

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
            <input
              className="input"
              list="model-candidates"
              placeholder="选择模型或输入自定义模型 ID"
              value={form.model}
              onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))}
            />
            <datalist id="model-candidates">
              {preset?.models.map((m) => (
                <option key={m.id} value={m.id} />
              ))}
            </datalist>
            {preset && preset.models.length > 0 && (
              <div className="chip-row">
                {preset.models.map((m) => (
                  <button
                    key={m.id}
                    className={`chip ${form.model === m.id ? 'active' : ''}`}
                    onClick={() => pickModelPreset(m.id)}
                  >
                    {m.display_name}
                  </button>
                ))}
              </div>
            )}
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