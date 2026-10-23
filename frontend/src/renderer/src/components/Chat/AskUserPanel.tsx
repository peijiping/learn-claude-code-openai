import { useEffect, useRef, useState } from 'react'
import { Icon } from '@components/common/Icon'
import type { AskAnswer } from '@protocols/agentProtocol'
import { useAgentStore } from '@store/agentStore'

/** 输入框上方的「待确认」面板：模型用 `ask_user` 提问时出现，作答后消失。
 *
 * 交互约定（2026-09-21，用户拍板）：
 * - **一题一屏**，点「下一步」逐步前进；步骤条可点击跳转；
 * - 选项支持**单选 / 多选**（按 `q.multi_select`）+ 「其他」自由文本
 *   （按 `q.allow_custom`，**显式字段**，不靠 label === '其他' 猜）；
 * - **不强制作答**：允许空提交，后端会渲染「（未选择）」——
 *   强行要求选择只会逼用户编答案，不如让模型拿到"用户没选"这个真实信号；
 * - 可「取消」本次提问（后端回填固定的取消文案，模型据此自选默认值继续）；
 * - **提交后不本地关面板**：等后端广播的 `ask_resolved` 关闭（见 store.answerAsk）。
 *   乐观关面板会在"后端丢弃作答"时造成假象 —— 后端 `resolve()` 对迟到/重复提交
 *   是幂等丢弃，前端必须能区分"还没结算"与"已结算"。
 *
 * 数据源是 `interactionBySession[activeSession]`（纯 UI 态，不落盘）：
 * 由 `ask_request` 建立、`ask_resolved` 清除；断线重连时后端重放 ask_request。
 *
 * **面板出现时输入区整块让位**（`chat--asking`，2026-09-22 用户拍板）：面板与输入框
 * 并存等于同时给两条作答路径 —— 用户可能在输入框里打一半、又去点选项，两边都像
 * "已经答了"。所以作答期间隐藏输入区，只保留选择 / 各题的「其他」自由文本 / 取消；
 * 输入区里的草稿**不会丢**（编辑器不卸载，只是被 CSS 隐藏，见 InputBox.suspended）。
 * 想要"放弃选择、直接说句话"的用户路径由「取消」承担（模型据此自选默认方案继续）。
 */

/** 单题作答草稿（面板内部状态，随 requestId 重置）。
 *  `customOn` 是「其他」这一项的开关，**显式布尔** —— 不往 `selected` 里塞
 *  特殊哨兵值（否则 `selected` 就不能直接当 AskAnswer.selected 用）。 */
interface DraftAnswer {
  selected: string[]
  customOn: boolean
  customText: string
}

const EMPTY_DRAFT: DraftAnswer = { selected: [], customOn: false, customText: '' }

export default function AskUserPanel(): JSX.Element | null {
  const interaction = useAgentStore((s) =>
    s.activeSession ? s.interactionBySession[s.activeSession] ?? null : null
  )
  const answerAsk = useAgentStore((s) => s.answerAsk)
  const cancelAsk = useAgentStore((s) => s.cancelAsk)

  const [idx, setIdx] = useState(0)
  const [drafts, setDrafts] = useState<Record<string, DraftAnswer>>({})
  const [submitting, setSubmitting] = useState(false)
  const customRef = useRef<HTMLInputElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  const requestId = interaction?.requestId ?? ''
  const questions = interaction?.questions ?? []

  // 换一次提问（新 requestId）→ 重置进度与作答；同一提问内切题不重置。
  useEffect(() => {
    setIdx(0)
    setDrafts({})
    setSubmitting(false)
  }, [requestId])

  // 提交中锁定按钮，但**不能锁死**：万一作答在传输层丢了（连接抖动），
  // 面板不该变成一块点不动的砖。3s 后解锁，用户可再点一次
  //（后端对重复提交幂等丢弃，重复不会造成副作用）。
  useEffect(() => {
    if (!submitting) return
    const t = setTimeout(() => setSubmitting(false), 3000)
    return () => clearTimeout(t)
  }, [submitting])

  const q = questions[idx]
  const draft = (q && drafts[q.id]) || EMPTY_DRAFT

  const patch = (qid: string, next: Partial<DraftAnswer>): void => {
    setDrafts((d) => ({ ...d, [qid]: { ...(d[qid] ?? EMPTY_DRAFT), ...next } }))
  }

  const pickOption = (label: string): void => {
    if (!q) return
    if (q.multi_select) {
      const has = draft.selected.includes(label)
      patch(q.id, { selected: has ? draft.selected.filter((x) => x !== label) : [...draft.selected, label] })
      return
    }
    // 单选：再点一次已选项 = 取消选择（对应"允许空提交"）
    patch(q.id, { selected: draft.selected[0] === label ? [] : [label], customOn: false })
  }

  const toggleCustom = (): void => {
    if (!q) return
    const on = !draft.customOn
    patch(q.id, q.multi_select ? { customOn: on } : { customOn: on, selected: [] })
    if (on) setTimeout(() => customRef.current?.focus(), 0)
  }

  const submit = (): void => {
    if (submitting || !interaction) return
    setSubmitting(true)
    const answers: AskAnswer[] = questions.map((qq) => {
      const d = drafts[qq.id] ?? EMPTY_DRAFT
      const text = d.customText.trim()
      return {
        question_id: qq.id,
        selected: d.selected,
        // 只在自己实实在在写了字时才带 custom_text —— 点了「其他」却空着，
        // 传空串会让后端的小结出现空的「其他：」（看着像 bug）
        ...(d.customOn && text ? { custom_text: text } : {})
      }
    })
    answerAsk(interaction.requestId, answers)
  }

  // Enter = 下一步 / 提交（只在焦点位于面板内时触发，不会抢输入框的键）
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing) return
    e.preventDefault()
    if (idx < questions.length - 1) setIdx((v) => v + 1)
    else submit()
  }

  if (!interaction || questions.length === 0 || !q) return null

  const isLast = idx === questions.length - 1
  const answered = (qq: (typeof questions)[number]): boolean => {
    const d = drafts[qq.id]
    return !!d && (d.selected.length > 0 || (d.customOn && d.customText.trim().length > 0))
  }

  return (
    // 外层只负责"与输入区同宽 + 左右留白"（与 .task-card 同一套布局约定），
    // 视觉卡片是内层 .ask-panel__card —— 这样面板与任务面板的左右边界严格对齐。
    <div className="ask-panel" ref={rootRef} onKeyDown={onKeyDown}>
      <div className="ask-panel__card">
        <div className="ask-panel__head">
          <span className="ask-panel__icon">
            <Icon name="listTodo" size={14} />
          </span>
          <span className="ask-panel__title">需要你确认</span>
          <span className="ask-panel__step">
            第 {idx + 1}/{questions.length} 题
          </span>
          <span className="ask-panel__spacer" />
          {q.multi_select && <span className="ask-panel__hint">可多选</span>}
          <button
            className="ask-panel__cancel"
            title="取消本次提问（模型会自行选默认方案继续）"
            onClick={() => cancelAsk(interaction.requestId)}
          >
            取消
          </button>
        </div>

        {/* 步骤条：点击可任意跳题（已作答的题给对勾，未作答的给空心点） */}
        <div className="ask-panel__steps">
          {questions.map((qq, i) => (
            <button
              key={qq.id}
              className={`ask-step${i === idx ? ' ask-step--active' : ''}${
                answered(qq) ? ' ask-step--done' : ''
              }`}
              title={qq.question}
              onClick={() => setIdx(i)}
            >
              <span className="ask-step__dot">
                {answered(qq) ? <Icon name="check" size={10} /> : i + 1}
              </span>
              <span className="ask-step__label">{qq.header}</span>
            </button>
          ))}
        </div>

        <div className="ask-panel__body">
          <div className="ask-q">
            <span className="ask-q__header">{q.header}</span>
            {q.question}
          </div>

          <div className="ask-options" role={q.multi_select ? 'group' : 'radiogroup'}>
            {q.options.map((opt) => {
              const on = draft.selected.includes(opt.label)
              return (
                <button
                  key={opt.label}
                  className={`ask-option${on ? ' ask-option--on' : ''}`}
                  role={q.multi_select ? 'checkbox' : 'radio'}
                  aria-checked={on}
                  onClick={() => pickOption(opt.label)}
                >
                  <span className={`ask-mark${q.multi_select ? ' ask-mark--box' : ''}`}>
                    {on && <Icon name="check" size={11} />}
                  </span>
                  <span className="ask-option__body">
                    <span className="ask-option__label">{opt.label}</span>
                    {opt.description && <span className="ask-option__desc">{opt.description}</span>}
                  </span>
                </button>
              )
            })}

            {q.allow_custom && (
              <button
                className={`ask-option${draft.customOn ? ' ask-option--on' : ''}`}
                role={q.multi_select ? 'checkbox' : 'radio'}
                aria-checked={draft.customOn}
                onClick={toggleCustom}
              >
                <span className={`ask-mark${q.multi_select ? ' ask-mark--box' : ''}`}>
                  {draft.customOn && <Icon name="check" size={11} />}
                </span>
                <span className="ask-option__body">
                  <span className="ask-option__label">{q.custom_label || '其他'}</span>
                  <span className="ask-option__desc">自己写一个答案</span>
                </span>
              </button>
            )}
          </div>

          {q.allow_custom && draft.customOn && (
            <input
              ref={customRef}
              className="ask-custom"
              value={draft.customText}
              placeholder={`${q.custom_label || '其他'}…`}
              onChange={(e) => patch(q.id, { customText: e.target.value })}
            />
          )}
        </div>

        <div className="ask-panel__foot">
          {/* 2026-09-22：输入区在提问期间被隐藏 → **不再提示**"可以到输入框打字自由回答"
              （那句话此时是假的：输入框根本不可见）。自由作答的唯一入口是各题的「其他」项
              （`allow_custom`），不想作答则点右上角「取消」。 */}
          <span className="ask-panel__tip">逐题选择后提交；要自己写答案请选「其他」</span>
          <span className="ask-panel__spacer" />
          {idx > 0 && (
            <button className="ask-btn" onClick={() => setIdx((v) => Math.max(0, v - 1))}>
              上一步
            </button>
          )}
          {isLast ? (
            <button className="ask-btn ask-btn--primary" disabled={submitting} onClick={submit}>
              {submitting ? '提交中…' : '提交'}
            </button>
          ) : (
            <button className="ask-btn ask-btn--primary" onClick={() => setIdx((v) => v + 1)}>
              下一步
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
