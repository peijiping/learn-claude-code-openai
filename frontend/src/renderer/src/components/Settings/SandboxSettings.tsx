import { useEffect, useRef, useState } from 'react'
import { useAgentStore } from '@store/agentStore'

/** 沙盒设置页（设置弹窗「沙盒」，docs/frontend/20）。
 *
 * 沙盒 = 执行层的"绝对墙"（macOS Seatbelt / Linux bubblewrap），与权限管控
 * （策略层，判定层）互补：开关开启且平台后端可用时，bash 命令的写被限制在
 * 工作区内、网络默认断开、敏感目录（~/.ssh、~/.aigent）不可读。
 *
 * 双平台分区都展示（可提前为另一台机器准备配置），当前平台分区高亮：
 * - macOS：Seatbelt profile 文本编辑器（~/.aigent/sandbox/seatbelt.sb）
 * - Linux：bubblewrap 参数编辑器（~/.aigent/sandbox/bwrap_args.txt，每行一个参数）
 *
 * 模板支持占位符 {{WORKDIR}} / {{EXTRA_WRITABLE}} / {{TMPDIR}} / {{HOME}} /
 * {{COMMAND}}（bwrap 专用），执行时由后端按会话动态替换。
 *
 * 四条关键约束（改这个文件前先读）：
 * 1. **回执为权威源，但只在"后端值真的变了"时才回灌草稿**：早期版本对任何回执都
 *    无条件重置两份草稿，于是（a）保存被拒时把用户刚敲的内容抹成磁盘旧值、
 *    （b）只切一下总开关也清空两个编辑器（2026-09-24 修）。现在按**内容差分**
 *    判定：receipt 里该字段与上一次不同才回灌。这也顺手免疫了"同一次保存 store 被
 *    设置两次"（事件分支 `case 'sandbox_config'` + IPC promise 各一次）导致的
 *    重复 effect —— 第二次差分必为"未变"，不会再回灌。
 * 2. **被拒保存绝不回灌**：applied=false = 后端原子拒绝（一个字节都没写）→ 后端值
 *    与上一次相同 → 差分判定天然保留草稿，用户接着改。
 * 3. **开关即改即存**：toggle 直接发 save（后端写 config.json + 覆写 env 热生效），
 *    无需单独「保存」按钮；模板编辑器各带「保存 / 恢复默认模板」。
 * 4. **错误不 toast**：保存校验在后端（模板缺必需占位符 → applied=false + errors），
 *    页面内联展示 —— 用户要对着错误改文本，toast 一闪而过等于没提示。后端也**不再**
 *    额外发 `error` 信封（那会被 store 当全局 toast 弹出来）。
 */

const PLATFORM_LABELS: Record<string, string> = {
  darwin: 'macOS',
  linux: 'Linux',
  win32: 'Windows'
}

const BACKEND_LABELS: Record<string, string> = {
  seatbelt: 'Seatbelt',
  bwrap: 'bubblewrap (bwrap)'
}

export default function SandboxSettings(): JSX.Element {
  const result = useAgentStore((s) => s.sandboxConfig)
  const saving = useAgentStore((s) => s.sandboxSaving)
  const loadSandboxConfig = useAgentStore((s) => s.loadSandboxConfig)
  const saveSandboxConfig = useAgentStore((s) => s.saveSandboxConfig)

  // 两个编辑器的本地草稿；仅当回执里该字段**内容变化**时才回灌（见文件头约束 1）
  const [seatbeltDraft, setSeatbeltDraft] = useState('')
  const [bwrapDraft, setBwrapDraft] = useState('')
  // 上一份回执里的模板内容（差分基准；null = 还没收到过回执 → 首次全量初始化）
  const prevProfiles = useRef<{ seatbelt: string; bwrap: string } | null>(null)

  // 首次进页懒加载（切 tab 时由 SettingsModal 触发；直接进入时兜底）
  useEffect(() => {
    if (!result) void loadSandboxConfig()
  }, [result, loadSandboxConfig])

  useEffect(() => {
    if (!result) return
    const before = prevProfiles.current
    prevProfiles.current = {
      seatbelt: result.seatbelt_profile,
      bwrap: result.bwrap_args
    }
    if (!before || before.seatbelt !== result.seatbelt_profile) {
      setSeatbeltDraft(result.seatbelt_profile)
    }
    if (!before || before.bwrap !== result.bwrap_args) {
      setBwrapDraft(result.bwrap_args)
    }
  }, [result])

  if (!result) {
    return <div className="perm-loading">读取沙盒设置…</div>
  }

  const platformLabel = PLATFORM_LABELS[result.platform] ?? result.platform
  const backendLabel = result.backend ? (BACKEND_LABELS[result.backend] ?? result.backend) : null
  const isMac = result.platform === 'darwin'
  const isLinux = result.platform === 'linux'
  const dirtySeatbelt = seatbeltDraft !== result.seatbelt_profile
  const dirtyBwrap = bwrapDraft !== result.bwrap_args
  // save 回执才带 applied；get 回执不带 → 只有保存失败过才展示错误
  const lastErrors = result.applied === false ? (result.errors ?? []) : []

  // 状态行三态（+ off 一态）：总开关关（中性灰）/ 后端被 off（警告）/ 后端不可用
  // （警告）/ 生效中（绿）。**必须同时看 sandbox_enabled 与 backend_available**
  // —— 只看后者会出现"用户把开关关掉、状态行还写生效中"（2026-09-24 修）。
  const statusView = !result.sandbox_enabled
    ? { cls: 'off', text: '沙盒已关闭，命令不受隔离地执行' }
    : result.backend_available
      ? { cls: 'ok', text: `当前平台 ${platformLabel} · ${backendLabel} 可用，沙盒生效中` }
      : result.reason === 'off'
        ? { cls: 'warn', text: '沙盒后端被配置为 off（SANDBOX_BACKEND），命令不受隔离地执行' }
        : { cls: 'warn', text: `当前平台 ${platformLabel} 暂不支持沙盒，命令不受隔离地执行` }

  return (
    <div className="sbx">
      {/* ── 总开关 + 状态行 ── */}
      <section className="perm-sec">
        <div className="sbx-switch-row">
          <div className="sbx-switch-text">
            <h4 className="perm-sec-title">启用沙盒</h4>
            <p className="perm-sec-desc">
              开启后智能体执行的命令将被限制在项目目录内，无法访问网络与敏感文件
              （~/.ssh、~/.aigent）。保存后立即生效，无需重启。完全访问模式下沙盒同样生效
              —— 免审批不免墙。可写范围 = 工作区 ∪ 权限页「额外目录」∪ 临时目录。
            </p>
          </div>
          <button
            type="button"
            className={`switch ${result.sandbox_enabled ? 'on' : ''}`}
            title={result.sandbox_enabled ? '关闭沙盒' : '开启沙盒'}
            disabled={saving}
            onClick={() => void saveSandboxConfig({ sandbox_enabled: !result.sandbox_enabled })}
          >
            <span className="switch-knob" />
          </button>
        </div>
        <p className={`sbx-status ${statusView.cls}`}>{statusView.text}</p>
        {lastErrors.map((e, i) => (
          <p key={`${i}-${e}`} className="sbx-error">
            {e}
          </p>
        ))}
      </section>

      {/* ── macOS · Seatbelt profile ── */}
      <section className={`perm-sec sbx-section ${isMac ? 'current' : ''}`}>
        <h4 className="perm-sec-title">
          macOS · Seatbelt profile
          {isMac && <span className="sbx-current-tag">当前平台</span>}
        </h4>
        <p className="perm-sec-desc">
          配置文件 <code>{result.seatbelt_path ?? '~/.aigent/sandbox/seatbelt.sb'}</code>
          ，保存后立即生效。删除 <code>(deny network*)</code> 行即可放行网络；
          模板缺占位符会被拒存（错误条里写明缺哪个）。
        </p>
        <p className="sbx-placeholders">
          占位符：<code>{'{{WORKDIR}}'}</code> 工作区 ·{' '}
          <code>{'{{EXTRA_WRITABLE}}'}</code> 会话批准的可写目录 ·{' '}
          <code>{'{{TMPDIR}}'}</code> 临时目录 · <code>{'{{HOME}}'}</code> 主目录
        </p>
        <textarea
          className="sbx-editor mono"
          spellCheck={false}
          rows={14}
          value={seatbeltDraft}
          onChange={(e) => setSeatbeltDraft(e.target.value)}
        />
        <div className="sbx-actions">
          <button
            className="btn btn-sm"
            disabled={saving || !dirtySeatbelt}
            onClick={() => void saveSandboxConfig({ seatbelt_profile: seatbeltDraft })}
          >
            保存
          </button>
          <button
            className="btn btn-sm"
            disabled={saving}
            title="用系统默认模板覆盖当前内容"
            onClick={() => void saveSandboxConfig({ reset: 'seatbelt' })}
          >
            恢复默认模板
          </button>
        </div>
      </section>

      {/* ── Linux · bubblewrap 参数 ── */}
      <section className={`perm-sec sbx-section ${isLinux ? 'current' : ''}`}>
        <h4 className="perm-sec-title">
          Linux · bubblewrap 参数
          {isLinux && <span className="sbx-current-tag">当前平台</span>}
        </h4>
        <p className="perm-sec-desc">
          配置文件{' '}
          <code>{result.bwrap_path ?? '~/.aigent/sandbox/bwrap_args.txt'}</code>
          ，每行一个参数，保存后立即生效。网络靠 <code>--unshare-net</code> 隔离，
          删掉该行即放行（注意：Linux 上放行是全放，包括回环）。
        </p>
        <p className="sbx-placeholders">
          占位符：<code>{'{{WORKDIR}}'}</code> ·{' '}
          <code>{'{{EXTRA_WRITABLE}}'}</code> · <code>{'{{TMPDIR}}'}</code> ·{' '}
          <code>{'{{HOME}}'}</code> · <code>{'{{COMMAND}}'}</code>（必须保留，整行替换为要执行的命令）
        </p>
        <textarea
          className="sbx-editor mono"
          spellCheck={false}
          rows={14}
          value={bwrapDraft}
          onChange={(e) => setBwrapDraft(e.target.value)}
        />
        <div className="sbx-actions">
          <button
            className="btn btn-sm"
            disabled={saving || !dirtyBwrap}
            onClick={() => void saveSandboxConfig({ bwrap_args: bwrapDraft })}
          >
            保存
          </button>
          <button
            className="btn btn-sm"
            disabled={saving}
            title="用系统默认模板覆盖当前内容"
            onClick={() => void saveSandboxConfig({ reset: 'bwrap' })}
          >
            恢复默认模板
          </button>
        </div>
      </section>
    </div>
  )
}
