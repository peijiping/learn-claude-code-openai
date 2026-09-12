"""
utils.py - 公共工具方法模块

提供项目中通用的工具函数，避免代码重复。
"""


def is_binary_content(text: str) -> bool:
    """检测输出是否包含大量二进制垃圾数据"""
    if not text or len(text) < 100:
        return False
    sample = text[:2000]
    printable = sum(1 for c in sample if c.isprintable() or c in '\n\r\t')
    if (len(sample) - printable) / len(sample) > 0.3:
        return True
    binary_patterns = [
        'endobj', 'endstream', '/FontDescriptor', '/CIDToGIDMap',
        '/Type /Font', '/Subtype /CIDFont', '/BaseFont /',
        '0 obj<<', '/FontFile2', '/ToUnicode',
    ]
    pattern_hits = sum(1 for p in binary_patterns if p in sample)
    if pattern_hits >= 2:
        return True
    return False


def truncate_chars(text: str, max_chars: int = 200, suffix: str = "...") -> str:
    """按字符数截断字符串

    超过 max_chars 时切片前 max_chars 个字符并追加 suffix；否则原样返回。
    适用于在终端打印前对长字符串做安全截断，避免刷屏。

    注意：返回值最大长度为 max_chars + len(suffix)，即 suffix 不算在限额内。
    保持与"行内手写切片 `s[:N] + '...'`"一致的语义。

    Args:
        text:     原始字符串
        max_chars:最大允许字符数（不含 suffix 长度）
        suffix:   截断时追加的标记，默认 "..."

    Returns:
        截断后的字符串
    """
    if len(text) > max_chars:
        return text[:max_chars] + suffix
    return text


def smart_truncate(text: str, max_chars: int = 10000) -> str:
    """智能截断：保留首尾，中间用省略标记替代"""
    if len(text) <= max_chars:
        return text
    head_size = max_chars // 2
    tail_size = max_chars // 4
    head = text[:head_size]
    tail = text[-tail_size:]
    return f"{head}\n\n... [输出已截断，共 {len(text)} 字符，保留首 {head_size} + 尾 {tail_size} 字符] ...\n\n{tail}"