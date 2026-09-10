import { useEffect } from 'react'
import { useAgentStore, ConnState, PythonState } from '@store/agentStore'
import type { UiEvent } from '@protocols/agentProtocol'

/**
 * useAgentStream - 订阅主进程转发的后端事件与状态，写入 agentStore。
 * 渲染进程不直接连 Python，只经 preload 桥接收。
 */
export function useAgentStream(): void {
  const handleEvent = useAgentStore((s) => s.handleEvent)
  const setConnection = useAgentStore((s) => s.setConnection)
  const setPython = useAgentStore((s) => s.setPython)
  const connection = useAgentStore((s) => s.connection)

  useEffect(() => {
    if (!window.agent) return // preload 未就绪时静默跳过，避免整树崩溃
    const offEvent = window.agent.onEvent((e) => handleEvent(e as UiEvent))
    const offStatus = window.agent.onStatus((s) => setConnection(s as ConnState))
    const offPython = window.agent.onPythonStatus((s) => setPython(s as PythonState))
    // agent:status 是"变化驱动"（主进程只在状态翻转时推送）。渲染进程刷新
    // （HMR/Cmd+R）后 store 归零但主进程连接没变，必须主动查一次当前连接态，
    // 否则 connection 永远停在 disconnected（状态栏误显"未连接"）。
    window.agent
      .getConnectionStatus?.()
      .then((s) => setConnection(s as ConnState))
      .catch(() => undefined)
    // 初次进入查询一次连接与会话
    useAgentStore.getState().refreshSessions().catch(() => undefined)
    return () => {
      offEvent()
      offStatus()
      offPython()
    }
  }, [handleEvent, setConnection, setPython])

  // 启动瞬间后端往往尚未就绪，首次 listSessions 会超时；
  // 等 WebSocket 真正连上后再补一次会话列表查询。
  // 同时向后端拉取仍在运行的会话状态（重放 session_status）：渲染进程刷新
  // 不重建主进程 WS 连接，后端"新连接重放"覆盖不到，runningSessions/bgSessions
  // 会随刷新丢失（表现为"子智能体还在后台跑，前端执行指示却消失"），必须补偿。
  useEffect(() => {
    if (connection === 'connected') {
      useAgentStore.getState().refreshSessions().catch(() => undefined)
      window.agent?.queryStatus?.().catch(() => undefined)
    }
  }, [connection])
}