#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
llm_translate.py — 用 DeepSeek API 按用户风格翻译歌词

设计要点:
  1. **风格来自配方,不是靠模型自由发挥**。R1–R14 与 40 条术语表全部写进
     system prompt,模型只是在执行规则。
  2. **术语表命中结果也塞进 prompt**,让模型必须用指定译法。
  3. **语料召回优先**:如果这句用户自己译过,直接用他的,不问模型。
  4. **可对照评测**:带 --eval 就能拿语料当考题,量出准确率。

密钥读取顺序:
  环境变量 DEEPSEEK_API_KEY  ->  video-skill/.deepseek_key 文件

用法:
    python llm_translate.py --lines "<英文歌词文件>"
    python llm_translate.py --eval            # 用已有语料评测
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
KEY_FILE = os.path.join(ROOT, ".deepseek_key")

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def load_key():
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if k:
        return k
    if os.path.isfile(KEY_FILE):
        with open(KEY_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return ""


# ---------------------------------------------------------------- 风格提示词
# 风格规则不写死在代码里 —— 从 profiles/style_rules.txt 读取。
# 本文件只提供「机制」,风格是使用者的资产。
RULES_FILE = os.path.join(ROOT, "profiles", "style_rules.txt")

# 没有规则文件时的兜底:只讲通用要求,不假设任何个人风格
DEFAULT_RULES = """你是歌词英译中译者。把英文歌词译成中文,遵守以下要求:

1. 一句英文对应一句中文,不合并不拆分。
2. 用中文全角标点,句末用句号。
3. 括号里的和声/垫词(如 (Yeah) (Uh) (Ha))属于演唱效果,**中文直接省略**。
4. 星号遮蔽词(f**k、b**th)保持原样,不补全、不译字面。
5. 专有人名保留英文原样;品牌可音译或保留,前后一致即可。
6. 译文长度约为英文词数的 1.5–2 倍,不要为凑字数增删内容。
7. 只输出译文,不要解释、不要加引号。

（提示:把本文件换成你自己的 profiles/style_rules.txt,译文会更贴近你的风格。
  参考 profiles/style_rules.template.txt。）"""


def build_system_prompt():
    """把风格规则 + 术语表拼成 system prompt。"""
    if os.path.isfile(RULES_FILE):
        try:
            with open(RULES_FILE, "r", encoding="utf-8-sig") as fh:
                rules = fh.read().strip()
        except OSError:
            rules = DEFAULT_RULES
        src = "profiles/style_rules.txt"
    else:
        rules = DEFAULT_RULES
        src = "内置默认(未发现 style_rules.txt)"

    # 术语表:从 glossary.txt 读,塞进 prompt 强制模型采用
    try:
        import project_kit as K
        terms = K.load_glossary()
    except Exception:                                        # noqa: BLE001
        terms = []
    gl = ""
    if terms:
        gl = ("\n\n# 术语表(命中**必须**用右列译法,不得自创)\n"
              + " | ".join("%s=%s" % (a, b) for a, b in terms))

    head = ("你是歌词翻译器。严格遵守下面的风格规格 —— 这不是建议,是硬约束。\n"
            "规则来源:%s\n\n" % src)
    return head + rules + gl



def call_api(messages, model=DEFAULT_MODEL, key=None, temperature=0.3,
             max_tokens=4000, retries=2):
    """调 DeepSeek chat/completions。返回 (文本, 错误, 用量)。

    ⚠️ 实测要点:deepseek 的模型**都是推理模型**,回复里带 reasoning_content,
    推理过程也会消耗 completion tokens。
    如果 max_tokens 给太小(试过 10),预算全被推理吃掉,content 就是空字符串 ——
    看起来像"调用失败",其实是被截断了。所以:
      - max_tokens 要给足
      - 若 content 为空但 reasoning_content 有内容,说明是被截断,要重试并加倍预算
    """
    key = key or load_key()
    if not key:
        return None, "没有 API key", None
    used_tokens = max_tokens
    last_err = None
    for attempt in range(retries + 1):
        body = json.dumps({
            "model": model, "messages": messages,
            "temperature": temperature, "max_tokens": used_tokens,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            API_BASE + "/chat/completions", data=body,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                j = json.loads(r.read().decode("utf-8"))
            ch = j["choices"][0]
            msg = ch.get("message") or {}
            txt = (msg.get("content") or "").strip()
            reason = msg.get("reasoning_content") or ""
            usage = j.get("usage") or {}
            if not txt and reason:
                # 被推理吃光预算 -> 加倍再来
                last_err = "content 为空(推理占满预算),已提高到 %d tokens 重试" % (used_tokens * 2)
                used_tokens = min(used_tokens * 2, 16000)
                if attempt < retries:
                    continue
            return (txt or None), (None if txt else last_err), usage
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            last_err = "HTTP %s: %s" % (e.code, detail)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        except Exception as e:                              # noqa: BLE001
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
    return None, last_err, None


def _numbered(block):
    return "\n".join("%d. %s" % (i, s) for i, s in enumerate(block, 1))


def _parse_numbered(text, n):
    """从模型回复里按序号取回 n 条译文。"""
    out = [None] * n
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*[.、)]\s*(.+?)\s*$", line)
        if not m:
            continue
        i = int(m.group(1)) - 1
        if 0 <= i < n:
            out[i] = m.group(2).strip().strip('"').strip("「」")
    # 模型偶尔不编号:退回按行对齐
    if all(x is None for x in out):
        lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
        for i in range(min(n, len(lines))):
            out[i] = re.sub(r"^\s*\d+\s*[.、)]\s*", "", lines[i])
    return out


def translate_lines(pairs, model=DEFAULT_MODEL, key=None, batch=10,
                    use_corpus=True, progress=None):
    """把 [(英文, 已有中文)] 里缺中文的补齐。

    优先级:① 已有中文不动 ② 语料命中直接用 ③ 剩下的交给模型(分批)
    返回 (结果列表, 统计)
    """
    import translate_assist as TA
    n = len(pairs)
    res = [{"en": (a or "").strip(), "zh": (b or "").strip(), "来源": "已有"}
           for a, b in pairs]
    stat = {"已有": 0, "语料命中": 0, "模型翻译": 0, "失败": 0, "用量": {}}

    todo = []
    for i, r in enumerate(res):
        if r["zh"]:
            stat["已有"] += 1
            continue
        if use_corpus:
            hit = TA.corpus_lookup(r["en"])
            if hit:
                r["zh"] = hit
                r["来源"] = "语料命中"
                stat["语料命中"] += 1
                continue
        todo.append(i)

    if not todo:
        return res, stat

    for s in range(0, len(todo), batch):
        idxs = todo[s:s + batch]
        block = [res[i]["en"] for i in idxs]
        # 把术语命中塞进 prompt,强制模型用指定译法
        hints = []
        for i in idxs:
            hs = TA.term_hits(res[i]["en"])
            if hs:
                hints.append("%s  (必须用:%s)"
                             % (res[i]["en"], "; ".join("%s=%s" % h for h in hs)))
        user = ("请把下面英文歌词逐句译成中文,严格按规格执行。\n"
                "保持编号,每行一句,只输出编号和译文。\n\n"
                + _numbered(block))
        if hints:
            user += "\n\n已为你查好的术语命中(必须采用):\n" + "\n".join(hints)
        txt, err, usage = call_api(
            [{"role": "system", "content": build_system_prompt()},
             {"role": "user", "content": user}],
            model=model, key=key,
            # 推理模型会先"想"一遍,预算要给足:每句约 400 tokens
            max_tokens=max(2000, 400 * len(block)))
        if err:
            for i in idxs:
                res[i]["来源"] = "失败:" + err[:60]
            stat["失败"] += len(idxs)
            if progress:
                progress("  批次失败:%s" % err[:120])
            continue
        outs = _parse_numbered(txt, len(block))
        for i, o in zip(idxs, outs):
            if o:
                res[i]["zh"] = o
                res[i]["来源"] = "模型翻译"
                stat["模型翻译"] += 1
            else:
                res[i]["来源"] = "模型未返回"
                stat["失败"] += 1
        if usage:
            for k, v in usage.items():
                if isinstance(v, int):
                    stat["用量"][k] = stat["用量"].get(k, 0) + v
        if progress:
            progress("  已译 %d/%d" % (min(s + batch, len(todo)), len(todo)))
    return res, stat


# ---------------------------------------------------------------- 评测
def evaluate(limit=20, model=DEFAULT_MODEL, key=None):
    """拿用户语料当考题:隐藏中文,让模型译,再和他的真实译文比。"""
    import translate_assist as TA
    idx = TA.load_corpus()
    pairs = []
    for k, zhs in idx.items():
        if len(k.split()) >= 4 and zhs:
            pairs.append((k, zhs[0]))
    pairs = pairs[:limit]
    if not pairs:
        print("语料为空,无法评测")
        return
    print("评测 %d 句(模型:%s)" % (len(pairs), model))
    print("=" * 70)
    res, stat = translate_lines([(a, "") for a, _ in pairs],
                                model=model, key=key, use_corpus=False)
    same = 0
    for i, (en, gold) in enumerate(pairs):
        got = res[i]["zh"] or "(空)"
        print("\n[%d] %s" % (i + 1, en))
        print("    你的原文: %s" % gold)
        print("    模型译文: %s" % got)
        if got.strip() == gold.strip():
            same += 1
    print()
    print("=" * 70)
    print("完全一致 %d/%d(%.0f%%)" % (same, len(pairs), same / len(pairs) * 100))
    print("用量:", stat.get("用量"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", help="英文歌词文件(一行一句)")
    ap.add_argument("--out", help="输出 json")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--eval", action="store_true", help="用语料评测")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--check", action="store_true", help="只检查 key 与连通性")
    args = ap.parse_args()

    key = load_key()
    if not key:
        print("没有 API key。请设置环境变量 DEEPSEEK_API_KEY,"
              "或把 key 写入 %s" % KEY_FILE)
        return 2

    if args.check:
        txt, err, usage = call_api([{"role": "user", "content": "回复:ok"}],
                                   model=args.model, key=key, max_tokens=10)
        print("模型 %s -> %s" % (args.model, txt if txt else ("失败:" + str(err))))
        return 0 if txt else 1

    if args.eval:
        evaluate(limit=args.limit, model=args.model, key=key)
        return 0

    if not args.lines:
        print("要么给 --lines,要么用 --eval")
        return 2
    with open(args.lines, "r", encoding="utf-8-sig") as fh:
        ens = [l.strip() for l in fh if l.strip()]
    print("待译 %d 句,模型 %s" % (len(ens), args.model))
    res, stat = translate_lines([(e, "") for e in ens], model=args.model,
                                key=key, batch=args.batch,
                                progress=lambda m: print(m))
    print()
    for r in res:
        print("%-52s | %s   [%s]" % (r["en"][:52], r["zh"] or "(空)", r["来源"]))
    print()
    print("统计:", json.dumps(stat, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print("已写 ->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
