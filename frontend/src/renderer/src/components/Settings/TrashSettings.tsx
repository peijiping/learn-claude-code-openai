import { useEffect, useMemo, useRef, useState } from 'react'
import { Icon } from '@components/common/Icon'
import { sessionDisplayName, useAgentStore } from '@store/agentStore'

const pad = (n: number): string => String(n).padStart(2, '0')

/** 「全部工作空间」筛选项的哨兵值（真实空间 id 以 'ws' 开头，不会撞） */
const FILTER_ALL = '__all__'
/** 筛选默认值：默认工作空间（2026-09-20 需求：归档页默认只看 default） */
const FILTER_DEFAULT = 'default'

/** iso 时间 → "MM-DD HH:mm"（归档列表展示用） */
function fmtTime(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 归档页（原回收站，key 仍为 trash）：已归档会话列表，支持按工作空间筛选、
 *  单选/多选彻底删除与还原。归档≠删除：还原即回到原工作空间的会话列表；
 *  彻底删除不可恢复。这里**只有会话级操作，不能删除工作空间** —— 工作空间的
 *  删除在侧边栏空间菜单（只删元数据目录），两者互不可达。 */
export default function TrashSettings(): JSX.Element {
  const trashSessions = useAgentStore((s) => s.trashSessions)
  const refreshTrash = useAgentStore((s) => s.refreshTrash)
  const restoreSession = useAgentStore((s) => s.restoreSession)
  const deleteSessions = useAgentStore((s) => s.deleteSessions)
  const projects = useAgentStore((s) => s.projects)

  // 工作空间筛选：默认「默认工作空间」；切筛选清空勾选，避免跨空间批量误删
  const [wsFilter, setWsFilter] = useState<string>(FILTER_DEFAULT)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // 永久删除二次确认：首次点击记录目标并进入确认态，3 秒内再点执行，超时恢复
  const [confirming, setConfirming] = useState<string[] | null>(null)
  const confirmTimer = useRef<number | undefined>(undefined)

  useEffect(() => {
    void refreshTrash()
    return () => window.clearTimeout(confirmTimer.current)
  }, [refreshTrash])

  // 当前筛选下可见的归档会话（后端每条都带 project；存量缺省视为 default）
  const visible = useMemo(
    () =>
      trashSessions.filter(
        (s) => wsFilter === FILTER_ALL || (s.project ?? 'default') === wsFilter
      ),
    [trashSessions, wsFilter]
  )

  const selectedArr = useMemo(
    () => visible.filter((s) => selected.has(s.id)).map((s) => s.id),
    [visible, selected]
  )
  const allChecked = visible.length > 0 && selectedArr.length === visible.length

  const changeFilter = (v: string): void => {
    setConfirming(null)
    setSelected(new Set())
    setWsFilter(v)
  }
  const toggle = (id: string): void => {
    setConfirming(null)
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const toggleAll = (): void => {
    setConfirming(null)
    setSelected(allChecked ? new Set() : new Set(visible.map((s) => s.id)))
  }

  const armConfirm = (ids: string[]): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(ids)
    confirmTimer.current = window.setTimeout(() => setConfirming(null), 3000)
  }
  const doDelete = (ids: string[]): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(null)
    setSelected(new Set())
    void deleteSessions(ids)
  }

  const batchConfirming =
    confirming !== null &&
    confirming.length === selectedArr.length &&
    confirming.every((n) => selectedArr.includes(n))

  return (
    <div className="trash-page">
      {/* 工作空间筛选：位于全选按钮上方（2026-09-20 需求），默认「默认工作空间」 */}
      <div className="trash-filter-row">
        <label className="trash-ws-filter">
          <span>工作空间</span>
          <select value={wsFilter} onChange={(e) => changeFilter(e.target.value)}>
            <option value={FILTER_ALL}>全部工作空间</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.id === 'default' ? '默认工作空间' : p.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="trash-header">
        <label className="trash-check-all">
          <input type="checkbox" checked={allChecked} onChange={toggleAll} />
          <span>{selectedArr.length > 0 ? `已选 ${selectedArr.length} 项` : '全选'}</span>
        </label>
        <button
          className="btn trash-delete-btn"
          disabled={selectedArr.length === 0}
          onClick={() => (batchConfirming ? doDelete(selectedArr) : armConfirm(selectedArr))}
        >
          {batchConfirming ? `确认删除 ${selectedArr.length} 项？` : `彻底删除(${selectedArr.length})`}
        </button>
      </div>

      {visible.length === 0 ? (
        <div className="trash-empty">暂无归档会话</div>
      ) : (
        <div className="trash-list">
          {visible.map((s) => (
            <div key={s.id} className="trash-row">
              <input
                type="checkbox"
                checked={selected.has(s.id)}
                onChange={() => toggle(s.id)}
              />
              <span className="trash-title" title={sessionDisplayName(s)}>
                {sessionDisplayName(s)}
              </span>
              <span className="trash-meta">
                归档于 {fmtTime(s.trashed_at ?? s.created_at) || '—'}
              </span>
              <button
                className="btn trash-restore-btn"
                onClick={() => void restoreSession(s.id)}
              >
                <Icon name="restore" size={13} />
                <span>还原</span>
              </button>
              <button
                className={`btn trash-row-delete-btn ${
                  confirming?.length === 1 && confirming[0] === s.id ? 'confirm' : ''
                }`}
                onClick={() =>
                  confirming?.length === 1 && confirming[0] === s.id
                    ? doDelete([s.id])
                    : armConfirm([s.id])
                }
              >
                {confirming?.length === 1 && confirming[0] === s.id ? '确认' : '删除'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
