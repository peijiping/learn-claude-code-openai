#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 个人AI助手 应用图标设计源图 build/icon.png（1024x1024 RGBA 透明底）。
v5 极简风单色浅蓝机器人：一个圆角方形头 + 两点 LED 像素眼，去掉一切多余细节。
扁平、无立体；"眼睛"以底色(CUT)镂空呈现。"""
from PIL import Image, ImageDraw

SIZE = 1024
ROBOT = (0x43, 0xA9, 0xF6)
BG    = (0xE6, 0xEA, 0xF1)
CUT   = BG

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# ---- 浅灰圆角方形底 ----
d.rounded_rectangle([84, 84, SIZE - 84, SIZE - 84], radius=200, fill=BG)

# ---- 机器人头：单个圆角方头 ----
d.rounded_rectangle([312, 318, 712, 688], radius=96, fill=ROBOT)

# ---- 两只 LED 像素眼（圆角矩形镂空，克制不卖萌）----
eye_w, eye_h = 88, 62
y0, y1 = 452, 452 + eye_h
d.rounded_rectangle([388, y0, 388 + eye_w, y1], radius=26, fill=CUT)
d.rounded_rectangle([SIZE - 388 - eye_w, y0, SIZE - 388, y1], radius=26, fill=CUT)

img.save("build/icon.png")
print("saved build/icon.png (v5 极简)")