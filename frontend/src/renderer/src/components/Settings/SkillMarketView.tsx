import { useEffect, useRef, useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import type { SkillMarketItem } from '@protocols/agentProtocol'

/** 技能市场浏览（设置弹窗「技能」→ 从市场安装），docs/frontend/24。
 *
 * **技能这一侧没有 MCP registry 那样的单一官方源** —— 所以这个页面比 MCP 市场多
 * 一个「源」维度：先在顶部选一个源，再在它里面搜。源列表由后端下发
 * （`skillConfig.markets`），内置源只能停用、不能删。
 *
 * 三条必须守住的点（与 MCP 市场同源，改之前先读）：
 * 1. **「搜失败」≠「搜不到」**：`error` 非空时 `items` 必为空，两者要分开渲染 ——
 *    把超时渲染成"没有结果"会让用户以为这个技能不存在。
 * 2. **首次搜索很慢**：git 源要扫一次仓库树 + 抓若干 SKILL.md；第三方 API 实测
 *    能到十几秒。必须有明确的加载态，否则用户会以为卡死。
 * 3. **来源标记不是安全评级**：三档（官方/社区/第三方）**都没有代码审计**。
 *    文案必须这么讲，不能让人误以为"官方"= 安全。
 */

const PUBLISHER_VIEW: Record<string, { label: string; cls: string }> = {
  official: { label: '官方', cls: 'official' },
  community: { label: '社区', cls: 'community' },
  'third-party': { label: '第三方', cls: 'domain' }
}

const TYPE_LABEL: Record<string, string> = {
  git: 'git 仓库',
  api: '第三方 API',
  index: 'JSON 索引'
}

interface Props {
  onBack: () => void
  /** 点「安装」——由容器去抓安装计划并切到确认视图 */
  onPick: (item: SkillMarketItem) => void
}

/** 新建源的字段（git / api / index 三种类型各需要不同字段，故一次全给、由后端校验） */
const EMPTY_ENTRY = {
  id: '',
  name: '',
  type: 'git',
  repo: '',
  ref: '',
  provider: 'ruleskill',
  base_url: '',
  url: '',
  enabled: true
}

export default function SkillMarketView({ onBack, onPick }: Props): JSX.Element {
  const config = useAgentStore((s) => s.skillConfig)
  const market = useAgentStore((s) => s.skillMarket)
  const loading = useAgentStore((s) => s.skillMarketLoading)
  const saving = useAgentStore((s) => s.skillSaving)
  const searchSkillMarket = useAgentStore((s) => s.searchSkillMarket)
  const upsertSkillMarket = useAgentStore((s) => s.upsertSkillMarket)
  const removeSkillMarket = useAgentStore((s) => s.removeSkillMarket)

  const markets = config?.markets ?? []
  const [sourceId, setSourceId] = useState(() => market?.market_id || config?.default_market || '')
  const [query, setQuery] = useState('')
  const [showSources, setShowSources] = useState(false)
  const [draft, setDraft] = useState<Record<string, unknown>>({ ...EMPTY_ENTRY })
  const debounce = useRef<number | undefined>(undefined)
  const seq = useRef(0)

  // 进页先拉一页（空查询 = 全部条目），让面板立刻有内容而不是空白等用户输入
  useEffect(() => {
    if (!market && sourceId) void searchSkillMarket(sourceId, '')
    // 只在首次挂载时触发，之后由用户输入 / 换源 / 翻页驱动
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => () => window.clearTimeout(debounce.current), [])

  const runSearch = (q: string): void => {
    window.clearTimeout(debounce.current)
    debounce.current = window.setTimeout(() => {
      seq.current += 1
      void searchSkillMarket(sourceId, q.trim())
    }, 450)
  }

  const switchSource = (id: string): void => {
    setSourceId(id)
    seq.current += 1
    window.clearTimeout(debounce.current)
    void searchSkillMarket(id, query.trim())
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
        {market?.cached && <span className="mcp-hint">（缓存结果）</span>}
        {market && !market.error && (
          <span className="mcp-hint">
            {market.elapsed_ms}ms · 共 {market.total} 条
          </span>
        )}
      </div>

      {/* ── 源选择（本页与 MCP 市场的最大差别：技能没有单一官方源）── */}
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
              {m.type ? ` · ${TYPE_LABEL[m.type] ?? m.type}` : ''}
            </option>
          ))}
          {markets.length === 0 && <option value="">（没有可用的源）</option>}
        </select>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => setShowSources((v) => !v)}
        >
          {showSources ? '收起源管理' : '管理源'}
        </button>
      </div>
      {active?.note && <p className="mcp-inline-note">{active.note}</p>}

      {showSources && (
        <section className="perm-sec">
          <h4 className="perm-sec-title">市场源</h4>
          <p className="perm-sec-desc">
            内置源只能停用、不能删除（它们的身份字段以程序为准，改一次默认地址不必让你去编辑
            JSON）。自定义源支持三种：<code>git</code> 仓库（扫其中所有{' '}
            <code>SKILL.md</code>）、第三方 <code>api</code>、自建 <code>index</code>。
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
                    {m.type_label ?? TYPE_LABEL[m.type] ?? m.type}
                    {m.repo ? ` · ${m.repo}` : ''}
                    {m.url ? ` · ${m.url}` : ''}
                    {m.base_url ? ` · ${m.base_url}` : ''}
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
                    title="删除这个自定义源（不会卸载已安装的技能）"
                    onClick={() => void removeSkillMarket(m.id)}
                  >
                    删除
                  </button>
                )}
                <button
                  type="button"
                  className={`switch ${m.enabled ? 'on' : ''}`}
                  disabled={saving}
                  title={m.enabled ? '停用（搜索时不再出现）' : '启用'}
                  onClick={() =>
                    void upsertSkillMarket({ id: m.id, name: m.name, enabled: !m.enabled })
                  }
                >
                  <span className="switch-knob" />
                </button>
              </div>
            ))}
          </div>

          {/* 新增自定义源 —— 字段全给，由后端按 type 逐项校验（校验规则只应有一处） */}
          <div className="mcp-field" style={{ marginTop: 'var(--sp-3)' }}>
            <label className="mcp-label">
              新增源
              <span className="mcp-hint">git 填 repo；api 选 provider；index 填 url</span>
            </label>
            <div className="mcp-kv-row">
              <input
                className="mcp-input mono"
                placeholder="id（英文，如 my-team-skills）"
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
              <select
                className="mcp-input"
                value={String(draft.type ?? 'git')}
                onChange={(e) => setDraft((d) => ({ ...d, type: e.target.value }))}
              >
                <option value="git">git 仓库</option>
                <option value="api">第三方 API</option>
                <option value="index">JSON 索引</option>
              </select>
              {draft.type === 'git' && (
                <>
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
                </>
              )}
              {draft.type === 'api' && (
                <select
                  className="mcp-input"
                  value={String(draft.provider ?? 'ruleskill')}
                  onChange={(e) => setDraft((d) => ({ ...d, provider: e.target.value }))}
                >
                  <option value="ruleskill">RuleSkill</option>
                  <option value="openpaths">OpenPaths</option>
                </select>
              )}
              {draft.type === 'index' && (
                <input
                  className="mcp-input mono"
                  placeholder="https://…/index.json"
                  value={String(draft.url ?? '')}
                  spellCheck={false}
                  onChange={(e) => setDraft((d) => ({ ...d, url: e.target.value }))}
                />
              )}
            </div>
            <div className="mcp-kv-row">
              <button
                type="button"
                className="btn btn-sm"
                disabled={saving || !String(draft.id ?? '').trim()}
                onClick={() => void upsertSkillMarket(draft)}
              >
                {saving ? '保存中…' : '添加源'}
              </button>
              <span className="mcp-hint">
                保存失败的原因会内联显示在列表页顶部（不弹 toast）
              </span>
            </div>
          </div>
        </section>
      )}

      <div className="mcp-market-search">
        <input
          className="mcp-input"
          placeholder="按名称 / 描述搜索，例如 pdf / review / 数据分析"
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
          // 串行化：主进程 pending 表按 kind FIFO 配对且无 id，同 kind 并发会串台
          disabled={loading || !sourceId}
          onClick={() => {
            window.clearTimeout(debounce.current)
            seq.current += 1
            void searchSkillMarket(sourceId, query.trim())
          }}
        >
          {loading ? '搜索中…' : '搜索'}
        </button>
      </div>

      <p className="mcp-risk">
        技能正文是<b>写给我们模型看的指令</b>（不是可执行文件，但能指挥模型去执行东西），
        而这些源<b>都没有代码审计</b>。被列出来不等于安全 —— 安装确认页会展示
        <b>完整的 SKILL.md 原文</b>，那是你判断的唯一依据。首次搜索可能要十几秒。
      </p>

      {error && <p className="sbx-error">{error}</p>}

      {loading && items.length === 0 && <div className="perm-loading">正在搜索市场…</div>}

      {!loading && !error && items.length === 0 && (
        <div className="perm-empty">
          {query.trim()
            ? `在「${active?.name ?? sourceId}」里没有匹配「${query.trim()}」的技能。`
            : '该源当前没有返回任何条目（换个源或关键词试试）。'}
        </div>
      )}

      {items.length > 0 && (
        <div className="mcp-market-list">
          {items.map((it) => {
            const pub = PUBLISHER_VIEW[it.publisher] ?? PUBLISHER_VIEW['third-party']
            return (
              <div className="mcp-market-item" key={it.id}>
                <div className="mcp-market-head">
                  <span className="mcp-market-title" title={it.id}>
                    {it.name}
                  </span>
                  {it.version && <span className="mcp-tag">{it.version}</span>}
                  <span className={`mcp-tag src ${pub.cls}`}>{pub.label}</span>
                  {(it.tags ?? []).slice(0, 4).map((t) => (
                    <span className="mcp-tag" key={t}>
                      {t}
                    </span>
                  ))}
                </div>
                <p className="mcp-market-id mono">{it.path || it.id}</p>
                {it.description && <p className="mcp-market-desc">{it.description}</p>}
                <div className="mcp-market-actions">
                  {it.url && (
                    <span className="mcp-market-repo mono" title={it.url}>
                      {it.url}
                    </span>
                  )}
                  <button
                    type="button"
                    // 不可安装时用普通 `.btn`（而不是暗掉的 btn-primary）——
                    // 暗紫实心按钮看起来仍像"可以点"，中性描边才读得出"不可用"。
                    className={`btn btn-sm ${it.installable ? 'btn-primary' : ''}`}
                    disabled={!it.installable}
                    title={it.installable ? '查看 SKILL.md 原文并确认安装' : it.reason}
                    onClick={() => onPick(it)}
                  >
                    {it.installable ? '安装' : '暂不支持'}
                  </button>
                </div>
                {!it.installable && it.reason && (
                  <p className="mcp-inline-warn">{it.reason}</p>
                )}
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
            // 记录当前页序号：翻页回来后若已换过关键词 / 源（seq 变了），不追加
            if (mySeq === seq.current) {
              void searchSkillMarket(sourceId, query.trim(), market?.next_cursor)
            }
          }}
        >
          {loading ? '加载中…' : `加载更多（已显示 ${items.length} / ${market?.total ?? 0}）`}
        </button>
      )}
    </div>
  )
}
