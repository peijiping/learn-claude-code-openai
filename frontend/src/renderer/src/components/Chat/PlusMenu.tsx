import { useEffect, useRef } from 'react'
import { Icon } from '@components/common/Icon'

/**
 * 加号「添加内容」菜单的条目 key。
 * 本期只交付**弹框 + 菜单项外壳**，各条目功能待逐项确认后再实现；
 * 接线点见 InputBox.tsx 的 handlePlusPick（按 key 分支）。
 */
export type PlusMenuKey =
  | 'attachFile' // 添加文件或图片
  | 'refPath' // 引用文件或文件夹
  | 'command' // 使用命令或 Skill
  | 'planMode' // 计划模式
  | 'goalMode' // 目标模式

interface PlusMenuItem {
  key: PlusMenuKey
  /** Icon 组件里的图标名 */
  icon: string
  label: string
}

interface PlusMenuSection {
  title: string
  items: PlusMenuItem[]
}

/** 菜单结构：两段式（添加内容 / 执行方式），顺序与文案对齐设计稿。
 *  原第三段「验收 → 交付」已于 2026-09-20 按用户要求移除（语义未想清楚，参考产品也去掉了）。 */
export const PLUS_MENU_SECTIONS: PlusMenuSection[] = [
  {
    title: '添加内容',
    items: [
      { key: 'attachFile', icon: 'filePlus', label: '添加文件或图片' },
      { key: 'refPath', icon: 'at', label: '引用文件或文件夹' },
      { key: 'command', icon: 'slash', label: '使用命令或 Skill' }
    ]
  },
  {
    title: '执行方式',
    items: [
      { key: 'planMode', icon: 'listTodo', label: '计划模式' },
      { key: 'goalMode', icon: 'target', label: '目标模式' }
    ]
  }
]

/** key → 中文名（提示文案/日志复用，避免各处再抄一份字面量） */
export const PLUS_MENU_LABELS: Record<PlusMenuKey, string> = Object.fromEntries(
  PLUS_MENU_SECTIONS.flatMap((s) => s.items.map((it) => [it.key, it.label]))
) as Record<PlusMenuKey, string>

interface PlusMenuProps {
  /**
   * 当前激活的「执行方式」条目（计划模式 / 目标模式），null = 都未开启。
   * 「执行方式」二选一属于**会话级状态**（后端持有唯一真相），调用方把会话当前的
   * `execution_mode` 映射进来即可显示勾选；**新建任务（无会话）时映射的是
   * `pendingExecMode` 草稿**（2026-09-27）—— 两项在无会话态同样可选、勾选态照常显示。
   */
  activeKey?: PlusMenuKey | null
  /** 置灰的条目（**先于操作的反馈**：比"点了再弹错误"好）。
   *  2026-09-27 起只剩**一类**场景：默认工作空间下「引用文件或文件夹」不可用
   *  （草稿目录没有可引用的文件）。「执行方式」两项**任何情况下都不再置灰** ——
   *  无会话时点选记为 `pendingExecMode` 草稿，随首条消息落地（原"无会话置灰 +
   *  「请先发送一条消息创建会话」提示"已删除）。 */
  disabledKeys?: PlusMenuKey[]
  /** 置灰原因（作为 title 提示） */
  disabledReason?: string
  onPick: (key: PlusMenuKey) => void
  onClose: () => void
}

/** 加号「添加内容」面板：向上弹出，两段式分组，点击条目即回调并收起 */
export default function PlusMenu({
  activeKey = null,
  disabledKeys = [],
  disabledReason = '',
  onPick,
  onClose
}: PlusMenuProps): JSX.Element {
  const ref = useRef<HTMLDivElement>(null)

  // 点击面板外部 / Esc 关闭（与 MessageMenu 同款；遮罩负责绝大多数外部点击）
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

  return (
    <>
      <div className="plus-menu-mask" onClick={onClose} />
      <div className="plus-menu" role="menu" ref={ref}>
        {PLUS_MENU_SECTIONS.map((sec) => (
          <div className="plus-menu-section" key={sec.title}>
            <div className="plus-menu-group">{sec.title}</div>
            {sec.items.map((it) => {
              const disabled = disabledKeys.includes(it.key)
              return (
                // 用 div 而非 button：菜单项内容简单但也避免与父级 button 语义冲突
                <div
                  key={it.key}
                  role="menuitem"
                  aria-disabled={disabled}
                  className={`plus-menu-item ${activeKey === it.key ? 'active' : ''} ${disabled ? 'disabled' : ''}`}
                  title={disabled ? (disabledReason || '当前不可用') : undefined}
                  onClick={() => {
                    if (disabled) return
                    onPick(it.key)
                  }}
                >
                  <Icon name={it.icon} size={16} />
                  <span className="plus-menu-label">{it.label}</span>
                  {activeKey === it.key && <Icon name="check" size={13} />}
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </>
  )
}
