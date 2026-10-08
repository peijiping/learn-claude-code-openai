import { useEffect, useMemo, useState } from 'react'
import type {
  McpMarketItem,
  McpMarketPlan,
  McpPkgActionResult,
  McpPkgPlan,
  McpTestResult
} from '@protocols/agentProtocol'

/** 市场安装确认（设置弹窗「MCP」→ 市场 → 安装）。
 *
 * 这是整个 MCP 功能里**唯一**的安全闸门，两条不可妥协的约束：
 * 1. **必须原样展示将要写入的 `command` / `args` / `url`**。那是"本机即将执行什么
 *    代码"的唯一凭据 —— 市场条目只经过命名空间校验，没有任何代码审计，用户唯一
 *    能判断的依据就是这段原文。不要折叠、不要摘要、不要只显示包名。
 * 2. **不提供"记住我的选择"之类的便捷开关**。stdio 类型是任意代码执行，
 *    每次都该看一眼。
 *
 * 2026-10-07 增加「下载到本地」（docs/frontend/23 §本地安装）。它改变的是
 * **包从哪里来**，不是"要不要看原文"：
 *   · 由 npx 拉取 —— 包躺在 `~/.npm/_npx/<hash>/`，每次连接都可能重新解析版本，
 *     没有"装了什么"的概念，也无法卸载；
 *   · 下载到本地 —— 钉死精确版本 + 记录完整性哈希，落在 `~/.aigent/mcp/pkgs/`，
 *     可复核、可卸载。`command` 相应变成包内 bin 的绝对路径。
 * **默认选「下载到本地」**（`pkg.local_installable` 为真时）。默认值本身就是一条
 * 安全主张，所以它必须在界面上看得见，而不是藏进某个设置里。
 *
 * ⚠️ 解析失败**不阻断安装**：registry 不可达时仍可退回 npx 方式，只把失败内联展示。
 * 否则一个网络抖动会让用户彻底装不上任何 MCP。
 */

const CONFIG_LABEL: Record<string, string> = {
  type: '传输类型',
  command: '命令',
  url: 'URL',
  cwd: '工作目录'
}

interface Props {
  item: McpMarketItem
  plan: McpMarketPlan
  saving: boolean
  testing: boolean
  test: McpTestResult | null
  /** 上一次安装被拒 / 失败的原因（来自 mcp_config 回执，内联展示不 toast） */
  saveErrors: string[]
  /** 本地安装计划（勾选「下载到本地」后由 `mcp_pkg_resolve` 给出，**不落盘**） */
  pkgPlan: McpPkgPlan | null
  /** 本地包在途操作（resolve / install / remove / verify） */
  pkgBusy: 'resolve' | 'install' | 'remove' | 'verify' | null
  /** 最近一次本地包动作结果（install 的 `command` 在这里） */
  pkgAction: McpPkgActionResult | null
  onResolvePkg: (name: string, version: string) => void
  onClearPkgPlan: () => void
  /** 下载 + 校验（**不写配置**） */
  onDownload: (payload: {
    name: string
    version: string
    bin: string
    allow_scripts: boolean
  }) => Promise<McpPkgActionResult | null>
  onSave: (payload: {
    name: string
    config: Record<string, unknown>
    meta: Record<string, unknown>
  }) => Promise<boolean>
  onTest: (config: Record<string, unknown>) => void
  onClearTest: () => void
  onBack: () => void
}

export default function McpInstallView({
  item,
  plan,
  saving,
  testing,
  test,
  saveErrors,
  pkgPlan,
  pkgBusy,
  pkgAction,
  onResolvePkg,
  onClearPkgPlan,
  onDownload,
  onSave,
  onTest,
  onClearTest,
  onBack
}: Props): JSX.Element {
  const [name, setName] = useState(plan.name)
  /** 包坐标（远程端点条目为 null → 只能走 npx/远程，没有本地安装这条路） */
  const pkg = plan.pkg
  const canLocal = !!pkg?.local_installable
  const [local, setLocal] = useState(canLocal)
  const [allowScripts, setAllowScripts] = useState(false)
  const [bin, setBin] = useState('')
  /** 两步式安装（下载 → 写配置）在途标记 */
  const [installing, setInstalling] = useState(false)

  const [envValues, setEnvValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {}
    for (const v of plan.env_required ?? []) init[v.name] = v.default || ''
    return init
  })

  // 勾选本地安装 → 立刻去问 registry 要真值（版本/哈希/依赖树/脚本清单）。
  // 没拿到就得让用户看见"正在解析"，否则确认区显示的是一份不存在的计划。
  useEffect(() => {
    if (!local || !canLocal || !pkg) return
    if (pkgPlan || pkgBusy === 'resolve') return
    onResolvePkg(pkg.identifier, pkg.version)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [local, canLocal, pkg?.identifier, pkg?.version, pkgPlan, pkgBusy])

  // 解析结果里的 bin 候选变化时复位选择（避免沿用上一个包的 bin 名）
  useEffect(() => {
    setBin(pkgPlan?.default_bin ?? '')
  }, [pkgPlan?.slug, pkgPlan?.default_bin])

  const missingRequired = useMemo(
    () =>
      (plan.env_required ?? [])
        .filter((v) => v.required && !String(envValues[v.name] ?? '').trim())
        .map((v) => v.name),
    [plan.env_required, envValues]
  )

  const envObject = (): Record<string, string> => {
    const env: Record<string, string> = {}
    for (const v of plan.env_required ?? []) {
      const val = String(envValues[v.name] ?? '').trim()
      if (val) env[v.name] = val
    }
    return env
  }

  /** npx 方式：`plan.config` 原文 + 用户填的环境变量 + 默认启用。 */
  const npxConfig = (): Record<string, unknown> => {
    const cfg: Record<string, unknown> = { ...plan.config, enable: true }
    const env = envObject()
    if (Object.keys(env).length > 0) cfg.env = env
    return cfg
  }

  /** 本地方式可用的可执行文件绝对路径。两个来源：
   *  ① 本次刚装好的（`pkgAction.command`）；② 本机早就装过同一版本
   *  （`already_installed.command`）—— 后者**不必再下一次**。 */
  const readyCommand = local
    ? pkgAction?.action === 'install' && pkgAction.ok && pkgAction.command
      ? pkgAction.command
      : (pkgPlan?.already_installed?.command ?? '')
    : ''

  /** 本地方式：command 换成包内 bin 的绝对路径，args 只保留**属于服务本身**的那部分
   *  （`package_args`）—— npx 的 `-y` 与包名对新命令毫无意义。 */
  const localConfig = (command: string): Record<string, unknown> => {
    const cfg: Record<string, unknown> = { type: 'stdio', command }
    const args = plan.package_args ?? []
    if (args.length > 0) cfg.args = args
    if (plan.config.cwd) cfg.cwd = plan.config.cwd
    const env = envObject()
    if (Object.keys(env).length > 0) cfg.env = env
    cfg.enable = true
    return cfg
  }

  const useLocal = local && canLocal && !!pkg
  /** 正在复用本机已有的那份副本（而不是这次刚下下来的）。
   *
   *  ⚠️ 判据**不能**写成 `already_installed && !readyCommand` —— `readyCommand`
   *  恰恰是从 `already_installed.command` 推出来的，两者不可能同时成立，那条提示
   *  是**死代码**（headless 实测抓到的：预览命令已是绝对路径 = 确实在复用，但提示
   *  一个字都没渲染）。要判的是"当前这个 command 是不是正好来自它"。 */
  const reusingExisting =
    !!pkgPlan?.already_installed?.command && readyCommand === pkgPlan.already_installed.command

  /** 「将要写入的配置」区展示的内容。三种形态，**必须各说各的实话**：
   *  1. 本地 + 已就绪 → 包内 bin 的绝对路径（真正要写的那条）；
   *  2. 本地 + 还没下载 → **绝不能显示 npx 那条命令**。选了"下载到本地"却摆出
   *     `npx -y pkg@ver`，再配一句"直接执行下面这个绝对路径"，是自相矛盾 ——
   *     而这一区是整页唯一的凭据（截图核对时抓到的）。所以只展示**不会变的那部分**
   *     （服务自己的参数），命令留空并说明下载完会补上；
   *  3. npx → `plan.config` 原文。 */
  const displayConfig = (): Record<string, unknown> => {
    if (useLocal && readyCommand) return localConfig(readyCommand)
    if (useLocal) {
      const cfg: Record<string, unknown> = { type: 'stdio', command: '' }
      const args = plan.package_args ?? []
      if (args.length > 0) cfg.args = args
      if (plan.config.cwd) cfg.cwd = plan.config.cwd
      return cfg
    }
    return npxConfig()
  }
  const previewConfig = displayConfig()
  // 命令位**只放命令**（参数有独立一行）—— 拼在一起会让"命令还没填"这种状态
  // 显示成 `" /tmp"`（前导空格 + 参数），既难看又掩盖了"这里确实还没有命令"。
  const cmdPreview =
    previewConfig.type === 'stdio'
      ? String(previewConfig.command ?? '')
      : String(previewConfig.url ?? '')

  const pkgOk = !!pkgPlan?.ok
  const needsDownload = useLocal && !readyCommand
  const busy = installing || saving || pkgBusy === 'install' || pkgBusy === 'resolve'

  const doInstall = async (): Promise<void> => {
    if (needsDownload) {
      if (!pkg || !pkgOk || !pkgPlan) return
      setInstalling(true)
      try {
        const res = await onDownload({
          name: pkgPlan.name,
          version: pkgPlan.version,
          bin,
          allow_scripts: allowScripts
        })
        if (!res?.ok || !res.command) return
        // 下载成功 → 紧接着写配置。**下载与写配置是两步**（见 ws_bridge 的
        // mcp_pkg_install 注释）：下载失败绝不会留下一条指向不存在文件的条目。
        await onSave({
          name: name.trim(),
          config: localConfig(res.command),
          meta: {
            source: 'market',
            market_id: item.id,
            market_name: item.short_name,
            publisher: item.publisher,
            pkg: {
              slug: res.slug ?? '',
              name: pkgPlan.name,
              version: pkgPlan.version,
              command: res.command,
              integrity: res.integrity ?? '',
              registry: res.registry ?? ''
            }
          }
        })
      } finally {
        setInstalling(false)
      }
      return
    }
    const cfg = useLocal && readyCommand ? localConfig(readyCommand) : npxConfig()
    await onSave({
      name: name.trim(),
      config: cfg,
      meta: {
        source: 'market',
        market_id: item.id,
        market_name: item.short_name,
        publisher: item.publisher,
        ...(useLocal && readyCommand && pkgPlan
          ? {
              pkg: {
                slug: pkgAction?.slug ?? pkgPlan.slug,
                name: pkgPlan.name,
                version: pkgPlan.version,
                command: readyCommand,
                integrity: pkgPlan.integrity,
                registry: pkgPlan.registry
              }
            }
          : {})
      }
    })
  }

  return (
    <div className="mcp">
      <div className="mcp-form-head">
        <button type="button" className="mcp-back" onClick={onBack}>
          ← 返回市场
        </button>
        <span className="mcp-form-title">确认安装「{item.title || item.short_name}」</span>
      </div>

      {saveErrors.map((e, i) => (
        <p className="sbx-error" key={`${i}-${e}`}>
          {e}
        </p>
      ))}

      {/* ── 安装方式（二选一，默认在本机下载） ── */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">安装方式</h4>
        {canLocal ? (
          <div className="mcp-radio-group">
            <label className={`mcp-radio ${local ? 'on' : ''}`}>
              <input
                type="radio"
                name="mcp-install-mode"
                checked={local}
                onChange={() => setLocal(true)}
              />
              <span className="mcp-radio-body">
                <span className="mcp-radio-title">下载到本地（推荐）</span>
                <span className="mcp-radio-desc">
                  钉死精确版本并记录完整性哈希，装在 <code>~/.aigent/mcp/pkgs/</code> 下，
                  可复核、可卸载、不随上游发版漂移。
                </span>
              </span>
            </label>
            <label className={`mcp-radio ${!local ? 'on' : ''}`}>
              <input
                type="radio"
                name="mcp-install-mode"
                checked={!local}
                onChange={() => {
                  setLocal(false)
                  setAllowScripts(false)
                  onClearPkgPlan()
                }}
              />
              <span className="mcp-radio-body">
                <span className="mcp-radio-title">由 npx 在连接时拉取</span>
                <span className="mcp-radio-desc">
                  不写本地目录：包缓存落在 <code>~/.npm</code> 里，应用管不到它的版本与去留。
                  只在你不想多下一份时选。
                </span>
              </span>
            </label>
          </div>
        ) : (
          <p className="mcp-inline-note">
            {pkg
              ? `该条目声明的是 ${pkg.registry_type} 包，本地安装暂只支持 npm；将按运行时方式拉取。`
              : '该条目是远程端点（URL），没有需要下载的包。'}
          </p>
        )}
      </section>

      {/* ── 本地安装计划（解析真值，非下载） ── */}
      {useLocal && (
        <section className="perm-sec">
          <h4 className="perm-sec-title">将要下载的包</h4>
          {pkgBusy === 'resolve' || !pkgPlan ? (
            <p className="mcp-inline-note">正在向 registry 核对版本与依赖…</p>
          ) : !pkgPlan.ok ? (
            <>
              <p className="sbx-error">{pkgPlan.error || '解析失败'}</p>
              <p className="mcp-inline-note">
                可以改成上面的「由 npx 在连接时拉取」继续安装，或稍后重试。
              </p>
            </>
          ) : (
            <>
              <div className="mcp-transcript">
                <div className="mcp-transcript-line">
                  <span className="mcp-transcript-key">包</span>
                  <code className="mcp-transcript-val">{pkgPlan.spec}</code>
                </div>
                <div className="mcp-transcript-line">
                  <span className="mcp-transcript-key">来源</span>
                  <code className="mcp-transcript-val">{pkgPlan.registry}</code>
                </div>
                <div className="mcp-transcript-line">
                  <span className="mcp-transcript-key">依赖</span>
                  <code className="mcp-transcript-val">
                    {pkgPlan.dep_count === null || pkgPlan.dep_count === undefined
                      ? `未知（直接依赖 ${pkgPlan.direct_dep_count} 个）`
                      : `共 ${pkgPlan.dep_count} 个包（直接依赖 ${pkgPlan.direct_dep_count} 个）`}
                  </code>
                </div>
                <div className="mcp-transcript-line">
                  <span className="mcp-transcript-key">完整性</span>
                  <code className="mcp-transcript-val">
                    {pkgPlan.integrity ||
                      (pkgPlan.shasum ? `sha1:${pkgPlan.shasum}` : 'registry 未提供')}
                  </code>
                </div>
                <div className="mcp-transcript-line">
                  <span className="mcp-transcript-key">安装目录</span>
                  <code className="mcp-transcript-val">{pkgPlan.dir}</code>
                </div>
                {pkgPlan.bins.length > 1 && (
                  <div className="mcp-transcript-line">
                    <span className="mcp-transcript-key">可执行文件</span>
                    <select
                      className="mcp-input mono"
                      value={bin}
                      onChange={(e) => {
                        setBin(e.target.value)
                        onClearTest()
                      }}
                    >
                      {pkgPlan.bins.map((b) => (
                        <option key={b} value={b}>
                          {b}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
              </div>

              {pkgPlan.pinned_from_latest && (
                <p className="mcp-warn">
                  该条目没有声明版本，已钉死为 registry 当前 latest：
                  <b> {pkgPlan.version}</b>。
                </p>
              )}

              {/* 安装期脚本：**默认一律跳过**，要跑必须在这里显式开 */}
              {pkgPlan.has_scripts && (
                <div className="mcp-scripts-warn">
                  <p className="mcp-scripts-title">
                    这个包声明了安装期脚本，默认<b>不会</b>执行
                  </p>
                  <div className="mcp-transcript">
                    {Object.entries(pkgPlan.install_hooks).map(([k, v]) => (
                      <div className="mcp-transcript-line" key={k}>
                        <span className="mcp-transcript-key">{k}</span>
                        <code className="mcp-transcript-val">{v}</code>
                      </div>
                    ))}
                  </div>
                  <p className="mcp-inline-note">
                    安装期脚本会在<b>下载过程中</b>以你的身份执行任意代码，且早于任何连接
                    握手。跳过它可能让需要本地编译的包装不起来 —— 那种情况请改用 npx 方式，
                    而不是在这里放开。
                  </p>
                  <label className="mcp-check">
                    <input
                      type="checkbox"
                      checked={allowScripts}
                      onChange={(e) => setAllowScripts(e.target.checked)}
                    />
                    <span>我确认要执行上面这些脚本</span>
                  </label>
                </div>
              )}

              {reusingExisting && (
                <p className="mcp-inline-note">
                  本机已装过这个版本（{pkgPlan?.already_installed?.installed_at || '时间未知'}），
                  将直接复用，不会重新下载。
                </p>
              )}
            </>
          )}
        </section>
      )}

      {/* ── 将写入的配置原文（安全闸门） ── */}
      <section className="perm-sec">
        <h4 className="perm-sec-title">将要写入的配置</h4>
        <p className="perm-sec-desc">
          来源 <code>{item.id}</code>
          {item.version ? ` · 版本 ${item.version}` : ''}。
          {!useLocal
            ? '安装只写入本地配置，包由运行时（npx / uvx）在首次连接时拉取。'
            : readyCommand
              ? '安装方式：下载到本地（直接执行下面这个绝对路径）。'
              : '安装方式：下载到本地 —— 下面这条命令的「命令」会在下载完成后补上包内可执行文件的绝对路径。'}
        </p>
        <div className="mcp-transcript">
          <div className="mcp-transcript-line">
            <span className="mcp-transcript-key">
              {CONFIG_LABEL[previewConfig.type === 'stdio' ? 'command' : 'url']}
            </span>
            <code className="mcp-transcript-val">{cmdPreview || '（下载完成后填入）'}</code>
          </div>
          {previewConfig.type === 'stdio' && (
            <div className="mcp-transcript-line">
              <span className="mcp-transcript-key">参数</span>
              <code className="mcp-transcript-val">
                {((previewConfig.args as string[]) ?? []).length > 0
                  ? JSON.stringify(previewConfig.args)
                  : '(无)'}
              </code>
            </div>
          )}
          <div className="mcp-transcript-line">
            <span className="mcp-transcript-key">传输</span>
            <code className="mcp-transcript-val">{String(previewConfig.type ?? '')}</code>
          </div>
        </div>
        {previewConfig.type === 'stdio' && (
          <p className="mcp-inline-warn">
            这一条会在本机直接执行上面的命令。请确认包名与来源可信 —— 市场条目
            <b>没有经过代码审计</b>。
          </p>
        )}
      </section>

      {/* ── 条目名 ── */}
      <section className="perm-sec">
        <div className="mcp-field">
          <label className="mcp-label">
            条目名称
            <span className="mcp-hint">
              模型侧工具前缀为 <code>mcp__&lt;名称&gt;__&lt;工具&gt;</code>
            </span>
          </label>
          <input
            className="mcp-input mono"
            value={name}
            spellCheck={false}
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        {/* ── 声明的环境变量 ── */}
        {(plan.env_required ?? []).length > 0 && (
          <div className="mcp-field">
            <label className="mcp-label">
              环境变量
              <span className="mcp-hint">该服务声明的配置项；标 * 的为必填</span>
            </label>
            {(plan.env_required ?? []).map((v) => (
              <div className="mcp-env-row" key={v.name}>
                <span className="mcp-env-name mono">
                  {v.name}
                  {v.required ? ' *' : ''}
                </span>
                <input
                  className="mcp-input mono"
                  // isSecret 只影响这里的输入遮挡；落盘仍是明文（文件权限 0600）
                  type={v.secret ? 'password' : 'text'}
                  placeholder={v.description || '（可留空）'}
                  value={envValues[v.name] ?? ''}
                  spellCheck={false}
                  onChange={(e) => {
                    const val = e.target.value
                    setEnvValues((prev) => ({ ...prev, [v.name]: val }))
                    onClearTest()
                  }}
                />
              </div>
            ))}
            <p className="mcp-inline-note">
              值以明文写入 <code>~/.aigent/mcp/mcp_servers.json</code>（文件权限 0600）。
            </p>
          </div>
        )}
      </section>

      {plan.warnings.length > 0 &&
        plan.warnings.map((w, i) => (
          <p className="mcp-warn" key={`${i}-${w}`}>
            {w}
          </p>
        ))}

      {/* ── 下载结果（失败时这里是唯一线索） ── */}
      {pkgAction?.action === 'install' && (
        <div className={`mcp-test-box ${pkgAction.ok ? 'ok' : 'fail'}`}>
          <p className="mcp-test-title">
            {pkgAction.ok
              ? `${pkgAction.reused ? '已复用本机副本' : '下载完成'}${
                  pkgAction.dep_count ? ` · ${pkgAction.dep_count} 个包` : ''
                }`
              : '下载失败'}
          </p>
          {pkgAction.ok ? (
            <p className="mcp-inline-note mono">{pkgAction.command}</p>
          ) : (
            <>
              <p className="mcp-test-error mono">{pkgAction.error}</p>
              {pkgAction.log && <p className="mcp-test-error mono">{pkgAction.log}</p>}
            </>
          )}
          {(pkgAction.warnings ?? []).map((w, i) => (
            <p className="mcp-inline-warn" key={`${i}-${w}`}>
              {w}
            </p>
          ))}
        </div>
      )}

      {test && (
        <section className={`mcp-test-box ${test.ok ? 'ok' : 'fail'}`}>
          {test.ok ? (
            <>
              <p className="mcp-test-title">
                连接成功 · 发现 {test.tool_count} 个工具 · {test.elapsed_ms}ms
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
          // 本地方式在下载**之前**测不了（bin 还不存在）；npx 方式可以。
          disabled={
            testing || busy || missingRequired.length > 0 || (useLocal && !readyCommand && !pkgOk)
          }
          title={
            missingRequired.length > 0
              ? `请先填写必填项：${missingRequired.join('、')}`
              : useLocal && !readyCommand
                ? '先下载到本地，再试连'
                : '用当前填好的配置试连一次（不会保存）'
          }
          onClick={() => onTest(previewConfig)}
        >
          {testing ? '正在连接…' : '测试连接'}
        </button>
        <span className="perm-foot-status">
          {missingRequired.length > 0 && (
            <span className="mcp-inline-warn">缺少必填：{missingRequired.join('、')}</span>
          )}
        </span>
        <button type="button" className="btn" disabled={busy} onClick={onBack}>
          取消
        </button>
        <button
          type="button"
          className="btn btn-primary"
          // 必填环境变量没填齐时**必须拦住**：后端只校验传输/命令这类结构，
          // 不会（也不该）知道某个 env 是业务必填 —— 漏了这条就能装出一个
          // 永远连不上的条目，而用户看不出为什么。
          disabled={busy || testing || !name.trim() || missingRequired.length > 0 || (useLocal && !pkgOk)}
          title={
            missingRequired.length > 0
              ? `请先填写必填项：${missingRequired.join('、')}`
              : useLocal && !pkgOk
                ? '本地安装计划还没解析成功；可改用 npx 方式'
                : needsDownload
                  ? '下载到本地并写入配置'
                  : '写入配置并启用'
          }
          onClick={() => void doInstall()}
        >
          {pkgBusy === 'install' || installing
            ? '正在下载…'
            : saving
              ? '保存中…'
              : needsDownload
                ? '下载并安装'
                : '确认安装'}
        </button>
      </div>
    </div>
  )
}
