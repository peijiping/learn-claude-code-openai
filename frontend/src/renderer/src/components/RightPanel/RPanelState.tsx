import type { ReactNode } from 'react'
import { Icon } from '@components/common/Icon'

/**
 * 右栏统一的「空 / 加载中 / 失败 / 降级」状态块（19 篇 §5.9）。
 *
 * 为什么统一成一个组件：右栏有**五处**要表达"这里现在没内容"
 * （文件树不可用 / 树为空 / 预览读取中 / 文件过大 / git 不是仓库 / git 干净），
 * 各写各的会让"同一件事在五个地方长得不一样"，而这五处的差别**只在文案与语义色**。
 *
 * ⚠️ 语义色的取舍：`warn`（琥珀）与 `error`（红）**必须分清**
 * —— 降级不是错误（tokens.css:25-27 的既有约定）。"文件太大"、"内容被截断"
 * 都是 warn；只有"真的读不到"才是 error。用红色吓人一次，用户就会开始
 * 忽略所有红色。
 */
export type RPanelStateKind = 'empty' | 'loading' | 'error' | 'warn'

interface RPanelStateProps {
  kind: RPanelStateKind
  title: string
  /** 补充说明（原因 / 尺寸 / 上限），可缺省 */
  sub?: string
  /** 非 loading 时显示的图标名（默认 fileText） */
  icon?: string
  /** 操作区（按钮等），可缺省 */
  action?: ReactNode
}

export default function RPanelState({
  kind,
  title,
  sub,
  icon,
  action
}: RPanelStateProps): JSX.Element {
  return (
    <div className={`rpanel-state rpanel-state--${kind}`}>
      <div className="rpanel-state__icon">
        {kind === 'loading' ? (
          <span className="rpanel-spinner" />
        ) : (
          <Icon name={icon ?? 'fileText'} size={22} />
        )}
      </div>
      <div className="rpanel-state__title">{title}</div>
      {sub ? <div className="rpanel-state__sub">{sub}</div> : null}
      {action ? <div className="rpanel-state__action">{action}</div> : null}
    </div>
  )
}
