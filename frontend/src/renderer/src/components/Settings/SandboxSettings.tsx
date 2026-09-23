import { useEffect, useState } from 'react'
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
 * 三条关键约束（改这个文件前先读）：
 * 1. **回执为权威源**：get/save 回执（sandbox_config）到来即整份重置编辑器草稿 ——
 *    后端磁盘内容才是真值，保留本地 draft 会让界面与磁盘不一致。
 * 2. **保存校验在后端**：模板缺必需占位符时 save 回执 applied=false + errors，
 *    页面内联展示（不 toast —— 用户要对着错误改文本，toast 一闪而过等于没提示）。
 * 3. **开关即改即存**：toggle 直接发 save（后端写 config.json + 覆写 env 热生效），
 *    无需单独「保存」按钮；模板编辑器各带「保存 / 恢复默认模板」。
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

  // 两个编辑器的本地草稿；回执（get / save）到来即重置 —— 后端是权威源
  const [seatbeltDraft, setSeatbeltDraft] = useState('')
  const [bwrapDraft, setBwrapDraft] = useState('')

  // 首次进页懒加载（切 tab 时由 SettingsModal 触发；直接进入时兜底）
  useEffect(() => {
    if (!result) void loadSandboxConfig()
  }, [result, loadSandboxConfig])

  useEffect(() => {
    if (result) {
      setSeatbeltDraft(result.seatbelt_profile)
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
              —— 免审批不免墙。
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
        <p className={`sbx-status ${result.backend_available ? 'ok' : 'warn'}`}>
          {result.backend_available
            ? `当前平台 ${platformLabel} · ${backendLabel} 可用，沙盒生效中`
            : `当前平台 ${platformLabel} 暂不支持沙盒，命令不受隔离地执行`}
        </p>
        {lastErrors.map((e) => (
          <p key={e} className="sbx-error">{e}</p>
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
          模板损坏时点「恢复默认模板」找回。
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
          ，每行一个参数，保存后立即生效。删除 <code>--unshare-net</code> 行即可放行网络
          （注意：Linux 上放行是全放，包括回环）。
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
