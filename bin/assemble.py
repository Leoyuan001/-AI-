#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
assemble.py — 把「装配规格 spec.json」渲染成成片 mp4

设计要点:配方(profile)产出的是**参数**,渲染器吃的是**镜头列表**。
中间隔一层稳定契约(spec),配方字段怎么改都不必重写渲染器。

用法:
    python assemble.py --spec <spec.json> [--out 成片.mp4]
    python assemble.py --spec <spec.json> --dry-run     # 只打印命令,不渲染
    python assemble.py --spec <spec.json> --check       # 只校验素材与字体

spec.json 结构(字段可省略,默认见下):
{
  "项目名": "demo",
  "输出": {"分辨率": "1080x1920", "帧率": 30, "码率": "8M", "音频码率": "192k"},
  "镜头": [
    {"素材": "raw/a.mp4", "入": 0, "出": 3.0,
     "适配": "cover",            // cover 裁切填满 / contain 留黑边
     "字幕": "第一句台词", "字幕样式": "default"}
  ],
  "字幕样式": {
     "default": {"字体": "msyhbd.ttc", "字号": 64, "颜色": "#FFFFFF",
                 "描边色": "#000000", "描边宽": 6,
                 "位置": "bottom", "底距": 220, "每行字数": 14,
                 "背景": "#00000099", "行距": 1.25}
  },
  "音频": {
     "人声": {"音量": 1.0},
     "背景音乐": {"文件": "raw/bgm.mp3", "音量": 0.18, "循环": true,
                  "闪避": {"启用": true, "强度": 6}},
     "响度归一": {"启用": true, "目标LUFS": -14}
  },
  "转场": {"类型": "cut"}      // cut 目前支持;xfade 需逐镜标注
}

产物:
    out/<项目名>.mp4        成片
    out/<项目名>.srt        字幕文件(即使已压入画面也另存一份)
    out/<项目名>.spec.json  实际使用的规格(存档,便于复现)
    out/_cmd.log            实际执行的 ffmpeg 命令(便于排查)
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
FONT_DIRS = [r"C:\Windows\Fonts", "/usr/share/fonts", "/System/Library/Fonts"]
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_RE = re.compile(r"[A-Za-z]")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_STYLE = {
    "字体": "msyhbd.ttc", "字号": 64, "颜色": "#FFFFFF",
    "描边色": "#000000", "描边宽": 6,
    "位置": "bottom", "底距": 220, "每行字数": 14,
    "背景": None, "行距": 1.25,
}

# 标题样式默认值。标题默认常驻全片;给了「持续」则只显示前 N 秒。
DEFAULT_TITLE_STYLE = {
    "字体": "msyhbd.ttc", "字号": 56, "颜色": "#FFFFFF",
    "描边色": "#000000", "描边宽": 5,
    "位置": "top", "顶部占比": 0.045, "边距": 0,
    "背景": None, "行距": 1.2, "持续": None,
    "副标题": None, "副字号": None, "副颜色": None,
}


# ---------------------------------------------------------------- 基础工具
def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=CREATE_NO_WINDOW)
    return ((p.stdout or b"").decode("utf-8", "replace"),
            (p.stderr or b"").decode("utf-8", "replace"), p.returncode)


def find_font(name):
    if os.path.isabs(name) and os.path.isfile(name):
        return name
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def probe_duration(path):
    out, _, code = run([FFPROBE, "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path])
    if code != 0:
        return None
    try:
        return round(float(out.strip()), 3)
    except ValueError:
        return None


def has_audio(path):
    out, _, code = run([FFPROBE, "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=index", "-of", "csv=p=0", path])
    return code == 0 and bool(out.strip())


def parse_res(s):
    m = re.match(r"^(\d+)\s*[x×]\s*(\d+)$", str(s).strip())
    if not m:
        raise ValueError("分辨率格式应为 1080x1920,收到:%s" % s)
    return int(m.group(1)), int(m.group(2))


def hex_rgba(s, default_alpha=255):
    if s is None:
        return None
    s = s.strip().lstrip("#")
    if len(s) == 6:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4)) + (default_alpha,)
    if len(s) == 8:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4)) + (int(s[6:8], 16),)
    raise ValueError("颜色格式应为 #RRGGBB 或 #RRGGBBAA,收到:%s" % s)


# ---------------------------------------------------------------- 字幕渲染
def wrap_lines(text, font, stroke, max_w, draw):
    """按**像素宽度**换行,而不是按字数硬切。

    实测教训:按字数切会把词切断(「…和换行是 / 否正常」)。
    这里用 textbbox 量真实宽度,并尽量在标点或空格处断。
    英文靠空格断词,所以空格也是合法的断点。
    """
    text = (text or "").strip()
    if not text:
        return [""]

    def width(s):
        bbox = draw.textbbox((0, 0), s, font=font, stroke_width=stroke)
        return bbox[2] - bbox[0]

    if width(text) <= max_w:
        return [text]

    lines, cur = [], ""
    for ch in text:
        trial = cur + ch
        if cur and width(trial) > max_w:
            # 到了要断的位置:优先在标点或空格处断
            cut = max((cur.rfind(p) for p in "，。！？、；：,.!?;: "), default=-1)
            if cut >= 0 and cut < len(cur) - 1:
                lines.append(cur[:cut + 1].strip())
                cur = cur[cut + 1:].strip() + ch
            else:
                lines.append(cur)
                cur = ch
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [text]


def detect_line_lang(s):
    """判断一行是中文还是英文。返回 'zh' / 'en' / None。"""
    if not s or not s.strip():
        return None
    cjk = len(CJK_RE.findall(s))
    lat = len(LATIN_RE.findall(s))
    if cjk == 0 and lat > 0:
        return "en"
    if lat == 0 and cjk > 0:
        return "zh"
    if cjk > lat:
        return "zh"
    if lat > cjk:
        return "en"
    return None


def normalize_subtitle_layers(text, style):
    """把字幕文本统一成「层」数组。

    单语:  "译文"                -> [{文本:"译文", ...样式}]
    双语:  "英文\\n中文"          -> [{英文层, 用英文字体}, {中文层, 用中文字体}]
    已是字典列表时原样使用。

    为什么不能按行序硬分配字体(实测踩过):
    早期版本假设「第一行中文、第二行英文」,直接按顺序套字体。
    但用户的 SRT 是**英文在前、中文在后**,结果英文被套上中文字体、
    中文被套上 Arial —— Arial 没有汉字,整行中文变成方框(tofu)。
    正确做法是**按每行的实际语言判断**该用哪个字体。
    """
    if text is None:
        return []
    if isinstance(text, list):
        out = []
        for item in text:
            if isinstance(item, dict):
                layer = dict(style)
                layer.update(item)
                out.append(layer)
            else:
                layer = dict(style)
                layer["文本"] = str(item)
                out.append(layer)
        return out

    parts = [p.strip() for p in str(text).split("\n") if p.strip()]
    if not parts:
        parts = [str(text).strip()]

    zh_font = style.get("中文字体") or style.get("字体", "msyhbd.ttc")
    en_font = style.get("英文字体") or style.get("字体", "msyhbd.ttc")
    zh_size = int(style.get("中文字号") or style.get("字号", 64))
    en_size = int(style.get("英文字号") or int(zh_size * 0.72))

    if len(parts) == 1:
        layer = dict(style)
        layer["文本"] = parts[0]
        lang = detect_line_lang(parts[0])
        if lang == "en":
            layer["字体"] = en_font
            layer["字号"] = en_size
        elif lang == "zh":
            layer["字体"] = zh_font
            layer["字号"] = zh_size
        return [layer]

    out = []
    for p in parts:
        layer = dict(style)
        layer["文本"] = p
        lang = detect_line_lang(p)
        if lang == "en":
            layer["字体"] = en_font
            layer["字号"] = en_size
        elif lang == "zh":
            layer["字体"] = zh_font
            layer["字号"] = zh_size
        else:
            # 混杂行(中英混排):用中文字体,它对拉丁字形也有覆盖
            layer["字体"] = zh_font
            layer["字号"] = zh_size
        out.append(layer)
    return out


def measure_layer(text, style, W, draw, ImageFont):
    """量一层的尺寸,返回 (行列表, 行高, 内边距, 文本宽, 文本高, 描边)。"""
    size = int(style.get("字号", 64))
    stroke = int(style.get("描边宽", 6) or 0)
    font = ImageFont.truetype(find_font(style.get("字体", "msyhbd.ttc")), size)
    line_h = int(size * float(style.get("行距", 1.25)))
    pad = stroke + 6
    margin_x = int(W * 0.05)
    max_w = W - margin_x * 2 - pad * 2
    lines = wrap_lines(text, font, stroke, max_w, draw)

    # 逐行量**墨迹**范围,而不是用字体行盒。
    # 为什么必须这样(实测踩过):Pillow 的 anchor="ma" 是按字体度量定位的,
    # 而字体行盒在基线上方含有一段字形并不占用的空白(内部行距),
    # 于是文字整体被推低 —— 用户反馈"标题文字明显在底框中线点正下方"。
    # 改成量墨迹后再垂直居中,才能真正居中。
    widths, tops, bots = [], [], []
    for ln in lines:
        bb = draw.textbbox((0, 0), ln, font=font, stroke_width=stroke)
        widths.append(bb[2] - bb[0])
        tops.append(bb[1])
        bots.append(bb[3])
    ink_top = min(tops) if tops else 0
    ink_bot = max(bots) if bots else line_h
    ink_h = max(1, ink_bot - ink_top)

    return {"行": lines, "字体": font, "字号": size, "行高": line_h,
            "内边距": pad, "描边": stroke,
            "文本宽": max(widths) if widths else 0,
            "墨迹上偏": ink_top,          # 相对绘制原点的墨迹上边界
            "行墨迹上": tops,             # 每行各自的墨迹上边界
            "行高墨迹": [max(1, b - t) for t, b in zip(tops, bots)],
            "文本高": ink_h}              # 用墨迹高,块高才紧凑


def render_block_png(layers, style, W, out_png, bg_override=None):
    """把一个「字幕块」(可含多层:中文行 + 英文行)渲染成一张透明 PNG。

    分层是为了让中英文各用自己的字体和字号,同时又共用一块背景色。
    """
    from PIL import Image, ImageDraw, ImageFont

    tmp = Image.new("RGBA", (10, 10))
    d0 = ImageDraw.Draw(tmp)

    # 关键:metas 与 layers 必须同步过滤。
    # 实测踩过一个隐蔽 bug:metas 过滤掉了空文本层,但下面 zip(metas, layers)
    # 用的是**未过滤**的 layers,导致层与样式错位 —— 中文那层取到了空层的样式,
    # 颜色取成透明,于是中文被"画"了却完全看不见(PNG 里查不到中文像素)。
    # 所以这里先把两者一起过滤,保证一一对应。
    pairs = [(m, l) for m, l in
             ((measure_layer(l.get("文本", ""), l, W, d0, ImageFont), l)
              for l in layers)
             if (l.get("文本") or "").strip()]
    if not pairs:
        return None
    metas = [p[0] for p in pairs]
    used = [p[1] for p in pairs]

    gap = int(style.get("层间距", 10))
    sep = int(style.get("分隔线宽", 0))
    # 背景块整体内边距。标题块两行之间需要留白,否则副标题会贴住主标题。
    block_pad = int(style.get("块内边距", 0) or 0)
    any_bg = any(hex_rgba(l.get("背景")) for l in layers)
    bg = hex_rgba(bg_override) if bg_override else (
        hex_rgba(next((l.get("背景") for l in layers if l.get("背景")), None))
        if any_bg else None)

    pad = max(m["内边距"] for m in metas) + block_pad
    content_w = max(m["文本宽"] for m in metas)
    # 关键:一层的块高 = 它**所有换行**的墨迹高之和,不是单行高。
    # 实测踩过:英文换行成 2 行时,文本高只报了第 1 行的高度,
    # 盒子矮了一整行,结果中文那一层被挤出画布 —— 中文"消失"的真相。
    layer_h = [sum(m["行高墨迹"]) for m in metas]
    content_h = sum(layer_h) + gap * (len(metas) - 1) + sep

    box_w = min(W, content_w + pad * 2 + (16 if bg else 0))
    box_h = content_h + pad * 2

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if bg:
        radius = max(8, int(max(m["字号"] for m in metas) * 0.28))
        draw.rounded_rectangle([0, 0, box_w - 1, box_h - 1], radius=radius, fill=bg)

    y = pad
    for i, (m, l) in enumerate(zip(metas, used)):
        fill = hex_rgba(l.get("颜色", "#FFFFFF"))
        stroke_fill = hex_rgba(l.get("描边色", "#000000"))
        nlines = len(m["行"])
        for j, ln in enumerate(m["行"]):
            # 绘制原点要减去该行的「墨迹上偏」,这样字形顶边正好落在 y。
            t = m["行墨迹上"][j]
            draw.text((box_w // 2, y - t), ln, font=m["字体"],
                      fill=fill, stroke_width=m["描边"], stroke_fill=stroke_fill,
                      anchor="ma")
            y += m["行高墨迹"][j]
        # 该层画完:回退到最后一行的高度,补上标准行距,保证层间留白一致
        if nlines:
            y += int(m["字号"] * float(style.get("行距", 1.25))) - m["行高墨迹"][-1]
        if i < len(metas) - 1:
            if sep:
                draw.line([(pad, y + gap // 2), (box_w - pad, y + gap // 2)],
                          fill=hex_rgba(style.get("分隔线色", "#FFFFFF88")), width=sep)
            y += gap + sep
    img.save(out_png)
    return {"w": box_w, "h": box_h, "层数": len(metas)}


# xfade 转场名映射:把中文/常用叫法映射到 ffmpeg 的 transition 名。
# 未知的名字一律退回 fade(xfade 对未知 transition 会直接报错,不能放任)。
XFADE_MAP = {
    "fade": "fade", "淡入淡出": "fade", "叠化": "fade", "dissolve": "dissolve",
    "fadeblack": "fadeblack", "黑场": "fadeblack", "fadewhite": "fadewhite",
    "白场": "fadewhite", "wipeleft": "wipeleft", "左擦除": "wipeleft",
    "wiperight": "wiperight", "右擦除": "wiperight", "wipeup": "wipeup",
    "上擦除": "wipeup", "wipedown": "wipedown", "下擦除": "wipedown",
    "slideleft": "slideleft", "左滑": "slideleft", "slideright": "slideright",
    "右滑": "slideright", "slideup": "slideup", "上滑": "slideup",
    "slidedown": "slidedown", "下滑": "slidedown", "circleopen": "circleopen",
    "圆形开": "circleopen", "circleclose": "circleclose", "圆形闭": "circleclose",
    "radial": "radial", "放射": "radial", "smoothleft": "smoothleft",
    "smoothright": "smoothright", "pixelize": "pixelize", "像素化": "pixelize",
    "hblur": "hblur", "模糊": "hblur", "zoomin": "zoomin", "推近": "zoomin",
}


def xfade_name(kind):
    k = (kind or "fade").strip().lower()
    return XFADE_MAP.get(k, XFADE_MAP.get(kind, "fade"))


def style_of(spec, name):
    styles = spec.get("字幕样式") or {}
    st = dict(DEFAULT_STYLE)
    st.update(styles.get("default") or {})
    if name and name != "default":
        st.update(styles.get(name) or {})
    return st


def subtitle_overlay_y(style, H, box_h):
    pos = (style.get("位置") or "bottom").lower()
    margin = int(style.get("底距", 220))
    if pos in ("bottom", "底部", "下"):
        return max(0, H - margin - box_h)
    if pos in ("top", "顶部", "上"):
        return margin
    return max(0, (H - box_h) // 2)


def title_overlay_y(style, H):
    """标题位置。默认顶部。支持 安全区占比 避免贴边。"""
    pos = (style.get("位置") or "top").lower()
    margin = int(style.get("边距", 0) or 0)
    if not margin:
        margin = int(H * float(style.get("顶部占比", 0.045)))
    if pos in ("bottom", "底部", "下"):
        return None            # 由调用方按 box 高度算
    return max(0, margin)


def render_title_png(title, style, W, out_png):
    """标题单层渲染。可带副标题(第二行)。"""
    from PIL import Image, ImageDraw, ImageFont

    layers = []
    main = dict(style)
    main["文本"] = title
    layers.append(main)
    sub = style.get("副标题")
    if sub:
        sl = dict(style)
        sl["文本"] = sub
        sl["字号"] = int(style.get("副字号") or int(int(style.get("字号", 64)) * 0.62))
        sl["颜色"] = style.get("副颜色", style.get("颜色", "#FFFFFF"))
        sl["背景"] = None
        layers.append(sl)
    return render_block_png(layers, style, W, out_png)


# ---------------------------------------------------------------- 校验
def validate(spec, base_dir):
    errs, warns = [], []
    out = spec.get("输出") or {}
    try:
        W, H = parse_res(out.get("分辨率", "1080x1920"))
    except ValueError as e:
        errs.append(str(e))
        W = H = 0
    fps = float(out.get("帧率", 30))

    shots = spec.get("镜头") or []
    if not shots:
        errs.append("spec 里没有任何镜头")

    checked = set()
    for i, s in enumerate(shots, 1):
        src = s.get("素材")
        if not src:
            errs.append("第 %d 镜缺「素材」" % i)
            continue
        p = src if os.path.isabs(src) else os.path.join(base_dir, src)
        s["_abs"] = p
        if not os.path.isfile(p):
            errs.append("第 %d 镜素材不存在:%s" % (i, p))
            continue
        if p not in checked:
            checked.add(p)
            if is_image(p):
                # 静图没有"素材时长"概念,由 spec 的时长决定
                s["_media_dur"] = None
                s["_is_image"] = True
            else:
                d = probe_duration(p)
                s["_media_dur"] = d
                if d is None:
                    warns.append("无法读取时长:%s" % os.path.basename(p))
        else:
            s["_media_dur"] = None
            s["_is_image"] = is_image(p)
        t_in = float(s.get("入", 0) or 0)
        t_out = s.get("出")
        if t_out is None:
            # 静图允许只给「时长」
            if s.get("_is_image") and s.get("时长"):
                t_out = t_in + float(s["时长"])
            else:
                errs.append("第 %d 镜缺「出」时间点(静图可改用「时长」)" % i)
                continue
        t_out = float(t_out)
        dur = s.get("时长") or (t_out - t_in)
        if dur <= 0:
            errs.append("第 %d 镜时长非正:%.3f" % (i, dur))
        s["_dur"] = round(dur, 3)
        s["_in"] = round(t_in, 3)
        if s.get("_media_dur") and t_out > s["_media_dur"] + 0.05:
            warns.append("第 %d 镜出点 %.2fs 超出素材时长 %.2fs(%s)"
                         % (i, t_out, s["_media_dur"], os.path.basename(p)))
        ad = (s.get("适配") or "cover").lower()
        if ad not in ("cover", "contain", "stretch"):
            errs.append("第 %d 镜适配方式未知:%s" % (i, ad))
        if s.get("字幕"):
            st = style_of(spec, s.get("字幕样式"))
            for l in normalize_subtitle_layers(s["字幕"], st):
                fn = l.get("字体", "msyhbd.ttc")
                if not find_font(fn):
                    errs.append("找不到字体 %s(第 %d 镜字幕)" % (fn, i))
            # 双语检查:两行字幕时应能取到英文字体
            if isinstance(s["字幕"], str) and "\n" in s["字幕"].strip():
                if not find_font(st.get("英文字体") or st.get("字体", "msyhbd.ttc")):
                    warns.append("第 %d 镜是双语字幕,但找不到英文字体" % i)

    if spec.get("标题"):
        tst = dict(DEFAULT_TITLE_STYLE)
        tst.update(spec.get("标题样式") or {})
        if not find_font(tst.get("字体", "msyhbd.ttc")):
            errs.append("找不到标题字体:%s" % tst.get("字体"))
        if tst.get("持续") is not None:
            try:
                if float(tst["持续"]) <= 0:
                    errs.append("标题「持续」必须大于 0")
            except (TypeError, ValueError):
                errs.append("标题「持续」应为秒数,收到:%s" % tst.get("持续"))

    audio = spec.get("音频") or {}
    bgm = (audio.get("背景音乐") or {}).get("文件")
    if bgm:
        bp = bgm if os.path.isabs(bgm) else os.path.join(base_dir, bgm)
        bgm_abs = bp
        if not os.path.isfile(bp):
            errs.append("BGM 不存在:%s" % bp)
        else:
            spec["音频"]["背景音乐"]["_abs"] = bp
    return errs, warns, W, H, fps


# ---------------------------------------------------------------- 构建
def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXT


def transition_plan(spec, shots):
    """解析转场设置,并把每镜的入点转场挂到该镜上。

    返回 (转场列表, 总时长)。转场会吃掉重叠时间,所以总时长 =
    镜头时长之和 - 所有转场时长之和。这一点必须算进去,否则时长校验会误报。
    """
    tr = spec.get("转场") or {}
    default_type = tr.get("类型", "cut")
    default_dur = float(tr.get("时长", 0.5))
    total = sum(s.get("_dur", 0) for s in shots)
    plan = []
    for i, s in enumerate(shots):
        if i == 0:
            plan.append(None)
            continue
        t = (s.get("转场") or default_type or "cut")
        if isinstance(t, str):
            t = {"类型": t}
        kind = (t.get("类型") or "cut").lower()
        if kind in ("cut", "", "none", "硬切"):
            plan.append(None)
            continue
        d = float(t.get("时长", default_dur))
        # 转场不能超过相邻两镜较短者的一半,否则画面会错乱
        max_d = min(shots[i - 1]["_dur"], s["_dur"]) / 2.0
        if d > max_d:
            d = round(max_d, 3)
        d = round(max(d, 0.05), 3)
        plan.append({"类型": kind, "时长": d})
        total -= d
    return plan, round(total, 3)


def build_video_chain(spec, W, H, fps, tmpdir, base_dir):
    """构建视频滤镜链:逐镜归一化 -> (concat | xfade) -> 字幕 overlay。

    转场用 xfade。实测约束:xfade 要求两路输入分辨率与像素格式完全一致,
    这里每镜都先归一化到 WxH,所以天然满足。
    """
    shots = spec["镜头"]
    tplan, total_dur = transition_plan(spec, shots)
    parts, inputs = [], []
    idx = 0
    for i, s in enumerate(shots, 1):
        if is_image(s["_abs"]):
            # 静图:用 -loop 1 生成持续画面。实测 -loop 1 -t N 输出时长精确。
            inputs += ["-loop", "1", "-t", "%.3f" % s["_dur"], "-i", s["_abs"]]
        else:
            inputs += ["-i", s["_abs"]]
        v = "[%d:v]" % idx
        idx += 1
        ad = (s.get("适配") or "cover").lower()
        if ad == "cover":
            chain = ("%sscale=%d:%d:force_original_aspect_ratio=increase,"
                     "crop=%d:%d,setsar=1,fps=%s" % (v, W, H, W, H, fps))
        elif ad == "contain":
            chain = ("%sscale=%d:%d:force_original_aspect_ratio=decrease,"
                     "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=%s"
                     % (v, W, H, W, H, fps))
        else:
            chain = "%sscale=%d:%d,setsar=1,fps=%s" % (v, W, H, fps)
        if is_image(s["_abs"]):
            # 静图已被 -t 限定时长,只需对齐时间基
            chain += ",trim=duration=%.3f,setpts=PTS-STARTPTS[v%d]" % (s["_dur"], i)
        else:
            chain += ",trim=start=%.3f:duration=%.3f,setpts=PTS-STARTPTS[v%d]" % (
                s["_in"], s["_dur"], i)
        parts.append(chain)

    if any(tplan):
        # ---- xfade 链 ----
        cur = "v1"
        acc = shots[0]["_dur"]
        n = 1
        for i in range(1, len(shots)):
            t = tplan[i]
            nxt = "x%d" % i
            if t:
                offset = round(acc - t["时长"], 3)
                parts.append("[%s][v%d]xfade=transition=%s:duration=%.3f:offset=%.3f[%s]"
                             % (cur, i + 1, xfade_name(t["类型"]), t["时长"],
                                max(offset, 0.0), nxt))
                acc = round(acc + shots[i]["_dur"] - t["时长"], 3)
            else:
                parts.append("[%s][v%d]concat=n=2:v=1:a=0[%s]" % (cur, i + 1, nxt))
                acc = round(acc + shots[i]["_dur"], 3)
            cur = nxt
            n += 1
        parts.append("[%s]null[vc]" % cur)
    else:
        concat_in = "".join("[v%d]" % i for i in range(1, len(shots) + 1))
        parts.append("%sconcat=n=%d:v=1:a=0[vc]" % (concat_in, len(shots)))

    # ---- 文字层:先全部量好位置,再统一检查重叠,最后才构建 overlay 链 ----
    # 为什么要「先量后叠」:翻译字幕类作品上下都有文字块,
    # 双语字幕一长就会和上方标题撞在一起。若直接叠上去,只有肉眼才能发现,
    # 所以这里先把所有文字块的矩形算出来做碰撞检测。
    # 注意:有转场时时间轴会前移(重叠被吃掉),字幕起止时间必须按
    # 「累加每镜时长后再减去已发生的转场时长」算,否则整体错位。
    boxes = []           # [{kind, png, x, y, w, h, t0, t1, 文本}]
    overlays = []
    timeline = 0.0

    # ---- 字幕来源 A:SRT 文件驱动(用户的实际形态)----
    # 一条底片配几十条不同时间的字幕,时间轴由 SRT 决定。
    # 这种情况下镜头上不再写「字幕」,避免两套时间轴打架。
    srt_entries = spec.get("_srt_entries") or []
    if srt_entries:
        st = style_of(spec, "default")
        for k, e in enumerate(srt_entries, 1):
            png = os.path.join(tmpdir, "sub_%03d.png" % k)
            layers = normalize_subtitle_layers(e["文本"], st)
            meta = render_block_png(layers, st, W, png)
            if meta is None:
                continue
            boxes.append({
                "kind": "字幕", "png": png, "层数": meta.get("层数", 1),
                "w": meta["w"], "h": meta["h"],
                "x": max(0, (W - meta["w"]) // 2),
                "y": subtitle_overlay_y(st, H, meta["h"]),
                "t0": round(e["起"], 3), "t1": round(e["止"], 3),
                "文本": e["文本"]})
            overlays.append({"起": round(e["起"], 3), "止": round(e["止"], 3),
                             "文本": e["文本"]})

    # ---- 字幕来源 B:写在镜头上(时间 = 该镜时长)----
    for i, s in enumerate(shots, 1):
        t0 = timeline
        t1 = timeline + s["_dur"]
        if tplan[i - 1]:
            t1 -= tplan[i - 1]["时长"]
        text = s.get("字幕")
        if text and not srt_entries:
            st = style_of(spec, s.get("字幕样式"))
            png = os.path.join(tmpdir, "sub_%03d.png" % (len(boxes) + 1))
            layers = normalize_subtitle_layers(text, st)
            meta = render_block_png(layers, st, W, png)
            if meta is not None:
                boxes.append({
                    "kind": "字幕", "png": png, "层数": meta.get("层数", 1),
                    "w": meta["w"], "h": meta["h"],
                    "x": max(0, (W - meta["w"]) // 2),
                    "y": subtitle_overlay_y(st, H, meta["h"]),
                    "t0": round(t0, 3),
                    "t1": round(t1, 3), "文本": text})
                overlays.append({"起": round(t0, 3), "止": round(t1, 3),
                                 "文本": text})
        timeline = t1

    title_info = None
    title = spec.get("标题")
    if title:
        tst = dict(DEFAULT_TITLE_STYLE)
        tst.update(spec.get("标题样式") or {})
        png = os.path.join(tmpdir, "title.png")
        meta = render_title_png(title, tst, W, png)
        if meta:
            ty = title_overlay_y(tst, H)
            if ty is None:
                ty = max(0, H - int(tst.get("底距", 220)) - meta["h"])
            until = tst.get("持续")
            te = total_dur if until is None else float(until)
            boxes.append({
                "kind": "标题", "png": png, "层数": 1,
                "w": meta["w"], "h": meta["h"],
                "x": max(0, (W - meta["w"]) // 2), "y": ty,
                "t0": 0.0, "t1": round(te, 3), "文本": title})
            title_info = {"标题": title, "位置": tst.get("位置", "top"),
                          "止": round(te, 3), "宽": meta["w"], "高": meta["h"],
                          "常驻": until is None}

    warn = check_text_collisions(boxes, W, H)

    # ---- 构建 overlay 链(字幕在前,标题在后:标题应压在最上层)----
    cur = "vc"
    ordered = [b for b in boxes if b["kind"] == "字幕"] + \
              [b for b in boxes if b["kind"] == "标题"]
    for n, b in enumerate(ordered, 1):
        inputs += ["-i", b["png"]]
        oidx = idx
        idx += 1
        nxt = "ov%d" % n
        act = ("" if (b["kind"] == "标题" and b.get("t1", 0) >= total_dur - 0.01)
               else ":enable='between(t,%.3f,%.3f)'"
                    % (b["t0"] + 0.001, max(b["t0"] + 0.05, b["t1"] - 0.001)))
        # 叠加前必须把 PNG 显式转成 rgba 并统一到与主画面相同的彩色空间。
        # 实测踩过:RGBA 的 PNG 直接 overlay 到 yuv420p 上,alpha 会被当成
        # 颜色通道处理,标题两侧会冒出青色/白色杂点。加上 format=rgba +
        # format=auto 之后杂点消失。
        parts.append("[%d:v]format=rgba,scale=%d:%d[ovp%d]"
                     % (oidx, b["w"], b["h"], n))
        parts.append("[%s][ovp%d]overlay=%d:%d:format=auto%s[%s]"
                     % (cur, n, b["x"], b["y"], act, nxt))
        cur = nxt

    parts.append("[%s]null[vout]" % cur)
    return (parts, inputs, idx, overlays, tplan, total_dur, title_info,
            warn, boxes)

    # ---- 标题 overlay(你的作品需要:视频上方标题,常驻或前 N 秒)----
def parse_srt_file(path):
    """解析 SRT,返回 [{起, 止, 文本}]。文本保留换行(第一行英文、第二行中文)。

    为什么需要独立解析:用户的字幕是 SRT 驱动的 —— 一条底片配几十条不同时间的
    字幕,时间轴由 SRT 决定,不能绑在镜头上(早期版本绑镜头,是设计缺口)。
    """
    import re as _re
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()
    out = []
    for blk in _re.split(r"\n\s*\n", raw.replace("\r\n", "\n")):
        lines = [l.strip() for l in blk.split("\n") if l.strip()]
        tl = next((l for l in lines if "-->" in l), None)
        if not tl:
            continue
        m = _re.match(r"\s*([\d:.,]+)\s*-->\s*([\d:.,]+)", tl)
        if not m:
            continue
        body = lines[lines.index(tl) + 1:]
        if not body:
            continue
        out.append({"起": srt_to_sec(m.group(1)), "止": srt_to_sec(m.group(2)),
                    "文本": "\n".join(body)})
    return out


def srt_to_sec(ts):
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    try:
        p = [float(x) for x in parts]
    except ValueError:
        return 0.0
    if len(p) == 3:
        return round(p[0] * 3600 + p[1] * 60 + p[2], 3)
    if len(p) == 2:
        return round(p[0] * 60 + p[1], 3)
    return round(p[0], 3) if p else 0.0


def check_text_collisions(boxes, W, H):
    """检查所有文字块之间是否重叠、是否溢出画面。

    翻译字幕类作品上下都有文字块,双语字幕一长就会和上方标题撞在一起。
    这个问题只有肉眼才能发现,所以必须自动检查出来。
    """
    msgs = []
    n = len(boxes)
    for i in range(n):
        a = boxes[i]
        if a["x"] < 0 or a["x"] + a["w"] > W:
            msgs.append("「%s」横向超出画面(%d+%d > %d)"
                        % (str(a["文本"])[:16], a["x"], a["w"], W))
        if a["y"] < 0 or a["y"] + a["h"] > H:
            msgs.append("「%s」纵向超出画面(y=%d h=%d 画面高 %d)"
                        % (str(a["文本"])[:16], a["y"], a["h"], H))
        for j in range(i + 1, n):
            b = boxes[j]
            # 时间上是否同时出现
            if a["t1"] <= b["t0"] or b["t1"] <= a["t0"]:
                continue
            # 矩形是否相交
            if (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and
                    a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]):
                ov = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
                msgs.append(
                    "「%s」(%s)与「%s」(%s)重叠 %dpx —— "
                    "建议减小字号/底距,或让标题只显示前几秒"
                    % (str(a["文本"])[:14], a["kind"],
                       str(b["文本"])[:14], b["kind"], ov))
    return msgs


def build_audio_graph(spec, base_dir, tmpdir, total_dur):
    """音频单独一趟处理,避免与视频滤镜图相互牵制。

    人声轨 = 逐镜切片后 concat。注意:无音轨的镜头必须补等长静音,
    否则时间轴会前移,后面的镜头会整体错位(实测必须处理)。
    """
    shots = spec["镜头"]
    audio = spec.get("音频") or {}
    voice_vol = float((audio.get("人声") or {}).get("音量", 1.0))

    inputs, parts, labels = [], [], []
    idx = 0
    for i, s in enumerate(shots, 1):
        src = s["_abs"]
        if has_audio(src):
            inputs += ["-i", src]
            parts.append("[%d:a]atrim=start=%.3f:duration=%.3f,asetpts=PTS-STARTPTS,"
                         "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                         "volume=%.3f[va%d]" % (idx, s["_in"], s["_dur"], voice_vol, i))
            idx += 1
        else:
            # 该镜无音轨 -> 补等长静音,保证时间轴不错位
            # 注意:这里不加 -i,所以不占输入索引。BGM 的索引必须按 idx 取,
            # 不能按镜头序号推 —— 否则素材缺音轨时 BGM 索引会指向不存在的输入。
            parts.append("anullsrc=r=48000:cl=stereo,atrim=duration=%.3f,"
                         "asetpts=PTS-STARTPTS[va%d]" % (s["_dur"], i))
        labels.append("[va%d]" % i)

    cur = "voc"
    parts.append("%sconcat=n=%d:v=0:a=1[%s]"
                 % ("".join(labels), len(shots), cur))

    bgm = (audio.get("背景音乐") or {}).get("_abs")
    if bgm:
        bgm_vol = float((audio.get("背景音乐") or {}).get("音量", 0.18))
        loop = (audio.get("背景音乐") or {}).get("循环", True)
        # 关键:idx 只统计「真正加入 -i 的输入数」,与镜头序号无关
        bi = idx
        inputs += ["-i", bgm]
        idx += 1
        chain = "[%d:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo" % bi
        if loop:
            chain += ",aloop=loop=-1:size=2147483647"
        chain += ",atrim=start=0:duration=%.3f,asetpts=PTS-STARTPTS,volume=%.3f[bgm]" % (
            total_dur, bgm_vol)
        parts.append(chain)
        duck = (audio.get("背景音乐") or {}).get("闪避") or {}
        if duck.get("启用", True):
            strength = float(duck.get("强度", 6))
            # 实测坑:sidechaincompress 会按「较短的那一路」截断输出
            # (9s主+3s侧链 -> 输出仅 2.99s),而且 attack/release 还会吃掉几十毫秒。
            # 所以闪避之后必须 apad 补长,再切回目标总长,否则成片会缺尾。
            parts.append("[bgm][voc]sidechaincompress=threshold=0.03:ratio=%s:"
                         "attack=20:release=400,apad,atrim=duration=%.3f,"
                         "asetpts=PTS-STARTPTS[bgmd]" % (strength, total_dur))
            mixed = "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0"
            parts.append("[voc][bgmd]%s,apad,atrim=duration=%.3f,"
                         "asetpts=PTS-STARTPTS[amixed]" % (mixed, total_dur))
        else:
            parts.append("[voc][bgm]amix=inputs=2:duration=longest:"
                         "dropout_transition=0:normalize=0,apad,"
                         "atrim=duration=%.3f,asetpts=PTS-STARTPTS[amixed]"
                         % total_dur)
        cur = "amixed"

    norm = audio.get("响度归一") or {}
    if norm.get("启用", True):
        target = norm.get("目标LUFS", -14)
        parts.append("[%s]loudnorm=I=%s:TP=-1.5:LRA=11[aout]" % (cur, target))
        cur = "aout"
    else:
        parts.append("[%s]anull[aout]" % cur)
        cur = "aout"

    return inputs, parts, cur


def fmt_cmd(cmd):
    out, line = [], ""
    for c in cmd:
        if len(line) + len(c) + 1 > 110:
            out.append(line)
            line = "    " + c
        else:
            line = (line + " " + c).strip() if line else c
    if line:
        out.append(line)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--keep-temp", action="store_true")
    ap.add_argument("--debug", action="store_true",
                    help="用 ffmpeg -debug ts 输出滤镜链逐段读数,用于排查时长异常")
    args = ap.parse_args()

    if not os.path.isfile(args.spec):
        print("找不到 spec:%s" % args.spec)
        return 2
    with open(args.spec, "r", encoding="utf-8-sig") as fh:
        spec = json.load(fh)
    base_dir = os.path.dirname(os.path.abspath(args.spec))

    # 字幕文件驱动:用户的实际形态是一条底片 + 一份 SRT。
    srt_rel = spec.get("字幕文件")
    if srt_rel:
        srt_path = srt_rel if os.path.isabs(srt_rel) else os.path.join(base_dir, srt_rel)
        if not os.path.isfile(srt_path):
            print("找不到字幕文件:%s" % srt_path)
            return 2
        spec["_srt_entries"] = parse_srt_file(srt_path)
        print("字幕文件:%s -> %d 条" % (os.path.basename(srt_path),
                                    len(spec["_srt_entries"])))

    errs, warns, W, H, fps = validate(spec, base_dir)
    shots = spec.get("镜头") or []
    # 注意:transition_plan 依赖 validate 填好的 _dur,所以必须放在 validate 之后。
    # 而且 validate 里对「素材不存在」的镜头会跳过填 _dur —— 那种情况下
    # 绝对不能继续算时长,否则会静默少算。所以先判 errs,有错就停。
    if errs:
        print("== 校验 ==")
        print("  分辨率 %sx%s @ %sfps | 镜头 %d 个" % (W, H, fps, len(shots)))
        for w in warns:
            print("  ⚠️ %s" % w)
        for e in errs:
            print("  ❌ %s" % e)
        print("\n校验未通过,已停止(不会在素材缺失的情况下继续)。")
        return 4
    tplan, total_dur = transition_plan(spec, shots) if shots else ([], 0.0)
    raw_sum = round(sum(s.get("_dur", 0) for s in shots), 3)
    tr_list = [t for t in tplan if t]
    print("== 校验 ==")
    print("  分辨率 %sx%s @ %sfps | 镜头 %d 个" % (W, H, fps, len(shots)))
    if tr_list:
        eaten = round(raw_sum - total_dur, 3)
        print("  转场 %d 处 | 镜头时长合计 %.3fs - 转场吃掉 %.3fs = 成片 %.3fs"
              % (len(tr_list), raw_sum, eaten, total_dur))
    else:
        print("  转场:无(全硬切) | 成片 %.3fs" % total_dur)
    for w in warns:
        print("  ⚠️ %s" % w)
    if args.check:
        print("\n校验通过(--check 模式,未渲染)。")
        return 0

    total_dur = round(sum(s["_dur"] for s in spec["镜头"]), 3)
    name = spec.get("项目名") or os.path.splitext(os.path.basename(args.spec))[0]
    specdir = os.path.dirname(os.path.abspath(args.spec))
    outdir = os.path.join(specdir, "out")
    os.makedirs(outdir, exist_ok=True)
    outfile = args.out or os.path.join(outdir, "%s.mp4" % name)

    tmpdir = os.path.join(specdir, "_tmp")
    if os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)

    print("\n== 构建视频链 ==")
    (vparts, vinputs, vidx, overlays, tplan2, total_dur,
     title_info, collide, boxes) = build_video_chain(spec, W, H, fps, tmpdir, base_dir)
    vgraph = ";".join(vparts)
    tmp_video = os.path.join(tmpdir, "video.mp4")
    cmd_v = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + vinputs + [
        "-filter_complex", vgraph, "-map", "[vout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(fps), tmp_video]
    nsub = len([b for b in boxes if b["kind"] == "字幕"])
    print("  滤镜链 %d 段 | 文字层 %d 个(字幕 %d + 标题 %d)"
          % (len(vparts), len(boxes), nsub, len(boxes) - nsub))
    if collide:
        print("  ⚠️ 文字层排布警告:")
        for m in collide:
            print("     - %s" % m)
    # 文字层几何明细,便于核对排版(顶/底留白、是否撞层)
    print("  文字层几何:")
    for b in boxes:
        print("     %s y=%4d..%4d h=%3d x=%4d..%4d w=%4d | %.2f~%.2fs | %s"
              % (b["kind"], b["y"], b["y"] + b["h"], b["h"],
                 b["x"], b["x"] + b["w"], b["w"], b["t0"], b["t1"],
                 str(b["文本"]).replace("\n", " / ")[:34]))

    print("\n== 构建音频链 ==")
    ainputs, aparts, alabel = build_audio_graph(spec, base_dir, tmpdir, total_dur)
    agraph = ";".join(aparts)
    tmp_audio = os.path.join(tmpdir, "audio.m4a")
    cmd_a = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + ainputs + [
        "-filter_complex", agraph, "-map", "[%s]" % alabel,
        "-c:a", "aac", "-b:a", (spec.get("输出") or {}).get("音频码率", "192k"),
        tmp_audio]
    print("  音频链 %d 段" % len(aparts))

    log = os.path.join(outdir, "_cmd.log")
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("### 视频链\n%s\n\n### 音频链\n%s\n"
                 % (fmt_cmd(cmd_v), fmt_cmd(cmd_a)))
        fh.write("\n### 视频滤镜图\n%s\n\n### 音频滤镜图\n%s\n" % (vgraph, agraph))

    if args.dry_run:
        print("\n--dry-run:命令已写入 %s,未渲染。" % log)
        if not args.keep_temp:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return 0

    print("\n== 渲染视频 ==")
    if args.debug:
        with open(os.path.join(outdir, "_debug_video.txt"), "w", encoding="utf-8") as fh:
            fh.write(vgraph)
        dbg = [cmd_v[0], "-hide_banner", "-loglevel", "error", "-debug", "ts"] + cmd_v[3:]
        print("  debug 命令:%s ..." % " ".join(dbg[:6]))
        _, err, _ = run(dbg)
        with open(os.path.join(outdir, "_debug_video.log"), "w", encoding="utf-8") as fh:
            fh.write(err)
        print("  debug 日志 -> %s" % os.path.join(outdir, "_debug_video.log"))
    _, err, code = run(cmd_v)
    if code != 0:
        print("  视频渲染失败:\n%s" % err[-2500:])
        return 5
    vdur = probe_duration(tmp_video)
    print("  -> %s(实测 %ss,规格 %.3fs,偏差 %+.3fs)"
          % (os.path.basename(tmp_video), vdur, total_dur, (vdur or 0) - total_dur))

    print("== 渲染音频 ==")
    _, err, code = run(cmd_a)
    if code != 0:
        print("  音频渲染失败:\n%s" % err[-2500:])
        return 6
    adur = probe_duration(tmp_audio)
    print("  -> %s(实测 %ss,规格 %.3fs,偏差 %+.3fs)"
          % (os.path.basename(tmp_audio), adur, total_dur, (adur or 0) - total_dur))

    if vdur and adur and abs(vdur - adur) > 0.25:
        print("\n  ⚠️ 音视频时长不一致(%.3fs vs %.3fs)。合流会按较短的一边截断,"
              "成片会缺尾。" % (vdur, adur))
        print("     排查:通常是某个镜头的素材实际时长不足「出」时间点。")
        for i, s in enumerate(spec["镜头"], 1):
            md = s.get("_media_dur")
            if md and s["_in"] + s["_dur"] > md + 0.05:
                print("     第 %d 镜:需要 %.2f~%.2fs,素材只有 %.2fs ← 元凶"
                      % (i, s["_in"], s["_in"] + s["_dur"], md))

    # 硬校验:成片时长必须贴合规格。静默产出短成片是最坑人的失败模式,
    # 宁可报错也不交付缺尾的片子。
    problems = []
    if vdur is not None and abs(vdur - total_dur) > 0.20:
        problems.append("视频链 %.3fs,规格 %.3fs" % (vdur, total_dur))
    if adur is not None and abs(adur - total_dur) > 0.20:
        problems.append("音频链 %.3fs,规格 %.3fs" % (adur, total_dur))
    if problems:
        print("\n❌ 时长校验未通过,已停止合流(不会交付缺尾成片):")
        for p in problems:
            print("   - %s" % p)
        print("   规格明细:")
        for i, s in enumerate(spec["镜头"], 1):
            md = s.get("_media_dur")
            need = s["_in"] + s["_dur"]
            flag = "  ← 素材不足" if (md and need > md + 0.05) else ""
            print("     第 %d 镜 入%.2f 出%.2f 时长%.3f(素材%s)%s"
                  % (i, s["_in"], need, s["_dur"],
                     ("%.2fs" % md) if md else "未知", flag))
        if not args.keep_temp:
            print("   (中间产物保留在 %s 供排查)" % tmpdir)
        return 8

    print("== 合流 ==")
    cmd_m = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-i", tmp_video, "-i", tmp_audio, "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "copy", "-shortest",
             "-movflags", "+faststart", outfile]
    _, err, code = run(cmd_m)
    if code != 0:
        print("  合流失败:\n%s" % err[-2000:])
        return 7

    # 字幕另存一份(即使已压入画面)
    if overlays:
        srt = os.path.join(outdir, "%s.srt" % name)
        with open(srt, "w", encoding="utf-8") as fh:
            for i, o in enumerate(overlays, 1):
                txt = o["文本"]
                if isinstance(txt, list):
                    txt = "\n".join(
                        x.get("文本", "") if isinstance(x, dict) else str(x)
                        for x in txt)
                fh.write("%d\n%s --> %s\n%s\n\n" % (
                    i, srt_ts(o["起"]), srt_ts(o["止"]), txt))
        print("  -> %s" % os.path.basename(srt))

    # 存档实际规格
    saved = os.path.join(outdir, "%s.spec.json" % name)
    with open(saved, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2, default=str)

    dur = probe_duration(outfile)
    bilingual = sum(1 for o in overlays
                    if isinstance(o["文本"], str) and "\n" in o["文本"].strip())
    print("\n成片 -> %s" % outfile)
    print("  时长 %ss(规格 %.3fs,偏差 %+.3fs)"
          % (dur, total_dur, (dur or 0) - total_dur))
    print("  规格 %sx%s @ %sfps" % (W, H, fps))
    print("  字幕 %d 句%s | 标题 %s"
          % (len(overlays),
             ("(其中双语 %d 句)" % bilingual) if bilingual else "",
             ("「%s」%s" % (title_info["标题"],
                          "常驻全片" if not title_info.get("止")
                          else "前 %.1fs" % title_info["止"]))
             if title_info else "无"))
    if not args.keep_temp:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


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


if __name__ == "__main__":
    sys.exit(main())
