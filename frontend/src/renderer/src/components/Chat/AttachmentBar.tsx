import { useState } from 'react'
import { Icon } from '@components/common/Icon'
import { humanSize } from '@store/agentStore'
import type { AttachmentKind } from '@protocols/agentProtocol'

/**
 * 附件在 UI 上的**统一视图**：草稿项（输入区）与消息附件（气泡内）都先映射到
 * 这个形状再渲染，保证"发送前看到的样子"与"发送后（含回放）看到的样子"一致。
 */
export interface AttachmentView {
  key: string
  status: 'staging' | 'ready' | 'failed'
  kind: AttachmentKind | ''
  name: string
  size: number
  /** 图片缩略图 URL（`aigent-att://…`）；非图片 / 未就绪为 null */
  url: string | null
  /** 副标题（体积 / 已提取字数）；失败时不用它 */
  meta: string
  /** 失败原因（status=failed 时展示） */
  error?: string
  /** 后端归位时发现文件已不在 → 显示「文件已缺失」占位 */
  missing?: boolean
  sourcePath: string
  storedPath: string
}

interface AttachmentBarProps {
  items: AttachmentView[]
  /** 传了才显示删除按钮（草稿态） */
  onRemove?: (key: string) => void
  /** 点击 chip（消息态 = 在 Finder 中显示原文件） */
  onOpen?: (view: AttachmentView) => void
  /** 消息气泡内使用（尺寸更小、不换行显示名字） */
  compact?: boolean
}

function iconOf(kind: AttachmentKind | ''): string {
  if (kind === 'image') return 'image'
  if (kind === 'document') return 'fileText'
  return 'fileText'
}

/** 附件条：输入区（可删）与消息气泡（只读）共用 */
export default function AttachmentBar({
  items,
  onRemove,
  onOpen,
  compact = false
}: AttachmentBarProps): JSX.Element | null {
  // 缩略图加载失败（文件被手工删除 / 路径不合法）→ 就地降级为占位，
  // 而不是留一个破图（协议处理器对非法路径一律 404）
  const [broken, setBroken] = useState<Record<string, boolean>>({})
  if (items.length === 0) return null

  return (
    <div className={`att-bar ${compact ? 'compact' : ''}`}>
      {items.map((v) => {
        const failed = v.status === 'failed'
        const missing = !!v.missing || broken[v.key]
        const thumb = v.url && !missing && !failed ? v.url : null
        const subtitle = failed ? (v.error || '添加失败') : missing ? '文件已缺失' : v.meta
        const title = [
          v.name,
          v.status === 'staging' ? '读取中…' : '',
          failed ? subtitle : '',
          missing ? '文件已缺失（可能被手工删除）' : '',
          v.sourcePath ? `原始路径：${v.sourcePath}` : ''
        ]
          .filter(Boolean)
          .join('\n')

        return (
          <div
            key={v.key}
            className={`att-chip ${v.status} ${missing ? 'missing' : ''} ${onOpen ? 'clickable' : ''}`}
            title={title}
            onClick={onOpen ? () => onOpen(v) : undefined}
          >
            {thumb ? (
              <img
                className="att-thumb"
                src={thumb}
                alt={v.name}
                onError={() => setBroken((b) => ({ ...b, [v.key]: true }))}
              />
            ) : (
              <span className={`att-icon ${v.status}`}>
                <Icon name={v.status === 'staging' ? 'clock' : iconOf(v.kind)} size={compact ? 13 : 14} />
              </span>
            )}
            <span className="att-text">
              <span className="att-name">{v.name}</span>
              <span className={`att-meta ${failed ? 'err' : ''}`}>{subtitle}</span>
            </span>
            {onRemove && (
              <button
                type="button"
                className="att-remove"
                title="移除"
                onClick={(e) => {
                  e.stopPropagation()
                  onRemove(v.key)
                }}
              >
                <Icon name="close" size={12} />
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}

/** 体积 / 提取结果的副标题文案（草稿与消息共用同一口径） */
export function attachmentMeta(size: number, kind: AttachmentKind | '', textChars = 0): string {
  const parts: string[] = []
  if (size > 0) parts.push(humanSize(size))
  if (kind === 'image') parts.push('图片')
  else if (textChars > 0) parts.push(`已提取 ${textChars.toLocaleString()} 字`)
  return parts.join(' · ')
}
