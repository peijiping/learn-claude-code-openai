import { useState } from 'react'
import type { PluginMarketItem, PluginMarketPlan } from '@protocols/agentProtocol'

/** 插件市场安装确认（设置弹窗「插件」→ 市场 → 安装），docs/frontend/25。
 *
 * 这是整个插件功能里**唯一**的安全闸门。与 MCP / 技能确认页相比，它有自己必须
 * 讲清楚的一件事：**插件的六类组件里，本期只有「技能」真正接入运行时**。
 *
 * 不把这一点说清楚的后果很具体：用户看到一个插件带 3 个 hook、2 个 MCP 服务器，
 * 会很自然地以为"装上这些就开始跑了"—— 而实际上它们只落了盘。反过来，如果只说
 * "未接入"却不说"技能是生效的"，用户又会以为整个插件都没用。**两侧都要说。**
 *
 * 另外两条不可妥协的约束：
 * 1. **必须列出全部组件清单**（含未接入的）—— 那是"这个插件会往你机器上放什么"
 *    的完整凭据，不能只显示生效的那部分。
 * 2. **必须展示 `plugin.json` 原文** —— 那是"它自称是什么"的一手材料。
 */

const COMPONENT_LABEL: Record<string, string> = {
  skills: '技能',
  commands: '命令',
  agents: '子智能体',
  hooks: '钩子',
  mcp_servers: 'MCP 服务器',
  lsp_servers: '语言服务器'
}

const COMPONENT_HINT: Record<string, string> = {
  skills: '会注入模型的技能列表（本期**生效**）',
  commands: '斜杠命令（本期只入库，未接入运行时）',
  agents: '子智能体定义（本期只入库，未接入运行时）',
  hooks: '钩子会在本机执行命令（本期只入库，**不会执行**）',
  mcp_servers: 'MCP 服务器声明（本期不会自动连接；可在「MCP」页手工添加）',
  lsp_servers: '语言服务器（本期只入库，未接入运行时）'
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / 1024 / 1024).toFixed(1)} MiB`
}

interface Props {
  item: PluginMarketItem
  plan: PluginMarketPlan
  saving: boolean
  saveErrors: string[]
  onInstall: (payload: { name: string; item: PluginMarketItem }) => void
  onBack: () => void
}

export default function PluginInstallView({
  item,
  plan,
  saving,
  saveErrors,
  onInstall,
  onBack
}: Props): JSX.Element {
  const [name, setName] = useState(plan.name)
  const wired = plan.wired ?? []
  const kinds = Object.keys(plan.components ?? {}).filter(
    (k) => (plan.components[k] ?? []).length > 0
  )
  const pending = kinds.filter((k) => !wired.includes(k))

  return (
    <div className="mcp">
      <div className="mcp-form-head">
        <button type="button" className="mcp-back" onClick={onBack}>
          ← 返回市场
        </button>
        <span className="mcp-form-title">
          确认安装「{plan.display_name || item.display_name || item.name}」
        </span>
      </div>

      {saveErrors.map((e, i) => (
        <p className="sbx-error" key={`${i}-${e}`}>
          {e}
        </p>
      ))}

      {/* ── 能力边界：这一块是插件确认页与 MCP / 技能确认页最大的差别 ── */}
      <p className="mcp-risk">
        插件可贡献 <b>6 类组件</b>，但本期<b>只有「技能」真正接入运行时</b>
        {pending.length > 0 && (
          <>
            ：该插件还带 {pending.map((k) => COMPONENT_LABEL[k] ?? k).join('、')}
            ，它们会落到 <code>~/.aigent/plugins/</code>，<b>但不会被执行</b>
          </>
        )}
        。
      </p>

      {/* ── 组件清单 ── */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">这个插件会带来什么</h4>
        <p className="perm-sec-desc">
          来源 <code>{item.id}</code>
          {plan.version ? ` · 版本 ${plan.version}` : ''} · {plan.source_kind} ·{' '}
          {fmtSize(plan.total_bytes)} / {plan.file_count} 个文件
        </p>
        {kinds.length === 0 ? (
          <p className="mcp-inline-note">
            没有识别到任何组件（清单里没声明，目录里也没有 skills/commands/agents/hooks/MCP）。
          </p>
        ) : (
          <div className="mcp-src-list">
            {kinds.map((k) => (
              <div className="mcp-src-row" key={k}>
                <div className="mcp-src-info">
                  <span className="mcp-src-name">
                    {COMPONENT_LABEL[k] ?? k}
                    <span className={`mcp-tag ${wired.includes(k) ? 'on' : 'off'}`}>
                      {wired.includes(k) ? '本期生效' : '本期未接入'}
                    </span>
                  </span>
                  <span className="mcp-src-meta">
                    {(plan.components[k] ?? []).join('、')}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
        {kinds.length > 0 && (
          <p className="mcp-inline-note">
            {kinds
              .map((k) => `${COMPONENT_LABEL[k] ?? k}：${COMPONENT_HINT[k] ?? ''}`)
              .join('；')}
          </p>
        )}
      </section>

      {/* ── 生效技能的首行说明（让用户看出"这些技能会教模型做什么"）── */}
      {(plan.skill_previews ?? []).length > 0 && (
        <section className="perm-sec">
          <h4 className="perm-sec-title">会进入模型技能列表的技能</h4>
          <p className="perm-sec-desc">
            命名空间为 <code>{plan.name}:&lt;技能名&gt;</code>（与 Claude Code 的规则一致，
            避免不同插件的同名技能互相覆盖）。
          </p>
          <div className="mcp-src-list">
            {(plan.skill_previews ?? []).map((s) => (
              <div className="mcp-src-row" key={s.name}>
                <div className="mcp-src-info">
                  <span className="mcp-src-name">
                    {plan.name}:{s.name}
                  </span>
                  <span className="mcp-src-meta">{s.description || '（无描述）'}</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ── plugin.json 原文（安全闸门）── */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">插件清单原文</h4>
        <p className="perm-sec-desc">
          <code>.claude-plugin/plugin.json</code> —— 这是"它自称是什么"的一手材料。
        </p>
        <pre className="mcp-code">{plan.plugin_json}</pre>
      </section>

      {/* ── 文件清单 ── */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">将要写入的文件</h4>
        <p className="perm-sec-desc">
          落在 <code>~/.aigent/plugins/{name || plan.name}/</code>
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

      <section className="perm-sec">
        <div className="mcp-field">
          <label className="mcp-label">
            插件目录名
            <span className="mcp-hint">
              只能用字母、数字与 <code>. _ -</code>；也是技能命名空间的前缀
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
          {item.source_label && (
            <span className="mcp-inline-note">来源形态：{item.source_label}</span>
          )}
        </span>
        <button type="button" className="btn" disabled={saving} onClick={onBack}>
          取消
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={saving || !name.trim()}
          onClick={() => onInstall({ name: name.trim(), item })}
        >
          {saving ? '安装中…' : '确认安装'}
        </button>
      </div>
    </div>
  )
}
