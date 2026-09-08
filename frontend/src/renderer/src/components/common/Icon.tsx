import type { ReactNode } from 'react'

/** 轻量内联 SVG 图标集（避免引入重型图标库） */
const paths: Record<string, ReactNode> = {
  plus: <path d="M12 5v14M5 12h14" />,
  puzzle: (
    <>
      <path d="M10 4h4v2a2 2 0 0 0 4 0V4h2v6h-2a2 2 0 0 0 0 4h2v6h-6v-2a2 2 0 0 0-4 0v2H4v-6h2a2 2 0 0 0 0-4H4V4z" />
    </>
  ),
  clipboard: (
    <>
      <rect x="5" y="4" width="14" height="16" rx="2" />
      <path d="M9 2h6v3H9zM9 10h6M9 14h6" />
    </>
  ),
  gear: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5h.1a1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z" />
    </>
  ),
  bolt: <path d="M13 2 3 14h7l-1 8L20 10h-7z" />,
  chat: <path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />,
  asterisk: (
    <>
      <path d="M12 3v18M5.6 7.5l12.8 9M18.4 7.5l-12.8 9" />
    </>
  ),
  filter: <path d="M4 6h16M7 12h10M10 18h4" />,
  chevronDown: <path d="m6 9 6 6 6-6" />,
  chevronRight: <path d="m9 6 6 6-6 6" />,
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />,
  send: <path d="M22 2 11 13M22 2l-7 20-4-9-9-4z" />,
  paperclip: <path d="M21 12A9 9 0 0 1 7.3 12L14 5.3a5.7 5.7 0 0 1 8 8L13 22.5" />,
  chart: (
    <>
      <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
    </>
  ),
  bell: <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0" />,
  terminal: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="m7 9 3 3-3 3M13 15h4" />
    </>
  ),
  stop: <rect x="6" y="6" width="12" height="12" rx="1" />,
  user: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21c0-4 3.6-6 8-6s8 2 8 6" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </>
  ),
  refresh: <path d="M3 12a9 9 0 1 0 3-6.7M3 4v5h5" />,
  restore: (
    <>
      <path d="M3 7v6h6" />
      <path d="M21 17a9 9 0 0 0-15-6.7L3 13" />
    </>
  ),
  more: <path d="M5 12h.01M12 12h.01M19 12h.01" />,
  edit: (
    <>
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4z" />
    </>
  ),
  check: <path d="m4 12.5 5 5L20 6.5" />,
  trash: (
    <>
      <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6M14 11v6" />
    </>
  ),
  close: <path d="M18 6 6 18M6 6l12 12" />,
  eye: (
    <>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  eyeSlash: (
    <>
      <path d="M17.9 6.1A10.5 10.5 0 0 0 12 5C5 5 2 12 2 12s1.5 3.2 4.2 5.4M9.9 4.2A9.9 9.9 0 0 1 12 4c7 0 10 8 10 8a18 18 0 0 1-2.2 3.5M6.6 6.6C3.8 8.6 2 12 2 12s3 8 10 8a9.8 9.8 0 0 0 5.4-1.6" />
      <path d="m2 2 20 20" />
    </>
  )
}

interface IconProps {
  name: keyof typeof paths | string
  size?: number
  color?: string
  className?: string
}

export function Icon({ name, size = 16, color = 'currentColor', className }: IconProps): ReactNode {
  const body = paths[name] ?? <circle cx="12" cy="12" r="8" />
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={{ flex: 'none', display: 'block' }}
    >
      {body}
    </svg>
  )
}