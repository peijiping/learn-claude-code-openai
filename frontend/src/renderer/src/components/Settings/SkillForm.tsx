import { useState } from 'react'

/** 手动新建技能（设置弹窗「技能」→ + 新建），docs/frontend/24。
 *
 * 与 MCP 的「+ 手动添加」对齐：这是"自己写技能"的最短路径 —— 不必先建仓库、
 * 再走市场。前端只收四个字段，**SKILL.md 由后端拼**（YAML 的引号/换行转义容易写错，
 * 拼装规则只应有一处，见 `skill_store.build_skill_md`）。
 *
 * `description` 是硬必填：技能列表与系统提示里的那一行就是它，缺了就只能退回
 * "正文首行"，模型挑技能的准确率会明显下降。
 */

interface Props {
  existingNames: string[]
  saving: boolean
  /** 上一次保存被拒的原因（来自 skill_config 回执，内联展示不 toast） */
  saveErrors: string[]
  onSubmit: (payload: {
    name: string
    description: string
    body: string
    tags: string[]
  }) => void
  onCancel: () => void
}

export default function SkillForm({
  existingNames,
  saving,
  saveErrors,
  onSubmit,
  onCancel
}: Props): JSX.Element {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [tags, setTags] = useState('')
  const [body, setBody] = useState('')

  const missing: string[] = []
  if (!name.trim()) missing.push('名称')
  if (!description.trim()) missing.push('用途说明')
  if (!body.trim()) missing.push('正文')
  const duplicate = existingNames.includes(name.trim())

  return (
    <div className="mcp">
      <div className="mcp-form-head">
        <button type="button" className="mcp-back" onClick={onCancel}>
          ← 返回列表
        </button>
        <span className="mcp-form-title">新建技能</span>
      </div>

      {saveErrors.map((e, i) => (
        <p className="sbx-error" key={`${i}-${e}`}>
          {e}
        </p>
      ))}

      <section className="perm-sec">
        <div className="mcp-field">
          <label className="mcp-label">
            技能名称
            <span className="mcp-hint">
              目录名，同时是 <code>load_skill</code> 的标识符；只能用字母、数字与{' '}
              <code>. _ -</code>
            </span>
          </label>
          <input
            className="mcp-input mono"
            placeholder="例如 data-audit"
            value={name}
            spellCheck={false}
            onChange={(e) => setName(e.target.value)}
          />
          {duplicate && <p className="mcp-inline-warn">已存在同名技能，请换一个名字。</p>}
        </div>

        <div className="mcp-field">
          <label className="mcp-label">
            用途说明
            <span className="mcp-hint">
              模型每一轮都会看到这一行（静态段会按预算截断）—— 写"什么时候该用它"
            </span>
          </label>
          <input
            className="mcp-input"
            placeholder="例如：检查 CSV/JSON 数据里的缺失值、重复行与类型不一致"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>

        <div className="mcp-field">
          <label className="mcp-label">
            标签
            <span className="mcp-hint">逗号分隔，可留空</span>
          </label>
          <input
            className="mcp-input"
            placeholder="data, csv, qa"
            value={tags}
            spellCheck={false}
            onChange={(e) => setTags(e.target.value)}
          />
        </div>
      </section>

      <section className="perm-sec">
        <h4 className="perm-sec-title">技能正文</h4>
        <p className="perm-sec-desc">
          只在模型判断"该用这个技能"时才会被加载（<code>load_skill</code>）——
          所以正文可以写详细些：步骤、边界条件、示例、反例都值得写。
        </p>
        <textarea
          className="mcp-input"
          style={{ minHeight: '220px', fontFamily: 'var(--font-mono, ui-monospace, monospace)' }}
          placeholder={'# Data audit\n\n按以下顺序检查：\n1. …\n2. …\n\n不要做的事：…'}
          value={body}
          spellCheck={false}
          onChange={(e) => setBody(e.target.value)}
        />
        <p className="mcp-inline-note">
          frontmatter（name / description / tags）由后端自动生成，正文里不要自己写{' '}
          <code>---</code> 开头的块。
        </p>
      </section>

      <div className="perm-foot">
        <span className="perm-foot-status">
          {missing.length > 0 && (
            <span className="mcp-inline-warn">还缺：{missing.join('、')}</span>
          )}
        </span>
        <button type="button" className="btn" disabled={saving} onClick={onCancel}>
          取消
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={saving || missing.length > 0 || duplicate}
          onClick={() =>
            onSubmit({
              name: name.trim(),
              description: description.trim(),
              body,
              tags: tags
                .split(',')
                .map((t) => t.trim())
                .filter(Boolean)
            })
          }
        >
          {saving ? '创建中…' : '创建技能'}
        </button>
      </div>
    </div>
  )
}
