import { useEffect, useState } from 'react'
import { useAgentStore } from '@store/agentStore'

/** 启动画面遮罩（方案B）：后端冷启动期间覆盖主界面，避免直接暴露"连接中"状态条。
 * 后端 connected（或崩溃，避免永久遮挡）后淡出并卸载。 */
export default function StartupOverlay(): JSX.Element | null {
  const connection = useAgentStore((s) => s.connection)
  const python = useAgentStore((s) => s.python)
  const ready = connection === 'connected' || python === 'crashed'
  const [leaving, setLeaving] = useState(false)
  const [done, setDone] = useState(false)

  useEffect(() => {
    if (ready) {
      setLeaving(true)
      const t = setTimeout(() => setDone(true), 320)
      return () => clearTimeout(t)
    }
  }, [ready])

  if (done) return null

  return (
    <div className={`startup-overlay${leaving ? ' startup-overlay-leave' : ''}`}>
      <div className="startup-brand">AIGENT</div>
      <div className="startup-spinner" />
      <div className="startup-text">正在启动后端…</div>
    </div>
  )
}