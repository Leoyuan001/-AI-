#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_corpus.py — 把用户的英中对照字幕整理成「翻译对照语料」

用户的作品是歌词翻译:英译中。要学他的翻译习惯,必须先把
「英文原文 ↔ 他的中文译文」成对抽出来,变成可逐条比对、可统计的语料。

支持三种输入形态:
  A. 交错对照(本项目实际形态):奇数行英文、偶数行中文
  B. 双语 SRT:同一条字幕里第一行英文、第二行中文
  C. 制表符/竖线分隔:英文<TAB>中文 或 英文 | 中文

用法:
    python build_corpus.py --in <文件> [--out 目录] [--name 歌名]

产物:
    corpus.md    人读对照表(含统计摘要)
    corpus.json  机读明细,供后续规则归纳
"""
import argparse
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")


def is_english(line):
    """判英文行:拉丁字母占比高且几乎没有汉字。"""
    if not line.strip():
        return False
    lat = len(LATIN.findall(line))
    cjk = len(CJK.findall(line))
    letters = lat + cjk
    if letters == 0:
        return False
    return cjk == 0 and lat / letters > 0.5


def is_chinese(line):
    if not line.strip():
        return False
    cjk = len(CJK.findall(line))
    lat = len(LATIN.findall(line))
    return cjk > 0 and cjk >= lat


def parse_srt_strict(text):
    """双语 SRT:同一条里第一行英文、第二行中文。"""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    pairs = []
    for b in blocks:
        lines = [l.strip() for l in b.split("\n") if l.strip()]
        if not lines:
            continue
        body = [l for l in lines if "-->" not in l and not re.fullmatch(r"\d+", l)]
        if len(body) < 2:
            continue
        en = next((l for l in body if is_english(l)), None)
        zh = next((l for l in body if is_chinese(l)), None)
        if en and zh:
            pairs.append((en, zh))
    return pairs


def parse_interleaved(text):
    """交错形态:英文行后面跟着中文行。跳过 SRT 的时间码与序号。"""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        s = raw.strip()
        if not s or "-->" in s or re.fullmatch(r"\d+", s):
            continue
        lines.append(s)
    pairs, i = [], 0
    while i < len(lines):
        cur = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if is_english(cur) and nxt and is_chinese(nxt):
            pairs.append((cur, nxt))
            i += 2
        elif is_english(cur) and not (nxt and is_chinese(nxt)):
            # 英文行后面没有中文 -> 记录为「缺译文」
            pairs.append((cur, None))
            i += 1
        else:
            i += 1
    return pairs


def parse_separated(text):
    pairs = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if "\t" in raw:
            a, b = raw.split("\t", 1)
        elif "|" in raw:
            a, b = raw.split("|", 1)
        else:
            continue
        a, b = a.strip(), b.strip()
        if not a or not b:
            continue
        if is_english(a):
            pairs.append((a, b))
        elif is_english(b):
            pairs.append((b, a))
    return pairs


def detect_and_parse(text):
    if re.search(r"-->\s*\d", text):
        srt = parse_srt_strict(text)
        if len(srt) >= 3:
            return "双语SRT", srt
    sep = parse_separated(text)
    if len(sep) >= 3:
        return "分隔符", sep
    return "交错对照", parse_interleaved(text)


def zh_len(s):
    return len(CJK.findall(s or ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    if not os.path.isfile(args.inp):
        print("找不到文件:%s" % args.inp)
        return 2
    with open(args.inp, "r", encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read()

    kind, pairs = detect_and_parse(text)
    name = args.name or os.path.splitext(os.path.basename(args.inp))[0]
    outdir = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "profiles", "_语料")
    os.makedirs(outdir, exist_ok=True)

    good = [(a, b) for a, b in pairs if a and b]
    missing = [a for a, b in pairs if a and not b]

    # ---- 统计 ----
    en_words = [len(re.findall(r"[A-Za-z']+", a)) for a, _ in good]
    zh_chars = [zh_len(b) for _, b in good]
    ratio = [round(z / w, 2) for w, z in zip(en_words, zh_chars) if w]
    stats = {
        "解析形态": kind,
        "对照条数": len(good),
        "缺译文条数": len(missing),
        "英文词数中位": sorted(en_words)[len(en_words) // 2] if en_words else 0,
        "中文汉字数中位": sorted(zh_chars)[len(zh_chars) // 2] if zh_chars else 0,
        "汉字/英文词比中位": sorted(ratio)[len(ratio) // 2] if ratio else 0,
    }

    data = {"名称": name, "统计": stats,
            "对照": [{"英文": a, "中文": b, "英词数": len(re.findall(r"[A-Za-z']+", a)),
                    "中字数": zh_len(b)} for a, b in good],
            "缺译文": missing}
    with open(os.path.join(outdir, "%s.json" % name), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    L = ["# 翻译对照语料:%s" % name, "",
         "解析形态 **%s** | 有效对照 **%d** 条 | 缺译文 %d 条"
         % (kind, len(good), len(missing)), "",
         "| 项 | 值 |", "| --- | --- |",
         "| 英文词数中位 | %s |" % stats["英文词数中位"],
         "| 中文汉字数中位 | %s |" % stats["中文汉字数中位"],
         "| 汉字/英文词 比中位 | %s |" % stats["汉字/英文词比中位"], "",
         "> 汉字/英文词比是关键指标:英文 10 个词 -> 中文多少字。",
         "> 这个比值决定译文是「贴原文」还是「放开改写」,也决定字幕每屏字数。", "",
         "## 逐条对照", "",
         "| # | 英文原文 | 中文译文 | 词 | 字 | 比 |",
         "| --- | --- | --- | --- | --- | --- |"]
    for i, (a, b) in enumerate(good, 1):
        w = len(re.findall(r"[A-Za-z']+", a))
        z = zh_len(b)
        L.append("| %d | %s | %s | %d | %d | %s |"
                 % (i, a.replace("|", "\\|"), b.replace("|", "\\|"),
                    w, z, round(z / w, 2) if w else "—"))
    if missing:
        L += ["", "## 缺译文的英文行", ""]
        for m in missing:
            L.append("- %s" % m)

    mdp = os.path.join(outdir, "%s.md" % name)
    with open(mdp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    print("解析形态:%s" % kind)
    print("有效对照 %d 条 | 缺译文 %d 条" % (len(good), len(missing)))
    print("英文词数中位 %s | 中文汉字数中位 %s | 汉字/词比 %s"
          % (stats["英文词数中位"], stats["中文汉字数中位"],
             stats["汉字/英文词比中位"]))
    print("产物 -> %s" % mdp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
