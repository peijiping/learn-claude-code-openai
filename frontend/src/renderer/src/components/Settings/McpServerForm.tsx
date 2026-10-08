import { useEffect, useMemo, useState } from 'react'
import type { McpServer, McpTransport, McpTestResult } from '@protocols/agentProtocol'

/** MCP 条目表单（新增 / 编辑），docs/frontend/23。
 *
 * 三条设计约定（改这个文件前先读）：
 * 1. **不落盘的自检**：「测试连接」走 `mcp_server_test`，真实起子进程 / 建连接并等
 *    握手（最长 15s），**不写配置、不登记运行时**。用户先验证再保存 —— 填错 URL 时
 *    当场知道，而不是等模型调用时才报错。
 * 2. **密钥掩码原样回传**：编辑已保存条目时，`initial.env` / `initial.headers` 里的
 *    密钥值是后端脱敏后的 `••••••`。用户不动它 → 原样提交 → 后端换回磁盘真值。
 *    所以这里**绝不能**把掩码当"已填好的值"做校验或展示为可读密钥。
 * 3. **错误内联展示、不 toast**：后端校验失败只走 `mcp_config` 回执的 `errors[]`，
 *    由本组件在表单顶部渲染红字（用户要对着字段改，toast 一闪而过等于没提示）。
 */

const TRANSPORTS: { key: McpTransport; label: string; hint: string }[] = [
  { key: 'stdio', label: 'stdio', hint: '启动本地命令，走标准输入输出（npx / uvx / 本地可执行文件）' },
  {
    key: 'streamable-http',
    label: 'streamable-http',
    hint: '连一个 HTTP 端点 —— 引用本机已启动的 MCP 也走这条（如 http://127.0.0.1:8766/mcp）'
  },
  { key: 'sse', label: 'sse', hint: '连一个 Server-Sent Events 端点（较老的远程传输）' }
]

const SECRET_HINT = '••••••'

interface KvRow {
  k: string
  v: string
}

export interface McpFormPayload {
  name: string
  config: Record<string, unknown>
  original_name?: string
}

interface Props {
  /** null = 新增；非 null = 编辑该条目（保留其 enable 与来源元数据） */
  initial: McpServer | null
  /** 现有条目名（用于本地查重提示；后端仍会再校验一次） */
  existingNames: string[]
  saving: boolean
  testing: boolean
  /** 试连结果（来自 store 的 mcpTest，已由容器按目标条目过滤） */
  test: McpTestResult | null
  /** 上一次保存被拒 / 失败的原因（来自 mcp_config 回执） */
  saveErrors: string[]
  onTest: (payload: { config: Record<string, unknown>; name?: string }) => void
  onClearTest: () => void
  onSubmit: (payload: McpFormPayload) => void
  onCancel: () => void
}

function kvToRows(v: Record<string, string> | null | undefined): KvRow[] {
  if (!v) return []
  return Object.entries(v).map(([k, val]) => ({ k, v: String(val) }))
}

function rowsToKv(rows: KvRow[]): Record<string, string> | undefined {
  const out: Record<string, string> = {}
  for (const r of rows) {
    const k = r.k.trim()
    if (k) out[k] = r.v
  }
  return Object.keys(out).length > 0 ? out : undefined
}

export default function McpServerForm({
  initial,
  existingNames,
  saving,
  testing,
  test,
  saveErrors,
  onTest,
  onClearTest,
  onSubmit,
  onCancel
}: Props): JSX.Element {
  const [name, setName] = useState(initial?.name ?? '')
  const [transport, setTransport] = useState<McpTransport>(initial?.transport ?? 'stdio')
  const [command, setCommand] = useState(initial?.command ?? '')
  // args 用「每行一个参数」而不是空格分隔：后者遇到带空格的路径要用户自己加引号，
  // 而 MCP 配置里 args 本来就是数组 —— 一行一项与目标结构一一对应，无歧义。
  const [argsText, setArgsText] = useState((initial?.args ?? []).join('\n'))
  const [cwd, setCwd] = useState(initial?.cwd ?? '')
  const [envRows, setEnvRows] = useState<KvRow[]>(kvToRows(initial?.env))
  const [url, setUrl] = useState(initial?.url ?? '')
  const [headerRows, setHeaderRows] = useState<KvRow[]>(kvToRows(initial?.headers))

  // 切换被编辑的条目时重建全部草稿（同一条目内不重建，避免打断输入）
  useEffect(() => {
    setName(initial?.name ?? '')
    setTransport(initial?.transport ?? 'stdio')
    setCommand(initial?.command ?? '')
    setArgsText((initial?.args ?? []).join('\n'))
    setCwd(initial?.cwd ?? '')
    setEnvRows(kvToRows(initial?.env))
    setUrl(initial?.url ?? '')
    setHeaderRows(kvToRows(initial?.headers))
    onClearTest()
    // onClearTest 是 store 上的稳定引用，不进依赖数组以免每次渲染都重置草稿
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial])

  const isStdio = transport === 'stdio'
  const duplicate = useMemo(
    () => name.trim() !== '' && name.trim() !== initial?.name && existingNames.includes(name.trim()),
    [name, existingNames, initial?.name]
  )

  /** 组装后端要的配置体。**故意不在前端做字段级校验** ——
   *  校验规则只应有一处（`mcp_store.validate`），前端重复实现必然分叉。 */
  const buildConfig = (): Record<string, unknown> => {
    const cfg: Record<string, unknown> = { type: transport, enable: initial?.enable ?? true }
    if (isStdio) {
      cfg.command = command.trim()
      const args = argsText
        .split('\n')
        .map((a) => a.trim())
        .filter(Boolean)
      if (args.length > 0) cfg.args = args
      if (cwd.trim()) cfg.cwd = cwd.trim()
      const env = rowsToKv(envRows)
      if (env) cfg.env = env
    } else {
      cfg.url = url.trim()
      const headers = rowsToKv(headerRows)
      if (headers) cfg.headers = headers
    }
    return cfg
  }

  const submit = (): void => {
    const trimmed = name.trim()
    onSubmit({
      name: trimmed,
      config: buildConfig(),
      ...(initial && trimmed !== initial.name ? { original_name: initial.name } : {})
    })
  }

  const renderKv = (
    label: string,
    hint: string,
    rows: KvRow[],
    setRows: (r: KvRow[]) => void,
    keyPlaceholder: string
  ): JSX.Element => (
    <div className="mcp-field">
      <label className="mcp-label">
        {label}
        <span className="mcp-hint">{hint}</span>
      </label>
      {rows.map((row, i) => (
        <div className="mcp-kv-row" key={`${label}-${i}`}>
          <input
            className="mcp-input mono"
            placeholder={keyPlaceholder}
            value={row.k}
            spellCheck={false}
            onChange={(e) =>
              setRows(rows.map((r, j) => (j === i ? { ...r, k: e.target.value } : r)))
            }
          />
          <input
            className="mcp-input mono"
            placeholder="值"
            value={row.v}
            spellCheck={false}
            onChange={(e) =>
              setRows(rows.map((r, j) => (j === i ? { ...r, v: e.target.value } : r)))
            }
          />
          <button
            type="button"
            className="mcp-del"
            title="移除这一项"
            onClick={() => setRows(rows.filter((_, j) => j !== i))}
          >
            ×
          </button>
        </div>
      ))}
      <button
        type="button"
        className="mcp-add"
        onClick={() => setRows([...rows, { k: '', v: '' }])}
      >
        + 添加一项
      </button>
    </div>
  )

  const hasSecret = [...envRows, ...headerRows].some((r) => r.v === SECRET_HINT)

  return (
    <div className="mcp">
      <div className="mcp-form-head">
        <button type="button" className="mcp-back" onClick={onCancel}>
          ← 返回列表
        </button>
        <span className="mcp-form-title">{initial ? `编辑「${initial.name}」` : '添加 MCP 服务'}</span>
      </div>

      {saveErrors.map((e, i) => (
        <p className="sbx-error" key={`${i}-${e}`}>
          {e}
        </p>
      ))}

      <section className="perm-sec">
        <div className="mcp-field">
          <label className="mcp-label">
            名称
            <span className="mcp-hint">
              用于生成模型侧工具前缀 <code>mcp__&lt;名称&gt;__&lt;工具&gt;</code>
            </span>
          </label>
          <input
            className="mcp-input mono"
            placeholder="例如 zotero-mcp"
            value={name}
            spellCheck={false}
            onChange={(e) => setName(e.target.value)}
          />
          {duplicate && <p className="mcp-inline-warn">已存在同名条目，保存会覆盖它</p>}
        </div>

        <div className="mcp-field">
          <label className="mcp-label">传输类型</label>
          <div className="mcp-transports">
            {TRANSPORTS.map((t) => (
              <button
                type="button"
                key={t.key}
                className={`mcp-transport ${transport === t.key ? 'active' : ''}`}
                onClick={() => setTransport(t.key)}
              >
                <span className="mcp-transport-label">{t.label}</span>
                <span className="mcp-transport-hint">{t.hint}</span>
              </button>
            ))}
          </div>
        </div>

        {isStdio ? (
          <>
            <div className="mcp-field">
              <label className="mcp-label">
                命令
                <span className="mcp-hint">会被直接执行的本地可执行文件（npx / uvx / 绝对路径）</span>
              </label>
              <input
                className="mcp-input mono"
                placeholder="例如 npx"
                value={command}
                spellCheck={false}
                onChange={(e) => setCommand(e.target.value)}
              />
            </div>
            <div className="mcp-field">
              <label className="mcp-label">
                参数
                <span className="mcp-hint">每行一个参数（数组结构与之一一对应）</span>
              </label>
              <textarea
                className="mcp-input mono"
                rows={4}
                spellCheck={false}
                placeholder={'-y\n@modelcontextprotocol/server-filesystem\n/path/to/dir'}
                value={argsText}
                onChange={(e) => setArgsText(e.target.value)}
              />
            </div>
            <div className="mcp-field">
              <label className="mcp-label">
                工作目录
                <span className="mcp-hint">可留空，默认仓库根</span>
              </label>
              <input
                className="mcp-input mono"
                placeholder="留空即可"
                value={cwd}
                spellCheck={false}
                onChange={(e) => setCwd(e.target.value)}
              />
            </div>
            {renderKv('环境变量', '传给子进程的 env', envRows, setEnvRows, 'KEY')}
          </>
        ) : (
          <>
            <div className="mcp-field">
              <label className="mcp-label">
                URL
                <span className="mcp-hint">本机已启动的 MCP 就填它的地址，如 http://127.0.0.1:8766/mcp</span>
              </label>
              <input
                className="mcp-input mono"
                placeholder="http://127.0.0.1:8766/mcp"
                value={url}
                spellCheck={false}
                onChange={(e) => setUrl(e.target.value)}
              />
            </div>
            {renderKv('请求头', '鉴权等 HTTP header', headerRows, setHeaderRows, 'Authorization')}
          </>
        )}

        {hasSecret && (
          <p className="mcp-inline-note">
            带 <code>{SECRET_HINT}</code> 的值表示「保持原值不变」。要改就整体替换，
            留空则删除该项。
          </p>
        )}
      </section>

      {/* ── 试连结果（不落盘） ── */}
      {test && (
        <section className={`mcp-test-box ${test.ok ? 'ok' : 'fail'}`}>
          {test.ok ? (
            <>
              <p className="mcp-test-title">
                连接成功 · 发现 {test.tool_count} 个工具
                {test.resource_count > 0 ? ` / ${test.resource_count} 个资源` : ''} ·{' '}
                {test.elapsed_ms}ms
              </p>
              <div className="mcp-chips">
                {test.tools.map((t) => (
                  <code className="mcp-chip" key={t}>
                    {t}
                  </code>
                ))}
              </div>
            </>
          ) : (
            <>
              <p className="mcp-test-title">连接失败</p>
              <p className="mcp-test-error mono">{test.error}</p>
            </>
          )}
        </section>
      )}

      <div className="perm-foot">
        <button
          type="button"
          className="btn"
          disabled={testing || saving}
          onClick={() => onTest({ config: buildConfig() })}
        >
          {/* 进行中必须 disabled：主进程 pending 表按 kind FIFO 配对且无 id，
              同一 kind 并发会串台 */}
          {testing ? '正在连接…' : '测试连接'}
        </button>
        <span className="perm-foot-status" />
        <button type="button" className="btn" disabled={saving} onClick={onCancel}>
          取消
        </button>
        <button type="button" className="btn btn-primary" disabled={saving || testing} onClick={submit}>
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </div>
  )
}
