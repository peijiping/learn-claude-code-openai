import { useAgentStore, type SettingsTab } from '@store/agentStore'
import { Icon } from '@components/common/Icon'
import ModelSettings from './Settings/ModelSettings'

const NAV: { key: SettingsTab; label: string }[] = [
  { key: 'general', label: '通用' },
  { key: 'model', label: '模型' },
  { key: 'about', label: '关于' }
]

/** 设置弹窗：应用窗口正中央弹出，左侧菜单栏（通用/模型/关于）。 */
export default function SettingsModal(): JSX.Element {
  const tab = useAgentStore((s) => s.settingsTab)
  const openSettings = useAgentStore((s) => s.openSettings)
  const closeSettings = useAgentStore((s) => s.closeSettings)
  const loadLlConfig = useAgentStore((s) => s.loadLlConfig)

  const switchTab = (k: SettingsTab): void => {
    openSettings(k)
    if (k === 'model') void loadLlConfig()
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
          <div className="settings-body">
            {tab === 'model' && <ModelSettings />}
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