# -*- coding: utf-8 -*-
"""
extract_subs.py — 从成品视频里抽出字幕区,拼成接触表供人工读取译文

用途:用户给的是成品(字幕已烧入),需要把字幕条抽出来逐条读。
按分辨率自适应:竖屏取底部条带,横屏取底部条带。

用法:
    python extract_subs.py --dir <视频目录> --out <输出目录> [--step 4]
"""
import argparse
import glob
import os
import re
import subprocess
import sys

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=CREATE_NO_WINDOW)
    return ((p.stdout or b"").decode("utf-8", "replace"),
            (p.stderr or b"").decode("utf-8", "replace"), p.returncode)


def probe(path):
    out, _, code = run([FFPROBE, "-v", "error", "-select_streams", "v",
                        "-show_entries", "stream=width,height",
                        "-show_entries", "format=duration",
                        "-of", "csv=p=0", path])
    nums = [x for x in out.replace("\n", ",").split(",") if x.strip()]
    try:
        if len(nums) >= 3:
            w, h, d = int(float(nums[0])), int(float(nums[1])), float(nums[2])
        elif len(nums) == 2:
            w, h = int(float(nums[0])), int(float(nums[1]))
            d = None
        else:
            return None
    except (ValueError, IndexError):
        return None
    return {"w": w, "h": h, "dur": d}


def safe(name):
    return re.sub(r'[\\/:*?"<>|() ]', "_", name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--step", type=float, default=4.0)
    ap.add_argument("--per-sheet", type=int, default=12)
    ap.add_argument("--only", default=None, help="只处理名字含该串的文件")
    args = ap.parse_args()

    files = []
    for ext in ("*.mp4", "*.mov", "*.mkv"):
        files += glob.glob(os.path.join(args.dir, ext))
    files = sorted(set(files))
    if args.only:
        files = [f for f in files if args.only in os.path.basename(f)]
    if not files:
        print("没找到视频")
        return 2

    os.makedirs(args.out, exist_ok=True)
    report = []
    for f in files:
        info = probe(f)
        if not info:
            print("跳过(读取失败):%s" % os.path.basename(f))
            continue
        w, h, dur = info["w"], info["h"], info["dur"] or 0
        tag = safe(os.path.splitext(os.path.basename(f))[0])
        vdir = os.path.join(args.out, tag)
        os.makedirs(vdir, exist_ok=True)

        # 字幕条带位置。注意:字幕块通常含「英文行 + 中文行」两层,
        # 中文在下、英文在上,所以条带要往上多取一些,否则英文行会被裁掉。
        if h > w:                      # 竖屏
            band_h = int(h * 0.24)
            y0 = h - int(h * 0.27)
        else:                          # 横屏
            band_h = int(h * 0.30)
            y0 = h - band_h - int(h * 0.02)
        y0 = max(0, y0)
        band_h = min(band_h, h - y0)

        # 抽帧
        times = []
        t = min(2.0, dur / 4)
        while t < dur - 0.5:
            times.append(round(t, 2))
            t += args.step
        if not times:
            times = [1.0]
        strips = []
        for i, tt in enumerate(times):
            png = os.path.join(vdir, "s_%03d.png" % i)
            _, err, code = run([FFMPEG, "-hide_banner", "-loglevel", "error",
                                "-y", "-ss", str(tt), "-i", f,
                                "-frames:v", "1",
                                "-vf", "crop=%d:%d:0:%d" % (w, band_h, y0),
                                png])
            if code == 0 and os.path.exists(png):
                strips.append((tt, png))
        if not strips:
            print("抽帧失败:%s" % os.path.basename(f))
            continue

        # 拼接触表
        sheets = []
        for si in range(0, len(strips), args.per_sheet):
            chunk = strips[si:si + args.per_sheet]
            if len(chunk) < 2:
                sheets.append((chunk[0][1], [chunk[0][0]]))
                continue
            inp = []
            for _, p in chunk:
                inp += ["-i", p]
            pairs = "".join("[%d:v]" % k for k in range(len(chunk)))
            fc = pairs + "vstack=inputs=%d" % len(chunk)
            sheet = os.path.join(vdir, "sheet_%02d.png" % (si // args.per_sheet + 1))
            _, err, code = run([FFMPEG, "-hide_banner", "-loglevel", "error",
                                "-y"] + inp + ["-filter_complex", fc, sheet])
            if code == 0 and os.path.exists(sheet):
                sheets.append((sheet, [c[0] for c in chunk]))
        print("%-22s %dx%d %.1fs  抽帧 %d  接触表 %d"
              % (os.path.basename(f), w, h, dur, len(strips), len(sheets)))
        report.append({"视频": os.path.basename(f), "尺寸": "%dx%d" % (w, h),
                       "时长": dur, "帧数": len(strips), "目录": vdir,
                       "接触表": [s[0] for s in sheets],
                       "时间点": [s[1] for s in sheets]})

    import json
    with open(os.path.join(args.out, "_index.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print("\n索引 -> %s" % os.path.join(args.out, "_index.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
