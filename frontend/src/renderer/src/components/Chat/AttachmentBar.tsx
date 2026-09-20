import { useState } from 'react'
import { Icon } from '@components/common/Icon'
import { humanSize } from '@store/agentStore'
import type { AttachmentKind, AttachmentStats } from '@protocols/agentProtocol'

/** 后端"什么都没抽到"时写入的 warning 字面量。
 *
 *  与 `agents/attachments.py` 的 `_stage_one` / `_convert_document` 里那一处**逐字对应**
 *  —— 改后端文案必须同步改这里（doc 12 §3.2 有记录）。用**精确相等**而不是 includes：
 *  子串匹配会把"某些页无文本层，已按图像发送"误判成未交付。
 */
const WARN_NO_TEXT = '未提取到文本'

/** staging 期的空统计（占位项没有真实解析结果） */
export const EMPTY_STATS: AttachmentStats = {
  text_chars: 0,
  text_truncated: false,
  pages: null,
  images: 0,
  tables: 0,
  converter: '',
  warnings: []
}

/**
 * 附件在 UI 上的**统一视图**：草稿项（输入区）与消息附件（气泡内）都先映射到
 * 这个形状再渲染，保证"发送前看到的样子"与"发送后（含回放）看到的样子"一致。
 */
export interface AttachmentView {
  key: string
  status: 'staging' | 'ready' | 'degraded' | 'failed'
  kind: AttachmentKind | ''
  name: string
  size: number
  /** 图片缩略图 URL（`aigent-att://…`）；非图片 / 未就绪为 null */
  url: string | null
  /** 解析统计（见 `AttachmentStats`）；副标题与降级提示都由它算 */
  stats: AttachmentStats
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
        const degraded = v.status === 'degraded'
        const subtitle = failed
          ? v.error || '添加失败'
          : missing
            ? '文件已缺失'
            : attachmentMeta(v)
        const title = [
          v.name,
          v.status === 'staging' ? '读取中…' : '',
          failed ? subtitle : '',
          missing ? '文件已缺失（可能被手工删除）' : '',
          // 降级原因可能很长（"第 1、2、3 页无文本层 等 24 页，已按图像发送"），
          // 塞进单行副标题只会被省略号吃掉 —— 放 tooltip 里完整给出。
          ...(degraded ? v.stats.warnings : []),
          degraded && v.stats.converter ? `解析方式：${v.stats.converter}` : '',
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
              <span className={`att-meta ${failed ? 'err' : degraded ? 'warn' : ''}`}>
                {subtitle}
              </span>
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

/** 附件是否"什么都没交付"：后端明确说了没抽到文本，且没有图片随附。
 *
 *  注意与"扫描件"区分：扫描件的 warning 是「第 N 页无文本层，**已按图像发送**」，
 *  内容其实通过页图交付了，副标题照常显示「1 页 · 1 图」。
 */
export function nothingDelivered(stats: AttachmentStats): boolean {
  const warnings = stats?.warnings ?? []
  return warnings.includes(WARN_NO_TEXT) && (stats?.images ?? 0) === 0
}

/** 该附件是否已降级（后端给过任何 warning） */
export function isDegraded(stats: AttachmentStats): boolean {
  return (stats?.warnings ?? []).length > 0
}

/**
 * 副标题文案（草稿与消息共用同一口径 —— 这是"发送前看到的"与"回放看到的"一致的保证）。
 *
 * 口径要点：**不再拿 `text_chars > 0` 当"解析成功"的证据**。本次事故里前端显示的
 * "已提取 23 字"就是占位串本身的长度，用户与模型都被误导。现在：
 *
 * - 什么都没交付 → 直说「未能完整解析」（chip 同时标琥珀，原因在 tooltip）；
 * - 有交付 → 显示 `2.1MB · 12 页 · 5 图 · 3 表`，让用户一眼看到"这一份里有图/表"；
 * - 图片附件 → 只显示体积 + 「图片」。
 */
export function attachmentMeta(v: AttachmentView): string {
  if (v.status === 'staging') return '读取中…'
  const parts: string[] = []
  if (v.size > 0) parts.push(humanSize(v.size))
  if (v.kind === 'image') {
    parts.push('图片')
    return parts.join(' · ')
  }
  const s = v.stats ?? EMPTY_STATS
  if (nothingDelivered(s)) {
    parts.push('未能完整解析')
    return parts.join(' · ')
  }
  if (s.pages) parts.push(`${s.pages} 页`)
  else if (s.text_chars > 0) parts.push(`已提取 ${s.text_chars.toLocaleString()} 字`)
  if (s.images > 0) parts.push(`${s.images} 图`)
  if (s.tables > 0) parts.push(`${s.tables} 表`)
  return parts.join(' · ')
}
