import { useEffect, useRef } from 'react'
import { Icon } from '@components/common/Icon'
import { REF_RENDER_LIMIT, capsuleLabel, type RefCandidate } from '@lib/refFilter'

/**
 * `@` 引用候选面板（挂在 `.composer` 内，**向上弹出**）。
 *
 * 定位刻意**相对输入框**而不是相对光标：需求要的是"高度固定、在对话框上方"，
 * 所以用 `position:absolute; bottom:100%` 即可，不引 tippy / floating-ui。
 *
 * 键盘与鼠标交互一律由上层（refExtension 的 onKeyDown + 本组件的事件）驱动，
 * 本组件只负责渲染 + 把 hover/click 回传 —— 单一事实源是 `selected`。
 */

export interface RefPickerState {
  query: string
  command: (item: RefCandidate) => void
}

interface RefPickerProps {
  state: RefPickerState | null
  /** 过滤后的条目（由渲染方从候选缓存现算，见 InputBox —— 不放进 state，
   *  否则异步到货的候选列表不会刷新面板） */
  items: RefCandidate[]
  /** 当前高亮项（键盘与鼠标共用，见 refExtension 的注释） */
  selected: number
  loading: boolean
  error: string
  /** 当前空间不可引用（default 草稿空间 / 目录不可用） */
  disabled: boolean
  reason: string
  /** 后端条目上限截断 */
  truncated: boolean
  onHover: (index: number) => void
  onPick: (item: RefCandidate) => void
}

export default function RefPicker({
  state,
  items,
  selected,
  loading,
  error,
  disabled,
  reason,
  truncated,
  onHover,
  onPick
}: RefPickerProps): JSX.Element | null {
  const listRef = useRef<HTMLDivElement>(null)

  // 键盘移动后让高亮项留在可视区（block:'nearest' = 已在视野内就不动，避免跳动）
  useEffect(() => {
    if (!state) return
    const el = listRef.current?.querySelector(`[data-idx="${selected}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [selected, state])

  if (!state) return null

  const shown = items.slice(0, REF_RENDER_LIMIT)
  const hidden = items.length - shown.length

  return (
    <div
      className="ref-picker"
      role="listbox"
      // 在面板上按下鼠标**不能**让输入框失焦：失焦会打断 Suggestion 的激活态，
      // 后续的选中命令就落不到编辑器上。所以整块面板阻止默认行为。
      onMouseDown={(e) => e.preventDefault()}
    >
      {disabled ? (
        <div className="ref-picker-empty">{reason || '当前工作空间没有可引用的文件'}</div>
      ) : error ? (
        <div className="ref-picker-empty">{error}</div>
      ) : loading ? (
        <div className="ref-picker-empty">正在读取工作空间…</div>
      ) : shown.length === 0 ? (
        <div className="ref-picker-empty">无匹配项</div>
      ) : (
        <div className="ref-picker-list" ref={listRef}>
          {shown.map((item, idx) => (
            <div
              key={item.path}
              data-idx={idx}
              role="option"
              aria-selected={idx === selected}
              className={`ref-picker-item ${idx === selected ? 'active' : ''}`}
              title={item.path}
              onMouseEnter={() => onHover(idx)}
              onMouseDown={(e) => {
                e.preventDefault()
                onPick(item)
              }}
            >
              <Icon name={item.isDir ? 'folder' : 'fileText'} size={14} />
              <span className="ref-picker-name">{capsuleLabel(item)}</span>
              <span className="ref-picker-dir">{item.dir}</span>
            </div>
          ))}
        </div>
      )}

      {(truncated || hidden > 0) && (
        <div className="ref-picker-foot">
          {hidden > 0
            ? `还有 ${hidden} 项，继续输入以缩小范围`
            : '列表已达上限，仅显示前一部分，继续输入以缩小范围'}
        </div>
      )}
    </div>
  )
}
