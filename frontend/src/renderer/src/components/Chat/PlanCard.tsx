import { Icon } from '@components/common/Icon'
import { isPlanDocOpenable, openPlanDocInPanel, useAgentStore, type PlanState } from '@store/agentStore'

interface PlanCardProps {
  plan: PlanState
  /** 「选择操作」：把操作栏（`PlanActionBar`）请回输入区位置。
   *
   *  只在操作栏**当前被收起**时传入（`ChatPanel` 决定）—— 操作栏在输入区里的
   *  那一刻，这里不渲染按钮，避免同一件事出现两个入口。
   *  收起态只有一种来源：用户在操作栏里选了「暂不执行先看方案」（见 PlanActionBar）。 */
  onChoose?: () => void
}

/**
 * 计划卡片 —— 对话里的**锚点行**（2026-09-29 二次改版瘦身；四次改版定终态）。
 *
 * ── 为什么改成这么"薄" ────────────────────────────────────────────────
 * 上一版的卡片把**整份 Markdown 正文**渲染在消息流里，还带着行动区。两处问题：
 *   1. **正文不该在这里。** 计划文书写在工作空间里（`.aiagent/plan/<name>.md`），
 *      生成完就**主动在右栏打开**了 —— 对话里再铺一遍全文，等于同一份东西看两遍，
 *      还把消息流顶得很长（正文限高 420px 也只是缓解）。
 *   2. **操作不该在这里。** 用户拍板的形态是"操作选项**替换输入框的位置**"
 *      （见 `PlanActionBar`）：决策就在手边、且与自由输入互斥。动作留在卡片里
 *      会让"到底该点哪儿"变成两处。
 *
 * 于是卡片只剩一个职责：**在消息流里标出"这里产出过一份计划"**，并给两个入口 ——
 * 「打开文档」（右栏本来就会自动开，但用户可能已经关掉标签）与「选择操作」（操作栏被
 * 「暂不执行先看方案」收起后，从这里请回来；**执行期间禁用**，见下）。
 *
 * ── 五次改版：「选择操作」在智能体跑的时候禁用 ──────────────────────────
 * 「继续修改」提交后本栏收起、模型起新一轮，但新一轮的 `plan_ready` 就在这一轮**之内**
 * （写文书只是本轮一个工具调用）—— 也就是说"正在执行"与"有一份 `ready` 计划"会同时成立，
 * 这段时间卡片上的「选择操作」若不设防，用户就能在模型干活时点开操作栏并按下「开始执行」。
 * 后端 `rt.busy` 的守卫是"照样落 `approved`、只不起续跑"（配一句 toast），壳被撤掉后
 * 用户以为已在执行 —— 用户实测报的"操作和执行都变混乱"正是这一条。
 * 现在判据统一为 `isSending`（与 `ChatPanel` 的 `planBarShown` 同源），本轮落地即恢复。
 *
 * ── 四次改版：它只在"等你做决定"期间存在 ──────────────────────────────
 * 上一版让 `approved` 的壳**恒留**（说它是"历史"）。但壳贴在消息流**末尾**、不锚定
 * 产出它的那条消息（事件不带 `message_id`，这条链不存在）—— 于是它既不是历史
 * （不跟着历史走），也不是状态（`approve_plan` 已把 mode 回落 normal，卡片上那句
 * "正在按计划执行"当场就是错的），只剩一条过期横幅赖在屏幕上直到会话结束。
 *
 * 现在**`approved` 的壳根本不会存在**：撤壳判据在 store 的 `planShellAlive`
 * （三条恢复通道共用）。本组件因此只会拿到 `ready` —— 绿边、绿徽标、"已批准"文案
 * 一并删除（连带 `chat.css` 里那两条 `.approved` 规则）。
 *
 * ── 位置仍然贴消息流末尾 ─────────────────────────────────────────────
 * 不做"锚定产出该计划的 assistant 消息"：那条链路**不存在** —— `plan_ready` 不带
 * `message_id`、`Message` 类型没有 plan 字段、store 里也没有"消息 id → 计划"的映射。
 * 而 `plan_status` + `plan_path` 两个字段就足以还原锚点，实时与回放天然一致。
 * （四次改版后这个位置才真正自洽：既然只在待批准期间短暂存在，不锚定消息就不再是缺陷。）
 *
 * **正文为何仍然照拉**：`plan_read`（`fetchPlanContent`）得以保留的原因变了 ——
 * 不再是为了显示，而是为了 `plan.reason`（文书被清理 / 空间目录不可用）：读不到时
 * 要禁用「开始执行」并说明原因。**读不到是常规降级，不是错误**，所以这里是占位说明
 * 而不是白屏。
 */
export default function PlanCard({ plan, onChoose }: PlanCardProps): JSX.Element {
  const lost = !!plan.reason
  const sid = useAgentStore((s) => s.activeSession)
  /** 正在跑一轮（`running` / `background`）—— 「选择操作」在此期间**禁用**。
   *
   *  与 `ChatPanel` 的 `planBarShown`（`&& !isSending`）是**同一件事的两个面**：那边保证
   *  执行期不占输入区，这边堵住"绕过输入区把操作栏请回来"的唯一入口。少这一处，用户提交
   *  「继续修改」后仍能从卡片点开操作栏、在模型执行中点下「开始执行」（2026-09-29 实测
   *  报的就是这条路径）—— 后端 `rt.busy` 时照样落 `approved`，状态与执行两边都乱。
   *
   *  用**禁用**而不是**隐藏**：按钮凭空消失会被当成"功能没了"，灰着 + `title` 说清
   *  "本轮跑完就能再选"，用户才知道该等（想提前结束就点输入区的「停止」）。 */
  const isSending = useAgentStore((s) => s.isSending)
  /** 能否进右栏（存量会话的绝对路径不行，判据在 store，与操作栏同源）。 */
  const openable = isPlanDocOpenable(plan.path)

  return (
    <div className="plan-card" data-plan-status={plan.status}>
      <div className="plan-card-head">
        <Icon name="listTodo" size={14} />
        <span className="plan-card-title">执行计划</span>
        {!lost && openable && (
          <button
            type="button"
            className="plan-card-open"
            title="在右侧面板打开计划文档"
            onClick={() => openPlanDocInPanel(sid ?? '', plan.path)}
          >
            <Icon name="externalLink" size={12} />
            打开文档
          </button>
        )}
        {onChoose && (
          <button
            type="button"
            className="plan-card-open"
            disabled={isSending}
            title={
              isSending
                ? '智能体正在执行，本轮结束后可再选择操作'
                : '在输入框位置展开操作选项'
            }
            onClick={onChoose}
          >
            <Icon name="listTodo" size={12} />
            选择操作
          </button>
        )}
        <span className="plan-card-badge">待批准</span>
      </div>

      <div className={`plan-card-line${lost ? ' lost' : ''}`}>
        <Icon name="fileText" size={13} />
        <span>{lost ? plan.reason : '计划文书已生成，正文在右侧面板中打开'}</span>
      </div>
    </div>
  )
}
