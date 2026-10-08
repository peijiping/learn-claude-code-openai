import { useEffect, useRef, useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import type { PluginMarketItem } from '@protocols/agentProtocol'

/** 插件市场浏览（设置弹窗「插件」→ 从市场安装），docs/frontend/25。
 *
 * 数据源形态很简单：**一个市场 = 一个 git 仓库 + 根目录一份
 * `.claude-plugin/marketplace.json`**（Claude Code 的既定事实标准，所以
 * "兼容即市场"）。实测官方市场那份目录里有 314 个插件、184KB，一次 raw 拉下来即可。
 *
 * 三条必须守住的点（与 MCP / 技能市场同源）：
 * 1. **「搜失败」≠「搜不到」**：`error` 非空时 `items` 必为空，分开渲染。
 * 2. **来源标记不是安全评级**：官方市场做了筛查，社区 / 任意第三方仓库只做格式校验。
 *    文案必须这么讲。
 * 3. **安装进行中禁用按钮**：主进程 pending 表按 kind FIFO 配对且无 id。
 */

const PUBLISHER_VIEW: Record<string, { label: string; cls: string }> = {
  official: { label: '官方', cls: 'official' },
  community: { label: '社区', cls: 'community' }
}

const EMPTY_ENTRY = { id: '', name: '', repo: '', ref: '', enabled: true }

interface Props {
  onBack: () => void
  onPick: (item: PluginMarketItem) => void
}

export default function PluginMarketView({ onBack, onPick }: Props): JSX.Element {
  const config = useAgentStore((s) => s.pluginConfig)
  const market = useAgentStore((s) => s.pluginMarket)
  const loading = useAgentStore((s) => s.pluginMarketLoading)
  const saving = useAgentStore((s) => s.pluginSaving)
  const searchPluginMarket = useAgentStore((s) => s.searchPluginMarket)
  const upsertPluginMarket = useAgentStore((s) => s.upsertPluginMarket)
  const removePluginMarket = useAgentStore((s) => s.removePluginMarket)

  const markets = config?.markets ?? []
  const [sourceId, setSourceId] = useState(
    () => market?.market_id || config?.default_market || ''
  )
  const [query, setQuery] = useState('')
  const [showSources, setShowSources] = useState(false)
  const [draft, setDraft] = useState<Record<string, unknown>>({ ...EMPTY_ENTRY })
  const debounce = useRef<number | undefined>(undefined)
  const seq = useRef(0)

  useEffect(() => {
    if (!market && sourceId) void searchPluginMarket(sourceId, '')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => () => window.clearTimeout(debounce.current), [])

  const runSearch = (q: string): void => {
    window.clearTimeout(debounce.current)
    debounce.current = window.setTimeout(() => {
      seq.current += 1
      void searchPluginMarket(sourceId, q.trim())
    }, 450)
  }

  const switchSource = (id: string): void => {
    setSourceId(id)
    seq.current += 1
    window.clearTimeout(debounce.current)
    void searchPluginMarket(id, query.trim())
  }

  const items = market?.items ?? []
  const error = market?.error ?? ''
  const hasMore = !!market?.next_cursor
  const mySeq = seq.current
  const active = markets.find((m) => m.id === sourceId)

  return (
    <div className="mcp">
      <div className="mcp-form-head">
        <button type="button" className="mcp-back" onClick={onBack}>
          ← 返回列表
        </button>
        <span className="mcp-form-title">从市场安装</span>
        {market && !market.error && (
          <span className="mcp-hint">
            {market.elapsed_ms}ms · 共 {market.total} 个插件
            {market.catalog_name ? ` · ${market.catalog_name}` : ''}
          </span>
        )}
      </div>

      <div className="mcp-market-src">
        <select
          className="mcp-input"
          value={sourceId}
          disabled={loading || saving}
          onChange={(e) => switchSource(e.target.value)}
        >
          {markets.map((m) => (
            <option key={m.id} value={m.id} disabled={!m.enabled}>
              {m.name}
              {m.enabled ? '' : '（已停用）'}
            </option>
          ))}
          {markets.length === 0 && <option value="">（没有可用的市场）</option>}
        </select>
        <button type="button" className="btn btn-sm" onClick={() => setShowSources((v) => !v)}>
          {showSources ? '收起源管理' : '管理市场'}
        </button>
      </div>
      {active?.repo && (
        <p className="mcp-inline-note">
          目录仓库 <code>{active.repo}</code>
          {active.note ? ` · ${active.note}` : ''}
        </p>
      )}

      {showSources && (
        <section className="perm-sec">
          <h4 className="perm-sec-title">插件市场</h4>
          <p className="perm-sec-desc">
            任何 <b>git 仓库 + 根目录一份 <code>.claude-plugin/marketplace.json</code></b>{' '}
            都是一个市场。内置市场只能停用、不能删除。
          </p>
          <div className="mcp-src-list">
            {markets.map((m) => (
              <div className={`mcp-src-row ${m.id === sourceId ? 'active' : ''}`} key={m.id}>
                <div className="mcp-src-info">
                  <span className="mcp-src-name">
                    {m.name}
                    {m.builtin && <span className="mcp-tag">内置</span>}
                  </span>
                  <span className="mcp-src-meta">
                    {m.repo}
                    {m.ref && m.ref !== 'main' ? ` @${m.ref}` : ''}
                  </span>
                </div>
                <button
                  type="button"
                  className="mcp-act"
                  disabled={saving}
                  onClick={() => switchSource(m.id)}
                >
                  选它
                </button>
                {!m.builtin && (
                  <button
                    type="button"
                    className="mcp-act danger"
                    disabled={saving}
                    title="删除这个自定义市场（不会卸载已安装的插件）"
                    onClick={() => void removePluginMarket(m.id)}
                  >
                    删除
                  </button>
                )}
                <button
                  type="button"
                  className={`switch ${m.enabled ? 'on' : ''}`}
                  disabled={saving}
                  title={m.enabled ? '停用（选择框里不再可用）' : '启用'}
                  onClick={() =>
                    void upsertPluginMarket({ id: m.id, name: m.name, repo: m.repo, enabled: !m.enabled })
                  }
                >
                  <span className="switch-knob" />
                </button>
              </div>
            ))}
          </div>

          <div className="mcp-field" style={{ marginTop: 'var(--sp-3)' }}>
            <label className="mcp-label">
              添加市场
              <span className="mcp-hint">
                仓库地址写 <code>owner/name</code>，或完整 GitHub URL
              </span>
            </label>
            <div className="mcp-kv-row">
              <input
                className="mcp-input mono"
                placeholder="id（英文，如 my-team-plugins）"
                value={String(draft.id ?? '')}
                spellCheck={false}
                onChange={(e) => setDraft((d) => ({ ...d, id: e.target.value }))}
              />
              <input
                className="mcp-input"
                placeholder="显示名称"
                value={String(draft.name ?? '')}
                onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
              />
            </div>
            <div className="mcp-kv-row">
              <input
                className="mcp-input mono"
                placeholder="owner/name"
                value={String(draft.repo ?? '')}
                spellCheck={false}
                onChange={(e) => setDraft((d) => ({ ...d, repo: e.target.value }))}
              />
              <input
                className="mcp-input mono"
                placeholder="分支（默认 main）"
                value={String(draft.ref ?? '')}
                spellCheck={false}
                onChange={(e) => setDraft((d) => ({ ...d, ref: e.target.value }))}
              />
            </div>
            <div className="mcp-kv-row">
              <button
                type="button"
                className="btn btn-sm"
                disabled={saving || !String(draft.repo ?? '').trim()}
                onClick={() => void upsertPluginMarket(draft)}
              >
                {saving ? '保存中…' : '添加市场'}
              </button>
              <span className="mcp-hint">保存失败的原因会内联显示在列表页顶部</span>
            </div>
          </div>
        </section>
      )}

      <div className="mcp-market-search">
        <input
          className="mcp-input"
          placeholder="搜索插件，例如 code-review / security / frontend"
          value={query}
          spellCheck={false}
          onChange={(e) => {
            setQuery(e.target.value)
            runSearch(e.target.value)
          }}
        />
        <button
          type="button"
          className="btn btn-sm"
          disabled={loading || !sourceId}
          onClick={() => {
            window.clearTimeout(debounce.current)
            seq.current += 1
            void searchPluginMarket(sourceId, query.trim())
          }}
        >
          {loading ? '搜索中…' : '搜索'}
        </button>
      </div>

      <p className="mcp-risk">
        插件能带 <b>钩子与 MCP 服务器</b>（= 在本机跑代码）。官方市场经质量与安全筛查，
        <b>社区与任意第三方仓库只做格式校验</b>。被列出来不等于安全 ——
        安装确认页会列出全部组件与 <code>plugin.json</code> 原文。
      </p>

      {error && <p className="sbx-error">{error}</p>}

      {loading && items.length === 0 && <div className="perm-loading">正在读取市场目录…</div>}

      {!loading && !error && items.length === 0 && (
        <div className="perm-empty">
          {query.trim()
            ? `在「${active?.name ?? sourceId}」里没有匹配「${query.trim()}」的插件。`
            : '该市场当前没有返回任何插件。'}
        </div>
      )}

      {items.length > 0 && (
        <div className="mcp-market-list">
          {items.map((it) => {
            const pub = PUBLISHER_VIEW[it.publisher] ?? PUBLISHER_VIEW.community
            return (
              <div className="mcp-market-item" key={it.id}>
                <div className="mcp-market-head">
                  <span className="mcp-market-title" title={it.id}>
                    {it.display_name || it.name}
                  </span>
                  {it.version && <span className="mcp-tag">{it.version}</span>}
                  <span className={`mcp-tag src ${pub.cls}`}>{pub.label}</span>
                  <span className="mcp-tag">{it.source_label || it.source_kind}</span>
                  {it.category && <span className="mcp-tag">{it.category}</span>}
                </div>
                <p className="mcp-market-id mono">
                  {it.name}
                  {it.author ? ` · ${it.author}` : ''}
                  {it.repo ? ` · ${it.repo}` : ''}
                </p>
                {it.description && <p className="mcp-market-desc">{it.description}</p>}
                <div className="mcp-market-actions">
                  {it.homepage && (
                    <span className="mcp-market-repo mono" title={it.homepage}>
                      {it.homepage}
                    </span>
                  )}
                  <button
                    type="button"
                    className={`btn btn-sm ${it.installable ? 'btn-primary' : ''}`}
                    disabled={!it.installable}
                    title={it.installable ? '查看组件清单并确认安装' : it.reason}
                    onClick={() => onPick(it)}
                  >
                    {it.installable ? '安装' : '不支持'}
                  </button>
                </div>
                {!it.installable && it.reason && <p className="mcp-inline-warn">{it.reason}</p>}
              </div>
            )
          })}
        </div>
      )}

      {hasMore && (
        <button
          type="button"
          className="btn"
          disabled={loading}
          onClick={() => {
            if (mySeq === seq.current) {
              void searchPluginMarket(sourceId, query.trim(), market?.next_cursor)
            }
          }}
        >
          {loading ? '加载中…' : `加载更多（已显示 ${items.length} / ${market?.total ?? 0}）`}
        </button>
      )}
    </div>
  )
}
