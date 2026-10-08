import { useState } from 'react'
import type { SkillMarketItem, SkillMarketPlan } from '@protocols/agentProtocol'

/** 技能市场安装确认（设置弹窗「技能」→ 市场 → 安装），docs/frontend/24。
 *
 * 这是整个技能功能里**唯一**的安全闸门。两条不可妥协的约束：
 *
 * 1. **必须原样展示 SKILL.md 全文**。技能不是可执行文件，但它是**写给模型看的
 *    指令** —— 决定"模型接下来会照着做什么"。市场条目没有任何代码审计，用户
 *    唯一能判断的依据就是这段原文。不折叠、不摘要、不只显示描述行。
 * 2. **必须列出将要写入的全部文件**。技能可以带脚本（`scripts/*.py`），那些脚本
 *    会被模型通过 `run_bash` 执行 —— 只看 SKILL.md 会漏掉它们。
 *
 * 不提供"记住我的选择"之类的便捷开关：这套东西装进去是长期生效的，每次都该看一眼。
 */

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / 1024 / 1024).toFixed(1)} MiB`
}

interface Props {
  item: SkillMarketItem
  plan: SkillMarketPlan
  saving: boolean
  /** 上一次安装被拒 / 失败的原因（来自 skill_config 回执，内联展示不 toast） */
  saveErrors: string[]
  onInstall: (payload: { name: string; item: SkillMarketItem }) => void
  onBack: () => void
}

export default function SkillInstallView({
  item,
  plan,
  saving,
  saveErrors,
  onInstall,
  onBack
}: Props): JSX.Element {
  const [name, setName] = useState(plan.name)
  const scripts = (plan.files ?? []).filter((f) => !f.path.toLowerCase().endsWith('skill.md'))

  return (
    <div className="mcp">
      <div className="mcp-form-head">
        <button type="button" className="mcp-back" onClick={onBack}>
          ← 返回市场
        </button>
        <span className="mcp-form-title">确认安装「{plan.skill_name || item.name}」</span>
      </div>

      {saveErrors.map((e, i) => (
        <p className="sbx-error" key={`${i}-${e}`}>
          {e}
        </p>
      ))}

      {/* ── SKILL.md 全文（安全闸门） ── */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">将要生效的技能正文</h4>
        <p className="perm-sec-desc">
          来源 <code>{item.id}</code>
          {plan.version ? ` · 版本 ${plan.version}` : ''} · 共 {plan.file_count} 个文件 ·{' '}
          {fmtSize(plan.total_bytes)}
          {plan.description ? ` · ${plan.description}` : ''}
        </p>
        <pre className="mcp-code">{plan.skill_md}</pre>
        <p className="mcp-inline-warn">
          这段正文会进入模型的系统提示（或经 <code>load_skill</code> 按需加载），
          等于<b>给模型下指令</b>。请通读一遍再装 —— 这些源<b>没有代码审计</b>。
        </p>
      </section>

      {/* ── 文件清单 ── */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">将要写入的文件</h4>
        <p className="perm-sec-desc">
          落在 <code>~/.aigent/skills/{name || plan.name}/</code>
          {scripts.length > 0 && (
            <>
              。其中 <b>{scripts.length} 个是附属文件</b> —— 技能正文可能让模型通过{' '}
              <code>run_bash</code> 执行它们
            </>
          )}
        </p>
        <div className="mcp-filelist">
          {(plan.files ?? []).map((f) => (
            <div className="mcp-file-row" key={f.path}>
              <span className="mcp-file-path">{f.path}</span>
              <span className="mcp-file-size">{fmtSize(f.size)}</span>
            </div>
          ))}
        </div>
      </section>

      {/* ── 落地目录名 ── */}
      <section className="perm-sec">
        <div className="mcp-field">
          <label className="mcp-label">
            技能目录名
            <span className="mcp-hint">
              只能用字母、数字与 <code>. _ -</code>；它同时是 <code>load_skill</code>{' '}
              的标识符
            </span>
          </label>
          <input
            className="mcp-input mono"
            value={name}
            spellCheck={false}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
      </section>

      {plan.warnings.length > 0 &&
        plan.warnings.map((w, i) => (
          <p className="mcp-warn" key={`${i}-${w}`}>
            {w}
          </p>
        ))}

      <div className="perm-foot">
        <span className="perm-foot-status">
          {(plan.tags ?? []).length > 0 && (
            <span className="mcp-inline-note">标签：{plan.tags.join('、')}</span>
          )}
        </span>
        <button type="button" className="btn" disabled={saving} onClick={onBack}>
          取消
        </button>
        <button
          type="button"
          className="btn btn-primary"
          // 名称空 / 有在途请求时必须拦住。名称是目录名，后端还会再校验一次
          // （前端只是不给明显非法的载荷）。
          disabled={saving || !name.trim()}
          onClick={() => onInstall({ name: name.trim(), item })}
        >
          {saving ? '安装中…' : '确认安装'}
        </button>
      </div>
    </div>
  )
}
