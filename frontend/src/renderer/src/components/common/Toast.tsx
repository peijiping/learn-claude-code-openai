import { useAgentStore } from '@store/agentStore'

/** 轻量全局 Toast */
export default function Toast(): JSX.Element | null {
  const toast = useAgentStore((s) => s.toast)
  const type = useAgentStore((s) => s.toastType)
  if (!toast) return null
  return <div className={`toast toast-${type}`}>{toast}</div>
}