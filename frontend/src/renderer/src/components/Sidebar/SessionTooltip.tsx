import { Icon } from '@components/common/Icon'
import type { SessionMeta, UsageStats } from '@protocols/agentProtocol'
import { sessionDisplayName } from '@store/agentStore'

interface SessionTooltipProps {
  /** 视口坐标（悬停会话行右缘），fixed 定位于此 */
  x: number
  y: number
  session: SessionMeta
}

/** 千分位 token 数；无数据（0/缺省）显示 — */
function fmtTokens(u?: UsageStats | null): string {
  const n = u?.total_tokens
  if (!n || n <= 0) return '—'
  return n.toLocaleString()
}

/** 缓存命中率 = cached / prompt（输入侧）；provider 未返回缓存信息时显示 — */
function cachePct(u?: UsageStats | null): string {
  if (!u || !u.cached_tokens || !u.prompt_tokens) return '—'
  return `${Math.round((u.cached_tokens / u.prompt_tokens) * 100)}%`
}

/** ISO 本地时间 → "YYYY-MM-DD HH:mm:ss"；无数据/非法显示 — */
function fmtTime(iso?: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function Row({ icon, label, value }: { icon: string; label: string; value: string }): JSX.Element {
  return (
    <div className="session-tip-row">
      <Icon name={icon} size={13} className="session-tip-icon" />
      <span className="session-tip-label">{label}</span>
      <span className="session-tip-value">{value}</span>
    </div>
  )
}

/** 会话行悬停信息卡：完整标题 / 所属项目（工作空间）/ 总 token 消耗 / 缓存命中率 / 最后更新。
 *  纯展示（pointer-events: none，不拦截鼠标、不闪烁），fixed 定位于悬停行右缘，
 *  靠近视口边缘时回收。数据全部来自会话元数据（list_sessions 透传 project / usage_totals）。 */
export default function SessionTooltip({ x, y, session }: SessionTooltipProps): JSX.Element {
  const CARD_W = 280
  const CARD_H = 150
  const left = Math.min(x, window.innerWidth - CARD_W - 8)
  const top = Math.min(y, window.innerHeight - CARD_H - 8)
  const u = session.usage_totals

  return (
    <div className="session-tip" style={{ left, top }}>
      <div className="session-tip-title" title={sessionDisplayName(session)}>
        {sessionDisplayName(session)}
      </div>
      <div className="session-tip-rows">
        <Row icon="folder" label="所属项目" value={session.project || 'default'} />
        <Row icon="chart" label="总消耗" value={`${fmtTokens(u)} tokens`} />
        <Row icon="refresh" label="缓存命中率" value={cachePct(u)} />
        <Row icon="clock" label="最后更新" value={fmtTime(session.updated_at)} />
      </div>
    </div>
  )
}
