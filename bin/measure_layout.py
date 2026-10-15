#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
measure_layout.py — 从用户的成片里精确量出排版参数

为什么要这个:排版参数(标题位置、字号、字幕底距)从截图肉眼看只能估个大概,
而"大概"会让成片"看着能用但不像你的"。这里用像素统计精确量出来,
直接换算成 spec.json 能用的值。

测量项:
  - 标题白底框的位置与尺寸 -> 顶部占比、宽度占比
  - 标题文字的高度 -> 字号推算
  - 英文/中文两行字幕的基线位置 -> 底距、中英字号比
  - 是否有底色块 / 描边
  - 主色(白字?黑字?描边色)

用法:
    python measure_layout.py --frames <帧目录> [--glob "*.png"]
"""
import argparse
import glob
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def bands(mask, min_len=4, gap=3):
    """把布尔数组切成连续段。"""
    out, start, miss = [], None, 0
    for i, v in enumerate(mask):
        if v:
            if start is None:
                start = i
            miss = 0
        else:
            if start is not None:
                miss += 1
                if miss > gap:
                    if i - miss - start + 1 >= min_len:
                        out.append((start, i - miss + 1))
                    start, miss = None, 0
    if start is not None:
        out.append((start, len(mask)))
    return out


def analyze(path):
    import numpy as np
    from PIL import Image

    im = Image.open(path).convert("RGB")
    a = np.asarray(im, dtype="float32")
    H, W = a.shape[:2]
    g = a.mean(axis=2)

    res = {"文件": os.path.basename(path), "尺寸": [W, H]}

    # --- 1) 找"近白底块":整行大部分像素接近纯白,且行高成片 ---
    white = (g > 235).mean(axis=1)
    runs = bands(white > 0.55, min_len=10)
    boxes = []
    for y0, y1 in runs:
        # 在该行区间里找白底的左右边界(取中间行的连续白段)
        mid = g[(y0 + y1) // 2]
        cols = bands(mid > 225, min_len=20)
        if not cols:
            continue
        x0, x1 = max(cols, key=lambda c: c[1] - c[0])
        # 白块内部的深色像素 = 文字
        sub = g[y0:y1, x0:x1]
        dark = sub < 120
        if dark.sum() < 20:
            continue
        rows = bands(dark.mean(axis=1) > 0.02, min_len=3)
        if not rows:
            continue
        ty0 = y0 + rows[0][0]
        ty1 = y0 + rows[-1][1]
        cols_d = bands(dark.mean(axis=0) > 0.02, min_len=3)
        tx0 = x0 + (cols_d[0][0] if cols_d else 0)
        tx1 = x0 + (cols_d[-1][1] if cols_d else (x1 - x0))
        boxes.append({
            "白底框": [int(x0), int(y0), int(x1), int(y1)],
            "框高": int(y1 - y0),
            "文字": [int(tx0), int(ty0), int(tx1), int(ty1)],
            "文字高": int(ty1 - ty0),
            "文字宽": int(tx1 - tx0),
            "顶边距": int(y0),
            "宽度占比": round((x1 - x0) / W, 3),
            "距顶占比": round(y0 / H, 4),
            "文字色": "深色(黑字白底)",
        })
    res["白底块"] = boxes

    # --- 2) 下方字幕:亮字(接近纯白)且无底色 ---
    bright = (g > 215)
    # 只看下半部分,避开画面主体的高光
    half = int(H * 0.58)
    sub = bright[half:, :]
    rows = bands(sub.mean(axis=1) > 0.012, min_len=6, gap=4)
    lines = []
    for y0, y1 in rows:
        seg = sub[y0:y1]
        colmask = seg.mean(axis=0) > 0.02
        cols = bands(colmask, min_len=3)
        if not cols:
            continue
        # 合并该行区间内所有词块,得到整行范围
        x0, x1 = cols[0][0], cols[-1][1]
        # 统计该行"是否几乎占满整行白" -> 判断是不是底色块
        fill = float(bright[half + y0:half + y1, :].mean())
        lines.append({
            "行范围绝对Y": [int(half + y0), int(half + y1)],
            "行高": int(y1 - y0),
            "横范围": [int(x0), int(x1)],
            "行宽": int(x1 - x0),
            "亮度填充率": round(fill, 3),
            "距底": int(H - (half + y1)),
        })
    res["下方亮字行"] = lines

    # --- 3) 整体:是否有描边(亮字周围是否普遍存在暗像素圈) ---
    if lines:
        y0, y1 = lines[0]["行范围绝对Y"]
        strip = g[max(0, y0 - 2):y1 + 2, :]
        res["字幕区暗像素占比"] = round(float((strip < 90).mean()), 3)
        res["字幕区亮像素占比"] = round(float((strip > 210).mean()), 3)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", required=True, help="帧所在目录")
    ap.add_argument("--glob", default="*.png")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.frames, args.glob)))
    if not files:
        print("没找到帧文件:%s/%s" % (args.frames, args.glob))
        return 2

    out = [analyze(f) for f in files]
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)

    for r in out:
        print("=" * 62)
        print("%s  %dx%d" % (r["文件"], r["尺寸"][0], r["尺寸"][1]))
        for b in r.get("白底块", []):
            print("  [白底框] x %d-%d  y %d-%d  框高 %d  距顶 %d (%.1f%%)"
                  % (b["白底框"][0], b["白底框"][2], b["白底框"][1],
                     b["白底框"][3], b["框高"], b["顶边距"],
                     b["距顶占比"] * 100))
            print("           文字高 %d  文字宽 %d  宽度占比 %.0f%%  %s"
                  % (b["文字高"], b["文字宽"], b["宽度占比"] * 100, b["文字色"]))
        for ln in r.get("下方亮字行", []):
            print("  [亮字行] y %d-%d  高 %d | x %d-%d  宽 %d | 距底 %d | 填充率 %.2f"
                  % (ln["行范围绝对Y"][0], ln["行范围绝对Y"][1], ln["行高"],
                     ln["横范围"][0], ln["横范围"][1], ln["行宽"],
                     ln["距底"], ln["亮度填充率"]))
        if "字幕区暗像素占比" in r:
            print("  [字幕区] 暗 %.2f  亮 %.2f"
                  % (r["字幕区暗像素占比"], r["字幕区亮像素占比"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
