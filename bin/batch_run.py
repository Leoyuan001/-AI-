#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
batch_run.py — 往期作品集批量蒸馏跑批器

对 samples/ 下每条视频调用 probe_video.py 提取证据,并汇总成一张总表,
供 Agent 逐张读接触表、填《制作配方》。

用法:
    python batch_run.py                     # 跑 samples/ 全部
    python batch_run.py --samples D:\\片子   # 指定目录
    python batch_run.py --batch 2026-10-15  # 指定批次名
    python batch_run.py --force             # 已有产物也重跑

产物:
    distill/<批次>/_batch_summary.md   人读总表(核心产物)
    distill/<批次>/_batch_detail.json  机读明细
    distill/<批次>/<视频名>/           每条视频的完整证据包
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "probe_video.py")
SKILL_ROOT = os.path.dirname(HERE)
VIDEO_EXT = ("*.mp4", "*.mov", "*.mkv", "*.avi", "*.flv", "*.webm", "*.m4v", "*.ts")
SIDECAR_EXT = (".srt", ".txt", ".ass", ".vtt", ".json")

# Windows 控制台默认 GBK,子进程回传的 UTF-8 文本会让 print 抛 UnicodeEncodeError。
# 实测踩过:必须强制 UTF-8 且不可编码字符替换而非崩溃。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def find_videos(samples_dir):
    out = []
    for ext in VIDEO_EXT:
        out.extend(glob.glob(os.path.join(samples_dir, ext)))
        out.extend(glob.glob(os.path.join(samples_dir, ext.upper())))
    return sorted(set(os.path.abspath(p) for p in out))


def find_sidecars(samples_dir, video_path):
    """找同名旁车文件:台词稿 / 数据 / 笔记。"""
    stem = os.path.splitext(os.path.basename(video_path))[0]
    found = {}
    for ext in SIDECAR_EXT:
        p = os.path.join(samples_dir, stem + ext)
        if os.path.isfile(p):
            found[ext.lstrip(".")] = p
    return found


def run_probe(video, outdir, step, threshold, merge):
    cmd = [sys.executable, PROBE, video, "--out", outdir,
           "--step", str(step), "--threshold", str(threshold),
           "--merge", str(merge)]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    flags = 0x08000000 if os.name == "nt" else 0
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=flags, env=env)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace")


def read_sidecar_text(path):
    """读旁车文本,取前几行的有效内容做摘要。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    txt = raw.strip()
    return txt or None


def parse_metrics(text):
    """从 .txt 旁车里抓「键: 值」形式的后台数据。"""
    if not text:
        return {}
    keys = ("播放量", "播放", "5s完播率", "5秒完播率", "完播率", "点赞",
            "评论", "分享", "收藏", "涨粉", "文件名")
    out = {}
    for line in text.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        m = re.match(r"^([^:：]{1,12})[:：]\s*(.+)$", line)
        if m and m.group(1).strip() in keys:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def normalize_metrics(d):
    """把同义键统一,避免 total 表里漏读。"""
    alias = {"播放": "播放量", "5秒完播率": "5s完播率"}
    for a, b in alias.items():
        if a in d and b not in d:
            d[b] = d[a]
    return d


def fmt(v, suffix=""):
    if v is None:
        return "—"
    return "%s%s" % (v, suffix)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default=os.path.join(SKILL_ROOT, "samples"))
    ap.add_argument("--batch", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--step", type=float, default=1.0)
    ap.add_argument("--threshold", type=float, default=0.30)
    ap.add_argument("--merge", type=float, default=0.4)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.samples):
        print("样本目录不存在: %s" % args.samples)
        return 2
    videos = find_videos(args.samples)
    if not videos:
        print("样本目录里没有视频文件: %s" % args.samples)
        print("支持格式: %s" % ", ".join(e.strip("*") for e in VIDEO_EXT))
        return 3

    batch_dir = os.path.join(SKILL_ROOT, "distill", args.batch)
    os.makedirs(batch_dir, exist_ok=True)
    print("批次 %s | 视频 %d 条 | 输出 %s\n" % (args.batch, len(videos), batch_dir))

    detail = []
    for i, v in enumerate(videos, 1):
        name = os.path.splitext(os.path.basename(v))[0]
        safe = re.sub(r'[\\/:*?"<>|]', "_", name)
        outdir = os.path.join(batch_dir, safe)
        sidecars = find_sidecars(args.samples, v)
        print("[%d/%d] %s" % (i, len(videos), name))

        meta_path = os.path.join(outdir, "meta.json")
        if os.path.isfile(meta_path) and not args.force:
            print("       已有产物,跳过(用 --force 重跑)")
            code = 0
        else:
            os.makedirs(outdir, exist_ok=True)
            code, out = run_probe(v, outdir, args.step, args.threshold, args.merge)
            tail = [l for l in out.splitlines() if l.strip()][-1:] or [""]
            print("       probe 退出码 %s | %s" % (code, tail[0].strip()))

        rec = {"文件": os.path.basename(v), "路径": v, "目录": outdir,
               "probe退出码": code, "旁车": {k: os.path.basename(p)
                                         for k, p in sidecars.items()}}
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    m = json.load(fh)
                rec.update({
                    "时长秒": m.get("时长秒"),
                    "分辨率": m.get("分辨率"),
                    "画幅": m.get("画幅"),
                    "帧率": m.get("帧率"),
                    "镜头数": m.get("镜头数"),
                    "单镜时长": m.get("单镜时长"),
                    "场景检测": m.get("场景检测"),
                    "接触表数": len(m.get("接触表") or []),
                    "有音轨": m.get("有音轨"),
                })
            except (OSError, ValueError) as e:
                rec["读取meta失败"] = str(e)

        for kind, p in sidecars.items():
            txt = read_sidecar_text(p)
            if kind == "txt":
                rec["后台数据"] = normalize_metrics(parse_metrics(txt))
                rec["台词稿字数"] = len(txt.replace("\n", "")) if txt and not rec["后台数据"] else None
                rec["txt前80字"] = (txt or "")[:80].replace("\n", " / ")
            else:
                rec["%s字数" % kind] = len(txt or "")
        detail.append(rec)
        print("")

    # ---------- 汇总总表 ----------
    lines = ["# 往期作品集蒸馏总表 — 批次 %s" % args.batch, ""]
    lines.append("样本 %d 条 | 目录 `%s`" % (len(videos), args.samples))
    lines.append("")
    lines.append("## 一、样本清单与技术规格")
    lines.append("")
    lines.append("| # | 文件 | 时长s | 分辨率 | 画幅 | 帧率 | 镜头数 | 单镜中位s | 镜头原始→合并 | 旁车 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(detail, 1):
        sd = r.get("单镜时长") or {}
        sc = r.get("场景检测") or {}
        side = ", ".join(sorted(r.get("旁车", {}).keys())) or "**无**"
        lines.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s→%s | %s |" % (
            i, r["文件"], fmt(r.get("时长秒")), fmt(r.get("分辨率")),
            fmt(r.get("画幅")), fmt(r.get("帧率")), fmt(r.get("镜头数")),
            fmt(sd.get("中位")), fmt(sc.get("原始检出")), fmt(sc.get("合并后")), side))

    lines.append("")
    lines.append("## 二、后台数据(决定配方向谁对齐)")
    lines.append("")
    lines.append("| # | 文件 | 播放量 | 5s完播率 | 完播率 | 点赞 | 评论 | 分享 | 收藏 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    any_data = False
    for i, r in enumerate(detail, 1):
        d = r.get("后台数据") or {}
        if d:
            any_data = True
        g = lambda *ks: next((d[k] for k in ks if k in d), "—")
        lines.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            i, r["文件"], g("播放量", "播放"), g("5s完播率", "5秒完播率"),
            g("完播率"), g("点赞"), g("评论"), g("分享"), g("收藏")))
    if not any_data:
        lines.append("")
        lines.append("> ⚠️ **没有检测到任何后台数据。** 请在 `samples/` 下放同名 `.txt`,")
        lines.append("> 内容形如 `播放量: 12.3万` / `5s完播率: 42%` 逐行书写。")
        lines.append("> 没有数据时,配方只能对齐「你的习惯」,无法对齐「观众买账」——两者常不一致。")

    lines.append("")
    lines.append("## 三、Agent 下一步动作")
    lines.append("")
    lines.append("1. **逐张看接触表**(每条视频目录下的 `sheet_*.jpg`),判读节奏/字幕/包装。")
    lines.append("2. 若某条 `单镜中位s < 0.6` 且内容是慢节奏口播 → 上调 `--threshold` 重跑该条。")
    lines.append("3. 台词:有 `.srt` 的用 `subtitles.py --srt` 对齐到镜头;只有视频的用 `subtitles.py --auto`。")
    lines.append("4. 按 `profiles/_TEMPLATE.md` 填写,产出《制作配方 v1》。")
    lines.append("5. 样本 ≥3 条时,统计「风格锚点(出现率≥80%)」与「偶发元素」。")

    summary = os.path.join(batch_dir, "_batch_summary.md")
    with open(summary, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    with open(os.path.join(batch_dir, "_batch_detail.json"), "w", encoding="utf-8") as fh:
        json.dump(detail, fh, ensure_ascii=False, indent=2)

    print("=" * 56)
    print("总表 -> %s" % summary)
    print("明细 -> %s" % os.path.join(batch_dir, "_batch_detail.json"))
    miss = [r["文件"] for r in detail if not r.get("后台数据")]
    if miss:
        print("缺后台数据的样本 %d 条: %s" % (len(miss), ", ".join(miss[:4])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
