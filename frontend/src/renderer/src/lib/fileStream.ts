/**
 * aigent-file:// 文件流的渲染层侧工具（多格式预览，docs/frontend/21）。
 *
 * 图片 / PDF / Office 转出的 PDF **不经 WS 传字节**（帧上限 1 MiB），由主进程的
 * `aigent-file` 协议直读磁盘喂给 `<img>` / `<embed>`。协议只认"后端 file_read
 * 回执给过、渲染层报备过"的路径 —— 报备动作统一收在这里，谁构造 URL 谁（或其
 * 数据入口）先报备。
 */

/** 绝对路径 → 协议 URL。`local` 是固定 host（privileged scheme 需要）。 */
export function fileStreamUrl(path: string): string {
  return `aigent-file://local/?path=${encodeURIComponent(path)}`
}

/** 把一批路径报备进主进程白名单（fire-and-forget；无 preload 的环境静默跳过）。 */
export function reportFileStream(paths: Array<string | undefined | null | void>): void {
  const list = paths.filter((p): p is string => typeof p === 'string' && p.length > 0)
  if (!list.length) return
  try {
    void window.agent?.allowFileStream?.(list)
  } catch {
    /* 浏览器回退通道缺失：忽略（该环境本来也不渲染文件预览） */
  }
}
