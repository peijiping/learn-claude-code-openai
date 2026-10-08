import { useEffect, useRef, useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import type {
  McpLocalPkg,
  McpMarketItem,
  McpServer,
  McpServerStatus
} from '@protocols/agentProtocol'
import McpServerForm, { type McpFormPayload } from './McpServerForm'
import McpMarketView from './McpMarketView'
import McpInstallView from './McpInstallView'

/** MCP 服务管理设置页（设置弹窗「MCP」，docs/frontend/23）。
 *
 * 四个子视图（同一容器内切换，**不用嵌套模态** —— 设置本身已是模态，再叠一层
 * 遮罩会引出 z-index 与点击穿透问题，而这里没有必须并存的编辑场景）：
 *   `list`    条目列表（增 / 改 / 删 / 启停 / 试连 / 看工具）
 *   `form`    新增或编辑单条（`McpServerForm`）
 *   `market`  浏览官方 registry（`McpMarketView`）
 *   `install` 安装确认（`McpInstallView`）—— 全流程唯一的安全闸门
 *
 * 四条硬约束（都踩过坑，改之前先读）：
 * 1. **回执为权威源，整份替换**：`mcp_config` 三条命令共用同一信封，每次都回读全量
 *    → `mcpConfig` 直接换掉，不做增量拼接（拼接会留下已删条目的残影）。
 * 2. **错误内联、不 toast**：后端校验失败只走回执 `errors[]`，在页内渲染红字；
 *    若后端多发一封 `error`，会被 store 当全局 toast 弹出来 —— 约定是内联。
 * 3. **读配置失败 ≠ 没有条目**：`servers: [] + errors` 时绝不能渲染成"还没添加过"
 *    （会诱导用户重新添加，反而覆盖掉磁盘上还在的内容）。
 * 4. **试连 / 搜索进行中必须禁用按钮**：主进程 `pending` 表按 kind FIFO 配对且无 id，
 *    同一 kind 并发会串台（`main/index.ts` 注释同款）。
 */

const TRANSPORT_LABEL: Record<string, string> = {
  stdio: 'stdio',
  sse: 'sse',
  'streamable-http': 'http'
}

/** 状态五态 → 展示文案。`cls` 复用沙盒页的 `.sbx-status ok/warn/off` 配色。 */
const STATUS_VIEW: Record<McpServerStatus, { cls: string; text: string }> = {
  connected: { cls: 'ok', text: '已连接' },
  disconnected: { cls: 'off', text: '未连接' },
  disabled: { cls: 'off', text: '已禁用' },
  idle: { cls: 'off', text: '待会话启动后连接' },
  error: { cls: 'warn', text: '连接失败' }
}

const PUBLISHER_LABEL: Record<string, string> = {
  official: '官方',
  community: '社区',
  'domain-verified': '域名认证'
}

/** 本地包状态 → 展示（`ok` 正常；`incomplete` = 上次没装完；`broken` = 文件没了） */
const PKG_STATUS_VIEW: Record<string, { cls: string; text: string }> = {
  ok: { cls: 'ok', text: '已就绪' },
  incomplete: { cls: 'warn', text: '安装未完成' },
  broken: { cls: 'warn', text: '文件缺失' }
}

function formatBytes(n: number): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}

/** 把列表条目还原成后端要的配置体 —— **必须显式重建，不能直接回传 `sv`**：
 *  展示字段名是 `transport`，而配置键是 `type`；直接回传会在 `_clean_entry`
 *  的白名单里丢掉 `type`，导致传输类型被误判成默认值。 */
function toConfig(sv: McpServer, enable: boolean): Record<string, unknown> {
  const cfg: Record<string, unknown> = { type: sv.transport, enable }
  if (sv.transport === 'stdio') {
    cfg.command = sv.command ?? ''
    if (sv.args?.length) cfg.args = sv.args
    if (sv.cwd) cfg.cwd = sv.cwd
    if (sv.env) cfg.env = sv.env
  } else {
    cfg.url = sv.url ?? ''
    if (sv.headers) cfg.headers = sv.headers
  }
  return cfg
}

export default function McpSettings(): JSX.Element {
  const result = useAgentStore((s) => s.mcpConfig)
  const saving = useAgentStore((s) => s.mcpSaving)
  const testing = useAgentStore((s) => s.mcpTesting)
  const test = useAgentStore((s) => s.mcpTest)
  const loadMcpConfig = useAgentStore((s) => s.loadMcpConfig)
  const upsertMcpServer = useAgentStore((s) => s.upsertMcpServer)
  const removeMcpServer = useAgentStore((s) => s.removeMcpServer)
  const testMcpServer = useAgentStore((s) => s.testMcpServer)
  const clearMcpTest = useAgentStore((s) => s.clearMcpTest)
  const mcpMarketPlan = useAgentStore((s) => s.mcpMarketPlan)
  const mcpResolving = useAgentStore((s) => s.mcpResolving)
  const resolveMcpInstall = useAgentStore((s) => s.resolveMcpInstall)
  const clearMcpMarketPlan = useAgentStore((s) => s.clearMcpMarketPlan)
  const mcpPkgPlan = useAgentStore((s) => s.mcpPkgPlan)
  const mcpPkgBusy = useAgentStore((s) => s.mcpPkgBusy)
  const mcpPkgAction = useAgentStore((s) => s.mcpPkgAction)
  const resolveMcpPkg = useAgentStore((s) => s.resolveMcpPkg)
  const clearMcpPkgPlan = useAgentStore((s) => s.clearMcpPkgPlan)
  const installMcpPkg = useAgentStore((s) => s.installMcpPkg)
  const removeMcpPkg = useAgentStore((s) => s.removeMcpPkg)
  const verifyMcpPkg = useAgentStore((s) => s.verifyMcpPkg)

  const [view, setView] = useState<'list' | 'form' | 'market' | 'install'>('list')
  const [editing, setEditing] = useState<McpServer | null>(null)
  const [pickedItem, setPickedItem] = useState<McpMarketItem | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)
  /** 正在二次确认卸载的本地包 slug（与条目的 `confirming` 分开 —— 两者是不同的资源，
   *  共用一个 state 会让"确认删条目"顺手把某个包也标记成待删） */
  const [confirmPkg, setConfirmPkg] = useState<string | null>(null)
  /** 试连结果只在点它的那一行展示（试连不落盘，属一次性结论） */
  const [testTarget, setTestTarget] = useState<string | null>(null)
  /** 本地包的展开详情（按 slug 键控） */
  const [pkgOpen, setPkgOpen] = useState<string | null>(null)
  const confirmTimer = useRef<number | undefined>(undefined)
  /** 开关的乐观位（2026-09-30）：点击即本地翻转，不等回执。
   *
   *  为什么必须有：`mcp_server_upsert` 回执要等 `_reload_mcp_all_runtimes`
   *  跑完才发（对每条启用中的条目做 connect 握手，最长 MCP_CONNECT_TIMEOUT
   *  15s / 运行时）—— 慢服务器 + 多会话时回执可晚到十几秒。乐观位缺席的
   *  表现就是"点了开关没反应，关掉设置页重开才看到已切换"。
   *
   *  收敛三条（缺一不可）：
   *  1. **任何新 `mcpConfig` 回执都撤位**（回执是权威源，撤位后直接渲染回执；
   *     保存成功时回执内容与乐观值一致，视觉无跳变；被拒时 errors 已内联，回执旧值自然回显）；
   *  2. **30s 兜底**：回执始终不来（主进程超时 + 事件丢失）→ 撤位 + 重拉权威态，
   *     绝不让乐观位悬着变成"假状态"；
   *  3. `saving` 期间该行 switch 带 `pending` 样式 + 保持 `disabled`
   *     （主进程 pending 表按 kind FIFO 配对且无 id，同 kind 并发会串台，不能放开连点）。
   */
  const [optToggle, setOptToggle] = useState<{ name: string; enable: boolean } | null>(null)
  const optTimer = useRef<number | undefined>(undefined)
  const clearOpt = (): void => {
    window.clearTimeout(optTimer.current)
    setOptToggle(null)
  }

  // 首次进页懒加载（切 tab 时由 SettingsModal 触发；直接进入时兜底）
  useEffect(() => {
    if (!result) void loadMcpConfig()
  }, [result, loadMcpConfig])
  // 回执对账（乐观位收敛 #1）：任何新回执都撤乐观位 —— 回执是权威源
  useEffect(() => {
    if (optToggle) clearOpt()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result])
  // 组件卸载时兜底清定时器
  useEffect(() => () => window.clearTimeout(optTimer.current), [])
  useEffect(() => () => window.clearTimeout(confirmTimer.current), [])

  if (!result) return <div className="perm-loading">读取 MCP 配置…</div>

  const servers = result.servers
  const packages: McpLocalPkg[] = result.packages ?? []
  const enabled = result.summary?.enabled ?? servers.filter((s) => s.enable).length
  const connected = result.summary?.connected ?? servers.filter((s) => s.status === 'connected').length
  const errors = result.errors ?? []
  const warnings = result.warnings ?? []
  /** 本地包动作结果（校验/卸载的结论内联展示；install 的结论在安装确认页展示） */
  const pkgAction = mcpPkgAction

  const armConfirm = (name: string): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(name)
    confirmTimer.current = window.setTimeout(() => setConfirming(null), 3000)
  }
  const doRemove = (name: string): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(null)
    void removeMcpServer(name)
  }
  const armConfirmPkg = (slug: string): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirmPkg(slug)
    confirmTimer.current = window.setTimeout(() => setConfirmPkg(null), 3000)
  }
  const doRemovePkg = (slug: string): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirmPkg(null)
    void removeMcpPkg(slug)
  }

  if (view === 'form') {
    return (
      <McpServerForm
        initial={editing}
        existingNames={servers.map((s) => s.name)}
        saving={saving}
        testing={testing}
        test={test}
        // 保存被拒的原因只在**失败的那一次**回执里；成功后 errors 会被清空，
        // 所以直接透传不会留下陈旧红字。
        saveErrors={errors}
        onTest={(payload) => void testMcpServer(payload)}
        onClearTest={clearMcpTest}
        onSubmit={(payload: McpFormPayload) => {
          void upsertMcpServer(payload).then((ok) => {
            if (ok) {
              setView('list')
              setEditing(null)
            }
          })
        }}
        onCancel={() => {
          setView('list')
          setEditing(null)
          clearMcpTest()
        }}
      />
    )
  }

  if (view === 'market') {
    return (
      <McpMarketView
        onBack={() => setView('list')}
        onPick={(item: McpMarketItem) => {
          setPickedItem(item)
          clearMcpTest()
          // 先拿翻译结果（不落盘），再进确认页 —— 确认页要展示的就是这份原文。
          // 翻译失败（如只有 oci 包）也进确认页，由它渲染原因 + 返回按钮。
          void resolveMcpInstall(item).then(() => setView('install'))
        }}
      />
    )
  }

  if (view === 'install') {
    const backToMarket = (): void => {
      clearMcpMarketPlan()
      clearMcpPkgPlan()
      setPickedItem(null)
      clearMcpTest()
      setView('market')
    }
    if (!pickedItem) {
      // 理论上进不来（pickedItem 与 view 同步设置）；兜底回列表而不是白屏
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
    if (mcpResolving || !mcpMarketPlan) {
      if (mcpResolving) {
        return <div className="perm-loading">正在解析安装配置…</div>
      }
      // 没有在途请求却也没有结果 = 状态被清掉了（例如另一个入口清了 plan）。
      // **绝不能停在这里的加载态** —— 权限页与沙盒页都踩过"塌成加载态"这个坑
      // （界面永远转圈、用户以为卡死）。给一条明确的出路。
      return (
        <div className="mcp">
          <div className="mcp-form-head">
            <button type="button" className="mcp-back" onClick={backToMarket}>
              ← 返回市场
            </button>
          </div>
          <div className="perm-empty">安装信息已失效，请重新选择条目。</div>
        </div>
      )
    }
    if (!mcpMarketPlan.ok) {
      return (
        <div className="mcp">
          <div className="mcp-form-head">
            <button type="button" className="mcp-back" onClick={backToMarket}>
              ← 返回市场
            </button>
            <span className="mcp-form-title">无法一键安装</span>
          </div>
          <p className="sbx-error">{mcpMarketPlan.unsupported || mcpMarketPlan.error || '未知原因'}</p>
          <p className="mcp-inline-note">
            可以用列表页的「+ 手动添加」按该条目的文档自行填写命令或 URL。
          </p>
        </div>
      )
    }
    return (
      <McpInstallView
        item={pickedItem}
        plan={mcpMarketPlan}
        saving={saving}
        testing={testing}
        test={test}
        saveErrors={errors}
        pkgPlan={mcpPkgPlan}
        pkgBusy={mcpPkgBusy}
        pkgAction={mcpPkgAction}
        onResolvePkg={(pkgName, pkgVersion) => void resolveMcpPkg(pkgName, pkgVersion)}
        onClearPkgPlan={clearMcpPkgPlan}
        onDownload={(payload) => installMcpPkg(payload)}
        onSave={async (payload) => {
          // 复用既有的 mcp_server_upsert（含热重载、校验、密钥掩码回填）——
          // 本地安装只负责把包放到磁盘上并给出 command，写配置这件事只有这一个出口。
          const okOk = await upsertMcpServer(payload)
          if (okOk) {
            clearMcpMarketPlan()
            clearMcpPkgPlan()
            setPickedItem(null)
            setView('list')
          }
          return okOk
        }}
        onTest={(config) => void testMcpServer({ config })}
        onClearTest={clearMcpTest}
        onBack={backToMarket}
      />
    )
  }

  return (
    <div className="mcp">
      <div className="mcp-head">
        <div className="mcp-summary">
          已安装 <b>{servers.length}</b> 个 · 已启用 <b>{enabled}</b> 个 · 已连接{' '}
          <b>{connected}</b> 个
          {packages.length > 0 && (
            <>
              {' '}
              · 本地包 <b>{packages.length}</b> 个
            </>
          )}
        </div>
        <div className="mcp-head-actions">
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              clearMcpTest()
              setView('market')
            }}
          >
            从市场安装
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              setEditing(null)
              clearMcpTest()
              setView('form')
            }}
          >
            + 手动添加
          </button>
        </div>
      </div>

      <p className="mcp-risk">
        MCP 服务器是<b>可执行的第三方代码</b> —— stdio 类型会直接在本机启动该命令。
        请只添加你信任的来源。
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

      {servers.length === 0 ? (
        <div className="perm-empty">
          {/* 读失败时上面已有红字，这里不再说"还没添加" —— 两者同时出现会误导 */}
          {errors.length > 0 ? '配置读取失败，请先处理上面的错误。' : '还没有添加任何 MCP 服务。'}
        </div>
      ) : (
        <div className="mcp-list">
          {servers.map((sv) => {
            const st = STATUS_VIEW[sv.status] ?? STATUS_VIEW.disconnected
            const isOpen = expanded === sv.name
            return (
              <div className={`mcp-row ${sv.status}`} key={sv.name}>
                <div
                  className="mcp-row-main"
                  onClick={() => setExpanded(isOpen ? null : sv.name)}
                  title={isOpen ? '收起详情' : '展开详情'}
                >
                  <span className={`mcp-dot ${sv.status}`} />
                  <span className="mcp-name">{sv.name}</span>
                  <span className="mcp-tag">{TRANSPORT_LABEL[sv.transport] ?? sv.transport}</span>
                  <span className="mcp-tag src">
                    {sv.source === 'market'
                      ? (PUBLISHER_LABEL[sv.publisher ?? ''] ?? '市场')
                      : '本地'}
                  </span>
                  {/* 状态点已由 .mcp-dot 承担 → 这里只上文案，不套 .sbx-status
                      （它带 ::before 圆点，会与 .mcp-dot 叠成两个点） */}
                  <span className={`mcp-status-cell ${st.cls}`}>{st.text}</span>
                  <span className="mcp-tools-cell">
                    {sv.status === 'connected' ? `${sv.tool_count} 个工具` : ''}
                  </span>
                  <span className="mcp-chevron">{isOpen ? '▾' : '▸'}</span>
                </div>

                <div className="mcp-row-actions">
                  <button
                    type="button"
                    className="mcp-act"
                    disabled={testing}
                    title="用磁盘上的真实配置试连一次（不落盘）"
                    onClick={() => {
                      setTestTarget(sv.name)
                      setExpanded(sv.name)
                      void testMcpServer({ name: sv.name })
                    }}
                  >
                    {testing && testTarget === sv.name ? '连接中…' : '测试'}
                  </button>
                  <button
                    type="button"
                    className="mcp-act"
                    onClick={() => {
                      setEditing(sv)
                      clearMcpTest()
                      setView('form')
                    }}
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    className={`mcp-act danger ${confirming === sv.name ? 'armed' : ''}`}
                    disabled={saving}
                    title={confirming === sv.name ? '再点一次确认删除' : '删除'}
                    onClick={() => (confirming === sv.name ? doRemove(sv.name) : armConfirm(sv.name))}
                  >
                    {confirming === sv.name ? '确认删除' : '删除'}
                  </button>
                  <button
                    type="button"
                    className={[
                      'switch',
                      (optToggle?.name === sv.name ? optToggle.enable : sv.enable) ? 'on' : '',
                      saving && optToggle?.name === sv.name ? 'pending' : '',
                    ].filter(Boolean).join(' ')}
                    title={
                      saving && optToggle?.name === sv.name
                        ? '正在保存并热重载（对慢服务器可能需要数秒）…'
                        : (optToggle?.name === sv.name ? optToggle.enable : sv.enable)
                          ? '禁用（不连接、工具不进模型工具池）'
                          : '启用'
                    }
                    disabled={saving}
                    onClick={() => {
                      const next = !(optToggle?.name === sv.name ? optToggle.enable : sv.enable)
                      // 乐观翻转：点击立即生效，回执到达后由对账 effect 撤位
                      setOptToggle({ name: sv.name, enable: next })
                      window.clearTimeout(optTimer.current)
                      // 兜底（收敛 #3）：回执 30s 仍不来 → 撤位并重拉权威态
                      optTimer.current = window.setTimeout(() => {
                        setOptToggle(null)
                        void loadMcpConfig()
                      }, 30000)
                      void upsertMcpServer({ name: sv.name, config: toConfig(sv, next) })
                    }}
                  >
                    <span className="switch-knob" />
                  </button>
                </div>

                {isOpen && (
                  <div className="mcp-detail">
                    {sv.status === 'error' && sv.last_error && (
                      <p className="sbx-error">{sv.last_error}</p>
                    )}
                    {sv.status === 'idle' && (
                      <p className="mcp-inline-note">
                        当前没有活动会话，无法判断连接状态；开启后会在下一个会话启动时连接。
                      </p>
                    )}
                    {sv.transport === 'stdio' ? (
                      <p className="mcp-cmd mono">
                        {[sv.command, ...(sv.args ?? [])].filter(Boolean).join(' ')}
                        {sv.cwd ? `  (cwd: ${sv.cwd})` : ''}
                      </p>
                    ) : (
                      <p className="mcp-cmd mono">{sv.url}</p>
                    )}
                    {sv.installed_at && (
                      <p className="mcp-meta-line">
                        来源 {sv.market_id || '市场'} · 安装于 {sv.installed_at}
                      </p>
                    )}
                    {sv.tools.length > 0 ? (
                      <>
                        <p className="mcp-meta-line">
                          该服务暴露的工具（模型侧名前缀为{' '}
                          <code>mcp__&lt;名称&gt;__</code>）：
                        </p>
                        <div className="mcp-chips">
                          {sv.tools.map((t) => (
                            <code className="mcp-chip" key={t}>
                              {t}
                            </code>
                          ))}
                        </div>
                      </>
                    ) : (
                      <p className="mcp-meta-line">
                        {sv.enable ? '暂无已发现的工具' : '已禁用，未连接'}
                      </p>
                    )}
                    {/* 试连结果只在点它的那一行展示（结果不落盘，属一次性结论） */}
                    {testTarget === sv.name && test && (
                      <div className={`mcp-test-box ${test.ok ? 'ok' : 'fail'}`}>
                        <p className="mcp-test-title">
                          {test.ok
                            ? `试连成功 · ${test.tool_count} 个工具 · ${test.elapsed_ms}ms`
                            : '试连失败'}
                        </p>
                        {!test.ok && <p className="mcp-test-error mono">{test.error}</p>}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* ── 本地包（docs/frontend/23 §本地安装）──────────────────────────────
          与上面的「条目」是**两种独立资源**：条目是配置，本地包是磁盘上的下载产物。
          删条目**不动**包（它可能还被别的条目用），删包也**不动**条目（只在下方提示
          哪些条目因此失效）—— 耦合起来就会出现"改个名字顺手把包删了"。 */}
      <div className="mcp-subhead">
        <span>本地包（{packages.length}）</span>
        <span className="mcp-hint mono">{result.pkgs_dir ?? '~/.aigent/mcp/pkgs'}</span>
      </div>

      {pkgAction && pkgAction.action !== 'install' && (
        <div className={`mcp-test-box ${pkgAction.ok ? 'ok' : 'fail'}`}>
          <p className="mcp-test-title">
            {pkgAction.action === 'verify'
              ? pkgAction.ok
                ? '校验通过：哈希与安装时一致，可执行文件在安装目录内'
                : '校验未通过'
              : pkgAction.ok
                ? `已卸载${pkgAction.freed_bytes ? ` · 释放 ${formatBytes(pkgAction.freed_bytes)}` : ''}`
                : '卸载失败'}
          </p>
          {pkgAction.error && <p className="mcp-test-error mono">{pkgAction.error}</p>}
          {(pkgAction.errors ?? []).map((e, i) => (
            <p className="mcp-test-error mono" key={`${i}-${e}`}>
              {e}
            </p>
          ))}
        </div>
      )}

      {packages.length === 0 ? (
        <div className="perm-empty">
          本机还没有下载过任何 MCP 包。从市场安装时选「下载到本地」即可。
        </div>
      ) : (
        <div className="mcp-list">
          {packages.map((p) => {
            const st = PKG_STATUS_VIEW[p.status] ?? PKG_STATUS_VIEW.broken
            const isOpen = pkgOpen === p.slug
            const orphan = p.referenced_by.length === 0
            return (
              <div className={`mcp-row ${p.status === 'ok' ? 'pkgo' : 'error'}`} key={p.slug}>
                <div
                  className="mcp-row-main"
                  onClick={() => setPkgOpen(isOpen ? null : p.slug)}
                  title={isOpen ? '收起详情' : '展开详情'}
                >
                  <span className={`mcp-dot ${p.status === 'ok' ? 'connected' : 'error'}`} />
                  <span className="mcp-name">{p.name || p.slug}</span>
                  <span className="mcp-tag">{p.version || '—'}</span>
                  <span className="mcp-tag src">{formatBytes(p.size_bytes)}</span>
                  <span className={`mcp-status-cell ${st.cls}`}>{st.text}</span>
                  <span className="mcp-tools-cell">
                    {p.referenced_by.length > 0
                      ? `${p.referenced_by.length} 个条目在用`
                      : orphan
                        ? '无条目引用'
                        : ''}
                  </span>
                  <span className="mcp-chevron">{isOpen ? '▾' : '▸'}</span>
                </div>

                <div className="mcp-row-actions">
                  <button
                    type="button"
                    className="mcp-act"
                    disabled={mcpPkgBusy !== null}
                    title="重新核对完整性哈希与可执行文件位置"
                    onClick={() => void verifyMcpPkg(p.slug)}
                  >
                    {mcpPkgBusy === 'verify' && pkgAction?.slug === p.slug ? '校验中…' : '校验'}
                  </button>
                  <button
                    type="button"
                    className={`mcp-act danger ${confirmPkg === p.slug ? 'armed' : ''}`}
                    disabled={mcpPkgBusy !== null}
                    title={
                      confirmPkg === p.slug
                        ? '再点一次确认删除这个目录'
                        : p.referenced_by.length > 0
                          ? `仍被条目「${p.referenced_by.join('、')}」引用，删了它们会起不来`
                          : '删除这个包的目录（释放磁盘）'
                    }
                    onClick={() => (confirmPkg === p.slug ? doRemovePkg(p.slug) : armConfirmPkg(p.slug))}
                  >
                    {confirmPkg === p.slug ? '确认删除' : '删除'}
                  </button>
                </div>

                {isOpen && (
                  <div className="mcp-detail">
                    <p className="mcp-cmd mono">{p.command || '（没有记录可执行文件）'}</p>
                    <p className="mcp-meta-line mono">{p.dir}</p>
                    <p className="mcp-meta-line">
                      {p.registry || '未记录来源'} · 安装于 {p.installed_at || '未知'}
                      {p.dep_count ? ` · ${p.dep_count} 个包` : ''}
                      {p.scripts_allowed ? ' · ⚠️ 允许过安装期脚本' : ''}
                    </p>
                    {p.integrity && (
                      <p className="mcp-meta-line mono">完整性 {p.integrity}</p>
                    )}
                    {p.status === 'incomplete' && (
                      <p className="sbx-error">
                        上次安装没有跑完（残留标记未清除）。建议删除后重新安装。
                      </p>
                    )}
                    {p.status === 'broken' && (
                      <p className="sbx-error">
                        元数据还在，但可执行文件不见了。引用它的条目会连接失败。
                      </p>
                    )}
                    {p.referenced_by.length > 0 ? (
                      <p className="mcp-meta-line">
                        正在被这些条目引用：
                        {p.referenced_by.map((n) => (
                          <code className="mcp-chip" key={n}>
                            {n}
                          </code>
                        ))}
                      </p>
                    ) : (
                      <p className="mcp-meta-line">没有条目引用它 —— 可以安全删除。</p>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      <p className="mcp-path">
        配置文件 <code>{result.path ?? '~/.aigent/mcp/mcp_servers.json'}</code>
        {result.sessions !== undefined && (
          <span className="mcp-hint"> · 可观测到 {result.sessions} 个运行时</span>
        )}
      </p>
    </div>
  )
}
