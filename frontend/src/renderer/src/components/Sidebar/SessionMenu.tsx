import { useEffect, useRef } from 'react'
import { Icon } from '@components/common/Icon'

interface SessionMenuProps {
  /** 视口坐标（三点按钮的点击位置 / 右键的光标位置） */
  x: number
  y: number
  /** 流式进行中等场景禁用菜单项 */
  disabled?: boolean
  onRename: () => void
  onDelete: () => void
  onClose: () => void
}

/** 会话项弹出菜单：三点按钮与右键菜单共用，fixed 定位于触发坐标。 */
export default function SessionMenu({
  x,
  y,
  disabled,
  onRename,
  onDelete,
  onClose
}: SessionMenuProps): JSX.Element {
  const ref = useRef<HTMLDivElement>(null)

  // 点击菜单外部 / Esc 关闭
  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  // 靠近视口边缘时收回来，避免菜单被裁剪
  const left = Math.min(x, window.innerWidth - 150)
  const top = Math.min(y, window.innerHeight - 96)

  return (
    <div className="session-menu" style={{ left, top }} ref={ref}>
      <button
        className="session-menu-item"
        disabled={disabled}
        onClick={() => {
          onRename()
          onClose()
        }}
      >
        <Icon name="edit" size={14} />
        <span>重命名</span>
      </button>
      <button
        className="session-menu-item danger"
        disabled={disabled}
        onClick={() => {
          onDelete()
          onClose()
        }}
      >
        <Icon name="trash" size={14} />
        <span>删除</span>
      </button>
    </div>
  )
}
