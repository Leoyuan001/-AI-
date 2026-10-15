#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
translate_assist.py — 翻译辅助引擎(确定性部分)

**重要前提:本模块没有翻译能力。**
  它在无 LLM 的情况下工作,只做确定性的事;接上 LLM 后由 llm_translate.py 接管。

本模块只做三件**确定性**的事,做完之后把不确定的交给人:

  1. **语料召回**:如果这句在你的语料库里出现过,直接复用你本人的译文。
     这是质量最高的一路 —— 是你自己写的,不是猜的。
  2. **术语表替换**:命中 profiles/glossary.txt 的词条按表替换。
  3. **规则标记**:标出这句适用哪些规则(对仗 / 雅化 / 第一人称 /
     重复句 / 歧义词),以及**没有翻译能力的部分**。

明确**不做**的事:
  - 不凭空生成中文句子(会产出看不懂的垃圾)
  - 不自动处理重复句(需要换意象/换句式,是创作判断)
  - 不翻译未入表的俚语(歧义太大,选错整句就错)

调用方(网页/Agent)必须把 `需要人工` 为真的句子交给用户或 Agent 处理。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORPUS_DIR = os.path.join(ROOT, "profiles", "_语料")

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN = re.compile(r"[A-Za-z]")

# 第一人称词表(R11)。命中时提示,但不自动替换 —— 语气选择要看上下文。
FIRST_PERSON = {
    "i": "我", "i'm": "我", "im": "我", "me": "我", "my": "我的",
    "we": "我们", "we're": "我们", "we'll": "我们", "our": "我们的",
    "i've": "我", "i'll": "我", "i'd": "我", "us": "我们",
}

# 会改动语气、需要人工判断的信号词。
# 这里只放**通用的**类型提示,不放任何具体译法 ——
# 具体怎么译由 profiles/glossary.txt 与 style_rules.txt 决定。
HARD_WORDS = {
    "fuck": "脏话:按你的规则去脏化或弱化",
    "fxxk": "星号遮蔽词:保持原样,不补全",
    "f**k": "星号遮蔽词:保持原样,不补全",
    "b**th": "星号遮蔽词:保持原样,不补全",
    "bitch": "脏话/称呼语:需按语境与你的规则处理",
    "ball": "歧义词:球 / 得意 / 性,看语境选",
    "g": "歧义词:gangster(人)或 grand(千元),看语境选",
    "stick": "歧义词:钱 / 枪 / 枝条,看语境选",
}

_corpus_cache = None


# ---------------------------------------------------------------- 语料
def _norm_en(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def load_corpus():
    """加载所有语料 json,建立 归一化英文 -> [中文] 的索引。"""
    global _corpus_cache
    if _corpus_cache is not None:
        return _corpus_cache
    idx = {}
    if os.path.isdir(CORPUS_DIR):
        for fn in os.listdir(CORPUS_DIR):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            try:
                with open(os.path.join(CORPUS_DIR, fn), "r",
                          encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            for c in (data.get("对照") or []):
                en, zh = c.get("英文"), c.get("中文")
                if not en or not zh:
                    continue
                k = _norm_en(en)
                if k:
                    idx.setdefault(k, []).append(zh)
    _corpus_cache = idx
    return idx


def corpus_lookup(en_line):
    """在用户自己的语料里找这句 —— 命中就用他本人的译文。"""
    idx = load_corpus()
    k = _norm_en(en_line)
    if not k:
        return None
    if k in idx:
        return idx[k][0]
    # 去掉括号垫词再试一次(垫词常导致对不上)
    k2 = _norm_en(re.sub(r"\([^)]*\)", " ", en_line))
    if k2 and k2 in idx:
        return idx[k2][0]
    return None


# ---------------------------------------------------------------- 术语
def term_hits(en_line):
    """返回该行命中的术语(按长度降序,长词优先)。"""
    from project_kit import GLOSSARY
    low = " " + (en_line or "").lower() + " "
    hits = []
    for en, zh in sorted(GLOSSARY, key=lambda x: -len(x[0])):
        e = en.lower()
        if re.search(r"(?<![a-z])" + re.escape(e) + r"(?![a-z])", low):
            hits.append((en, zh))
    return hits


def glossary_prefill(en_line):
    """用术语表把英文里的词条替换成中文,得到一个**骨架**。

    返回 (骨架文本, 命中数, 覆盖度)
    覆盖度 = 被替换的实词数 / 实词总数。低于 0.5 说明骨架没意义。
    """
    hits = term_hits(en_line)
    out = en_line or ""
    for en, zh in hits:
        out = re.sub(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])",
                     zh, out, flags=re.I)
    # 覆盖度:统计还有多少英文实词没被替换(短虚词不计)
    stop = {"a", "an", "the", "and", "or", "but", "to", "of", "in", "on",
            "at", "for", "with", "my", "your", "his", "her", "its", "it",
            "is", "are", "was", "were", "be", "been", "am", "so", "that",
            "this", "i", "we", "you", "he", "she", "they", "me", "us"}
    words = [w for w in re.findall(r"[A-Za-z']+", out.lower()) if w not in stop]
    total = len(words) + len(hits)
    cov = (len(hits) / total) if total else 0.0
    return out, len(hits), round(cov, 2)


# ---------------------------------------------------------------- 主入口
def analyze(lines, english_only_hint=True):
    """对每句英文给出:可用译文(若有)、骨架、规则提示、是否需要人工。

    lines: [{"en":..., "zh":...}, ...]
    返回同长度的分析结果列表。
    """
    seen = {}
    out = []
    for i, item in enumerate(lines or []):
        en = (item.get("en") or "").strip()
        zh = (item.get("zh") or "").strip()
        rec = {"en": en, "zh": zh, "来源": "用户已填" if zh else "空",
               "规则": [], "提示": [], "需要人工": False}

        if not en:
            out.append(rec)
            continue

        if zh:
            out.append(rec)          # 用户已填,不动
            continue

        # ① 语料召回(最高优先:用户自己的译文)
        hit = corpus_lookup(en)
        if hit:
            rec["zh"] = hit
            rec["来源"] = "语料库命中(你以前的译文)"
            rec["需要人工"] = False
            out.append(rec)
            continue

        # ② 术语表骨架
        skel, nhit, cov = glossary_prefill(en)
        rec["术语"] = [("%s → %s" % (a, b)) for a, b in term_hits(en)]
        if nhit and cov >= 0.5:
            rec["zh"] = skel
            rec["来源"] = "术语表骨架(需润色)"
            rec["需要人工"] = True
        else:
            rec["来源"] = "需要翻译"
            rec["需要人工"] = True

        # ③ 规则标记
        rules = []
        low = " " + en.lower() + " "
        if re.search(r",\s*(never|but|and)\b", low) or " from " in low and " to " in low:
            rules.append("R1 对仗句式")
        if len(re.findall(r"[A-Za-z']+", en)) >= 6:
            rules.append("R8 雅化句式(动词用成语)")
        if any(w in low for w in (" i ", " i'm", " my ", " we ", " me ")):
            rules.append("R11 第一人称(狠话→老子/炫富→爷/抒情→我)")
        if re.search(r"\([^)]*\)", en):
            rules.append("R7 括号垫词:英文保留、中文省掉")
        if re.search(r"\*", en):
            rules.append("R12 星号遮蔽保持原样")
        for w, note in HARD_WORDS.items():
            if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
                rules.append(note)
        rec["规则"] = rules

        # ④ 重复句检测(需要换说法)
        key = _norm_en(en)
        if key in seen:
            rec["提示"].append("重复句(第 %d 句已出现)→ R9 必须换说法" % seen[key])
        else:
            seen[key] = i + 1

        out.append(rec)
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    demo = [
        {"en": "Addicted to codeine, where the lean at?", "zh": ""},
        {"en": "Got Rick Owen jeans, cost a cool rack", "zh": ""},
        {"en": "I was broke as hell, sleepin' on the mattress", "zh": ""},
        {"en": "Chase a check, never chase a bitch", "zh": ""},
        {"en": "whole lotta", "zh": ""},
    ]
    for r in analyze(demo):
        print("英文:", r["en"])
        print("  来源:", r["来源"])
        print("  中文:", repr(r["zh"]))
        if r.get("术语"):
            print("  术语:", " / ".join(r["术语"]))
        if r["规则"]:
            print("  规则:", " ; ".join(r["规则"]))
        if r["提示"]:
            print("  提示:", " ; ".join(r["提示"]))
        print()
