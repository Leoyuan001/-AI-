#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
subtitles.py — 台词证据提取与镜头对齐(本地 ASR 的替代方案)

本机装不了 whisper,所以分两条路拿到「台词结构」:

  模式 A(--srt 文件):解析剪映/抖音导出的 SRT/ASS/VTT,拿到逐句台词原文。
  模式 B(--auto):     用 ffmpeg silencedetect 切出语音段与停顿段。
                      拿不到「说了什么」,但拿得到「什么时候在说、说多久、哪里停」——
                      这已足够蒸馏节奏、语速、卡点与信息密度。

产物:
    lines.md    逐句表 + 对齐到镜头 + 语速/停顿统计(核心产物)
    lines.json  机读明细

用法:
    python subtitles.py --video <视频> --srt <字幕> [--out 目录]
    python subtitles.py --video <视频> --auto [--out 目录]
"""
import argparse
import json
import os
import re
import subprocess
import sys

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Windows 控制台默认 GBK,中文输出会抛 UnicodeEncodeError 直接中断。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=CREATE_NO_WINDOW)
    return ((p.stdout or b"").decode("utf-8", "replace"),
            (p.stderr or b"").decode("utf-8", "replace"))


def probe_duration(video):
    out, _ = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                  "-of", "csv=p=0", video])
    try:
        return round(float(out.strip()), 3)
    except ValueError:
        return None


def probe_fps(video):
    out, _ = run([FFPROBE, "-v", "error", "-select_streams", "v",
                  "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", video])
    raw = out.strip().splitlines()[0] if out.strip() else ""
    if "/" in raw:
        n, d = raw.split("/", 1)
        try:
            return round(float(n) / float(d), 3) if float(d) else None
        except ValueError:
            return None
    try:
        return round(float(raw), 3)
    except ValueError:
        return None


def text_len(s):
    """按「字数」计量:中文字符各算 1,连续拉丁词算 1。"""
    if not s:
        return 0
    cjk = len(CJK.findall(s))
    latin = len(re.findall(r"[A-Za-z]+", s))
    digits = len(re.findall(r"\d+", s))
    return cjk + latin + digits


def ts_to_sec(ts):
    """支持 00:00:01,200 / 00:00:01.200 / 0:00:01.2"""
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        return round(parts[0] * 3600 + parts[1] * 60 + parts[2], 3)
    if len(parts) == 2:
        return round(parts[0] * 60 + parts[1], 3)
    if len(parts) == 1:
        return round(parts[0], 3)
    return None


def parse_srt(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n"))
    out = []
    for b in blocks:
        bl = [l for l in b.split("\n") if l.strip()]
        if not bl:
            continue
        tline = next((l for l in bl if "-->" in l), None)
        if not tline:
            continue
        m = re.match(r"\s*([\d:.,]+)\s*-->\s*([\d:.,]+)", tline)
        if not m:
            continue
        s, e = ts_to_sec(m.group(1)), ts_to_sec(m.group(2))
        if s is None or e is None:
            continue
        idx = bl.index(tline)
        text = " ".join(bl[idx + 1:]).strip()
        text = re.sub(r"<[^>]+>", "", text)          # 去样式标签
        text = re.sub(r"\{\\[^}]*\}", "", text)      # 去 ASS 覆盖码
        out.append({"起": s, "止": e, "时长": round(e - s, 3), "文本": text})
    return sorted(out, key=lambda x: x["起"])


def parse_ass(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()
    out = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:"):].split(",", 9)
        if len(fields) < 10:
            continue
        s, e, text = ts_to_sec(fields[1]), ts_to_sec(fields[2]), fields[9]
        if s is None or e is None:
            continue
        text = re.sub(r"\{[^}]*\}", "", text).replace("\\N", " ").strip()
        out.append({"起": s, "止": e, "时长": round(e - s, 3), "文本": text})
    return sorted(out, key=lambda x: x["起"])


def parse_vtt(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()
    raw = re.sub(r"^WEBVTT.*?\n\n", "", raw, flags=re.S)
    return parse_srt_text(raw)


def parse_srt_text(raw):
    tmp_blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n"))
    out = []
    for b in tmp_blocks:
        bl = [l for l in b.split("\n") if l.strip()]
        tline = next((l for l in bl if "-->" in l), None)
        if not tline:
            continue
        m = re.match(r"\s*([\d:.,]+)\s*-->\s*([\d:.,]+)", tline)
        if not m:
            continue
        s, e = ts_to_sec(m.group(1)), ts_to_sec(m.group(2))
        if s is None or e is None:
            continue
        text = " ".join(bl[bl.index(tline) + 1:]).strip()
        out.append({"起": s, "止": e, "时长": round(e - s, 3), "文本": text})
    return sorted(out, key=lambda x: x["起"])


def parse_plain_text(path, duration, fps):
    """纯文案:没有时间戳。按字数占比把总时长分配下去,标注为「估算」。"""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()
    # 按句号/问号/感叹号/换行断句
    segs = [s.strip() for s in re.split(r"[。！？!?\n]+", raw) if s.strip()]
    if not segs:
        return [], True
    lens = [max(text_len(s), 1) for s in segs]
    total = sum(lens)
    out, t = [], 0.0
    for s, L in zip(segs, lens):
        d = (L / total) * (duration or 0)
        out.append({"起": round(t, 3), "止": round(t + d, 3),
                    "时长": round(d, 3), "文本": s})
        t += d
    return out, True


METRIC_KEY = re.compile(
    r"^\s*(文件名|播放量?|5s完播率|5秒完播率|完播率|点赞|评论|分享|收藏|涨粉)"
    r"\s*[:：]\s*\S+")


def looks_like_metrics(path):
    """判断一个 .txt 是不是「后台数据」而不是「台词稿」。

    实测踩过的坑:后台数据 txt 与视频同名,被 --auto 当成台词稿,
    于是「播放量: 128万」变成了一句台词并被安上假时间戳。
    这类"看起来像真数据"的错误最危险,必须拦住。
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return False
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return False
    hits = sum(1 for l in lines if METRIC_KEY.match(l))
    # 多数行都是「键: 值」数据格式 -> 判为数据文件
    return hits >= max(2, len(lines) // 2)


def find_script_sidecar(video_path):
    """按优先级找台词稿旁车。返回 (路径, 说明) 或 (None, None)。"""
    stem = os.path.splitext(os.path.basename(video_path))[0]
    d = os.path.dirname(os.path.abspath(video_path))
    order = [(stem + ".srt", "srt 字幕"),
             (stem + ".ass", "ass 字幕"),
             (stem + ".vtt", "vtt 字幕")]
    for fn, desc in order:
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p, desc
    # 纯文案类:显式命名优先,避免与后台数据 txt 冲突
    for suffix in (".台词", ".文案", ".脚本", ".script"):
        p = os.path.join(d, stem + suffix + ".txt")
        if os.path.isfile(p):
            return p, "纯文案(无时间戳)"
    p = os.path.join(d, stem + ".txt")
    if os.path.isfile(p):
        if looks_like_metrics(p):
            return None, "检测到同名 .txt 是后台数据,已忽略作为台词稿"
        return p, "纯文案(无时间戳)"
    return None, None


def detect_speech(video, noise_db=-32, min_sil=0.35):
    """用 silencedetect 切出语音段与停顿段。返回 (语音段, 停顿段)。"""
    out, err = run([FFMPEG, "-hide_banner", "-nostats", "-i", video,
                    "-af", "silencedetect=noise=%ddB:d=%s" % (noise_db, min_sil),
                    "-f", "null", "-"])
    log = err
    sil_starts, sil_ends = [], []
    for line in log.splitlines():
        m = re.search(r"silence_start:\s*(-?[0-9.]+)", line)
        if m:
            sil_starts.append(float(m.group(1)))
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m:
            sil_ends.append(float(m.group(1)))
    sil = []
    for i, s in enumerate(sil_starts):
        e = sil_ends[i] if i < len(sil_ends) else None
        if e is not None and e > s:
            sil.append((round(s, 3), round(e, 3)))
    return sil


def speech_segments(silences, duration):
    """把静音段反过来就是语音段。"""
    segs, cur = [], 0.0
    for s, e in silences:
        if s - cur > 0.10:
            segs.append((round(cur, 3), round(s, 3)))
        cur = max(cur, e)
    if duration and duration - cur > 0.10:
        segs.append((round(cur, 3), round(duration, 3)))
    return segs


def align_to_shots(lines, shots):
    """给每句台词标注落在哪个镜头,并统计每镜台词字数。"""
    for ln in lines:
        mid = (ln["起"] + ln["止"]) / 2.0
        hit = None
        for sh in shots:
            if sh["起"] <= mid < sh["止"]:
                hit = sh["镜号"]
                break
        if hit is None and shots:
            hit = shots[-1]["镜号"] if mid >= shots[-1]["止"] else shots[0]["镜号"]
        ln["镜号"] = hit
    per_shot = {}
    for ln in lines:
        k = ln.get("镜号")
        if k is None:
            continue
        per_shot[k] = per_shot.get(k, 0) + text_len(ln.get("文本", ""))
    return per_shot


def speech_reliability(stats):
    """判断 silencedetect 的结论是否可信,返回 (等级, 提示语)。

    实测:纯音乐/MV 类内容全程有声,静音几乎为零,会把「语音占比」顶到 95%+。
    这时停顿结构毫无意义,必须明确告知,否则会误导判读。
    """
    ratio = stats.get("语音占全片比")
    long_sil = stats.get("最长停顿秒") or 0
    seg = stats.get("语音段中位秒") or 0
    if ratio is None:
        return "未知", "未测到音轨,语音结构不可用。"
    if ratio >= 0.92 and long_sil < 1.0:
        return "低", ("语音占比 %.0f%% 且最长停顿仅 %.2fs —— 几乎全程有声。"
                    "这通常说明内容是**音乐/MV 类**,或音频底噪很高。"
                    "此时「停顿结构」不可信,请改用画面节奏(镜头表)作为主依据。"
                    % (ratio * 100, long_sil))
    if seg and seg < 0.25:
        return "中", ("语音段中位仅 %.2fs,疑似逐帧切片或强底噪,"
                    "停顿切分可能过碎,建议人工抽听确认。" % seg)
    return "高", "停顿结构可用,可作为语速与卡点判读依据。"


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--srt", help="字幕或文案文件(.srt/.ass/.vtt/.txt)")
    ap.add_argument("--auto", action="store_true",
                    help="自动查找旁车字幕/文案(会跳过后台数据 txt)")
    ap.add_argument("--meta", help="probe_video.py 产出的 meta.json(用于镜头对齐)")
    ap.add_argument("--out")
    ap.add_argument("--noise-db", type=int, default=-32)
    args = ap.parse_args()

    if not os.path.isfile(args.video):
        print("找不到视频: %s" % args.video)
        return 2
    name = os.path.splitext(os.path.basename(args.video))[0]
    outdir = args.out or os.path.join("video-skill", "distill", name)
    os.makedirs(outdir, exist_ok=True)

    duration = probe_duration(args.video)
    fps = probe_fps(args.video)

    # ---- 镜头表(可选,来自 probe_video.py)----
    shots = []
    meta_path = args.meta or os.path.join(outdir, "meta.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                m = json.load(fh)
            shots = m.get("镜头表") or []
            if not shots and m.get("场景切点秒") is not None:
                b = [0.0] + list(m.get("场景切点秒") or [])
                if m.get("时长秒"):
                    b.append(m["时长秒"])
                for i in range(len(b) - 1):
                    shots.append({"镜号": len(shots) + 1, "起": b[i],
                                  "止": b[i + 1], "时长": round(b[i + 1] - b[i], 3)})
        except (OSError, ValueError):
            shots = []

    result = {"视频": os.path.abspath(args.video), "时长秒": duration, "帧率": fps,
              "镜头数": len(shots) or None}
    lines, estimated = [], False
    source = None
    source_kind = None

    # ---- 台词来源 ----
    notes = []
    if args.srt and os.path.isfile(args.srt):
        ext = os.path.splitext(args.srt)[1].lower()
        if ext == ".ass":
            lines = parse_ass(args.srt)
        elif ext == ".vtt":
            lines = parse_vtt(args.srt)
        elif ext == ".txt":
            if looks_like_metrics(args.srt):
                notes.append("指定的 .txt 看起来是后台数据而非台词稿,已拒绝使用。")
            else:
                lines, estimated = parse_plain_text(args.srt, duration, fps)
        else:
            lines = parse_srt(args.srt)
        source = os.path.basename(args.srt)
        source_kind = ext.lstrip(".")

    if not lines and args.auto:
        found, desc = find_script_sidecar(args.video)
        if found:
            ext = os.path.splitext(found)[1].lower()
            if ext == ".ass":
                lines = parse_ass(found)
            elif ext == ".vtt":
                lines = parse_vtt(found)
            elif ext == ".txt":
                lines, estimated = parse_plain_text(found, duration, fps)
            else:
                lines = parse_srt(found)
            source = os.path.basename(found)
            source_kind = desc
            if desc and "无时间戳" in desc:
                notes.append("台词时间戳是按字数估算的,不是真实对齐 —— "
                             "只可用于大致节奏判读,不可当作精确时间轴。")
        elif desc:
            notes.append(desc + "。请把台词稿命名为 `<视频名>.台词.txt` 或导出 SRT。")

    # ---- 语音段(总是做,它不依赖台词文本)----
    sil = detect_speech(args.video, args.noise_db)
    speech = speech_segments(sil, duration)
    result["静音段"] = [{"起": s, "止": e, "时长": round(e - s, 3)} for s, e in sil]
    result["语音段"] = [{"起": s, "止": e, "时长": round(e - s, 3)} for s, e in speech]
    sp_durs = [round(e - s, 3) for s, e in speech]
    sil_durs = [round(e - s, 3) for s, e in sil]
    result["语音统计"] = {
        "段数": len(speech),
        "总语音秒": round(sum(sp_durs), 2),
        "总静音秒": round(sum(sil_durs), 2),
        "最长停顿秒": max(sil_durs) if sil_durs else 0,
        "停顿中位秒": median(sil_durs),
        "语音段中位秒": median(sp_durs),
        "语音占全片比": round(sum(sp_durs) / duration, 3) if duration else None,
    }

    if lines:
        for ln in lines:
            ln["字数"] = text_len(ln.get("文本", ""))
            ln["字每秒"] = round(ln["字数"] / ln["时长"], 2) if ln["时长"] else None
        per_shot = align_to_shots(lines, shots)
        gaps = [round(lines[i]["起"] - lines[i - 1]["止"], 3)
                for i in range(1, len(lines))]
        result["台词"] = lines
        result["台词统计"] = {
            "句数": len(lines),
            "总字数": sum(l["字数"] for l in lines),
            "句长中位字": median([l["字数"] for l in lines]),
            "句时长中位秒": median([l["时长"] for l in lines]),
            "字每秒中位": median([l["字每秒"] for l in lines if l["字每秒"]]),
            "估算语速字每分": (round(sum(l["字数"] for l in lines) /
                                max(sum(l["时长"] for l in lines), 0.01) * 60)),
            "句间空档中位秒": median(gaps),
            "时间戳是估算": estimated,
            "来源": source,
            "来源类型": source_kind,
        }
        result["每镜字数"] = per_shot
    else:
        result["台词统计"] = {"来源": None,
                          "说明": "未提供台词稿;仅输出语音段结构"}

    # 前 3 秒钩子(如果有台词)
    hook = [l for l in lines if l["起"] < 3.0]
    result["前3秒"] = {
        "台词": " ".join(l["文本"] for l in hook) or None,
        "字数": sum(l["字数"] for l in hook) or None,
        "该段是否有语音": any(s < 3.0 for s, _ in speech),
    }

    with open(os.path.join(outdir, "lines.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    # ---------- 报告 ----------
    L = ["# 台词与节奏证据:%s" % name, ""]
    if notes:
        for n in notes:
            L += ["> ⚠️ %s" % n, ""]
    st = result["语音统计"]
    rel_level, rel_note = speech_reliability(st)
    result["语音结构可信度"] = {"等级": rel_level, "说明": rel_note}
    L += ["## 一、语音结构(silencedetect 实测)", "",
          "> **可信度:%s** — %s" % (rel_level, rel_note), "",
          "| 项 | 值 |", "| --- | --- |",
          "| 语音段数 | %s |" % st["段数"],
          "| 总语音 | %ss / 全片 %ss |" % (st["总语音秒"], duration),
          "| 总静音 | %ss |" % st["总静音秒"],
          "| 语音占比 | %s |" % (("%.1f%%" % (st["语音占全片比"] * 100))
                              if st["语音占全片比"] else "—"),
          "| 语音段中位时长 | %ss |" % st["语音段中位秒"],
          "| 停顿中位时长 | %ss |" % st["停顿中位秒"],
          "| 最长停顿 | %ss |" % st["最长停顿秒"],
          "| 前3秒是否有语音 | %s |" % ("是" if result["前3秒"]["该段是否有语音"] else "**否**"),
          ""]
    L += ["### 前 10 个停顿(卡点/转场常落在这里)", "",
          "| # | 开始 | 时长 |", "| --- | --- | --- |"]
    for i, s in enumerate(result["静音段"][:10], 1):
        L.append("| %d | %ss | %ss |" % (i, s["起"], s["时长"]))

    if lines:
        ts = result["台词统计"]
        L += ["", "## 二、台词统计", "", "| 项 | 值 |", "| --- | --- |",
              "| 句数 | %s |" % ts["句数"],
              "| 总字数 | %s |" % ts["总字数"],
              "| 单句字数中位 | %s |" % ts["句长中位字"],
              "| 单句时长中位 | %ss |" % ts["句时长中位秒"],
              "| 语速中位 | %s 字/秒 |" % ts["字每秒中位"],
              "| 推算语速 | %s 字/分钟 |" % ts["估算语速字每分"],
              "| 句间空档中位 | %ss |" % ts["句间空档中位秒"],
              "| 时间戳 | %s |" % ("**按字数估算,非真实对齐**"
                                if ts["时间戳是估算"] else "真实时间戳"),
              "| 来源 | %s |" % ts["来源"], ""]
        L += ["### 前 3 秒钩子", "",
              "- 台词:%s" % (result["前3秒"]["台词"] or "—"),
              "- 字数:%s" % (result["前3秒"]["字数"] or "—"), ""]
        if shots:
            keep = min(60, len(shots))
            L += ["### 台词对齐到镜头(每镜字数,前 %d 镜)" % keep, "",
                  "| 镜号 | 字数 | 镜号 | 字数 |", "| --- | --- | --- | --- |"]
            keys = sorted(result["每镜字数"].keys())[:keep]
            half = (len(keys) + 1) // 2
            for i in range(half):
                a = keys[i]
                b = keys[i + half] if i + half < len(keys) else None
                L.append("| %s | %s | %s | %s |" % (
                    a, result["每镜字数"][a],
                    b if b is not None else "",
                    result["每镜字数"][b] if b is not None else ""))
            empty = [k for k in range(1, keep + 1) if k not in result["每镜字数"]]
            if empty:
                L += ["", "无台词镜头(%d 个):%s" % (
                    len(empty), ", ".join(str(e) for e in empty[:40])
                    + (" ..." if len(empty) > 40 else ""))]
            L.append("")
        L += ["### 逐句台词(前 30 句)", "",
              "| # | 起 | 止 | 时长 | 字数 | 字/秒 | 镜号 | 文本 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for i, l in enumerate(lines[:30], 1):
            L.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (
                i, l["起"], l["止"], l["时长"], l["字数"], l["字每秒"],
                l.get("镜号", "—"), (l["文本"] or "")[:40]))
    else:
        L += ["", "## 二、台词", "",
              "**未拿到台词文本。** 请提供 SRT/ASS/VTT 字幕,或同名 `.txt` 口播文案。",
              "当前仅凭语音段结构仍可蒸馏:语速节奏、停顿位置、卡点时机、信息密度。",
              "但拿不到「说了什么」,钩子句式与文案结构无法蒸馏。"]

    with open(os.path.join(outdir, "lines.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    print("完成 -> %s" % os.path.join(os.path.abspath(outdir), "lines.md"))
    print("  语音段 %s | 静音段 %s | 台词句数 %s"
          % (st["段数"], len(result["静音段"]), len(lines) or 0))
    for n in notes:
        print("  ⚠️ %s" % n)
    if not lines:
        print("  提示:未提供台词稿,仅输出语音结构。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
