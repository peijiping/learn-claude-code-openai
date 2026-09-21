import { Icon } from '@components/common/Icon'
import { capsuleLabel } from '@lib/refFilter'
import type { MessageRef } from '@protocols/agentProtocol'

/**
 * 消息气泡里的**只读**引用 chip（回放 / 实时消息共用）。
 *
 * 与输入区的 `.ref-capsule`（可编辑的 NodeView）刻意分成两套样式类：一个是
 * "正在引用"，一个是"这轮引用了什么"。视觉上必须能一眼区分，且都要与附件的
 * `.att-*` 明确不同 —— 引用是**零复制**的路径指向，不能让人误以为"已上传"。
 */
interface RefBarProps {
  refs?: MessageRef[]
  /** 点击 chip：在系统文件管理器中定位（可缺省） */
  onOpen?: (path: string) => void
}

export default function RefBar({ refs, onOpen }: RefBarProps): JSX.Element | null {
  if (!refs || refs.length === 0) return null
  return (
    <div className="ref-bar">
      {refs.map((r) => (
        <span
          key={r.path}
          className="ref-chip"
          title={r.path}
          onClick={onOpen ? () => onOpen(r.path) : undefined}
        >
          <Icon name={r.is_dir ? 'folder' : 'fileText'} size={12} />
          <span className="ref-chip-name">{capsuleLabel({ name: r.name, isDir: r.is_dir })}</span>
        </span>
      ))}
    </div>
  )
}
