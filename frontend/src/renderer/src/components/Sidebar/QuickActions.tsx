import { Icon } from '@components/common/Icon'
import { showToast, useAgentStore } from '@store/agentStore'

const items = [
  { key: 'new', label: '新建任务', icon: 'plus', shortcut: '⌘^N' },
  { key: 'plugin', label: '插件市场', icon: 'puzzle' },
  { key: 'template', label: '模板库', icon: 'clipboard' },
  { key: 'automation', label: '自动化', icon: 'bolt' },
  { key: 'assistant', label: '办公助理', icon: 'chat' }
]

export default function QuickActions(): JSX.Element {
  const newSession = useAgentStore((s) => s.newSession)

  const onClick = (key: string): void => {
    if (key === 'new') void newSession()
    else {
      showToast(`${key === 'plugin' ? '插件市场' : key === 'template' ? '模板库' : key === 'automation' ? '自动化' : '办公助理'}：后续增量`)
    }
  }

  return (
    <div className="sidebar-block">
      {items.map((it) => (
        <button key={it.key} className="quick-action" onClick={() => onClick(it.key)}>
          <Icon name={it.icon} size={16} />
          <span className="quick-label">{it.label}</span>
          {it.shortcut && <span className="quick-shortcut">{it.shortcut}</span>}
        </button>
      ))}
    </div>
  )
}