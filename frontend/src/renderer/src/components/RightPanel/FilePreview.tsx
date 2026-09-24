import { useEffect, useMemo, useState } from 'react'
import { Icon } from '@components/common/Icon'
import { showToast } from '@store/agentStore'
import { EMPTY_LIVE, useRightPanelStore } from '@store/rightPanelStore'
import { fileStreamUrl, reportFileStream } from '@lib/fileStream'
import RPanelState from './RPanelState'

interface FilePreviewProps {
  sid: string
  /** 当前激活的文件标签（绝对路径 + 显示名） */
  path: string
  name: string
}

/** 人话尺寸（预览头里的 "12.3 KB"）。只在此处用到，不值得做成公共模块。 */
function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** 把绝对路径压成工作空间内的相对路径（树尚未加载时回落到完整路径）。
 *  只影响**显示**：`title` 与复制一律给完整绝对路径。 */
function relativize(workdir: string, path: string): string {
  const base = (workdir || '').replace(/\/+$/, '')
  if (base && path.startsWith(base + '/')) return path.slice(base.length + 1)
  return path
}

/** PDF / Office 转出 PDF 的 `<embed>`。插件渲染交给 Electron 内置
 *  Chromium PDF viewer（webPreferences.plugins: true + CSP object-src
 *  放行 aigent-file:，二者缺一即白板 —— 真机探针验证过，docs/frontend/21）。 */
function PdfEmbed({ src, nonce }: { src: string; nonce: number }): JSX.Element {
  // nonce 只为了在重试时强制重建元素（同 src 的 embed 不会因为重渲染重载）
  return <embed key={nonce} className="rpanel-pdf" src={src} type="application/pdf" />
}

/**
 * 文件标签的正文：单个文件预览（19 篇 §5.7 / 21 篇）。
 *
 * **只显示文件、全宽** —— 这是"单栏混放"标签模型的既定取舍：激活文件标签时
 * 树不在旁边。缓解手段是预览头常驻一枚「浏览」按钮（一键回树），
 * 而不是把树塞回来（那会让标签模型失去意义）。
 *
 * 状态判定优先级固定（与 `RefPicker` 同口径）：
 * `传输错误 > loading > reason(读失败) > too_large(整屏替换) > binary(空态)
 * > 按后端 kind 分派渲染分支 > 文本（可能带截断横幅）`。
 *
 * `kind` 由**后端**分派（扩展名 + 魔数，`refs.classify_preview_kind`）：
 * - `image` → `<img>` 走 `aigent-file://`（CSP img-src 已放行）；
 * - `pdf`   → `<embed type="application/pdf">` 交给内置 PDF viewer；
 * - `office`→ 有 `pdf_path` 渲染转出的 PDF（LibreOffice 在场），否则 `text`
 *   是文本抽取降级（顶部琥珀横幅给 `office_hint`，版式丢失如实说明）；
 * - `text` / `binary` → 代码视图 / 平级空态（原有行为）。
 *
 * ⚠️ **超限**与**截断**必须分开表达：超限是"一点内容都没读"（整屏替换成说明），
 * 截断是"读到了但尾部砍了"（正文照常渲染 + 顶部琥珀条）。混起来会让用户
 * 以为"这个文件就这么点内容"。
 */
export default function FilePreview({ sid, path, name }: FilePreviewProps): JSX.Element {
  const live = useRightPanelStore((s) => s.liveBySession[sid]) ?? EMPTY_LIVE
  const openViewTab = useRightPanelStore((s) => s.openViewTab)
  const closeTab = useRightPanelStore((s) => s.closeTab)
  const ensurePreview = useRightPanelStore((s) => s.ensurePreview)
  const { preview, previewLoading, previewError } = live

  const workdir = live.tree?.workdir ?? ''

  // 媒体分支的加载失败与重试：aigent-file 协议 403/404 时 <img> 有 error 事件、
  // <embed> 没有 —— 统一靠"重试"按钮重新报备 + nonce 重建元素兜底。
  const [mediaFailed, setMediaFailed] = useState(false)
  const [mediaNonce, setMediaNonce] = useState(0)
  useEffect(() => {
    setMediaFailed(false)
  }, [path])

  // 自校验：主进程的请求配对**按 kind FIFO、没有 request id**（19 篇 §4.4），
  // 慢回执可能属于上一个被点开的文件 —— 路径对不上就当它没到（进而在下面显示"读取中"）。
  const data = preview && preview.path === path ? preview : null

  // 标签激活后懒读：只有当前激活的文件才发请求（其余标签切过去时再读）
  useEffect(() => {
    ensurePreview(sid, path)
  }, [sid, path, ensurePreview])

  const rel = useMemo(() => relativize(workdir, path), [workdir, path])
  const lines = useMemo(() => (data?.text ? data.text.split('\n') : []), [data?.text])

  const copyPath = (): void => {
    navigator.clipboard
      .writeText(path)
      .then(() => showToast('已复制路径'))
      .catch(() => showToast('复制失败', 'error', 3000))
  }

  const head = (
    <div className="rpanel-head">
      <div className="rpanel-head-title">
        <span className="rpanel-head-name" title={path}>
          {name || data?.name || path}
        </span>
        <span className="rpanel-head-path" title={path}>
          {rel}
        </span>
        {data && data.size > 0 ? (
          <span className="rpanel-head-path" style={{ flex: 'none' }}>
            {formatBytes(data.size)}
          </span>
        ) : null}
      </div>
      <div className="rpanel-head-actions">
        <button
          className="mini-btn"
          title="浏览文件树（⌘⇧E）"
          onClick={() => openViewTab(sid, 'files')}
        >
          <Icon name="folder" size={14} />
        </button>
        <button className="mini-btn" title="复制路径" onClick={copyPath}>
          <Icon name="copy" size={14} />
        </button>
        <button
          className="mini-btn"
          title="在 Finder 中显示"
          onClick={() => void window.agent.openInFinder(path)}
        >
          <Icon name="externalLink" size={14} />
        </button>
        <button className="mini-btn" title="关闭标签" onClick={() => closeTab(sid, `file:${path}`)}>
          <Icon name="close" size={14} />
        </button>
      </div>
    </div>
  )

  /** 错误态外壳：文件缺失时给「在 Finder 中显示」这个出口（目录可能还在，只是文件没了） */
  const errorState = (title: string, sub: string, missing: boolean): JSX.Element => (
    <div className="rpanel-preview">
      {head}
      <RPanelState
        kind="error"
        icon="fileText"
        title={title}
        sub={sub}
        action={
          missing ? (
            <button className="rpanel-btn" onClick={() => void window.agent.openInFinder(path)}>
              <Icon name="externalLink" size={13} />
              在 Finder 中显示
            </button>
          ) : (
            <button className="rpanel-btn" onClick={() => ensurePreview(sid, path)}>
              重试
            </button>
          )
        }
      />
    </div>
  )

  // ── 状态分派（顺序即优先级）───────────────────────────────────
  // 1) 传输层失败（超时 / 桥层报错）：后端把"读不到"当**正常结果**回，
  //    所以这里非空只可能是真的没拿到信封。
  if (previewError) return errorState('无法读取该文件', previewError, false)

  // 2) 内容还没到：`previewLoading` 与"回执被自校验丢掉"都会走到这 —— 都显示读取中。
  if (!data) {
    return (
      <div className="rpanel-preview">
        {head}
        <RPanelState kind="loading" title="正在读取…" />
      </div>
    )
  }

  // 3) 后端给了原因（越界 / 不存在 / 是目录 / 无权限）
  if (data.reason) {
    const missing = data.reason.includes('不存在')
    return errorState(missing ? '文件已不存在' : '无法读取该文件', data.reason, missing)
  }

  // 4) 超限：**一点内容都没读** → 整屏替换（琥珀，降级不是错误）。
  //    图片 / PDF 的上限是流式 64MB（与后端同值），文本仍是 512KB。
  if (data.too_large) {
    return (
      <div className="rpanel-preview">
        {head}
        <RPanelState
          kind="warn"
          icon="fileText"
          title={`文件过大（${formatBytes(data.size)}），已停止加载`}
          sub="为避免占满内存，超过上限的文件不会读取内容"
          action={
            <button className="rpanel-btn" onClick={() => void window.agent.openInFinder(path)}>
              <Icon name="externalLink" size={13} />
              用本机应用打开
            </button>
          }
        />
      </div>
    )
  }

  // 5) 二进制：**平级空态**，不是错误（压缩包等打不开是预期行为）
  if (data.binary || data.kind === 'binary') {
    return (
      <div className="rpanel-preview">
        {head}
        <RPanelState
          kind="empty"
          icon="fileText"
          title="此文件为二进制，无法预览"
          sub={data.size ? `大小 ${formatBytes(data.size)}` : undefined}
        />
      </div>
    )
  }

  // 6) 图片：`<img>` 直读磁盘。加载失败（白名单未报备上 / 文件刚被删）→ 可重试。
  if (data.kind === 'image' && !mediaFailed) {
    return (
      <div className="rpanel-preview">
        {head}
        <div className="rpanel-media-wrap">
          <img
            key={mediaNonce}
            className="rpanel-media"
            src={fileStreamUrl(data.path)}
            alt={data.name || name}
            onError={() => setMediaFailed(true)}
          />
        </div>
      </div>
    )
  }

  // 7) PDF（含 Office 转出成功的）：交给内置 PDF viewer
  const pdfSrc = data.kind === 'pdf' ? data.path : data.kind === 'office' ? data.pdf_path : ''
  if (pdfSrc && !mediaFailed) {
    return (
      <div className="rpanel-preview">
        {head}
        <PdfEmbed src={fileStreamUrl(pdfSrc)} nonce={mediaNonce} />
      </div>
    )
  }

  // 8) Office 文本降级（没装 LibreOffice / 转换失败）：顶部横幅如实说明，
  //    正文照旧走代码视图。抽不出来（text 为空）→ 平级空态。
  if (data.kind === 'office' || mediaFailed) {
    const hint = data.kind === 'office' ? data.office_hint : ''
    if (data.text) {
      return (
        <div className="rpanel-preview">
          {head}
          {hint ? (
            <div className="rpanel-notice">
              <Icon name="clock" size={13} />
              <span className="rpanel-notice__text">{hint}</span>
            </div>
          ) : null}
          <div className="rpanel-code">
            <div className="rpanel-code-gutter" aria-hidden="true">
              {lines.map((_, i) => `${i + 1}`).join('\n')}
            </div>
            <pre className="rpanel-code-pre">{data.text}</pre>
          </div>
        </div>
      )
    }
    return (
      <div className="rpanel-preview">
        {head}
        <RPanelState
          kind={mediaFailed ? 'error' : 'empty'}
          icon="fileText"
          title={mediaFailed ? '文件内容加载失败' : '无法预览此文件'}
          sub={
            mediaFailed
              ? '文件可能已被移动或删除'
              : hint || (data.size ? `大小 ${formatBytes(data.size)}` : undefined)
          }
          action={
            <button
              className="rpanel-btn"
              onClick={() => {
                // 重新报备（白名单可能因 200 上限被挤出）再重载
                reportFileStream([data.path, data.pdf_path || undefined])
                setMediaFailed(false)
                setMediaNonce((n) => n + 1)
                ensurePreview(sid, path)
              }}
            >
              重试
            </button>
          }
        />
      </div>
    )
  }

  // 9) 正常文本（可能带"被截断"横幅）
  return (
    <div className="rpanel-preview">
      {head}
      {data.truncated ? (
        <div className="rpanel-notice">
          <Icon name="clock" size={13} />
          <span className="rpanel-notice__text">
            内容过长，仅显示前 {data.lines || lines.length} 行
          </span>
        </div>
      ) : null}
      <div className="rpanel-code">
        <div className="rpanel-code-gutter" aria-hidden="true">
          {lines.map((_, i) => `${i + 1}`).join('\n')}
        </div>
        <pre className="rpanel-code-pre">{data.text}</pre>
      </div>
    </div>
  )
}
