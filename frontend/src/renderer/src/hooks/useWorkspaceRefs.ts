import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toCandidates, type RefCandidate } from '@lib/refFilter'
import type { RefsPayload } from '@protocols/agentProtocol'

/**
 * 工作空间引用候选的取数 hook。
 *
 * 策略（见 docs/frontend/13 的取舍）：**打开 `@` 时一次拉回完整扁平列表**，
 * 之后按键的过滤全在前端本地做（零延迟）。因此这里只在第一次 `ensure()` 时真正
 * 请求后端，后续复用缓存；空间 / 会话变化会让缓存作废并重拉（列表是按沙箱根
 * 解析出来的，换根必须重取）。
 */

export interface WorkspaceRefs {
  items: RefCandidate[]
  loading: boolean
  error: string
  /** 后端条目上限截断 */
  truncated: boolean
  /** 当前空间不可引用（default 草稿空间 / 目录不可用） */
  disabled: boolean
  reason: string
  /** 面板打开时调用；已加载过则直接返回 */
  ensure: () => void
}

interface RefState {
  key: string
  data: RefsPayload | null
  loading: boolean
  error: string
}

const EMPTY: RefState = { key: '', data: null, loading: false, error: '' }

export function useWorkspaceRefs(
  projectId: string | null,
  sessionId: string | null
): WorkspaceRefs {
  // 缓存键：沙箱根由 (空间, 会话) 共同决定（会话可能带 work_root 快照）
  const key = `${projectId ?? ''}|${sessionId ?? ''}`
  const [state, setState] = useState<RefState>(EMPTY)
  const stateRef = useRef(state)
  const inflight = useRef('')

  useEffect(() => {
    stateRef.current = state
  }, [state])

  // 空间/会话变了 → 作废旧缓存（旧数据属于另一个沙箱根，留着会选到读不到的路径）
  useEffect(() => {
    setState((prev) => (prev.key === key ? prev : { ...EMPTY, key }))
    inflight.current = ''
  }, [key])

  const ensure = useCallback((): void => {
    const cur = stateRef.current
    if (cur.key === key && (cur.loading || cur.data)) return
    if (inflight.current === key) return
    inflight.current = key
    setState({ key, data: null, loading: true, error: '' })
    window.agent
      .listRefs({ projectId, sessionId })
      .then((res) => {
        if (inflight.current === key) inflight.current = ''
        setState((prev) =>
          prev.key !== key
            ? prev
            : {
                key,
                data: (res as RefsPayload | null) ?? null,
                loading: false,
                // null = 主进程等待超时（后端没回 refs 信封）
                error: res ? '' : '读取工作空间超时，请重试'
              }
        )
      })
      .catch(() => {
        if (inflight.current === key) inflight.current = ''
        setState((prev) =>
          prev.key !== key ? prev : { key, data: null, loading: false, error: '读取工作空间失败' }
        )
      })
  }, [key, projectId, sessionId])

  const ready = state.key === key
  const data = ready ? state.data : null
  const items = useMemo(
    () => toCandidates(data?.workdir ?? '', data?.items ?? []),
    [data]
  )

  return {
    items,
    loading: ready && state.loading,
    error: ready ? state.error : '',
    truncated: !!data?.truncated,
    disabled: !!data?.disabled,
    reason: data?.reason ?? '',
    ensure
  }
}
