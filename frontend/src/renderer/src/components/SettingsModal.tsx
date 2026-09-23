import { useAgentStore, type SettingsTab } from '@store/agentStore'
import { Icon } from '@components/common/Icon'
import ModelSettings from './Settings/ModelSettings'
import PermissionSettings from './Settings/PermissionSettings'
import SandboxSettings from './Settings/SandboxSettings'
import TrashSettings from './Settings/TrashSettings'

const NAV: { key: SettingsTab; label: string }[] = [
  { key: 'general', label: '通用' },
  { key: 'model', label: '模型' },
  { key: 'permission', label: '权限' },
  { key: 'sandbox', label: '沙盒' },
  { key: 'trash', label: '归档' },
  { key: 'about', label: '关于' }
]

/** 设置弹窗：应用窗口正中央弹出，左侧菜单栏（通用/模型/权限/沙盒/归档/关于）。
 *  「归档」（key 仍为 trash）= 原回收站：软删除会话在此还原 / 彻底删除。
 *  「权限」= 权限管控全局配置（~/.aigent/config/permissions.json，docs/frontend/18）。
 *  「沙盒」= 执行隔离开关与双平台模板编辑（docs/frontend/20）。 */
export default function SettingsModal(): JSX.Element {
  const tab = useAgentStore((s) => s.settingsTab)
  const openSettings = useAgentStore((s) => s.openSettings)
  const closeSettings = useAgentStore((s) => s.closeSettings)
  const loadLlConfig = useAgentStore((s) => s.loadLlConfig)
  const refreshTrash = useAgentStore((s) => s.refreshTrash)

  const switchTab = (k: SettingsTab): void => {
    openSettings(k)
    if (k === 'model') void loadLlConfig()
    if (k === 'trash') void refreshTrash()
  }

  return (
    <div className="settings-mask" onClick={closeSettings}>
      <div className="settings" onClick={(e) => e.stopPropagation()}>
        <aside className="settings-nav">
          <div className="settings-nav-head">设置</div>
          {NAV.map((n) => (
            <button
              key={n.key}
              className={`settings-nav-item ${tab === n.key ? 'active' : ''}`}
              onClick={() => switchTab(n.key)}
            >
              {n.label}
            </button>
          ))}
        </aside>
        <section className="settings-content">
          <div className="settings-content-head">
            <span>{NAV.find((n) => n.key === tab)?.label}</span>
            <button className="icon-btn" title="关闭" onClick={closeSettings}>
              <Icon name="close" size={16} />
            </button>
          </div>
          <div className={`settings-body ${tab === 'model' ? 'flush' : ''}`}>
            {tab === 'model' && <ModelSettings />}
            {tab === 'permission' && <PermissionSettings />}
            {tab === 'sandbox' && <SandboxSettings />}
            {tab === 'trash' && <TrashSettings />}
            {tab === 'general' && <GeneralSettings />}
            {tab === 'about' && <AboutSettings />}
          </div>
        </section>
      </div>
    </div>
  )
}

function GeneralSettings(): JSX.Element {
  return (
    <div className="placeholder-page">
      <p>通用设置</p>
      <span>功能规划中，后续增量。</span>
    </div>
  )
}

function AboutSettings(): JSX.Element {
  return (
    <div className="placeholder-page">
      <p>关于</p>
      <span>桌面智能体客户端 · 基于 Python Agent Harness + Electron。</span>
    </div>
  )
}