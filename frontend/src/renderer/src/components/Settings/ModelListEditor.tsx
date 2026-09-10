import { useEffect, useRef, useState } from 'react'
import { Icon } from '@components/common/Icon'
import type { LlmConnectionModel } from '@protocols/agentProtocol'
import { hasImageInput } from './llmShared'

interface ModelListEditorProps {
  models: LlmConnectionModel[]
  /** 勾选中的模型 id（主面板 = 当前使用模型；添加供应商 = 本次纳入的模型） */
  checkedIds: string[]
  onToggle: (id: string) => void
  /** 点击行尾参数按钮（打开模型参数/能力编辑） */
  onEdit: (m: LlmConnectionModel) => void
  /** 「刷新」：调 GET {base_url}{models_path} 拉取远端模型列表 */
  onRefresh: () => void
  refreshing?: boolean
  /** 「添加模型」：自定义模型 id 与参数 */
  onAdd: () => void
  /** 预置目录里已识别的模型 id（未识别的标记「图片能力未识别」） */
  knownIds?: Set<string>
  /** 全部勾选 / 取消勾选（⋯ 菜单） */
  onCheckAll?: (checked: boolean) => void
  /** 当前使用中的模型 id（在该行显示「当前」标签） */
  activeId?: string | null
  /** 点击模型名设为当前使用模型（设置页主面板提供） */
  onSetActive?: (id: string) => void
  emptyText?: string
}

/** 模型列表编辑区（主面板与「添加供应商」弹窗共用）。
 * 行结构：[勾选] 名称 [图片能力] [标签] [参数] */
export default function ModelListEditor({
  models,
  checkedIds,
  onToggle,
  onEdit,
  onRefresh,
  refreshing = false,
  onAdd,
  knownIds,
  onCheckAll,
  activeId,
  onSetActive,
  emptyText
}: ModelListEditorProps): JSX.Element {
  const [moreOpen, setMoreOpen] = useState(false)
  const moreRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!moreOpen) return
    const onDoc = (e: MouseEvent): void => {
      if (moreRef.current && !moreRef.current.contains(e.target as Node)) setMoreOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [moreOpen])

  return (
    <div className="mlist">
      <div className="mlist-head">
        <span className="mlist-title">模型列表</span>
        <span className="mlist-count">已选择 {checkedIds.length} 个</span>
        <span className="mlist-actions">
          <button
            className="icon-btn"
            title="刷新模型列表"
            disabled={refreshing}
            onClick={onRefresh}
          >
            <Icon name="refresh" size={15} className={refreshing ? 'spin' : undefined} />
          </button>
          <button className="btn btn-sm" onClick={onAdd}>
            添加模型
          </button>
          <div className="mlist-more" ref={moreRef}>
            <button
              className="icon-btn"
              title="更多"
              onClick={() => setMoreOpen((v) => !v)}
            >
              <Icon name="more" size={15} />
            </button>
            {moreOpen && (
              <div className="mlist-more-menu">
                <button
                  className="mlist-more-item"
                  onClick={() => {
                    setMoreOpen(false)
                    onCheckAll?.(true)
                  }}
                >
                  全选
                </button>
                <button
                  className="mlist-more-item"
                  onClick={() => {
                    setMoreOpen(false)
                    onCheckAll?.(false)
                  }}
                >
                  取消全选
                </button>
              </div>
            )}
          </div>
        </span>
      </div>

      <div className="mlist-body">
        {models.length === 0 && <div className="mlist-empty">{emptyText ?? '没有匹配的模型'}</div>}
        {models.map((m) => {
          const checked = checkedIds.includes(m.id)
          // 能力标记：声明支持图片 → 「图片输入」；自动识别但预置目录里没有该模型
          // → 「图片能力未识别」（提示用户可手动覆盖）；其余不显示。
          const recognized = !knownIds || knownIds.has(m.model)
          const capChip = hasImageInput(m.capabilities)
            ? '图片输入'
            : !recognized && m.capability_source === 'auto'
              ? '图片能力未识别'
              : ''
          return (
            <div key={m.id} className={`mlist-row ${checked ? 'checked' : ''}`}>
              <input
                type="checkbox"
                className="mlist-check"
                checked={checked}
                aria-label={`选择 ${m.display_name || m.model}`}
                onChange={() => onToggle(m.id)}
              />
              <span
                className="mlist-name"
                title={onSetActive ? `${m.model}（点击设为当前使用模型）` : m.model}
                onClick={() => (onSetActive ? onSetActive(m.id) : onToggle(m.id))}
              >
                {m.display_name || m.model}
              </span>
              {activeId && activeId === m.id && <span className="mlist-current">当前</span>}
              {!m.enabled && <span className="mlist-off">已停用</span>}
              {capChip && <span className="mlist-cap">{capChip}</span>}
              <span className="mlist-tags">
                {(m.tags ?? []).map((t) => (
                  <span key={t} className="model-tag">
                    {t}
                  </span>
                ))}
              </span>
              <button
                className="icon-btn mlist-param"
                title="模型参数与能力"
                onClick={() => onEdit(m)}
              >
                <Icon name="sliders" size={15} />
              </button>
            </div>
          )
        })}
      </div>
    </div>
  )
}
