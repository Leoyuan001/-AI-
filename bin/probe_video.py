#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
probe_video.py — 单条视频的「结构化证据」提取器
只负责客观事实提取,不做任何风格判断。产物供人或 Agent 判读。

用法:
    python probe_video.py <视频文件> [--out 输出目录] [--step 1.0]

产物:
    meta.json         元数据 + 场景切点 + 时间轴
    frames/t_XXXX.jpg 全时间轴抽帧
    sheet_01.jpg...   接触表(缩略图拼图,便于快速目视拉片)
    audio/audio.wav   16k 单声道音频(供转写)
    audio/loudness.txt 响度分析(检测爆音/音量落差)
    report.md         人类可读摘要
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Windows 控制台默认 GBK。实测:输出含中文路径时会抛 UnicodeEncodeError 直接中断。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def run(cmd, capture=True):
    """执行命令。失败返回 (None, 错误信息),不抛异常。"""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            creationflags=CREATE_NO_WINDOW,
        )
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return out, err
    except FileNotFoundError:
        return None, "命令不存在: %s" % cmd[0]


def probe_meta(path):
    """用 ffprobe 读容器与流信息。"""
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    out, err = run(cmd)
    if out is None:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def pick_video_stream(info):
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return None


def pick_audio_stream(info):
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    return None


def parse_fps(stream):
    """把 '30000/1001' 之类的帧率串解析成浮点数。"""
    if not stream:
        return None
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if raw and raw not in ("0/0", "N/A"):
            if "/" in raw:
                num, den = raw.split("/", 1)
                try:
                    num, den = float(num), float(den)
                    if den:
                        return round(num / den, 3)
                except ValueError:
                    pass
            else:
                try:
                    return round(float(raw), 3)
                except ValueError:
                    pass
    return None


def detect_scenes(path, threshold=0.30, merge=0.4):
    """用 ffmpeg 的 scdet 滤镜检测镜头切换点。

    实测教训:scdet 在真实视频上会对同一次切换报多个相邻时间戳
    (频闪、运镜、转场动画都会触发),所以必须做「最小间隔合并」,
    否则镜头数会虚高数倍。merge 表示小于该间隔的切点视为同一次切换。
    返回 (合并后切点, 合并前原始切点数)。
    """
    # scdet 把检测结果打到日志,格式形如: lavfi.scd.score / lavfi.scd.time
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-i", path,
           "-filter:v", "scdet=threshold=%s" % threshold,
           "-f", "null", "-"]
    _, err = run(cmd)
    if err is None:
        return None, 0
    raw = []
    for line in err.splitlines():
        if "lavfi.scd.time" in line:
            m = re.search(r"lavfi\.scd\.time:\s*([0-9.]+)", line)
            if m:
                raw.append(round(float(m.group(1)), 3))
    raw = sorted(set(raw))
    merged = []
    last = None
    for c in raw:
        if last is None or (c - last) >= merge:
            merged.append(c)
            last = c
    return merged, len(raw)


def extract_frames(path, outdir, step, duration):
    """按固定间隔抽帧,输出目录 + 帧列表。"""
    frames_dir = os.path.join(outdir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
           "-vf", "fps=1/%s,scale=480:-2" % step,
           os.path.join(frames_dir, "t_%05d.jpg")]
    _, err = run(cmd)
    files = sorted(os.listdir(frames_dir)) if os.path.isdir(frames_dir) else []
    stamps = []
    for i, name in enumerate(files):
        t = round(i * float(step), 2)
        # 末帧可能超出时长,做一次保护
        if duration and t > duration:
            t = round(duration, 2)
        stamps.append({"file": os.path.join("frames", name), "t": t})
    return stamps


def build_sheets(outdir, stamps, per_sheet=16, cols=4):
    """把抽出的帧拼成接触表,便于一次性目视浏览。"""
    if not stamps:
        return []
    frames_dir = os.path.join(outdir, "frames")
    sheets = []
    for si in range(0, len(stamps), per_sheet):
        chunk = stamps[si:si + per_sheet]
        idx = si // per_sheet + 1
        sheet = os.path.join(outdir, "sheet_%02d.jpg" % idx)
        # 用 concat 解复用器把图片串起来再 tile,避免依赖 glob 顺序
        listfile = os.path.join(outdir, "_sheet_%02d.txt" % idx)
        with open(listfile, "w", encoding="utf-8") as fh:
            for s in chunk:
                p = os.path.join(frames_dir, os.path.basename(s["file"]))
                fh.write("file '%s'\n" % p.replace("\\", "/").replace("'", "'\\''"))
        rows = (len(chunk) + cols - 1) // cols
        vf = "scale=360:-2,tile=%dx%d" % (cols, rows)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", listfile,
               "-vf", vf, "-frames:v", "1", sheet]
        _, err = run(cmd)
        if err and not os.path.exists(sheet):
            # 回退:逐帧 image2 输入
            for s in chunk:
                pass
        if os.path.exists(sheet):
            sheets.append({"file": os.path.basename(sheet),
                           "range": [chunk[0]["t"], chunk[-1]["t"]]})
        try:
            os.remove(listfile)
        except OSError:
            pass
    return sheets


def extract_audio(path, outdir, has_audio):
    """导出 16k 单声道 wav,并做响度分析。"""
    if not has_audio:
        return None, None
    adir = os.path.join(outdir, "audio")
    os.makedirs(adir, exist_ok=True)
    wav = os.path.join(adir, "audio.wav")
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])
    loud = os.path.join(adir, "loudness.txt")
    _, err = run([FFMPEG, "-hide_banner", "-nostats", "-i", path,
                  "-af", "loudnorm=print_format=summary", "-f", "null", "-"])
    if err:
        with open(loud, "w", encoding="utf-8") as fh:
            fh.write(err)
    return (wav if os.path.exists(wav) else None,
            loud if os.path.exists(loud) else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", default=None)
    ap.add_argument("--step", type=float, default=1.0,
                    help="抽帧间隔秒数,默认 1.0")
    ap.add_argument("--threshold", type=float, default=0.30,
                    help="场景检测灵敏度 0-1,越小越敏感,默认 0.30")
    ap.add_argument("--merge", type=float, default=0.4,
                    help="小于该间隔(秒)的切点合并为同一次切换,默认 0.4")
    args = ap.parse_args()

    if not os.path.isfile(args.video):
        print("找不到视频文件: %s" % args.video)
        return 2

    name = os.path.splitext(os.path.basename(args.video))[0]
    outdir = args.out or os.path.join("video-skill", "distill", name)
    os.makedirs(outdir, exist_ok=True)

    print("[1/6] 读取元数据 ...")
    info = probe_meta(args.video)
    if not info:
        print("ffprobe 读取失败,确认 ffprobe 在 PATH 中")
        return 3
    v = pick_video_stream(info)
    a = pick_audio_stream(info)
    fmt = info.get("format", {})
    duration = None
    try:
        duration = round(float(fmt.get("duration")), 3)
    except (TypeError, ValueError):
        if v and v.get("duration"):
            duration = round(float(v["duration"]), 3)
    fps = parse_fps(v)
    width = v.get("width") if v else None
    height = v.get("height") if v else None
    orientation = None
    if width and height:
        if height > width * 1.05:
            orientation = "竖屏 9:16 类"
        elif width > height * 1.05:
            orientation = "横屏 16:9 类"
        else:
            orientation = "方形 1:1 类"

    meta = {
        "源文件": os.path.abspath(args.video),
        "时长秒": duration,
        "分辨率": "%sx%s" % (width, height) if width else None,
        "画幅": orientation,
        "帧率": fps,
        "视频编码": (v or {}).get("codec_name"),
        "像素格式": (v or {}).get("pix_fmt"),
        "码率_kbps": round(int(fmt.get("bit_rate", 0)) / 1000) if fmt.get("bit_rate") else None,
        "音频编码": (a or {}).get("codec_name"),
        "音频采样率": (a or {}).get("sample_rate"),
        "音频声道": (a or {}).get("channels"),
        "有音轨": bool(a),
    }

    print("[2/6] 场景检测(分镜切点) ...")
    cuts, raw_count = detect_scenes(args.video, args.threshold, args.merge)
    cuts = cuts or []
    meta["场景切点秒"] = cuts
    meta["场景检测"] = {"阈值": args.threshold, "合并间隔秒": args.merge,
                    "原始检出": raw_count, "合并后": len(cuts)}
    # 由切点推导逐镜时长
    shots = []
    if cuts:
        bounds = [0.0] + cuts + ([duration] if duration else [])
        for i in range(len(bounds) - 1):
            s, e = bounds[i], bounds[i + 1]
            if e - s < 0.05:
                continue
            shots.append({"镜号": len(shots) + 1,
                          "起": round(s, 3), "止": round(e, 3),
                          "时长": round(e - s, 3)})
    meta["镜头数"] = len(shots)
    meta["镜头表"] = shots
    if shots:
        durs = [s["时长"] for s in shots]
        meta["单镜时长"] = {
            "最短": min(durs), "最长": max(durs),
            "中位": sorted(durs)[len(durs) // 2],
            "均值": round(sum(durs) / len(durs), 3),
        }

    print("[3/6] 全时间轴抽帧(每 %ss 一帧) ..." % args.step)
    stamps = extract_frames(args.video, outdir, args.step, duration)

    print("[4/6] 生成接触表 ...")
    sheets = build_sheets(outdir, stamps)

    print("[5/6] 导出音频 ...")
    wav, loud = extract_audio(args.video, outdir, bool(a))

    print("[6/6] 写报告 ...")
    meta["抽帧"] = stamps
    meta["接触表"] = sheets
    meta["音频文件"] = os.path.relpath(wav, outdir) if wav else None
    with open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    lines = ["# 蒸馏证据包:%s" % name, ""]
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    for k in ("时长秒", "分辨率", "画幅", "帧率", "码率_kbps",
              "视频编码", "音频编码", "有音轨", "镜头数"):
        lines.append("| %s | %s |" % (k, meta.get(k)))
    if meta.get("单镜时长"):
        sd = meta["单镜时长"]
        lines.append("| 单镜时长 | 最短 %ss / 中位 %ss / 最长 %ss |"
                     % (sd["最短"], sd["中位"], sd["最长"]))
    sc = meta.get("场景检测", {})
    if sc:
        lines.append("| 场景检测 | 阈值 %s / 合并间隔 %ss / 原始 %s 刀 → 合并 %s 刀 |"
                     % (sc.get("阈值"), sc.get("合并间隔秒"),
                        sc.get("原始检出"), sc.get("合并后")))
    lines.append("")
    lines.append("> 判读提醒:原始检出数远高于合并后,说明视频含大量频闪/运镜/转场动画。")
    lines.append("> 若合并后单镜中位时长 < 0.6s,先确认内容本身是不是快切卡点向;")
    lines.append("> 若画面是慢节奏口播,则说明阈值偏低,需上调 --threshold。")
    lines.append("")
    lines.append("## 镜头表(前 40 镜)")
    lines.append("")
    lines.append("| 镜号 | 起 | 止 | 时长 |")
    lines.append("| --- | --- | --- | --- |")
    for s in shots[:40]:
        lines.append("| %s | %s | %s | %s |" % (s["镜号"], s["起"], s["止"], s["时长"]))
    if len(shots) > 40:
        lines.append("")
        lines.append("(其余 %d 镜见 meta.json)" % (len(shots) - 40))
    lines.append("")
    lines.append("## 接触表")
    for sh in sheets:
        lines.append("- `%s`  覆盖 %s–%ss" % (sh["file"], sh["range"][0], sh["range"][1]))
    with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print("")
    print("完成 -> %s" % os.path.abspath(outdir))
    print("  镜头数 %s | 时长 %ss | 接触表 %s 张"
          % (meta["镜头数"], meta["时长秒"], len(sheets)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
