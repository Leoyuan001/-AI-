#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
project_kit.py — 把「用户的实测排版参数 + 翻译配方」固化成可复用的构建器

为什么要有这一层:
  Web 界面、命令行、批量脚本都需要「从底片+歌词生成 spec.json」这同一件事。
  逻辑写一份,谁都能调,避免三处各写一遍然后慢慢跑偏。

参数来源:对成品视频逐帧像素统计得到,方法与推导过程见 profiles/layout-spec.md
基准分辨率:720×1280。其他分辨率按比例缩放,保证视觉比例一致。
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # 项目根(含 profiles/、projects/)

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN = re.compile(r"[A-Za-z]")

# ---------------------------------------------------------------- 实测排版参数
# 全部实测自用户的成品,基准画面 720×1280
BASE_H = 1280
LAYOUT = {
    "标题顶部占比": 0.109,      # 白框顶边距 / 画面高
    "标题字号": 38,             # 基准高 1280 下的字号
    "标题块内边距": 13,
    "标题底色": "#FFFFFF",
    "标题字色": "#111111",
    "字幕底距": 192,            # 中文行底边距 / 基准高
    "英文字号": 34,
    "中文字号": 22,
    "描边宽": 3,
    "描边色": "#000000",
    "字色": "#FFFFFF",
    "层间距": 20,
    "行距": 1.15,
}
中文标题模板 = "如果{艺人}出生于{年代}s"


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=CREATE_NO_WINDOW)
    return ((p.stdout or b"").decode("utf-8", "replace"),
            (p.stderr or b"").decode("utf-8", "replace"), p.returncode)


def probe_media(path):
    """读视频的宽高与时长。"""
    out, err, code = run([FFPROBE, "-v", "error", "-select_streams", "v",
                          "-show_entries", "stream=width,height",
                          "-show_entries", "format=duration",
                          "-of", "csv=p=0", path])
    nums = [x for x in out.replace("\n", ",").split(",") if x.strip()]
    if len(nums) < 3:
        # 退化:分开再问一次
        o2, _, _ = run([FFPROBE, "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path])
        try:
            dur = float(o2.strip())
        except ValueError:
            dur = None
        o3, _, _ = run([FFPROBE, "-v", "error", "-select_streams", "v",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", path])
        n3 = o3.strip().split(",")
        if len(n3) >= 2:
            return {"w": int(float(n3[0])), "h": int(float(n3[1])), "dur": dur}
        return None
    try:
        return {"w": int(float(nums[0])), "h": int(float(nums[1])),
                "dur": float(nums[2])}
    except (ValueError, IndexError):
        return None


def has_audio(path):
    out, _, code = run([FFPROBE, "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=index", "-of", "csv=p=0", path])
    return code == 0 and bool(out.strip())


def make_title(artist, era):
    """按用户的固定公式生成标题。"""
    artist = (artist or "").strip()
    era = re.sub(r"[^0-9]", "", str(era or ""))
    if not artist or not era:
        return ""
    return 中文标题模板.format(艺人=artist, 年代=era)


def _s(v):
    return "" if v is None else str(v).strip()


def parse_lyrics_block(text):
    """把用户粘贴的歌词解析成 [(英文, 中文), ...]。

    支持的输入形态(自动识别):
      A. 只有英文 -> [(en, "")]
      B. 交错对照 -> 英文行 + 紧跟的中文行
      C. 分隔符   -> 英文<TAB>中文 或 英文|中文
      D. SRT      -> 每条字幕里取英文行与中文行
    """
    text = (text or "").replace("\r\n", "\n")
    # D. SRT
    if re.search(r"-->\s*\d", text):
        out = []
        for blk in re.split(r"\n\s*\n", text):
            body = [l.strip() for l in blk.split("\n")
                    if l.strip() and "-->" not in l
                    and not re.fullmatch(r"\d+", l.strip())]
            if not body:
                continue
            en = next((l for l in body if is_english(l)), "")
            zh = next((l for l in body if is_chinese(l)), "")
            out.append((en, zh))
        return [(a, b) for a, b in out if a or b]

    # C. 分隔符
    sep = []
    for line in text.split("\n"):
        if "\t" in line:
            a, b = line.split("\t", 1)
        elif "|" in line:
            a, b = line.split("|", 1)
        else:
            continue
        sep.append((a.strip(), b.strip()))
    if len(sep) >= 2:
        return sep

    # B. 交错 / A. 纯英文
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out, i = [], 0
    while i < len(lines):
        cur = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if is_english(cur) and nxt and is_chinese(nxt):
            out.append((cur, nxt))
            i += 2
        elif is_english(cur):
            out.append((cur, ""))
            i += 1
        else:
            i += 1
    return out


def is_english(s):
    """判断是否是「需要处理的英文行」。

    实测踩过的坑:把 `Yeah, yeah ohhhhh` 这类**纯拟声/呼喊行**判成了
    "不是英文"(旧实现在 cjk==0 时只看拉丁字母,但调用处另有更严的判断),
    结果整行被丢掉,字幕漏句。修正:**只要不含汉字且有拉丁字母就算英文行**,
    是否纯拟声留给翻译阶段处理(规则 R7b)。
    """
    if not s or not s.strip():
        return False
    if CJK.findall(s):
        return False
    return bool(LATIN.search(s))


def is_english_sentence(s):
    """更严格:含实义英文单词。用来区分「纯拟声行」。"""
    words = re.findall(r"[A-Za-z']{2,}", s or "")
    stop = {"the", "and", "oh", "yeah", "uh", "ha", "woah", "im", "it", "is"}
    return any(w.lower() not in stop for w in words)


def is_chinese(s):
    if not s or not s.strip():
        return False
    cjk, lat = len(CJK.findall(s)), len(LATIN.findall(s))
    return cjk > 0 and cjk >= lat


def allocate_timing(n, duration, start=0.3, tail=0.4, min_dur=1.2):
    """把 n 条字幕均匀铺在 duration 内,返回 [(起, 止), ...]。

    为什么是均分:实测用户的音频**全程无静音**(音乐人声连续),
    silencedetect 切不出句子边界,所以自动对齐做不到。
    均分是可用起点,用户在剪映里微调即可。
    """
    if n <= 0 or duration <= 0:
        return []
    usable = max(0.5, duration - start - tail)
    step = usable / n
    out = []
    for i in range(n):
        t0 = round(start + i * step, 3)
        t1 = round(start + (i + 1) * step - 0.05, 3)
        if t1 - t0 < min_dur:
            t1 = round(t0 + min_dur, 3)
        if t1 > duration:
            t1 = round(duration, 3)
        out.append((t0, max(t1, t0 + 0.1)))
    return out


def srt_ts(sec):
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms = 999
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def build_srt(pairs, times):
    """pairs=[(en,zh)] times=[(起,止)] -> SRT 文本。第一行英文、第二行中文。"""
    out = []
    for i, ((en, zh), (t0, t1)) in enumerate(zip(pairs, times), 1):
        body = "\n".join(x for x in (_s(en), _s(zh)) if x)
        out.append("%d\n%s --> %s\n%s\n" % (i, srt_ts(t0), srt_ts(t1), body))
    return "\n".join(out)


# 这些键是「比例/倍数」,缩放分辨率时不能跟着变。
# 实测踩过:行距 1.15 被当像素值取整成 1,行距就丢了。
RATIO_KEYS = {"标题顶部占比", "行距"}


def scaled_layout(out_h):
    """把基准排版按输出高度等比缩放。

    只有「像素量」需要按 k 缩放(字号、底距、内边距、描边宽、层间距);
    比例类(顶部占比、行距)保持原值。
    """
    k = float(out_h) / BASE_H
    L = {}
    for key, val in LAYOUT.items():
        if key in RATIO_KEYS or not isinstance(val, (int, float)) \
                or isinstance(val, bool):
            L[key] = val
        else:
            L[key] = max(1, int(round(val * k)))
    return L


def build_spec(project_name, video_rel, out_w, out_h, fps, srt_rel,
               title, duration, layout=None):
    """生成渲染用的 spec 字典。layout 可覆盖默认值。"""
    L = dict(scaled_layout(out_h))
    if layout:
        L.update(layout)
    spec = {
        "项目名": project_name,
        "输出": {"分辨率": "%dx%d" % (out_w, out_h),
                 "帧率": int(round(fps or 30)),
                 "音频码率": "128k"},
        "字幕文件": srt_rel,
        "标题": title,
        "标题样式": {
            "字体": "msyhbd.ttc", "字号": L["标题字号"],
            "颜色": L["标题字色"], "描边宽": 0, "背景": L["标题底色"],
            "位置": "top", "顶部占比": L["标题顶部占比"],
            "块内边距": L["标题块内边距"], "行距": 1.0, "层间距": 0,
            "持续": None,
        },
        "字幕样式": {
            "default": {
                "中文字体": "msyhbd.ttc", "英文字体": "msyhbd.ttc",
                "中文字号": L["中文字号"], "英文字号": L["英文字号"],
                "颜色": L["字色"], "描边色": L["描边色"],
                "描边宽": L["描边宽"], "背景": None,
                "位置": "bottom", "底距": L["字幕底距"],
                "行距": L["行距"], "层间距": L["层间距"],
            }
        },
        "镜头": [{"素材": video_rel, "入": 0, "出": round(duration, 3),
                 "适配": "cover"}],
        "音频": {"人声": {"音量": 1.0}, "响度归一": {"启用": False}},
    }
    return spec


# ---------------------------------------------------------------- 术语表
# 不再写死在代码里 —— 从 profiles/glossary.txt 读取。
# 这样工具对任何语言/领域都通用,词库是使用者自己的资产。
GLOSSARY_FILE = os.path.join(os.path.dirname(HERE), "profiles", "glossary.txt")

# 内置兜底:只放最通用的词,证明格式。真正的词库请放 glossary.txt。
BUILTIN_GLOSSARY = [
    ("money", "票子"), ("friend", "兄弟"),
]

_glossary_cache = None


def load_glossary():
    """读取 glossary.txt。格式:英文=中文,一行一条,# 为注释。"""
    global _glossary_cache
    if _glossary_cache is not None:
        return _glossary_cache
    items = list(BUILTIN_GLOSSARY)
    if os.path.isfile(GLOSSARY_FILE):
        try:
            with open(GLOSSARY_FILE, "r", encoding="utf-8-sig") as fh:
                for line in fh:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    en, zh = s.split("=", 1)
                    en, zh = en.strip(), zh.strip()
                    if en and zh:
                        items.append((en, zh))
        except OSError:
            pass
    # 长词优先,避免 "check" 先命中 "a cool rack"
    items.sort(key=lambda x: -len(x[0]))
    _glossary_cache = items
    return items


# 兼容旧引用:GLOSSARY 仍是列表,只是内容来自文件
GLOSSARY = load_glossary()


def glossary_hint(en_line):
    """在术语表里找该行命中的词条,返回提示列表。"""
    low = (en_line or "").lower()
    hits = []
    for en, zh in load_glossary():
        e = en.lower()
        # 词边界匹配,避免 "check" 命中 "checking"
        if re.search(r"(?<![a-z])" + re.escape(e) + r"(?![a-z])", low):
            hits.append("%s → %s" % (en, zh))
    return hits


if __name__ == "__main__":
    # 自检
    print("基准排版(1280 高):", json.dumps(scaled_layout(1280), ensure_ascii=False))
    print("缩放到 1024 高:", json.dumps(scaled_layout(1024), ensure_ascii=False))
    print()
    print("标题公式示例:", make_title("Artist", "1950"))
    print("术语表条数:", len(load_glossary()),
          "(来源:%s)" % ("profiles/glossary.txt" if os.path.isfile(GLOSSARY_FILE)
                       else "内置兜底"))
    print()
    demo = ("Some line of english lyrics\n某句中文译文\n"
            "Another line here\n另一句中文")
    p = parse_lyrics_block(demo)
    print("解析交错对照:%d 条" % len(p))
    for a, b in p:
        print("   %s -> %s" % (a[:40], b[:20]))
    print("时间轴(34.3s):", allocate_timing(len(p), 34.3))
    print("术语命中示例:", glossary_hint("I got money and weed"))
