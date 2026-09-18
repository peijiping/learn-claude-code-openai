#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 build/icon.png 生成 macOS 应用图标 build/icon.icns。
先把 1024 PNG 缩放到 iconset 规定的各尺寸，再用 iconutil 打包为 .icns。"""
import os
import shutil
import subprocess
import tempfile

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "build", "icon.png")
ICNS = os.path.join(ROOT, "build", "icon.icns")

# (base, scale) -> 目标边长；Apple iconset 命名规则
SIZES = {
    ("16x16", 1): 16,
    ("16x16", 2): 32,
    ("32x32", 1): 32,
    ("32x32", 2): 64,
    ("128x128", 1): 128,
    ("128x128", 2): 256,
    ("256x256", 1): 256,
    ("256x256", 2): 512,
    ("512x512", 1): 512,
    ("512x512", 2): 1024,
}

src = Image.open(SRC).convert("RGBA")

tmp = tempfile.mkdtemp(prefix="aigent-iconset-")
iconset = os.path.join(tmp, "aigent.iconset")
os.makedirs(iconset, exist_ok=True)

for (base, scale), px in SIZES.items():
    suffix = "@2x" if scale == 2 else ""
    name = f"icon_{base}{suffix}.png"
    img = src.resize((px, px), Image.LANCZOS)
    img.save(os.path.join(iconset, name))

subprocess.run(
    ["iconutil", "-c", "icns", iconset, "-o", ICNS],
    check=True,
)
shutil.rmtree(tmp, ignore_errors=True)
print(f"done: {ICNS}")