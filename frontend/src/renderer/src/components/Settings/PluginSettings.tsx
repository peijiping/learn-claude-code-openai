import { useEffect, useRef, useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import type { PluginContentResult, PluginEntry, PluginMarketItem } from '@protocols/agentProtocol'
import PluginMarketView from './PluginMarketView'
import PluginInstallView from './PluginInstallView'

/** 插件管理设置页（设置弹窗「插件」，docs/frontend/25）。
 *
 * 三个子视图（同一容器内切换，不用嵌套模态 —— 同 MCP / 技能页的理由）：
 *   `list`    插件列表（查看详情 / 启停 / 删除）
 *   `market`  浏览市场（`PluginMarketView`，含市场管理）
 *   `install` 安装确认（`PluginInstallView`）—— 全流程唯一的安全闸门
 *
 * 五条硬约束与技能页**逐条相同**（回执整份替换 / 错误内联不 toast / 读失败≠没有 /
 * 进行中禁用 / 不能塌成加载态），见 `SkillSettings.tsx` 顶部注释，这里不重复。
 *
 * 插件页独有的一条：**能力边界必须写在列表上**，不能只写在安装确认页 ——
 * 用户装了之后回到列表，仍要能一眼看出"这个插件的哪些组件生效了"。
 */

const COMPONENT_LABEL: Record<string, string> = {
  skills: '技能',
  commands: '命令',
  agents: '子智能体',
  hooks: '钩子',
  mcp_servers: 'MCP',
  lsp_servers: 'LSP'
}

const PUBLISHER_LABEL: Record<string, string> = {
  official: '官方',
  community: '社区',
  'third-party': '第三方'
}

function statusOf(p: PluginEntry): { cls: string; text: string } {
  if (!p.has_manifest) return { cls: 'warn', text: '清单缺失' }
  if (!p.enabled) return { cls: 'off', text: '已禁用' }
  const n = p.components?.skills?.length ?? 0
  return n > 0
    ? { cls: 'ok', text: `已启用 · 贡献 ${n} 个技能` }
    : { cls: 'off', text: '已启用 · 无技能贡献' }
}

export default function PluginSettings(): JSX.Element {
  const config = useAgentStore((s) => s.pluginConfig)
  const saving = useAgentStore((s) => s.pluginSaving)
  const loadPluginConfig = useAgentStore((s) => s.loadPluginConfig)
  const setPluginEnabled = useAgentStore((s) => s.setPluginEnabled)
  const removePlugin = useAgentStore((s) => s.removePlugin)
  const installPlugin = useAgentStore((s) => s.installPlugin)
  const readPlugin = useAgentStore((s) => s.readPlugin)
  const pluginMarketPlan = useAgentStore((s) => s.pluginMarketPlan)
  const pluginResolving = useAgentStore((s) => s.pluginResolving)
  const resolvePluginInstall = useAgentStore((s) => s.resolvePluginInstall)
  const clearPluginMarketPlan = useAgentStore((s) => s.clearPluginMarketPlan)

  const [view, setView] = useState<'list' | 'market' | 'install'>('list')
  const [pickedItem, setPickedItem] = useState<PluginMarketItem | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)
  /** 「详情」的一次性结果（plugin.json 原文 + 文件清单），只写本组件 state */
  const [detail, setDetail] = useState<{ name: string; res: PluginContentResult } | null>(null)
  const [reading, setReading] = useState<string | null>(null)
  const confirmTimer = useRef<number | undefined>(undefined)

  useEffect(() => {
    if (!config) void loadPluginConfig()
  }, [config, loadPluginConfig])
  useEffect(() => () => window.clearTimeout(confirmTimer.current), [])

  if (!config) return <div className="perm-loading">读取插件配置…</div>

  const plugins = config.plugins
  const errors = config.errors ?? []
  const warnings = config.warnings ?? []
  const wired = config.wired_components ?? ['skills']

  const armConfirm = (name: string): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(name)
    confirmTimer.current = window.setTimeout(() => setConfirming(null), 3000)
  }
  const doRemove = (name: string): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(null)
    if (detail?.name === name) setDetail(null)
    void removePlugin(name)
  }
  const doDetail = (name: string): void => {
    if (detail?.name === name) {
      setDetail(null)
      return
    }
    setReading(name)
    void readPlugin(name).then((res) => {
      setReading(null)
      if (res) setDetail({ name, res })
    })
  }

  if (view === 'market') {
    return (
      <PluginMarketView
        onBack={() => setView('list')}
        onPick={(item: PluginMarketItem) => {
          setPickedItem(item)
          setDetail(null)
          // **立刻进确认页**：resolve 可能要十几秒（通道探测 / 并行抓整个插件目录），
          // 停在市场页没有任何指示 = "点了没反应"。确认页在 resolving 时
          // 本来就有「正在抓取插件内容…」加载态，失败也有明确出路。
          setView('install')
          void resolvePluginInstall(item.market_id, item)
        }}
      />
    )
  }

  if (view === 'install') {
    const backToMarket = (): void => {
      clearPluginMarketPlan()
      setPickedItem(null)
      setView('market')
    }
    if (!pickedItem) {
      return (
        <div className="mcp">
          <div className="mcp-form-head">
            <button type="button" className="mcp-back" onClick={() => setView('list')}>
              ← 返回列表
            </button>
          </div>
          <div className="perm-empty">未选择条目。</div>
        </div>
      )
    }
    if (pluginResolving || !pluginMarketPlan) {
      if (pluginResolving) {
        return <div className="perm-loading">正在抓取插件内容…</div>
      }
      return (
        <div className="mcp">
          <div className="mcp-form-head">
            <button type="button" className="mcp-back" onClick={backToMarket}>
              ← 返回市场
            </button>
          </div>
          <div className="perm-empty">安装信息已失效，请重新选择插件。</div>
        </div>
      )
    }
    if (!pluginMarketPlan.ok) {
      return (
        <div className="mcp">
          <div className="mcp-form-head">
            <button type="button" className="mcp-back" onClick={backToMarket}>
              ← 返回市场
            </button>
            <span className="mcp-form-title">无法安装</span>
          </div>
          <p className="sbx-error">
            {pluginMarketPlan.unsupported || pluginMarketPlan.error || '未知原因'}
          </p>
          <p className="mcp-inline-note">
            本地路径形态的插件（<code>source</code> 指向本机目录）不支持远程安装；
            其余情况可以换一个市场或关键词再试。
          </p>
        </div>
      )
    }
    return (
      <PluginInstallView
        item={pickedItem}
        plan={pluginMarketPlan}
        saving={saving}
        saveErrors={errors}
        onInstall={(payload) => {
          void installPlugin(payload).then((okOk) => {
            if (okOk) {
              clearPluginMarketPlan()
              setPickedItem(null)
              setView('list')
            }
          })
        }}
        onBack={backToMarket}
      />
    )
  }

  return (
    <div className="mcp">
      <div className="mcp-head">
        <div className="mcp-summary">
          已安装 <b>{plugins.length}</b> 个 · 已启用 <b>{config.summary?.enabled ?? 0}</b> 个
          {config.summary ? ` · 有技能贡献 ${config.summary.contributing} 个` : ''}
          {' '}· 技能表里来自插件 <b>{config.contributed_skill_count ?? 0}</b> 个
        </div>
        <div className="mcp-head-actions">
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              setDetail(null)
              setView('market')
            }}
          >
            从市场安装
          </button>
        </div>
      </div>

      <p className="mcp-risk">
        插件采用 <b>Claude Code 插件规范</b>（一个目录 + <code>.claude-plugin/plugin.json</code>），
        可贡献 6 类组件 —— 但本期<b>只有「技能」接入运行时</b>（
        {wired.map((k) => COMPONENT_LABEL[k] ?? k).join('、')}），
        命令 / 钩子 / MCP 服务器等只落到 <code>~/.aigent/plugins/</code>，<b>不会被执行</b>。
      </p>

      {errors.map((e, i) => (
        <p className="sbx-error" key={`${i}-${e}`}>
          {e}
        </p>
      ))}
      {warnings.map((w, i) => (
        <p className="mcp-warn" key={`${i}-${w}`}>
          {w}
        </p>
      ))}

      {plugins.length === 0 ? (
        <div className="perm-empty">
          {errors.length > 0
            ? '配置读取失败，请先处理上面的错误。'
            : '还没有安装任何插件。可以从市场装一个（官方市场有 300+ 个）。'}
        </div>
      ) : (
        <div className="mcp-list">
          {plugins.map((p) => {
            const st = statusOf(p)
            const isOpen = expanded === p.name
            const kinds = Object.keys(p.components ?? {}).filter(
              (k) => (p.components[k] ?? []).length > 0
            )
            return (
              <div className={`mcp-row ${st.cls === 'warn' ? 'error' : ''}`} key={p.name}>
                <div
                  className="mcp-row-main"
                  onClick={() => setExpanded(isOpen ? null : p.name)}
                  title={isOpen ? '收起详情' : '展开详情'}
                >
                  <span
                    className={`mcp-dot ${st.cls === 'ok' ? 'connected' : st.cls === 'warn' ? 'error' : ''}`}
                  />
                  <span className="mcp-name">{p.display_name || p.name}</span>
                  {p.version && <span className="mcp-tag">{p.version}</span>}
                  <span className="mcp-tag src">
                    {p.source === 'market'
                      ? (PUBLISHER_LABEL[p.publisher ?? ''] ?? '市场')
                      : '本地'}
                  </span>
                  <span className={`mcp-status-cell ${st.cls}`}>{st.text}</span>
                  <span className="mcp-tools-cell">
                    {kinds.length > 0 ? `${kinds.length} 类组件` : ''}
                  </span>
                  <span className="mcp-chevron">{isOpen ? '▾' : '▸'}</span>
                </div>

                <div className="mcp-row-actions">
                  <button
                    type="button"
                    className="mcp-act"
                    disabled={reading === p.name || !p.has_manifest}
                    title={p.has_manifest ? '查看 plugin.json 与文件清单' : '清单缺失'}
                    onClick={() => doDetail(p.name)}
                  >
                    {reading === p.name ? '读取中…' : '详情'}
                  </button>
                  <button
                    type="button"
                    className={`mcp-act danger ${confirming === p.name ? 'armed' : ''}`}
                    disabled={saving}
                    title={confirming === p.name ? '再点一次确认删除' : '删除（连带目录与元数据）'}
                    onClick={() =>
                      confirming === p.name ? doRemove(p.name) : armConfirm(p.name)
                    }
                  >
                    {confirming === p.name ? '确认删除' : '删除'}
                  </button>
                  <button
                    type="button"
                    className={`switch ${p.enabled ? 'on' : ''}`}
                    title={p.enabled ? '禁用（贡献的技能退出系统提示）' : '启用'}
                    disabled={saving || !p.has_manifest}
                    onClick={() => void setPluginEnabled(p.name, !p.enabled)}
                  >
                    <span className="switch-knob" />
                  </button>
                </div>

                {isOpen && (
                  <div className="mcp-detail">
                    {p.warnings.map((w, i) => (
                      <p className={p.has_manifest ? 'mcp-inline-note' : 'sbx-error'} key={i}>
                        {w}
                      </p>
                    ))}
                    {p.description && <p className="mcp-desc">{p.description}</p>}
                    <p className="mcp-meta-line">
                      目录 <code>{p.path}</code>
                    </p>
                    {!p.enabled && (
                      <p className="mcp-inline-note">
                        已禁用：它贡献的技能不会进入系统提示（磁盘上的文件仍在）。
                      </p>
                    )}
                    {kinds.length > 0 && (
                      <>
                        <p className="mcp-meta-line">贡献的组件（标「本期生效」的才会被用起来）：</p>
                        <div className="mcp-chips">
                          {kinds.map((k) => (
                            <code
                              className="mcp-chip"
                              key={k}
                              title={
                                wired.includes(k)
                                  ? '本期接入运行时'
                                  : '本期只落到磁盘、未接入运行时'
                              }
                            >
                              {COMPONENT_LABEL[k] ?? k} × {(p.components[k] ?? []).length}
                              {wired.includes(k) ? ' ✓' : ''}
                            </code>
                          ))}
                        </div>
                      </>
                    )}
                    {p.installed_at && (
                      <p className="mcp-meta-line">
                        来源 {p.market_name || p.market_id || '市场'}
                        {p.repo ? ` · ${p.repo}` : ''} · 安装于 {p.installed_at}
                      </p>
                    )}
                    {detail?.name === p.name && (
                      <>
                        {detail.res.error ? (
                          <p className="sbx-error">{detail.res.error}</p>
                        ) : (
                          <>
                            <p className="mcp-meta-line">
                              <code>.claude-plugin/plugin.json</code> 原文：
                            </p>
                            <pre className="mcp-code">{detail.res.plugin_json}</pre>
                            {detail.res.files.length > 0 && (
                              <>
                                <p className="mcp-meta-line">
                                  文件清单（{detail.res.files.length} 个）：
                                </p>
                                <div className="mcp-filelist">
                                  {detail.res.files.map((f) => (
                                    <div className="mcp-file-row" key={f.path}>
                                      <span className="mcp-file-path">{f.path}</span>
                                      <span className="mcp-file-size">{f.size} B</span>
                                    </div>
                                  ))}
                                </div>
                              </>
                            )}
                          </>
                        )}
                      </>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      <p className="mcp-path">
        插件目录 <code>{config.dir ?? '~/.aigent/plugins'}</code>
        <span className="mcp-hint">
          {' '}
          · 启停状态写在 <code>plugins_sources.json</code>，不改插件目录本体
        </span>
      </p>
    </div>
  )
}
