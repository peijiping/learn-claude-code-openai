import { Icon } from '@components/common/Icon'
import { capsuleLabel } from '@lib/refFilter'
import type { RefSegment } from '@lib/refTokens'

/**
 * 消息气泡里的**内联**正文：把 `@相对路径` token 就地渲染成引用胶囊。
 *
 * 与 RefCapsule（输入区那颗可编辑的胶囊）是同一件事的两个时刻：输入区是
 * "正在引用"，这里是"这轮引用了什么"。视觉口径必须一致（`.ref-chip` 与
 * `.ref-capsule` 共用浅蓝底色），只是这里**只读**：不可删除、点击在文件管理器中定位。
 *
 * 为什么不复用 `.ref-capsule` 这套类名：那一套带着"悬浮变删除图标"的交互，
 * 且是 ProseMirror NodeView 的宿主，两者语义不同，混用会让"这行能不能点掉"
 * 变得不可预测。分开类名、共享配色变量即可。
 */
interface RefTextProps {
  /** 已切好的片段序列（由 `renderRefText` 产出，本组件不做匹配） */
  segments: RefSegment[]
  /** 点击胶囊：在系统文件管理器中定位（可缺省） */
  onOpen?: (path: string) => void
}

export default function RefText({ segments, onOpen }: RefTextProps): JSX.Element {
  return (
    <>
      {segments.map((seg) =>
        seg.kind === 'text' ? (
          // 文本片段直接输出字符串（不包 span）：与改造前的纯文本节点逐字一致，
          // 不引入额外元素、不影响换行折叠与选区复制行为
          seg.text
        ) : (
          <span
            key={seg.key}
            className="ref-chip inline"
            title={seg.ref.path}
            data-ref-path={seg.ref.path}
            onClick={onOpen ? () => onOpen(seg.ref.path) : undefined}
          >
            <Icon name={seg.ref.is_dir ? 'folder' : 'fileText'} size={12} />
            <span className="ref-chip-name">
              {capsuleLabel({ name: seg.ref.name || seg.ref.path, isDir: seg.ref.is_dir })}
            </span>
          </span>
        )
      )}
    </>
  )
}
