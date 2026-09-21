import { Icon } from '@components/common/Icon'

/**
 * 拖拽遮罩：整块 `.composer` 变为投放区时的视觉反馈。
 *
 * `pointer-events: none` 是硬要求 —— 遮罩层若接收指针事件，`drop` 就会落在遮罩上
 * 而不是 composer，投放直接失效（拖拽交互最经典的坑）。
 */
export default function DropOverlay({ visible }: { visible: boolean }): JSX.Element | null {
  if (!visible) return null
  return (
    <div className="drop-overlay" aria-hidden="true">
      <div className="drop-overlay-inner">
        <Icon name="filePlus" size={22} />
        <span>松开即可添加文件或图片</span>
      </div>
    </div>
  )
}
