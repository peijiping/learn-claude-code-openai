import { useEffect, useRef, useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import type { McpMarketItem } from '@protocols/agentProtocol'

/** MCP 市场浏览（设置弹窗「MCP」→ 从市场安装），docs/frontend/23。
 *
 * 数据源是**官方 MCP Registry**（经后端代理，前端不直连：跨域 + 统一缓存 + 错误文案归口）。
 *
 * 三条必须守住的点（改之前先读）：
 * 1. **「搜失败」≠「搜不到」**：`error` 非空时 `items` 必为空，两者要分开渲染 ——
 *    把超时渲染成"没有结果"会让用户以为这个服务不存在。
 * 2. **首次搜索很慢**（实测 0.9s~17s，官方 registry 海外访问），必须有明确的
 *    加载态与"可能要十几秒"的说明，否则用户会以为卡死。
 * 3. **来源标记不是安全评级**：三档（官方/社区/域名认证）都只经过命名空间所有权
 *    校验，**没有代码审计**。文案必须这么讲，不能让人误以为"官方"= 安全。
 */

const PUBLISHER_VIEW: Record<string, { label: string; cls: string }> = {
  official: { label: '官方命名空间', cls: 'official' },
  community: { label: 'GitHub 社区', cls: 'community' },
  'domain-verified': { label: '域名认证', cls: 'domain' }
}

const KIND_LABEL: Record<string, string> = {
  stdio: 'stdio',
  sse: 'sse',
  'streamable-http': 'http'
}

interface Props {
  onBack: () => void
  /** 点「安装」——由容器去 fetch 翻译结果并切到确认视图 */
  onPick: (item: McpMarketItem) => void
}

export default function McpMarketView({ onBack, onPick }: Props): JSX.Element {
  const market = useAgentStore((s) => s.mcpMarket)
  const loading = useAgentStore((s) => s.mcpMarketLoading)
  const searchMcpMarket = useAgentStore((s) => s.searchMcpMarket)

  const [query, setQuery] = useState('')
  const debounce = useRef<number | undefined>(undefined)
  const seq = useRef(0)

  // 进页先拉一页（空查询 = 最新条目），让面板立刻有内容而不是空白等用户输入
  useEffect(() => {
    if (!market) void searchMcpMarket('')
    // 只在首次挂载时触发，之后由用户输入/翻页驱动
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => () => window.clearTimeout(debounce.current), [])

  const runSearch = (q: string): void => {
    window.clearTimeout(debounce.current)
    debounce.current = window.setTimeout(() => {
      seq.current += 1
      void searchMcpMarket(q.trim())
    }, 450)
  }

  const items = market?.items ?? []
  const error = market?.error ?? ''
  const hasMore = !!market?.next_cursor
  const mySeq = seq.current

  return (
    <div className="mcp">
      <div className="mcp-form-head">
        <button type="button" className="mcp-back" onClick={onBack}>
          ← 返回列表
        </button>
        <span className="mcp-form-title">从市场安装</span>
        {market?.cached && <span className="mcp-hint">（缓存结果）</span>}
        {market && !market.error && (
          <span className="mcp-hint">{market.elapsed_ms}ms</span>
        )}
      </div>

      <div className="mcp-market-search">
        <input
          className="mcp-input"
          placeholder="搜索 MCP 服务，例如 filesystem / github / postgres"
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
          disabled={loading}
          onClick={() => {
            window.clearTimeout(debounce.current)
            seq.current += 1
            void searchMcpMarket(query.trim())
          }}
        >
          {loading ? '搜索中…' : '搜索'}
        </button>
      </div>

      <p className="mcp-risk">
        市场条目来自<b>官方 MCP Registry</b>，只经过<b>命名空间所有权校验</b>
        （DNS / GitHub 组织），<b>没有代码审计、没有漏洞扫描</b>。被列出来不等于安全 ——
        安装前请确认来源可信。首次搜索可能需要十几秒。
      </p>

      {error && <p className="sbx-error">{error}</p>}

      {loading && items.length === 0 && <div className="perm-loading">正在搜索市场…</div>}

      {!loading && !error && items.length === 0 && (
        <div className="perm-empty">
          {query.trim() ? `没有匹配「${query.trim()}」的服务。` : '市场当前没有返回任何条目。'}
        </div>
      )}

      {items.length > 0 && (
        <div className="mcp-market-list">
          {items.map((it) => {
            const pub = PUBLISHER_VIEW[it.publisher] ?? PUBLISHER_VIEW['domain-verified']
            return (
              <div className="mcp-market-item" key={it.id}>
                <div className="mcp-market-head">
                  <span className="mcp-market-title" title={it.id}>
                    {it.title || it.short_name}
                  </span>
                  <span className="mcp-tag">{it.version || '—'}</span>
                  <span className={`mcp-tag src ${pub.cls}`}>{pub.label}</span>
                  {(it.kinds ?? []).map((k) => (
                    <span className="mcp-tag" key={k}>
                      {KIND_LABEL[k] ?? k}
                    </span>
                  ))}
                </div>
                <p className="mcp-market-id mono">{it.id}</p>
                {it.description && <p className="mcp-market-desc">{it.description}</p>}
                <div className="mcp-market-actions">
                  {it.repository && (
                    <span className="mcp-market-repo mono" title={it.repository}>
                      {it.repository}
                    </span>
                  )}
                  <button
                    type="button"
                    // 不可安装时用普通 `.btn`（而不是暗掉的 btn-primary）——
                    // 暗紫的实心按钮看起来仍像"可以点"，中性描边才读得出"不可用"。
                    className={`btn btn-sm ${it.installable ? 'btn-primary' : ''}`}
                    disabled={!it.installable}
                    title={it.installable ? '查看将写入的配置并确认安装' : it.reason}
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
          // 串行化：主进程 pending 表按 kind FIFO 配对且无 id，同 kind 并发会串台
          disabled={loading}
          onClick={() => {
            // 记录当前页序号：翻页回来后若已换过关键词（seq 变了），不追加
            if (mySeq === seq.current) void searchMcpMarket(query.trim(), market?.next_cursor)
          }}
        >
          {loading ? '加载中…' : '加载更多'}
        </button>
      )}
    </div>
  )
}
