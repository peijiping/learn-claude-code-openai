import { useEffect, useMemo, useRef, useState } from 'react'
import { Icon } from '@components/common/Icon'
import { showToast, useAgentStore } from '@store/agentStore'
import type { LlmApiFormat, LlmConnection, LlmConnectionModel } from '@protocols/agentProtocol'
import AddModelModal from './AddModelModal'
import AddProviderModal from './AddProviderModal'
import ModelListEditor from './ModelListEditor'
import { DEFAULT_MODELS_PATH, genId, knownModelIdsOf, providerInitial } from './llmShared'

const FALLBACK_FORMATS: LlmApiFormat[] = [
  { id: 'chat_completions', label: 'Chat Completions (/chat/completions)' }
]

/** 模型管理页（master-detail）：左侧已添加的「模型服务（连接）」列表，右侧该连接的端点/密钥/模型列表。
 * 编辑先落草稿，「保存更改」才写盘并触发热切换；「刷新」经后端调 GET {base_url}/models 拉取模型列表。 */
export default function ModelSettings(): JSX.Element {
  const llmConfig = useAgentStore((s) => s.llmConfig)
  const llmSaving = useAgentStore((s) => s.llmSaving)
  const saveLlConfig = useAgentStore((s) => s.saveLlConfig)
  const fetchModels = useAgentStore((s) => s.fetchModels)
  const loadLlConfig = useAgentStore((s) => s.loadLlConfig)

  const providers = llmConfig?.providers ?? {}
  const formats = llmConfig?.api_formats?.length ? llmConfig.api_formats : FALLBACK_FORMATS

  const [draft, setDraft] = useState<LlmConnection[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [search, setSearch] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [compatOpen, setCompatOpen] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [addProviderOpen, setAddProviderOpen] = useState(false)
  const [modelModal, setModelModal] = useState<{ model: LlmConnectionModel | null } | null>(null)
  const moreRef = useRef<HTMLDivElement>(null)

  // 首次进入拉取配置（后端未就绪时静默）
  useEffect(() => {
    if (!llmConfig) void loadLlConfig()
  }, [llmConfig, loadLlConfig])

  // 外部配置更新且当前无未保存更改时，同步进草稿
  useEffect(() => {
    if (!llmConfig || dirty) return
    const conns = llmConfig.connections ?? []
    setDraft(conns)
    setActiveId(llmConfig.active_model_id ?? null)
    setSelectedId((cur) => (cur && conns.some((c) => c.id === cur) ? cur : conns[0]?.id ?? null))
  }, [llmConfig, dirty])

  useEffect(() => {
    if (!moreOpen) return
    const onDoc = (e: MouseEvent): void => {
      if (moreRef.current && !moreRef.current.contains(e.target as Node)) setMoreOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [moreOpen])

  const conn = useMemo(
    () => draft.find((c) => c.id === selectedId) ?? null,
    [draft, selectedId]
  )
  const providerPreset = conn?.provider ? providers[conn.provider] ?? null : null
  // 自定义供应商没有预置目录可对照 → undefined（不做「能力未识别」提示）
  const knownIds = useMemo(
    () => (providerPreset ? knownModelIdsOf(providerPreset) : undefined),
    [providerPreset]
  )
  const checkedIds = (conn?.models ?? []).filter((m) => m.enabled).map((m) => m.id)

  const filteredConns = draft.filter(
    (c) => !search.trim() || c.name.toLowerCase().includes(search.trim().toLowerCase())
  )

  // ── 草稿编辑 ─────────────────────────────────────────────
  const patchConn = (patch: Partial<LlmConnection>): void => {
    if (!conn) return
    setDraft((cur) => cur.map((c) => (c.id === conn.id ? { ...c, ...patch } : c)))
    setDirty(true)
  }
  const patchModels = (models: LlmConnectionModel[]): void => patchConn({ models })

  const toggleModel = (id: string): void =>
    patchModels((conn?.models ?? []).map((m) => (m.id === id ? { ...m, enabled: !m.enabled } : m)))

  const checkAll = (checked: boolean): void =>
    patchModels((conn?.models ?? []).map((m) => ({ ...m, enabled: checked })))

  const setActive = (id: string): void => {
    setActiveId((cur) => (cur === id ? null : id))
    setDirty(true)
  }

  const refresh = async (): Promise<void> => {
    if (!conn) return
    if (!conn.base_url.trim()) {
      showToast('请先填写 API 地址', 'error', 3000)
      return
    }
    setRefreshing(true)
    const path = String(conn.compat?.models_path || DEFAULT_MODELS_PATH)
    const ids = await fetchModels({
      base_url: conn.base_url,
      api_key: conn.api_key,
      connection_id: conn.id,
      api_format: conn.api_format,
      models_path: path
    })
    setRefreshing(false)
    if (!ids.length) return
    const existing = new Set(conn.models.map((m) => m.model))
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
    if (added.length) patchModels([...conn.models, ...added])
    showToast(`已加载 ${ids.length} 个模型，新增 ${added.length} 个`, 'info')
  }

  const duplicate = (): void => {
    if (!conn) return
    const copy: LlmConnection = {
      ...conn,
      id: genId('c_'),
      name: `${conn.name} 副本`,
      provider: conn.provider.startsWith('custom:') ? conn.provider : `custom:${conn.provider}-copy`,
      custom: true,
      models: conn.models.map((m) => ({ ...m, id: genId('m_') }))
    }
    setDraft((cur) => {
      const i = cur.findIndex((c) => c.id === conn.id)
      const next = [...cur]
      next.splice(i + 1, 0, copy)
      return next
    })
    setSelectedId(copy.id)
    setDirty(true)
    setMoreOpen(false)
  }

  const removeConn = (): void => {
    if (!conn) return
    const removedModelIds = new Set(conn.models.map((m) => m.id))
    const next = draft.filter((c) => c.id !== conn.id)
    setDraft(next)
    setSelectedId(next[0]?.id ?? null)
    if (activeId && removedModelIds.has(activeId)) setActiveId(null)
    setDirty(true)
    setMoreOpen(false)
  }

  const revert = (): void => {
    const conns = llmConfig?.connections ?? []
    setDraft(conns)
    setActiveId(llmConfig?.active_model_id ?? null)
    setSelectedId((cur) => (cur && conns.some((c) => c.id === cur) ? cur : conns[0]?.id ?? null))
    setDirty(false)
  }

  const save = async (): Promise<void> => {
    await saveLlConfig({ active_model_id: activeId, connections: draft })
    setDirty(false)
  }

  const addConnection = (c: LlmConnection): void => {
    setDraft((cur) => [...cur, c])
    setSelectedId(c.id)
    if (!activeId && c.models.length) setActiveId(c.models[0].id)
    setDirty(true)
    setAddProviderOpen(false)
  }

  const submitModel = (m: LlmConnectionModel): void => {
    if (!conn) return
    const exists = conn.models.some((x) => x.id === m.id)
    patchModels(exists ? conn.models.map((x) => (x.id === m.id ? m : x)) : [...conn.models, m])
    if (!activeId) setActiveId(m.id)
    setModelModal(null)
  }

  const removeModel = (id: string): void => {
    if (!conn) return
    patchModels(conn.models.filter((m) => m.id !== id))
    if (activeId === id) setActiveId(null)
    setModelModal(null)
  }

  return (
    <div className="model-settings">
      {/* 左：已添加的模型服务 */}
      <aside className="msvc-rail">
        <input
          className="input msvc-search"
          placeholder="搜索已添加连接"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="msvc-list">
          {filteredConns.map((c) => (
            <button
              key={c.id}
              className={`msvc-item ${c.id === selectedId ? 'active' : ''}`}
              onClick={() => {
                setSelectedId(c.id)
                setRenaming(false)
                setCompatOpen(false)
                setShowKey(false)
              }}
            >
              <span className="provider-avatar">{providerInitial(c.name)}</span>
              <span className="msvc-item-name" title={c.name}>
                {c.name}
              </span>
              <span className={`msvc-status ${c.api_key ? 'on' : ''}`} title={c.api_key ? '已设置密钥' : '未设置密钥'} />
            </button>
          ))}
          {draft.length === 0 && (
            <div className="msvc-empty">还没有模型服务，点击下方按钮添加。</div>
          )}
          {draft.length > 0 && filteredConns.length === 0 && (
            <div className="msvc-empty">没有匹配的连接。</div>
          )}
        </div>
        <button className="btn msvc-add" onClick={() => setAddProviderOpen(true)}>
          <Icon name="plus" size={14} /> 添加模型服务
        </button>
      </aside>

      {/* 右：连接详情 */}
      <section className="msvc-detail">
        {!conn ? (
          <div className="msvc-placeholder">
            <p>模型管理</p>
            <span>添加一个模型服务（如 DeepSeek、硅基流动或自定义中转）后即可维护模型。</span>
          </div>
        ) : (
          <>
            <header className="msvc-head">
              {renaming ? (
                <input
                  className="input msvc-title-input"
                  autoFocus
                  value={conn.name}
                  onChange={(e) => patchConn({ name: e.target.value })}
                  onBlur={() => setRenaming(false)}
                  onKeyDown={(e) => e.key === 'Enter' && setRenaming(false)}
                />
              ) : (
                <h3 className="msvc-title">{conn.name}</h3>
              )}
              <button className="icon-btn" title="重命名" onClick={() => setRenaming(true)}>
                <Icon name="edit" size={13} />
              </button>
              <span className={`msvc-keychip ${conn.api_key ? '' : 'off'}`}>
                {conn.api_key ? '已设置密钥' : '未设置密钥'}
              </span>
              <span className="msvc-head-spacer" />
              <button className="icon-btn" title="复制此模型服务" onClick={duplicate}>
                <Icon name="copy" size={15} />
              </button>
              <div className="mlist-more" ref={moreRef}>
                <button className="icon-btn" title="更多" onClick={() => setMoreOpen((v) => !v)}>
                  <Icon name="more" size={15} />
                </button>
                {moreOpen && (
                  <div className="mlist-more-menu">
                    <button className="mlist-more-item" onClick={duplicate}>
                      复制此模型服务
                    </button>
                    <button className="mlist-more-item danger" onClick={removeConn}>
                      删除此模型服务
                    </button>
                  </div>
                )}
              </div>
            </header>

            <div className="msvc-body">
              <div className="form-field">
                <label className="form-label">API 地址</label>
                <input
                  className="input"
                  placeholder="https://api.deepseek.com/v1"
                  value={conn.base_url}
                  onChange={(e) => patchConn({ base_url: e.target.value })}
                />
                <p className="form-hint">填写完整请求地址：系统将原样使用。</p>
              </div>

              <div className="form-field">
                <label className="form-label">API 格式</label>
                <select
                  className="input"
                  value={conn.api_format}
                  onChange={(e) => patchConn({ api_format: e.target.value })}
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
                    value={conn.api_key}
                    onChange={(e) => patchConn({ api_key: e.target.value })}
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

              <hr className="msvc-divider" />

              <ModelListEditor
                models={conn.models}
                checkedIds={checkedIds}
                onToggle={toggleModel}
                onEdit={(m) => setModelModal({ model: m })}
                onRefresh={refresh}
                refreshing={refreshing}
                onAdd={() => setModelModal({ model: null })}
                knownIds={knownIds}
                onCheckAll={checkAll}
                activeId={activeId}
                onSetActive={setActive}
                emptyText="没有匹配的模型"
              />

              <div className="form-field adv msvc-compat">
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
                        value={String(conn.compat?.models_path || DEFAULT_MODELS_PATH)}
                        onChange={(e) =>
                          patchConn({ compat: { ...(conn.compat ?? {}), models_path: e.target.value } })
                        }
                      />
                      <p className="form-hint">
                        「刷新」时请求 {conn.base_url || 'API 地址'}
                        {String(conn.compat?.models_path || DEFAULT_MODELS_PATH)}
                      </p>
                    </div>
                  </div>
                )}
              </div>
            </div>

            <footer className="msvc-foot">
              <span className={`msvc-foot-status ${dirty ? 'dirty' : ''}`}>
                {dirty ? '未保存更改' : '无未保存更改'}
              </span>
              <button className="btn" disabled={!dirty || llmSaving} onClick={revert}>
                取消
              </button>
              <button className="btn btn-primary" disabled={!dirty || llmSaving} onClick={() => void save()}>
                {llmSaving ? '保存中…' : '保存更改'}
              </button>
            </footer>
          </>
        )}
      </section>

      {addProviderOpen && (
        <AddProviderModal
          providers={providers}
          apiFormats={formats}
          saving={llmSaving}
          onCancel={() => setAddProviderOpen(false)}
          onSubmit={addConnection}
          onFetchModels={fetchModels}
        />
      )}

      {modelModal && conn && (
        <AddModelModal
          initial={modelModal.model}
          providerPreset={providerPreset}
          saving={llmSaving}
          onCancel={() => setModelModal(null)}
          onSubmit={submitModel}
          onDelete={removeModel}
        />
      )}
    </div>
  )
}
