import { useEffect, useRef, useState } from 'react'
import { useAgentStore } from '@store/agentStore'
import type { SkillContentResult, SkillEntry, SkillMarketItem } from '@protocols/agentProtocol'
import SkillForm from './SkillForm'
import SkillMarketView from './SkillMarketView'
import SkillInstallView from './SkillInstallView'

/** 技能管理设置页（设置弹窗「技能」，docs/frontend/24）。
 *
 * 四个子视图（同一容器内切换，**不用嵌套模态** —— 设置本身已是模态，再叠一层
 * 遮罩会引出 z-index 与点击穿透问题，而这里没有必须并存的编辑场景）：
 *   `list`    技能列表（启停 / 查看正文 / 删除）
 *   `form`    手动新建（`SkillForm`）
 *   `market`  浏览市场源（`SkillMarketView`，含源管理）
 *   `install` 安装确认（`SkillInstallView`）—— 全流程唯一的安全闸门
 *
 * 五条硬约束（与 MCP 页同源，都踩过坑）：
 * 1. **回执为权威源，整份替换**：`skill_config` 六条命令共用同一信封，每次都回读全量
 *    → 直接换掉，不做增量拼接（拼接会留下已删技能的残影）。
 * 2. **错误内联、不 toast**：后端校验失败只走回执 `errors[]`；若后端多发一封 `error`，
 *    会被 store 当全局 toast 弹出来 —— 约定是内联。
 * 3. **读配置失败 ≠ 没有技能**：`skills: [] + errors` 时绝不能渲染成"还没装过"
 *    （会诱导用户去市场重装，反而覆盖掉磁盘上还在的技能，同 23 篇 §4.2-3）。
 * 4. **搜索 / 安装进行中必须禁用按钮**：主进程 pending 表按 kind FIFO 配对且无 id，
 *    同一 kind 并发会串台（`main/index.ts` 注释同款）。
 * 5. **`view === 'install'` 且无在途请求时不能停在加载态**：给一条明确的出路
 *    （"安装信息已失效，请重新选择"），权限页与沙盒页都踩过"塌成加载态"。
 */

/** 技能状态 → 展示文案与配色（**后端零硬编码，这里只是渲染**）。 */
function statusOf(sk: SkillEntry): { cls: string; text: string } {
  if (!sk.has_manifest) return { cls: 'warn', text: '缺少 SKILL.md' }
  if (!sk.enabled) return { cls: 'off', text: '已禁用' }
  return { cls: 'ok', text: '已启用' }
}

const PUBLISHER_LABEL: Record<string, string> = {
  official: '官方',
  community: '社区',
  'third-party': '第三方'
}

export default function SkillSettings(): JSX.Element {
  const config = useAgentStore((s) => s.skillConfig)
  const saving = useAgentStore((s) => s.skillSaving)
  const loadSkillConfig = useAgentStore((s) => s.loadSkillConfig)
  const setSkillEnabled = useAgentStore((s) => s.setSkillEnabled)
  const removeSkill = useAgentStore((s) => s.removeSkill)
  const createSkill = useAgentStore((s) => s.createSkill)
  const readSkill = useAgentStore((s) => s.readSkill)
  const installSkill = useAgentStore((s) => s.installSkill)
  const skillMarketPlan = useAgentStore((s) => s.skillMarketPlan)
  const skillResolving = useAgentStore((s) => s.skillResolving)
  const resolveSkillInstall = useAgentStore((s) => s.resolveSkillInstall)
  const clearSkillMarketPlan = useAgentStore((s) => s.clearSkillMarketPlan)

  const [view, setView] = useState<'list' | 'form' | 'market' | 'install'>('list')
  const [pickedItem, setPickedItem] = useState<SkillMarketItem | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)
  /** 「查看正文」的一次性结果 —— 只在点它的那一行展开区展示（不落全局状态） */
  const [preview, setPreview] = useState<{ name: string; res: SkillContentResult } | null>(null)
  const [previewing, setPreviewing] = useState<string | null>(null)
  const confirmTimer = useRef<number | undefined>(undefined)

  // 首次进页懒加载（切 tab 时由 SettingsModal 触发；直接进入时兜底）
  useEffect(() => {
    if (!config) void loadSkillConfig()
  }, [config, loadSkillConfig])
  useEffect(() => () => window.clearTimeout(confirmTimer.current), [])

  if (!config) return <div className="perm-loading">读取技能配置…</div>

  const skills = config.skills
  const enabled = config.summary?.enabled ?? skills.filter((s) => s.enabled).length
  const errors = config.errors ?? []
  const warnings = config.warnings ?? []

  const armConfirm = (name: string): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(name)
    confirmTimer.current = window.setTimeout(() => setConfirming(null), 3000)
  }
  const doRemove = (name: string): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(null)
    if (preview?.name === name) setPreview(null)
    void removeSkill(name)
  }
  const doPreview = (name: string): void => {
    if (preview?.name === name) {
      setPreview(null)
      return
    }
    setPreviewing(name)
    void readSkill(name).then((res) => {
      setPreviewing(null)
      if (res) setPreview({ name, res })
    })
  }

  if (view === 'form') {
    return (
      <SkillForm
        existingNames={skills.map((s) => s.name)}
        saving={saving}
        // 保存被拒的原因只在**失败的那一次**回执里；成功后 errors 会被清空，
        // 所以直接透传不会留下陈旧红字。
        saveErrors={errors}
        onSubmit={(payload) => {
          void createSkill(payload).then((ok) => {
            if (ok) setView('list')
          })
        }}
        onCancel={() => setView('list')}
      />
    )
  }

  if (view === 'market') {
    return (
      <SkillMarketView
        onBack={() => setView('list')}
        onPick={(item: SkillMarketItem) => {
          setPickedItem(item)
          setPreview(null)
          // **立刻进确认页**：resolve 可能要十几秒（通道探测 / 逐文件抓取），
          // 停在市场页没有任何指示 = "点了没反应"。确认页在 resolving 时
          // 本来就有「正在抓取技能内容…」加载态，失败也有明确出路。
          setView('install')
          void resolveSkillInstall(item.market_id, item)
        }}
      />
    )
  }

  if (view === 'install') {
    const backToMarket = (): void => {
      clearSkillMarketPlan()
      setPickedItem(null)
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
    if (skillResolving || !skillMarketPlan) {
      if (skillResolving) {
        return <div className="perm-loading">正在抓取技能内容…</div>
      }
      // 没有在途请求却也没有结果 = 状态被清掉了。**绝不能停在这里的加载态**
      // （界面永远转圈、用户以为卡死）。给一条明确的出路。
      return (
        <div className="mcp">
          <div className="mcp-form-head">
            <button type="button" className="mcp-back" onClick={backToMarket}>
              ← 返回市场
            </button>
          </div>
          <div className="perm-empty">安装信息已失效，请重新选择技能。</div>
        </div>
      )
    }
    if (!skillMarketPlan.ok) {
      return (
        <div className="mcp">
          <div className="mcp-form-head">
            <button type="button" className="mcp-back" onClick={backToMarket}>
              ← 返回市场
            </button>
            <span className="mcp-form-title">无法安装</span>
          </div>
          <p className="sbx-error">
            {skillMarketPlan.unsupported || skillMarketPlan.error || '未知原因'}
          </p>
          <p className="mcp-inline-note">
            可以换一个源或关键词；也可以退回列表用「+ 新建技能」自己写一份。
          </p>
        </div>
      )
    }
    return (
      <SkillInstallView
        item={pickedItem}
        plan={skillMarketPlan}
        saving={saving}
        saveErrors={errors}
        onInstall={(payload) => {
          void installSkill(payload).then((okOk) => {
            if (okOk) {
              clearSkillMarketPlan()
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
          已安装 <b>{skills.length}</b> 个 · 已启用 <b>{enabled}</b> 个
          {config.summary ? ` · 来自市场 ${config.summary.from_market} 个` : ''}
          {(config.plugin_skill_count ?? 0) > 0 && (
            <>
              {' '}
              · 另有 <b>{config.plugin_skill_count}</b> 个来自插件
            </>
          )}
        </div>
        <div className="mcp-head-actions">
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              setPreview(null)
              setView('market')
            }}
          >
            从市场安装
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              setPreview(null)
              setView('form')
            }}
          >
            + 新建技能
          </button>
        </div>
      </div>

      <p className="mcp-risk">
        技能是<b>写给模型看的指令</b>：正文会进入系统提示（或经{' '}
        <code>load_skill</code> 按需加载），附属脚本可能被模型通过{' '}
        <code>run_bash</code> 执行。请只安装你信任来源的技能。
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

      {skills.length === 0 ? (
        <div className="perm-empty">
          {/* 读失败时上面已有红字，这里不再说"还没装过" —— 两者同时出现会误导 */}
          {errors.length > 0
            ? '配置读取失败，请先处理上面的错误。'
            : '还没有安装任何技能。可以从市场装，或点「+ 新建技能」自己写一份。'}
        </div>
      ) : (
        <div className="mcp-list">
          {skills.map((sk) => {
            const st = statusOf(sk)
            const isOpen = expanded === sk.name
            const aux = sk.files.filter((f) => f.toLowerCase() !== 'skill.md')
            return (
              <div className={`mcp-row ${st.cls === 'warn' ? 'error' : ''}`} key={sk.name}>
                <div
                  className="mcp-row-main"
                  onClick={() => setExpanded(isOpen ? null : sk.name)}
                  title={isOpen ? '收起详情' : '展开详情'}
                >
                  <span className={`mcp-dot ${st.cls === 'ok' ? 'connected' : st.cls === 'warn' ? 'error' : ''}`} />
                  <span className="mcp-name">{sk.title || sk.name}</span>
                  {(sk.tags ?? []).slice(0, 3).map((t) => (
                    <span className="mcp-tag" key={t}>
                      {t}
                    </span>
                  ))}
                  <span className="mcp-tag src">
                    {sk.source === 'market'
                      ? (PUBLISHER_LABEL[sk.publisher ?? ''] ?? '市场')
                      : '本地'}
                  </span>
                  {/* 状态点已由 .mcp-dot 承担 → 这里只上文案，不套 .sbx-status
                      （它带 ::before 圆点，会与 .mcp-dot 叠成两个点） */}
                  <span className={`mcp-status-cell ${st.cls}`}>{st.text}</span>
                  <span className="mcp-tools-cell">
                    {sk.file_count} 个文件
                  </span>
                  <span className="mcp-chevron">{isOpen ? '▾' : '▸'}</span>
                </div>

                <div className="mcp-row-actions">
                  <button
                    type="button"
                    className="mcp-act"
                    disabled={previewing === sk.name || !sk.has_manifest}
                    title={sk.has_manifest ? '查看 SKILL.md 全文' : '缺少 SKILL.md，无法查看'}
                    onClick={() => doPreview(sk.name)}
                  >
                    {previewing === sk.name ? '读取中…' : '正文'}
                  </button>
                  <button
                    type="button"
                    className={`mcp-act danger ${confirming === sk.name ? 'armed' : ''}`}
                    disabled={saving}
                    title={confirming === sk.name ? '再点一次确认删除' : '删除（连带目录与元数据）'}
                    onClick={() =>
                      confirming === sk.name ? doRemove(sk.name) : armConfirm(sk.name)
                    }
                  >
                    {confirming === sk.name ? '确认删除' : '删除'}
                  </button>
                  <button
                    type="button"
                    className={`switch ${sk.enabled ? 'on' : ''}`}
                    title={sk.enabled ? '禁用（不注入系统提示）' : '启用'}
                    disabled={saving || !sk.has_manifest}
                    onClick={() => void setSkillEnabled(sk.name, !sk.enabled)}
                  >
                    <span className="switch-knob" />
                  </button>
                </div>

                {isOpen && (
                  <div className="mcp-detail">
                    {sk.warnings.map((w, i) => (
                      <p className={sk.has_manifest ? 'mcp-inline-note' : 'sbx-error'} key={i}>
                        {w}
                      </p>
                    ))}
                    {sk.description && <p className="mcp-desc">{sk.description}</p>}
                    <p className="mcp-meta-line">
                      目录 <code>{sk.path}</code>
                    </p>
                    {sk.installed_at && (
                      <p className="mcp-meta-line">
                        来源 {sk.market_name || sk.market_id || '市场'}
                        {sk.market_url ? ` · ${sk.market_url}` : ''} · 安装于 {sk.installed_at}
                      </p>
                    )}
                    {aux.length > 0 ? (
                      <>
                        <p className="mcp-meta-line">
                          附属文件（技能正文可能指示模型执行它们）：
                        </p>
                        <div className="mcp-chips">
                          {aux.map((f) => (
                            <code className="mcp-chip" key={f}>
                              {f}
                            </code>
                          ))}
                        </div>
                      </>
                    ) : (
                      <p className="mcp-meta-line">没有附属文件（只有 SKILL.md）。</p>
                    )}
                    {preview?.name === sk.name && (
                      <>
                        {preview.res.error ? (
                          <p className="sbx-error">{preview.res.error}</p>
                        ) : (
                          <pre className="mcp-code">{preview.res.text}</pre>
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
        技能目录 <code>{config.dir ?? '~/.aigent/skills'}</code>
        <span className="mcp-hint">
          {' '}
          · 启停状态写在 <code>skills_sources.json</code>，<b>不改写你的 SKILL.md</b>
        </span>
      </p>
    </div>
  )
}
