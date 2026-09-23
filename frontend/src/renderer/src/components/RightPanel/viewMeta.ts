import type { RPanelView } from '@protocols/agentProtocol'

/**
 * 视图标签的图标映射（右栏四处共用：标签栏 / 「+」菜单 / 欢迎列表 / 聊天区顶栏按钮）。
 *
 * 单独一个文件而不是塞进 `lib/rpanelTabs.ts`：那一层是**纯语义层**
 * （预览位顶替、超限淘汰这类规则），不该知道"文件夹长什么样"。
 * 图标名必须存在于 `components/common/Icon.tsx` 的 `paths` 表里
 * （`diff` / `globe` 就是本期为右栏新加的）。
 */
export const RPANEL_VIEW_ICONS: Record<RPanelView, string> = {
  files: 'folder',
  changes: 'diff',
  terminal: 'terminal',
  browser: 'globe'
}
