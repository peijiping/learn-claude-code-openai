#!/usr/bin/env python3
"""
echo_server.py - 本地示例 MCP server（验证用，离线）

基于 SDK 内置 FastMCP，同时支持 stdio（本地进程）与 streamable-http（远程）两种传输，
并暴露：
- 只读工具：echo / add（带 readOnlyHint 标注）
- 破坏性工具：delete_thing（带 destructiveHint 标注，用于测试审批门控）
- 只读资源：echo://greeting（用于测试 Resources list/read）

运行方式：
  stdio: python -m mcp_servers.echo_server stdio
  http : python -m mcp_servers.echo_server http     # 127.0.0.1:8766/mcp（8765 已被 ws_bridge 占用）
"""

import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


mcp = FastMCP("echo", host="127.0.0.1", port=8766)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(text: str) -> str:
    """Echo the input text back (read-only)."""
    return f"echo: {text}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def add(a: int, b: int) -> int:
    """Add two integers (read-only)."""
    return a + b


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
def delete_thing(name: str) -> str:
    """Delete a thing by name (destructive — requires approval)."""
    return f"[demo] deleted {name}"


@mcp.resource("echo://greeting", name="Greeting", description="A friendly greeting text")
def greeting() -> str:
    return "Hello from echo MCP server!"


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if mode == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
