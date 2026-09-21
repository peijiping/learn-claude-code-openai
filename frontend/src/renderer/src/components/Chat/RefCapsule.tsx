import { NodeViewWrapper, type NodeViewProps } from '@tiptap/react'
import { Icon } from '@components/common/Icon'
import { capsuleLabel } from '@lib/refFilter'

/**
 * 输入区里的引用胶囊（ProseMirror 的 React NodeView）。
 *
 * 交互（需求 5）：
 * - 显示**带后缀的文件名**，最左侧是文件 / 目录图标；
 * - **鼠标悬浮时最左侧的图标变成删除图标**，点它删掉整颗胶囊；
 * - tooltip 是**该文件/目录的绝对路径**。
 *
 * 实现要点：
 * - 节点是 `atom: true`（见 refExtension）→ Backspace 一次删整颗、光标不会落进
 *   胶囊内部，所以这里**不能有可编辑内容**；
 * - 图标位放两个图标，外面**各包一层 span** 再用 CSS 在 `:hover` 时切换显隐。
 *   为什么要包一层：`Icon` 组件自带 `style={{ display: 'block' }}`（内联样式），
 *   直接给 svg 加 class 写 `display:none` 会被内联样式压掉，hover 切换根本不生效；
 *   包一层 span 既绕开内联样式，又保证两种状态占同一个位宽、胶囊不会跳。
 * - tooltip 用原生 `title`（与 AttachmentBar 同一做法，不引新依赖）。
 */
export default function RefCapsule(props: NodeViewProps): JSX.Element {
  const attrs = props.node.attrs as {
    label?: string | null
    isDir?: boolean
    path?: string
    rel?: string
  }
  const name = capsuleLabel({ name: String(attrs.label ?? ''), isDir: !!attrs.isDir })
  const path = String(attrs.path ?? '')

  const remove = (e: React.MouseEvent): void => {
    // 必须拦住：否则点击会被 ProseMirror 当成"选中节点"，还可能拖走选区
    e.preventDefault()
    e.stopPropagation()
    props.deleteNode()
  }

  return (
    <NodeViewWrapper
      as="span"
      className="ref-capsule"
      data-ref-capsule=""
      title={path || name}
      contentEditable={false}
    >
      <span
        className="ref-capsule-icon"
        onMouseDown={remove}
        role="button"
        aria-label="移除引用"
      >
        <span className="ref-icon ref-icon-default">
          <Icon name={attrs.isDir ? 'folder' : 'fileText'} size={12} />
        </span>
        <span className="ref-icon ref-icon-remove">
          <Icon name="close" size={12} />
        </span>
      </span>
      <span className="ref-capsule-name">{name}</span>
    </NodeViewWrapper>
  )
}
