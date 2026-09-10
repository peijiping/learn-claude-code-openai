import { useEffect, useMemo, useState } from 'react'
import { Icon } from '@components/common/Icon'
import { showToast } from '@store/agentStore'
import type {
  LlmApiFormat,
  LlmConnection,
  LlmConnectionModel,
  LlmProvider
} from '@protocols/agentProtocol'
import AddModelModal from './AddModelModal'
import ModelListEditor from './ModelListEditor'
import { DEFAULT_MODELS_PATH, genId, knownModelIdsOf, providerInitial } from './llmShared'

interface AddProviderModalProps {
  providers: Record<string, LlmProvider>
  apiFormats: LlmApiFormat[]
  saving: boolean
  onCancel: () => void
  onSubmit: (conn: LlmConnection) => void
  /** 刷新远端模型列表（由上层注入 store 能力） */
  onFetchModels: (payload: {
    base_url?: string
    api_key?: string
    connection_id?: string
    api_format?: string
    models_path?: string
  }) => Promise<string[]>
}

const FALLBACK_FORMATS: LlmApiFormat[] = [
  { id: 'chat_completions', label: 'Chat Completions (/chat/completions)' }
]

/** 由预置厂商目录构造一份初始模型列表（全部启用） */
function modelsFromPreset(provider: LlmProvider): LlmConnectionModel[] {
  return (provider.models ?? []).map((m) => ({
    id: genId('m_'),
    model: m.id,
    display_name: m.display_name || m.id,
    enabled: true,
    tags: m.tags ?? [],
    capabilities: m.capabilities,
    capability_source: 'auto' as const,
    max_context: m.max_context,
    max_context_extended: m.max_context_extended,
    thinking_strengths: m.thinking_strengths,
    default_thinking: m.default_thinking
  }))
}

/** 「添加模型服务（供应商）」弹窗：推荐预设 / 自定义供应商 两个 tab。 */
export default function AddProviderModal({
  providers,
  apiFormats,
  saving,
  onCancel,
  onSubmit,
  onFetchModels
}: AddProviderModalProps): JSX.Element {
  const formats = apiFormats.length ? apiFormats : FALLBACK_FORMATS
  const presetKeys = useMemo(() => Object.keys(providers), [providers])

  const [tab, setTab] = useState<'preset' | 'custom'>('preset')
  const [search, setSearch] = useState('')
  const [presetKey, setPresetKey] = useState<string>(presetKeys[0] ?? '')

  // 自定义供应商表单
  const [customName, setCustomName] = useState('')
  const [customBaseUrl, setCustomBaseUrl] = useState('')
  const [customFormat, setCustomFormat] = useState(formats[0]?.id ?? 'chat_completions')
  const [customKey, setCustomKey] = useState('')

  // 预置供应商表单（Base URL / Key 可改）
  const [presetFormat, setPresetFormat] = useState(formats[0]?.id ?? 'chat_completions')
  const [presetBaseUrl, setPresetBaseUrl] = useState('')
  const [presetKeyInput, setPresetKeyInput] = useState('')

  const [models, setModels] = useState<LlmConnectionModel[]>([])
  const [modelsPath, setModelsPath] = useState(DEFAULT_MODELS_PATH)
  const [compatOpen, setCompatOpen] = useState(false)
  const [showKey, setShowKey] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [addModelOpen, setAddModelOpen] = useState(false)
  const [editingModel, setEditingModel] = useState<LlmConnectionModel | null>(null)

  const provider = providers[presetKey] as LlmProvider | undefined
  const filteredPresets = presetKeys.filter(
    (k) => !search.trim() || (providers[k].name || k).toLowerCase().includes(search.trim().toLowerCase())
  )

  // 预置目录异步到达时补选第一个厂商（打开弹窗时 llmConfig 可能尚未加载完）
  useEffect(() => {
    if (tab === 'preset' && !providers[presetKey] && presetKeys.length) setPresetKey(presetKeys[0])
  }, [tab, presetKey, presetKeys, providers])

  // 切换预置厂商 / 切到预设 tab：回填端点、格式与候选模型
  useEffect(() => {
    if (tab !== 'preset' || !provider) return
    setPresetFormat(provider.api_format || formats[0]?.id || 'chat_completions')
    setPresetBaseUrl(provider.base_url || '')
    setModels(modelsFromPreset(provider))
    setModelsPath(DEFAULT_MODELS_PATH)
    setPresetKeyInput('')
  }, [tab, presetKey, provider, formats])

  const checkedIds = models.filter((m) => m.enabled).map((m) => m.id)
  // 自定义供应商没有预置目录可对照，传 undefined 表示「不做识别提示」
  const knownIds = tab === 'preset' && provider ? knownModelIdsOf(provider) : undefined

  const applyModelEdit = (m: LlmConnectionModel): void => {
    setModels((cur) => cur.map((x) => (x.id === m.id ? m : x)))
    setEditingModel(null)
  }

  const toggleModel = (id: string): void =>
    setModels((cur) => cur.map((m) => (m.id === id ? { ...m, enabled: !m.enabled } : m)))

  const checkAll = (checked: boolean): void =>
    setModels((cur) => cur.map((m) => ({ ...m, enabled: checked })))

  const effectiveBaseUrl = tab === 'preset' ? presetBaseUrl : customBaseUrl
  const effectiveKey = tab === 'preset' ? presetKeyInput : customKey
  const effectiveFormat = tab === 'preset' ? presetFormat : customFormat

  const refresh = async (): Promise<void> => {
    if (!effectiveBaseUrl.trim()) {
      showToast('请先填写 API 地址', 'error', 3000)
      return
    }
    setRefreshing(true)
    const ids = await onFetchModels({
      base_url: effectiveBaseUrl,
      api_key: effectiveKey,
      api_format: effectiveFormat,
      models_path: modelsPath || DEFAULT_MODELS_PATH
    })
    setRefreshing(false)
    if (!ids.length) return
    const existing = new Set(models.map((m) => m.model))
    const added = ids
      .filter((id) => !existing.has(id))
      .map<LlmConnectionModel>((id) => ({
        id: genId('m_'),
        model: id,
        display_name: id,
        enabled: false,
        capabilities: { input: ['text'], output: ['text'] },
        capability_source: 'auto'
      }))
    if (added.length) setModels((cur) => [...cur, ...added])
    showToast(`已加载 ${ids.length} 个模型，新增 ${added.length} 个`, 'info')
  }

  const valid =
    !!effectiveBaseUrl.trim() && (tab === 'preset' ? !!provider : !!customName.trim())

  const submit = (): void => {
    if (!valid) return
    const isCustom = tab === 'custom'
    const slug = (customName.trim() || 'custom')
      .toLowerCase()
      .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '-')
      .replace(/^-+|-+$/g, '')
    onSubmit({
      id: genId('c_'),
      provider: isCustom ? `custom:${slug || 'provider'}` : presetKey,
      name: isCustom ? customName.trim() : provider?.name || presetKey,
      base_url: effectiveBaseUrl.trim(),
      api_format: effectiveFormat,
      api_key: effectiveKey.trim(),
      models,
      compat: { models_path: modelsPath || DEFAULT_MODELS_PATH },
      custom: isCustom
    })
  }

  return (
    <div className="addmodel-mask" onClick={() => !saving && onCancel()}>
      <div className="addmodel addprovider" onClick={(e) => e.stopPropagation()}>
        <div className="addmodel-head">
          <span className="addmodel-title">添加供应商</span>
          <button className="btn btn-sm" onClick={onCancel} disabled={saving}>
            取消
          </button>
        </div>

        <div className="addprovider-sub">
          选择服务商，再选择账号平台、套餐和 API 格式。
        </div>

        <div className="addprovider-tabs">
          <button
            className={`addprovider-tab ${tab === 'preset' ? 'active' : ''}`}
            onClick={() => setTab('preset')}
          >
            推荐预设
          </button>
          <button
            className={`addprovider-tab ${tab === 'custom' ? 'active' : ''}`}
            onClick={() => setTab('custom')}
          >
            自定义供应商
          </button>
        </div>

        {tab === 'preset' ? (
          <div className="addprovider-split">
            <aside className="addprovider-list">
              <input
                className="input"
                placeholder="搜索服务商"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <div className="addprovider-items">
                {filteredPresets.map((k) => (
                  <button
                    key={k}
                    className={`addprovider-item ${presetKey === k ? 'active' : ''}`}
                    onClick={() => setPresetKey(k)}
                  >
                    <span className="provider-avatar">{providerInitial(providers[k].name || k)}</span>
                    <span className="addprovider-item-name">{providers[k].name || k}</span>
                  </button>
                ))}
                {filteredPresets.length === 0 && (
                  <div className="addprovider-empty">没有匹配的服务商</div>
                )}
              </div>
            </aside>

            <section className="addprovider-detail">
              <h3 className="addprovider-name">{provider?.name || presetKey}</h3>

              <div className="form-field">
                <label className="form-label">API 格式</label>
                <select
                  className="input"
                  value={presetFormat}
                  onChange={(e) => setPresetFormat(e.target.value)}
                >
                  {formats.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.label}
                    </option>
                  ))}
                </select>
              </div>

              <div className="form-field">
                <label className="form-label">Base URL</label>
                <input
                  className="input"
                  value={presetBaseUrl}
                  onChange={(e) => setPresetBaseUrl(e.target.value)}
                />
                <p className="form-hint">可修改，仅用于当前连接，不影响预设默认值。</p>
              </div>

              <div className="form-field">
                <label className="form-label">API Key</label>
                <div className="input-suffix">
                  <input
                    className="input"
                    type={showKey ? 'text' : 'password'}
                    placeholder={
                      provider?.api_key_env
                        ? `设置 ${provider.api_key_env}（全局保存）`
                        : '设置 API Key'
                    }
                    value={presetKeyInput}
                    onChange={(e) => setPresetKeyInput(e.target.value)}
                  />
                  <button
                    className="suffix-btn"
                    title={showKey ? '隐藏' : '显示'}
                    onClick={() => setShowKey((v) => !v)}
                  >
                    <Icon name={showKey ? 'eyeSlash' : 'eye'} size={14} />
                  </button>
                </div>
                <p className="form-hint">密钥仅保存在本地 ~/.aigent/llmconfig.json。</p>
              </div>

              <ModelListEditor
                models={models}
                checkedIds={checkedIds}
                onToggle={toggleModel}
                onEdit={setEditingModel}
                onRefresh={refresh}
                refreshing={refreshing}
                onAdd={() => setAddModelOpen(true)}
                knownIds={knownIds}
                onCheckAll={checkAll}
                emptyText="没有匹配的模型"
              />

              <div className="form-field adv">
                <button
                  type="button"
                  className="ammodal-adv-toggle"
                  onClick={() => setCompatOpen((v) => !v)}
                >
                  兼容设置（通常不用改）
                  <span className={`select-caret ${compatOpen ? 'open' : ''}`}>▾</span>
                </button>
                {compatOpen && (
                  <div className="adv-body">
                    <div className="adv-group">
                      <div className="adv-group-title">模型列表接口</div>
                      <input
                        className="input"
                        value={modelsPath}
                        onChange={(e) => setModelsPath(e.target.value)}
                      />
                      <p className="form-hint">
                        刷新模型列表时请求 {effectiveBaseUrl || 'API 地址'}
                        {modelsPath || DEFAULT_MODELS_PATH}
                      </p>
                    </div>
                  </div>
                )}
              </div>

              <div className="addprovider-foot">
                <button className="btn btn-primary" disabled={!valid || saving} onClick={submit}>
                  {saving ? '保存中…' : '添加供应商'}
                </button>
              </div>
            </section>
          </div>
        ) : (
          <div className="addmodel-body addprovider-custom">
            <div className="form-field">
              <label className="form-label">自定义供应商名称</label>
              <input
                className="input"
                placeholder="例如 my-proxy"
                value={customName}
                onChange={(e) => setCustomName(e.target.value)}
              />
            </div>

            <div className="form-field">
              <label className="form-label">API 地址</label>
              <input
                className="input"
                placeholder="例如 https://api.openai.com/v1/chat/completions"
                value={customBaseUrl}
                onChange={(e) => setCustomBaseUrl(e.target.value)}
              />
              <p className="form-hint">填写完整请求地址：系统将原样使用。</p>
            </div>

            <div className="form-field">
              <label className="form-label">API 格式</label>
              <select
                className="input"
                value={customFormat}
                onChange={(e) => setCustomFormat(e.target.value)}
              >
                {formats.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.label}
                  </option>
                ))}
              </select>
            </div>

            <div className="form-field">
              <label className="form-label">API Key</label>
              <div className="input-suffix">
                <input
                  className="input"
                  type={showKey ? 'text' : 'password'}
                  placeholder="API Key"
                  value={customKey}
                  onChange={(e) => setCustomKey(e.target.value)}
                />
                <button
                  className="suffix-btn"
                  title={showKey ? '隐藏' : '显示'}
                  onClick={() => setShowKey((v) => !v)}
                >
                  <Icon name={showKey ? 'eyeSlash' : 'eye'} size={14} />
                </button>
              </div>
            </div>

            <ModelListEditor
              models={models}
              checkedIds={checkedIds}
              onToggle={toggleModel}
              onEdit={(m) => setModels((cur) => cur.map((x) => (x.id === m.id ? m : x)))}
              onRefresh={refresh}
              refreshing={refreshing}
              onAdd={() => setAddModelOpen(true)}
              knownIds={knownIds}
              onCheckAll={checkAll}
              emptyText="没有匹配的模型"
            />

            <div className="form-field adv">
              <button
                type="button"
                className="ammodal-adv-toggle"
                onClick={() => setCompatOpen((v) => !v)}
              >
                兼容设置（通常不用改）
                <span className={`select-caret ${compatOpen ? 'open' : ''}`}>▾</span>
              </button>
              {compatOpen && (
                <div className="adv-body">
                  <div className="adv-group">
                    <div className="adv-group-title">模型列表接口</div>
                    <input
                      className="input"
                      value={modelsPath}
                      onChange={(e) => setModelsPath(e.target.value)}
                    />
                    <p className="form-hint">
                      刷新模型列表时请求 {effectiveBaseUrl || 'API 地址'}
                      {modelsPath || DEFAULT_MODELS_PATH}
                    </p>
                  </div>
                </div>
              )}
            </div>

            <div className="addprovider-foot">
              <span className="addmodel-foot-hint">
                {models.length ? `已选择 ${checkedIds.length} 个` : '无未保存更改'}
              </span>
              <button className="btn" onClick={onCancel} disabled={saving}>
                取消
              </button>
              <button className="btn btn-primary" disabled={!valid || saving} onClick={submit}>
                {saving ? '保存中…' : '保存更改'}
              </button>
            </div>
          </div>
        )}
      </div>

      {addModelOpen && (
        <AddModelModal
          providerPreset={tab === 'preset' ? provider ?? null : null}
          onCancel={() => setAddModelOpen(false)}
          onSubmit={(m) => {
            setModels((cur) => [...cur, m])
            setAddModelOpen(false)
          }}
        />
      )}

      {editingModel && (
        <AddModelModal
          initial={editingModel}
          providerPreset={tab === 'preset' ? provider ?? null : null}
          onCancel={() => setEditingModel(null)}
          onSubmit={applyModelEdit}
          onDelete={(id) => {
            setModels((cur) => cur.filter((x) => x.id !== id))
            setEditingModel(null)
          }}
        />
      )}
    </div>
  )
}
