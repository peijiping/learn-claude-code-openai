import { useEffect, useMemo, useRef, useState } from 'react'
import { Icon } from '@components/common/Icon'
import { sessionDisplayName, useAgentStore } from '@store/agentStore'

const pad = (n: number): string => String(n).padStart(2, '0')

/** iso 时间 → "MM-DD HH:mm"（回收站列表展示用） */
function fmtTime(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 回收站：已软删除会话列表，支持单选/多选彻底删除与还原。 */
export default function TrashSettings(): JSX.Element {
  const trashSessions = useAgentStore((s) => s.trashSessions)
  const refreshTrash = useAgentStore((s) => s.refreshTrash)
  const restoreSession = useAgentStore((s) => s.restoreSession)
  const deleteSessions = useAgentStore((s) => s.deleteSessions)

  const [selected, setSelected] = useState<Set<number>>(new Set())
  // 永久删除二次确认：首次点击记录目标并进入确认态，3 秒内再点执行，超时恢复
  const [confirming, setConfirming] = useState<number[] | null>(null)
  const confirmTimer = useRef<number | undefined>(undefined)

  useEffect(() => {
    void refreshTrash()
    return () => window.clearTimeout(confirmTimer.current)
  }, [refreshTrash])

  const selectedArr = useMemo(
    () => trashSessions.filter((s) => selected.has(s.num)).map((s) => s.num),
    [trashSessions, selected]
  )
  const allChecked = trashSessions.length > 0 && selectedArr.length === trashSessions.length

  const toggle = (num: number): void => {
    setConfirming(null)
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(num)) next.delete(num)
      else next.add(num)
      return next
    })
  }
  const toggleAll = (): void => {
    setConfirming(null)
    setSelected(allChecked ? new Set() : new Set(trashSessions.map((s) => s.num)))
  }

  const armConfirm = (nums: number[]): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(nums)
    confirmTimer.current = window.setTimeout(() => setConfirming(null), 3000)
  }
  const doDelete = (nums: number[]): void => {
    window.clearTimeout(confirmTimer.current)
    setConfirming(null)
    setSelected(new Set())
    void deleteSessions(nums)
  }

  const batchConfirming =
    confirming !== null &&
    confirming.length === selectedArr.length &&
    confirming.every((n) => selectedArr.includes(n))

  return (
    <div className="trash-page">
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

      {trashSessions.length === 0 ? (
        <div className="trash-empty">回收站为空</div>
      ) : (
        <div className="trash-list">
          {trashSessions.map((s) => (
            <div key={s.num} className="trash-row">
              <input
                type="checkbox"
                checked={selected.has(s.num)}
                onChange={() => toggle(s.num)}
              />
              <span className="trash-title" title={sessionDisplayName(s)}>
                {sessionDisplayName(s)}
              </span>
              <span className="trash-meta">
                {s.message_count} 条 · 删除于 {fmtTime(s.trashed_at ?? s.created_at) || '—'}
              </span>
              <button
                className="btn trash-restore-btn"
                onClick={() => void restoreSession(s.num)}
              >
                <Icon name="restore" size={13} />
                <span>还原</span>
              </button>
              <button
                className={`btn trash-row-delete-btn ${
                  confirming?.length === 1 && confirming[0] === s.num ? 'confirm' : ''
                }`}
                onClick={() =>
                  confirming?.length === 1 && confirming[0] === s.num
                    ? doDelete([s.num])
                    : armConfirm([s.num])
                }
              >
                {confirming?.length === 1 && confirming[0] === s.num ? '确认' : '删除'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
