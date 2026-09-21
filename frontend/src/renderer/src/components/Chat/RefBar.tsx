import { Icon } from '@components/common/Icon'
import { capsuleLabel } from '@lib/refFilter'
import type { MessageRef } from '@protocols/agentProtocol'

/**
 * 消息气泡里的**兜底**引用行（回放 / 实时消息共用）。
 *
 * 2026-09-21 起，正文里的 `@相对路径` token 会被 `RefText` 就地渲染成内联胶囊
 * （只显示文件名，与输入区一致），**所以正常路径下这里没有东西可画** ——
 * MessageItem 只把"没能内联渲染"的引用传进来（例如正文里找不到 token 的老数据），
 * 避免同一批引用在气泡里出现两次。
 *
 * 与输入区的 `.ref-capsule`（可编辑的 NodeView）刻意分成两套样式类：一个是
 * "正在引用"，一个是"这轮引用了什么"。两者**共用同一组配色变量**（浅蓝底 + 深蓝字），
 * 且都要与附件的 `.att-*` 明确不同 —— 引用是**零复制**的路径指向，不能让人
 * 误以为"已上传"。
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
          data-ref-path={r.path}
          onClick={onOpen ? () => onOpen(r.path) : undefined}
        >
          <Icon name={r.is_dir ? 'folder' : 'fileText'} size={12} />
          <span className="ref-chip-name">{capsuleLabel({ name: r.name, isDir: r.is_dir })}</span>
        </span>
      ))}
    </div>
  )
}
