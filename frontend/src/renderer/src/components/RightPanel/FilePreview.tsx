import { useEffect, useMemo } from 'react'
import { Icon } from '@components/common/Icon'
import { showToast } from '@store/agentStore'
import { EMPTY_LIVE, useRightPanelStore } from '@store/rightPanelStore'
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

/**
 * 文件标签的正文：单个文件预览（19 篇 §5.7）。
 *
 * **只显示文件、全宽** —— 这是"单栏混放"标签模型的既定取舍：激活文件标签时
 * 树不在旁边。缓解手段是预览头常驻一枚「浏览」按钮（一键回树），
 * 而不是把树塞回来（那会让标签模型失去意义）。
 *
 * 五种状态 + 一个补充，判定优先级固定
 * `不可用 > error > loading > empty > content`（与 `RefPicker` 同口径）：
 * 加载中 / 读失败 / 文件已不存在 / 二进制 / 超限（整屏替换） / 被截断（横幅 + 正文）。
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

  // 4) 二进制：**平级空态**，不是错误（图片 / 压缩包打不开是预期行为）
  if (data.binary) {
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

  // 5) 超限：**一点内容都没读** → 整屏替换（琥珀，降级不是错误）
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

  // 6) 正常内容（可能带"被截断"横幅）
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
