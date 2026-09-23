import { Icon } from '@components/common/Icon'
import { RPANEL_AVAILABLE_VIEWS, RPANEL_VIEW_LABELS, type RPanelView } from '@protocols/agentProtocol'
import { RPANEL_VIEW_ORDER } from '@lib/rpanelTabs'
import { RPANEL_VIEW_ICONS } from './viewMeta'

interface RPanelWelcomeProps {
  /** 点任意一项 = 添加对应视图标签（与「+」菜单同一个动作） */
  onPick: (view: RPanelView) => void
}

/**
 * 欢迎态（19 篇 §5.4）：右栏开着但**一个标签都没有**时显示。
 *
 * 语义上它就是「+」菜单的平铺版 —— 点任意一项走的动作完全一致
 * （`openViewTab`），所以刻意**不做**"选了就退休"的隐藏逻辑之外的花样：
 * 把标签全关光，它会再回来，这是对的（那时用户确实需要一个入口）。
 *
 * 终端 / 浏览器置灰 + 「第二期」：**禁用不用红/琥珀** —— 禁用不是错误，
 * 也不是降级（tokens.css 里那两种色的语义都被别处占着）。
 */
export default function RPanelWelcome({ onPick }: RPanelWelcomeProps): JSX.Element {
  return (
    <div className="rpanel-welcome">
      <div className="rpanel-welcome-title">向右侧添加内容</div>
      <div className="rpanel-welcome-list">
        {RPANEL_VIEW_ORDER.map((view) => {
          const available = RPANEL_AVAILABLE_VIEWS.includes(view)
          return (
            <button
              key={view}
              className={`rpanel-welcome-item${available ? '' : ' disabled'}`}
              disabled={!available}
              onClick={() => onPick(view)}
            >
              <span className="rpanel-welcome-icon">
                <Icon name={RPANEL_VIEW_ICONS[view]} size={16} />
              </span>
              <span className="rpanel-welcome-label">{RPANEL_VIEW_LABELS[view]}</span>
              {available ? <Icon name="chevronRight" size={14} /> : <span className="rpanel-welcome-badge">第二期</span>}
            </button>
          )
        })}
      </div>
    </div>
  )
}
