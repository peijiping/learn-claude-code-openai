import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Icon } from '@components/common/Icon'
import { safeUrlTransform } from '@lib/pathLinks'
import type { PlanState } from '@store/agentStore'

/** remark 插件数组提到模块级：身份稳定，避免每次渲染重建（react-markdown 会把
 *  新数组当场次不同的插件表处理，导致整棵 markdown 树重挂）。 */
const REMARK_PLUGINS = [remarkGfm]

interface PlanCardProps {
  plan: PlanState
  /** 「批准执行」：fire-and-forget 发 `plan_approve`。**不乐观转只读** ——
   *  卡片状态由随后的 `execution_mode_changed`（带 `plan_status='approved'`）校正；
   *  后端此时还会自动起一轮执行（忙碌/有待答提问时只落状态并 toast）。 */
  onApprove: () => void
  /** 「继续修改」：聚焦输入框并提示（**不自动发消息** —— 自动发会把半截提示
   *  当成用户意图送出去）。计划模式保持生效，用户用自然语言描述要改什么。 */
  onRevise: () => void
}

/**
 * 计划卡片（2026-09-25，docs/frontend/22 §6.3）。
 *
 * **位置：消息流末尾固定块**（由 `MessageList` 渲染，与 `orphanApprovals` 同款）。
 * 为什么不做"锚定产出该计划的 assistant 消息"：那条链路**不存在** —— 事件不带
 * message_id、`Message` 类型没有 plan 字段、store 里也没有"消息 id → 计划"的映射。
 * 而 `plan_status` + `plan_path` 两个字段就足以还原卡片外壳，实时与回放天然一致，
 * 所以直接复用既有先例（贴消息流末尾），零新增锚定机制。
 *
 * **已知限制**：计划文书是**单份覆盖**（每会话一条 plan 状态），多轮 plan 时消息流
 * 里只保留"最新这一份"的正文 —— 这正是"末尾固定块"成为唯一自洽形态的原因。
 *
 * 正文来源：`plan_read` 拉取（**不是 `file_read`** —— 文书落在元数据目录
 * `<data_root>/plans/`，而后者的根被钉死在工作区 → 天然越界）。
 */
export default function PlanCard({ plan, onApprove, onRevise }: PlanCardProps): JSX.Element {
  const approved = plan.status === 'approved'
  const lost = !!plan.reason
  return (
    <div className={`plan-card${approved ? ' approved' : ''}`} data-plan-status={plan.status}>
      <div className="plan-card-head">
        <Icon name="listTodo" size={14} />
        <span className="plan-card-title">执行计划</span>
        <span className={`plan-card-badge${approved ? ' approved' : ''}`}>
          {approved ? '已批准' : '待批准'}
        </span>
      </div>

      {lost ? (
        // 读不到是**常规降级**（文书被清理 / 空间目录不可用 / 超 512KB 整份拒绝）
        // → 给占位块与原因，绝不白屏（与右栏文件预览的降级姿态一致）。
        <div className="plan-card-lost">
          <Icon name="fileText" size={13} />
          <span>{plan.reason}</span>
        </div>
      ) : plan.content === undefined ? (
        // 正文尚未到达（plan_read 在途）—— 只占位，不显示"加载失败"
        <div className="plan-card-loading">正在读取计划文书…</div>
      ) : (
        <div className="markdown-body plan-card-body">
          <ReactMarkdown
            remarkPlugins={REMARK_PLUGINS}
            urlTransform={safeUrlTransform}
            components={{
              table: ({ children }) => (
                <div className="table-scroll">
                  <table>{children}</table>
                </div>
              ),
              // 正文是**模型产出**的内容：绝不产出可导航元素（docs/frontend/21 §6.1
              // 事故 —— 渲染层一导航整个应用就没了）。外链交主进程
              // setWindowOpenHandler → shell.openExternal；其余一律降级为纯文本。
              a: ({ href, children }) =>
                /^(https?:)?\/\//i.test(String(href ?? '')) ? (
                  <a href={String(href)} target="_blank" rel="noreferrer noopener">
                    {children}
                  </a>
                ) : (
                  <span>{children}</span>
                )
            }}
          >
            {plan.content}
          </ReactMarkdown>
        </div>
      )}

      <div className="plan-card-actions">
        {approved ? (
          <span className="plan-card-note">已批准，正在按计划执行</span>
        ) : (
          <>
            <button type="button" className="plan-card-btn" onClick={onRevise}>
              继续修改
            </button>
            <button
              type="button"
              className="plan-card-btn primary"
              disabled={lost}
              title={lost ? '计划文书读不到，无法批准' : undefined}
              onClick={onApprove}
            >
              批准执行
            </button>
          </>
        )}
      </div>
    </div>
  )
}
