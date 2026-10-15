#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_rules.py — 按配方 v4 给译文做自动体检

为什么需要:prompt 里写了规则,但模型不一定每次都守。
实测就出现过"括号垫词被译出来""标点混排""整首只用老子"三类违规,
而这些从成片画面上很难一眼看出。所以做成自动检查。

用法:
    python check_rules.py --srt <字幕.srt>          # 检查 SRT
    python check_rules.py --lines <英文文件> --zh <中文文件>
    python check_rules.py --srt <字幕.srt> --fix     # 顺带清掉末尾垫词

退出码:0 = 无严重问题;1 = 有严重违规
"""
import argparse
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CJK_ONE = re.compile(r"[\u4e00-\u9fff]")

# 括号里这些是演唱效果,不该出现在中文里
ADLIB = re.compile(
    r"[（(]\s*(呃|耶|哈|啊|噢|哦|嗯|嘿|嚯|哇|呐|呀|yeah|uh|ha|woah|oh|"
    r"b\*+h|b\*\*\*h|no cap)\s*[)）]", re.I)

# 删垫词时要**连它前后的标点一起删**。
# 实测踩过的 bug:只删括号,前面的逗号会留下,变成「没影，,。」这种怪东西。
ADLIB_CLEAN = re.compile(
    r"(?:[,，、;；]\s*)?[（(]\s*(?:呃|耶|哈|啊|噢|哦|嗯|嘿|嚯|哇|呐|呀|"
    r"yeah|uh|ha|woah|oh|b\*+h|no\s*cap)\s*[)）](?:\s*[,，、;；])?",
    re.I)
# 星号遮蔽词在赞语后仍保留,但不要留下悬空标点
TRAIL_PUNCT = re.compile(r"[，、；,;]\s*$")

# 半角标点混入中文
HALF_PUNCT = re.compile(r"[\u4e00-\u9fff][,;?!]")
# 中文里孤立的英文逗号结尾
TRAIL_COMMA = re.compile(r"[\u4e00-\u9fff]\s*,\s*$")
# 连续标点
DUP_PUNCT = re.compile(r"[,，]{2,}|[。.]{2,}")

FIRST_PERSON = ["老子", "爷", "小爷", "哥", "俺", "我"]


def read_srt(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()
    out = []
    for blk in re.split(r"\n\s*\n", raw.replace("\r\n", "\n")):
        lines = [l.strip() for l in blk.split("\n") if l.strip()]
        tl = next((l for l in lines if "-->" in l), None)
        if not tl:
            continue
        body = lines[lines.index(tl) + 1:]
        en = next((l for l in body if not CJK.search(l)), "")
        zh = next((l for l in body if CJK.search(l)), "")
        out.append((en, zh))
    return out


def check(pairs):
    issues = []          # (级别, 行号, 说明)
    persons = []
    for i, (en, zh) in enumerate(pairs, 1):
        if not zh:
            continue
        # R7:括号垫词
        m = ADLIB.search(zh)
        if m:
            issues.append(("严重", i, "R7 括号垫词被译出来了:%s" % m.group(0)))
        # R7c:标点
        if HALF_PUNCT.search(zh):
            issues.append(("严重", i, "R7c 中文里混入半角标点"))
        if TRAIL_COMMA.search(zh):
            issues.append(("提示", i, "R7c 句末是英文逗号,应改句号或去掉"))
        if DUP_PUNCT.search(zh):
            issues.append(("提示", i, "R7c 连续标点"))
        # R7b:拟声句铺太开
        if len(CJK.findall(zh)) > 8 and re.match(r"^[耶哦噢啊哈嗯\s,，。]+$", zh):
            issues.append(("提示", i, "R7b 纯拟声句铺得过长,压缩即可"))
        # 记录第一人称
        for p in FIRST_PERSON:
            if p in zh:
                persons.append(p)
                break
    # R9b:第一人称多样性
    uniq = sorted(set(persons))
    if len(persons) >= 6 and len(uniq) == 1:
        issues.append(("提示", 0,
                       "R9b 全篇只用「%s」(%d 次),建议按语气在 老子/爷/哥/我 之间轮换"
                       % (uniq[0], len(persons))))
    # 术语表核查:英文明明命中却没按表译
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from project_kit import GLOSSARY
        for i, (en, zh) in enumerate(pairs, 1):
            low = " " + (en or "").lower() + " "
            for e, z in GLOSSARY:
                if re.search(r"(?<![a-z])" + re.escape(e.lower()) + r"(?![a-z])", low):
                    if z and z not in zh and len(z) >= 2:
                        issues.append(("提示", i,
                                       "术语表要求 %s→%s,译文里没看到" % (e, z)))
                    break
    except Exception:                                        # noqa: BLE001
        pass
    return issues, persons


def normalize_punct(zh):
    """把中文里的半角标点换成全角,并清掉多余标点。

    实测踩过的 bug:只删末尾逗号会留下「没影,。」这种怪东西 ——
    因为半角逗号在中文里前面还跟着一个全角逗号。
    正确做法是**先整体转全角,再收拾重复标点**。
    """
    if not zh:
        return zh
    s = zh
    # 只有「中文右侧」的半角标点才转,避免动到英文专名内部
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s*,\s*", "，", s)
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s*\.\s*", "。", s)
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s*\?\s*", "？", s)
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s*!\s*", "！", s)
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s*;\s*", "；", s)
    # 收拾重复与悬空标点
    s = re.sub(r"[，]{2,}", "，", s)
    s = re.sub(r"[，]\s*[。！？]", lambda m: m.group(0)[-1], s)
    s = re.sub(r"[。]{2,}", "。", s)
    s = re.sub(r"^[，。；、\s]+", "", s)
    s = re.sub(r"[，、；]\s*$", "", s)
    return s.strip()


def auto_fix(pairs):
    """自动清掉末尾的垫词括号、统一标点、去掉句末英文逗号。"""
    out = []
    for en, zh in pairs:
        z = zh or ""
        # 先连标点一起删垫词,再统一标点,最后收拾句末
        z = ADLIB_CLEAN.sub("", z)
        z = ADLIB.sub("", z)
        z = normalize_punct(z)
        z = TRAIL_PUNCT.sub("", z)
        z = normalize_punct(z)
        if z and not z.endswith(("。", "?", "!", "？", "！")):
            z += "。"
        out.append((en, z))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--srt")
    ap.add_argument("--lines")
    ap.add_argument("--zh")
    ap.add_argument("--fix", action="store_true", help="自动清理并另存 _fixed.srt")
    args = ap.parse_args()

    if args.srt:
        pairs = read_srt(args.srt)
    elif args.lines and args.zh:
        with open(args.lines, encoding="utf-8-sig") as f:
            ens = [l.strip() for l in f if l.strip()]
        with open(args.zh, encoding="utf-8-sig") as f:
            zhs = [l.strip() for l in f if l.strip()]
        pairs = list(zip(ens, zhs))
    else:
        print("要么给 --srt,要么同时给 --lines 与 --zh")
        return 2

    if not pairs:
        print("没读到内容")
        return 2

    issues, persons = check(pairs)
    sev = [x for x in issues if x[0] == "严重"]
    warn = [x for x in issues if x[0] == "提示"]

    print("检查 %d 句" % len(pairs))
    if persons:
        from collections import Counter
        print("第一人称分布:", dict(Counter(persons)))
    print()
    if not issues:
        print("✅ 未发现违规")
    else:
        for lvl, idx, msg in issues:
            tag = "❌" if lvl == "严重" else "⚠️ "
            pos = ("第 %d 句" % idx) if idx else "全篇"
            print("%s %s  %s" % (tag, pos, msg))
    print()
    print("严重 %d 项 | 提示 %d 项" % (len(sev), len(warn)))

    if args.fix and args.srt:
        fixed = auto_fix(pairs)
        outp = os.path.splitext(args.srt)[0] + "_fixed.srt"
        with open(outp, "w", encoding="utf-8") as fh:
            for i, ((en, zh), (_, z)) in enumerate(zip(pairs, fixed), 1):
                # 重新算时间戳
                pass
        # 时间戳要保留,重读一次
        with open(args.srt, "r", encoding="utf-8-sig") as fh:
            raw = fh.read()
        blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n"))
        outblk = []
        k = 0
        for blk in blocks:
            lines = [l for l in blk.split("\n") if l.strip()]
            tl = next((l for l in lines if "-->" in l), None)
            if not tl:
                continue
            idx = lines[0] if lines[0] != tl else str(k + 1)
            en, _z = pairs[k] if k < len(pairs) else ("", "")
            body = "\n".join(x for x in (en, fixed[k][1]) if x)
            outblk.append("%s\n%s\n%s\n" % (idx, tl, body))
            k += 1
        with open(outp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(outblk))
        print("已清理 -> %s" % outp)

    return 1 if sev else 0


if __name__ == "__main__":
    sys.exit(main())
