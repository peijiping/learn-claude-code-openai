import { useEffect, useRef } from 'react'
import { Icon } from '@components/common/Icon'

interface MessageMenuProps {
  /** 视口坐标（右键光标位置） */
  x: number
  y: number
  onCopy: () => void
  onClose: () => void
}

/** 消息右键菜单：fixed 定位于触发坐标，提供复制等操作。 */
export default function MessageMenu({ x, y, onCopy, onClose }: MessageMenuProps): JSX.Element {
  const ref = useRef<HTMLDivElement>(null)

  // 点击菜单外部 / 再次右键别处 / Esc 关闭
  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onDocContextMenu = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    document.addEventListener('contextmenu', onDocContextMenu)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      document.removeEventListener('contextmenu', onDocContextMenu)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  // 靠近视口边缘时收回来，避免菜单被裁剪
  const left = Math.min(x, window.innerWidth - 140)
  const top = Math.min(y, window.innerHeight - 72)

  return (
    <div className="session-menu message-menu" style={{ left, top }} ref={ref}>
      <button
        className="session-menu-item"
        onClick={() => {
          onCopy()
          onClose()
        }}
      >
        <Icon name="copy" size={14} />
        <span>复制</span>
      </button>
    </div>
  )
}