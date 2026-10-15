# -*- coding: utf-8 -*-
"""渲染候选字体对比图,用于挑选最接近「抖音体」的替代字体。"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1] if len(sys.argv) > 1 else "font_compare.png"
FDIR = r"C:\Windows\Fonts"

CANDS = [
    ("msyhbd.ttc", "微软雅黑粗体"),
    ("msyh.ttc", "微软雅黑"),
    ("simhei.ttf", "黑体"),
    ("Dengb.ttf", "等线粗体"),
    ("NotoSansSC-VF.ttf", "NotoSansSC (思源系)"),
]
ZH = "\u957f\u591c\u8fd8\u6ca1\u5230\u5934\uff0c\u6211\u4eec\u65e9\u5c31\u98d8\u4e0a\u5929\u4e86"
EN = "We'll be high as hell before the night ends"
W, H = 720, 220

img = Image.new("RGB", (W, H * len(CANDS)), (18, 18, 22))
dr = ImageDraw.Draw(img)

for i, (fn, label) in enumerate(CANDS):
    p = os.path.join(FDIR, fn)
    y = i * H
    dr.text((8, y + 6), label, fill=(150, 150, 165))
    if not os.path.exists(p):
        dr.text((8, y + 30), "字体文件缺失: " + fn, fill=(255, 80, 80))
        continue
    try:
        fz = ImageFont.truetype(p, 26)
        fe = ImageFont.truetype(p, 34)
    except Exception as exc:
        dr.text((8, y + 30), "载入失败: %s" % exc, fill=(255, 80, 80))
        continue
    # 中文行(白字 + 黑描边 3px)
    bb = dr.textbbox((0, 0), ZH, font=fz, stroke_width=3)
    x = max(0, (W - (bb[2] - bb[0])) // 2 - bb[0])
    dr.text((x, y + 34), ZH, font=fz, fill="white",
            stroke_width=3, stroke_fill="black")
    # 英文行
    bb2 = dr.textbbox((0, 0), EN, font=fe, stroke_width=3)
    x2 = max(0, (W - (bb2[2] - bb2[0])) // 2 - bb2[0])
    dr.text((x2, y + 100), EN, font=fe, fill="white",
            stroke_width=3, stroke_fill="black")

img.save(OUT)
print("对比图 ->", os.path.abspath(OUT))
