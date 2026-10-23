import { useEffect, useMemo, useState } from 'react'
import { showToast, useAgentStore } from '@store/agentStore'
import type { PermissionConfig } from '@protocols/agentProtocol'

/** 权限管理页（设置弹窗「权限」，docs/frontend/18）。
 *
 * 七分区单页滚动，与 `~/.aigent/config/permissions.json` 的键一一对应：
 * ① 模式 ② 审批超时 ③ 额外目录 ④ 白名单 ⑤ 危险 ⑥ 硬拒绝 ⑦ MCP。
 *
 * **自定义规则（原⑦）已于 2026-09-22 下线**：它的三种动作与三个分类区一一对应
 * （拒绝→⑥、询问→⑤、允许→④），单独成区只会让同一语义有两个写入口，而「谁先判定」
 * 对用户不可见。存量 `rules` 由后端迁移进三个列表（见 `agents/permission.py`）。
 * 页首「判定顺序」区块的数据来自后端 `builtin.order`，是用户理解冲突结果的唯一依据。
 *
 * 三条关键约束（改这个文件前先读）：
 * 1. **草稿模型**：进页拉取 → 本地 draft → 「保存更改」才发命令。改动不发任何命令。
 * 2. **保存后以后端回执为准整份回填**：后端 `_normalize` 会钳位/丢弃非法项，
 *    保留本地 draft 会让界面与磁盘内容不一致（下次打开才发现变了）。
 * 3. **内置清单与判定顺序都来自后端**（`builtin` / `builtin.order`），前端零硬编码 ——
 *    自建一份必然与后端常量、与 `evaluate` 的真实次序漂移。
 */

const MODE_OPTIONS: { value: PermissionConfig['default_mode']; label: string; desc: string }[] = [
  {
    value: 'default',
    label: '默认模式',
    desc: '只读命令自动放行；危险操作弹审批卡片，由你决定是否执行。'
  },
  {
    value: 'full_access',
    label: '完全访问',
    desc: '除硬拒绝清单（如 rm -rf /）外一律直接执行，不再询问。'
  }
]

const MCP_OPTIONS: { value: PermissionConfig['mcp_destructive']; label: string; desc: string }[] = [
  { value: 'ask', label: '询问', desc: 'MCP 破坏性工具一律弹审批 —— 注意：完全访问模式下也会问。' },
  { value: 'allow', label: '自动放行', desc: '完全访问模式下不询问；默认模式仍按常规判定。' }
]

const FALLBACK_TIMEOUT = { default: 300, min: 60, max: 3600 }

/** 深拷贝（不依赖 structuredClone 的 lib 声明） */
function clone(c: PermissionConfig): PermissionConfig {
  return JSON.parse(JSON.stringify(c)) as PermissionConfig
}

export default function PermissionSettings(): JSX.Element {
  const result = useAgentStore((s) => s.permissionConfig)
  const saving = useAgentStore((s) => s.permissionSaving)
  const loadPermissionConfig = useAgentStore((s) => s.loadPermissionConfig)
  const savePermissionConfig = useAgentStore((s) => s.savePermissionConfig)

  const config = result?.config ?? null
  const builtin = result?.builtin
  const timeoutRange = builtin?.timeout ?? FALLBACK_TIMEOUT

  const [draft, setDraft] = useState<PermissionConfig | null>(null)
  // 输入框的临时文本（数字框清空/中间态，不能直接绑 draft）
  const [timeoutText, setTimeoutText] = useState('')
  const [cmdInput, setCmdInput] = useState('')
  const [dangerInput, setDangerInput] = useState('')
  const [denyInput, setDenyInput] = useState('')

  // 首次进页懒加载（切 tab 时由 SettingsModal 触发；直接进入时兜底）
  useEffect(() => {
    if (!result) void loadPermissionConfig()
  }, [result, loadPermissionConfig])

  // 回执（get / save）到来即同步 draft —— 后端是归一化权威源
  useEffect(() => {
    if (config) setDraft(clone(config))
  }, [config])

  useEffect(() => {
    if (config) setTimeoutText(String(config.approval_timeout_seconds))
  }, [config?.approval_timeout_seconds])

  const dirty = useMemo(() => {
    if (!draft || !config) return false
    return JSON.stringify(draft) !== JSON.stringify(config)
  }, [draft, config])

  // save 回执才带 applied；get 回执不带 → 只有保存过才展示 msg/warnings
  const isSaveResult = result?.applied !== undefined
  const lastWarnings = isSaveResult ? (result?.warnings ?? []) : []
  const lastMsg = isSaveResult ? result?.msg : undefined

  const timeoutNum = Number(timeoutText)
  const timeoutInvalid =
    timeoutText.trim() === '' ||
    !Number.isInteger(timeoutNum) ||
    timeoutNum < timeoutRange.min ||
    timeoutNum > timeoutRange.max

  if (!draft || !builtin) {
    return <div className="perm-loading">读取权限配置…</div>
  }

  const patch = (p: Partial<PermissionConfig>): void => setDraft({ ...draft, ...p })

  // ── ③ 额外目录 ────────────────────────────────────────────────
  const addDir = async (): Promise<void> => {
    let picked: string | null = null
    try {
      picked = await window.agent.pickFolder()
    } catch {
      showToast('无法打开目录选择框', 'error', 4000)
      return
    }
    if (!picked) return
    if (draft.additional_dirs.includes(picked)) {
      showToast('该目录已在列表中', 'info')
      return
    }
    patch({ additional_dirs: [...draft.additional_dirs, picked] })
  }

  const removeDir = (dir: string): void =>
    patch({ additional_dirs: draft.additional_dirs.filter((d) => d !== dir) })

  // ── ④ 白名单 ─────────────────────────────────────────────────
  const customCmds = draft.safe_commands.list.filter((c) => !builtin.safe_commands.includes(c))

  const addCmd = (): void => {
    const v = cmdInput.trim()
    if (!v) return
    if (draft.safe_commands.list.includes(v)) {
      showToast('该命令已在白名单中', 'info')
      return
    }
    patch({ safe_commands: { ...draft.safe_commands, list: [...draft.safe_commands.list, v] } })
    setCmdInput('')
  }

  const removeCmd = (cmd: string): void => {
    const list = draft.safe_commands.list.filter((c) => c !== cmd)
    patch({ safe_commands: { ...draft.safe_commands, list } })
    // 删空 ≠ 关闭白名单：后端会把空列表回落到内置全量，这里当场说清楚
    if (list.length === 0) {
      showToast('白名单已空，保存后将回落到内置默认清单（关闭白名单请用上方开关）', 'info', 5000)
    }
  }

  const resetCmds = (): void =>
    patch({ safe_commands: { ...draft.safe_commands, list: [...builtin.safe_commands] } })

  // ── ⑤⑥ 追加模式 ──────────────────────────────────────────────
  const addPattern = (key: 'dangerous_patterns' | 'deny_patterns', value: string): void => {
    const v = value.trim()
    if (!v) return
    if (draft[key].includes(v)) {
      showToast('该项已在列表中', 'info')
      return
    }
    patch({ [key]: [...draft[key], v] } as Partial<PermissionConfig>)
  }

  const removePattern = (key: 'dangerous_patterns' | 'deny_patterns', value: string): void =>
    patch({ [key]: draft[key].filter((x) => x !== value) } as Partial<PermissionConfig>)

  // ── 保存 ─────────────────────────────────────────────────────
  const save = async (): Promise<void> => {
    if (timeoutInvalid) return
    patch({ approval_timeout_seconds: timeoutNum })
    const ok = await savePermissionConfig({ ...draft, approval_timeout_seconds: timeoutNum })
    if (ok) showToast('权限配置已保存并生效', 'info')
  }

  const revert = (): void => {
    if (config) setDraft(clone(config))
    setCmdInput('')
    setDangerInput('')
    setDenyInput('')
  }

  return (
    <div className="perm">
      <div className="perm-intro">
        <p>
          权限判定分三档：<b>直接放行</b> / <b>弹审批卡片</b> / <b>硬拒绝</b>。本页配置写入
          <code> {result?.path ?? '~/.aigent/config/permissions.json'}</code>，保存后<b>立即生效</b>
          （无需重启后端）。
        </p>
        {result?.exists === false && (
          <p className="perm-intro-note">
            尚未保存过自定义配置，当前显示的是内置默认值 —— 保存后会创建该文件。
          </p>
        )}
      </div>

      {/* 判定顺序：数据来自后端 builtin.order，与 evaluate 的实现次序同源。
          下面 ④⑤⑥ 三个分区是「怎么写配置」，这里回答「写了之后谁先说话」。 */}
      {(builtin.order ?? []).length > 0 && (
        <div className="perm-order">
          <div className="perm-order-head">
            判定顺序
            <span className="perm-order-sub">自上而下依次判定，命中即停 —— 越靠上的越严格</span>
          </div>
          <ol className="perm-order-list">
            {(builtin.order ?? []).map((o, i) => (
              <li key={o.key} className={`perm-order-item order-${o.key}`}>
                <span className="perm-order-rank">{i + 1}</span>
                <div className="perm-order-body">
                  <div className="perm-order-line">
                    <span className="perm-order-label">{o.label}</span>
                    <span className="perm-order-effect">{o.effect}</span>
                    <span className="perm-order-pos">{o.rank}</span>
                  </div>
                  <p className="perm-order-note">{o.note}</p>
                </div>
              </li>
            ))}
          </ol>
          <p className="perm-order-tail">
            冲突时更严的一方赢：同一条模式既在 ⑤ 又在 ④，按 ⑤ 处理（要过问）；既在 ⑥ 则直接拒绝。
          </p>
        </div>
      )}

      {/* ① 权限模式 */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">① 权限模式</h4>
        <p className="perm-sec-desc">
          仅决定<b>新建会话</b>的初始档位。已有会话各有自己的档位，在输入区的盾牌 chip 里单独切换。
        </p>
        <div className="perm-cards">
          {MODE_OPTIONS.map((m) => (
            <button
              key={m.value}
              className={`perm-card ${draft.default_mode === m.value ? 'active' : ''}`}
              onClick={() => patch({ default_mode: m.value })}
            >
              <span className="perm-card-label">{m.label}</span>
              <span className="perm-card-desc">{m.desc}</span>
            </button>
          ))}
        </div>
      </section>

      {/* ② 审批超时 */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">② 审批等待超时</h4>
        <p className="perm-sec-desc">
          审批卡片下发后等待作答的秒数，超时按拒绝处理（{timeoutRange.min}–{timeoutRange.max}，默认{' '}
          {timeoutRange.default}）。
        </p>
        <div className="perm-inline">
          <input
            className={`input perm-input-num ${timeoutInvalid ? 'invalid' : ''}`}
            value={timeoutText}
            onChange={(e) => {
              const text = e.target.value
              setTimeoutText(text)
              const n = Number(text)
              if (text.trim() !== '' && Number.isInteger(n) && n >= timeoutRange.min && n <= timeoutRange.max) {
                patch({ approval_timeout_seconds: n })
              }
            }}
          />
          <span className="perm-unit">秒</span>
          {timeoutInvalid && (
            <span className="perm-invalid">
              需为 {timeoutRange.min}–{timeoutRange.max} 的整数
            </span>
          )}
        </div>
      </section>

      {/* ③ 额外目录 */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">③ 额外目录</h4>
        <p className="perm-sec-desc">
          这些目录内的读写视同工作区（不弹审批）。敏感路径（密钥 / .env / .ssh）仍被硬拒绝。
        </p>
        {draft.additional_dirs.length === 0 ? (
          <p className="perm-empty">暂无额外目录</p>
        ) : (
          <ul className="perm-list">
            {draft.additional_dirs.map((d) => (
              <li key={d} className="perm-list-item">
                <code className="perm-path">{d}</code>
                <button className="icon-btn danger perm-del" title="移除" onClick={() => removeDir(d)}>
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
        <button className="btn btn-sm perm-add" onClick={() => void addDir()}>
          + 添加目录…
        </button>
      </section>

      {/* ④ 安全命令白名单 */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">
          ④ 安全命令白名单
          <button
            className={`switch ${draft.safe_commands.enabled ? 'on' : ''}`}
            title={draft.safe_commands.enabled ? '点击关闭白名单' : '点击启用白名单'}
            onClick={() =>
              patch({ safe_commands: { ...draft.safe_commands, enabled: !draft.safe_commands.enabled } })
            }
          >
            <span className="switch-knob" />
          </button>
        </h4>
        <p className="perm-sec-desc">
          默认模式下，命中白名单的命令直接执行（其余走审批）。关闭开关 = 所有命令都进审批流程。
          <b>逐段生效</b>：复合命令里只有命中的片段被放行，其余片段仍要过问 ——
          所以 <code>ls && rm x</code> 不会因为 <code>ls</code> 在白名单而整体放行。
        </p>
        <div className="perm-chips">
          {builtin.safe_commands.map((c) => (
            <span key={c} className="perm-chip builtin" title="内置项（只读）">
              {c}
            </span>
          ))}
          {customCmds.map((c) => (
            <span key={c} className="perm-chip custom">
              {c}
              <button className="perm-chip-del" title="移除" onClick={() => removeCmd(c)}>
                ×
              </button>
            </span>
          ))}
        </div>
        <div className="perm-inline">
          <input
            className="input"
            placeholder="输入命令前缀，回车添加（如 npm install）"
            value={cmdInput}
            onChange={(e) => setCmdInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') addCmd()
            }}
          />
          <button className="btn btn-sm" onClick={addCmd}>
            添加
          </button>
          <button className="btn btn-sm" onClick={resetCmds} title="恢复为内置白名单">
            恢复默认
          </button>
        </div>
      </section>

      {/* ⑤ 危险命令 */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">⑤ 危险命令</h4>
        <p className="perm-sec-desc">
          默认模式弹审批；完全访问模式直接执行。内置项只读，可追加自定义模式。
          <b>先于 ④ 白名单判定</b>，所以它同时是「给白名单开例外」的地方：
          ④ 放行 <code>git</code>（大类免问），这里追加 <code>git push</code>（特例仍过问）。
        </p>
        <div className="perm-chips">
          {builtin.dangerous.map((c) => (
            <span key={c} className="perm-chip builtin" title="内置项（只读）">
              {c}
            </span>
          ))}
          {draft.dangerous_patterns
            .filter((c) => !builtin.dangerous.includes(c))
            .map((c) => (
              <span key={c} className="perm-chip warn">
                {c}
                <button
                  className="perm-chip-del"
                  title="移除"
                  onClick={() => removePattern('dangerous_patterns', c)}
                >
                  ×
                </button>
              </span>
            ))}
        </div>
        <div className="perm-inline">
          <input
            className="input"
            placeholder="追加危险模式，回车添加（如 git push）"
            value={dangerInput}
            onChange={(e) => setDangerInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                addPattern('dangerous_patterns', dangerInput)
                setDangerInput('')
              }
            }}
          />
          <button
            className="btn btn-sm"
            onClick={() => {
              addPattern('dangerous_patterns', dangerInput)
              setDangerInput('')
            }}
          >
            添加
          </button>
        </div>
      </section>

      {/* ⑥ 硬拒绝 */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">⑥ 硬拒绝</h4>
        <div className="perm-danger-note">
          <b>最先判定</b>：任何模式下（含完全访问）均直接拒绝，无法被审批、白名单或额外目录放行。
          内置项不可删除。
        </div>
        <div className="perm-chips">
          {builtin.deny.map((c) => (
            <span key={c} className="perm-chip deny" title="内置项（只读）">
              {c}
            </span>
          ))}
          {draft.deny_patterns
            .filter((c) => !builtin.deny.includes(c))
            .map((c) => (
              <span key={c} className="perm-chip deny">
                {c}
                <button
                  className="perm-chip-del"
                  title="移除"
                  onClick={() => removePattern('deny_patterns', c)}
                >
                  ×
                </button>
              </span>
            ))}
        </div>
        <div className="perm-inline">
          <input
            className="input"
            placeholder="追加硬拒绝模式，回车添加"
            value={denyInput}
            onChange={(e) => setDenyInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                addPattern('deny_patterns', denyInput)
                setDenyInput('')
              }
            }}
          />
          <button
            className="btn btn-sm"
            onClick={() => {
              addPattern('deny_patterns', denyInput)
              setDenyInput('')
            }}
          >
            添加
          </button>
        </div>
        <div className="perm-sub">敏感路径（任何模式都拒绝访问）</div>
        <ul className="perm-list">
          {builtin.deny_paths.map((p) => (
            <li key={p} className="perm-list-item">
              <code className="perm-path">{p}</code>
            </li>
          ))}
        </ul>
      </section>

      {/* ⑦ MCP 破坏性工具 */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">⑦ MCP 破坏性工具</h4>
        <p className="perm-sec-desc">
          服务端声明了 <code>destructiveHint</code> 的工具（删除 / 覆盖类）。
        </p>
        <div className="perm-cards">
          {MCP_OPTIONS.map((m) => (
            <button
              key={m.value}
              className={`perm-card ${draft.mcp_destructive === m.value ? 'active' : ''}`}
              onClick={() => patch({ mcp_destructive: m.value })}
            >
              <span className="perm-card-label">{m.label}</span>
              <span className="perm-card-desc">{m.desc}</span>
            </button>
          ))}
        </div>
      </section>

      {lastWarnings.length > 0 && (
        <div className="perm-warns">
          <div className="perm-warns-title">保存成功，但以下内容被调整或未生效：</div>
          <ul>
            {lastWarnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      <footer className="perm-foot">
        <span className={`perm-foot-status ${dirty ? 'dirty' : ''}`}>
          {lastMsg && !dirty ? lastMsg : dirty ? '未保存更改' : '已是最新'}
        </span>
        <button className="btn" disabled={!dirty || saving} onClick={revert}>
          放弃更改
        </button>
        <button
          className="btn btn-primary"
          disabled={!dirty || saving || timeoutInvalid}
          onClick={() => void save()}
        >
          {saving ? '保存中…' : '保存更改'}
        </button>
      </footer>
    </div>
  )
}
