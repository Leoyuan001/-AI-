#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
serve.py — 本地网页服务(零依赖,只用 Python 标准库)

为什么不用 Flask:本机没有 Flask/FastAPI,而且装不上(无网络)。
标准库 http.server 完全够用,好处是**双击就能跑,不用装任何东西**。

启动:
    python serve.py            # 默认 http://127.0.0.1:8765
    python serve.py --port 9000

流程:
    拖底片 → 填艺人/年代 → 贴歌词 → 逐句核对 → 一键出片
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))     # video-skill/
ROOT = HERE                                            # 工程根 = video-skill/
BIN = os.path.join(HERE, "bin")
sys.path.insert(0, BIN)
WEB = os.path.join(HERE, "web")
PROJECTS = os.path.join(ROOT, "projects", "web")
TMP = os.path.join(ROOT, "projects", "_web_upload")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import project_kit as K                                # noqa: E402

FFMPEG = K.FFMPEG

# 目标宽高:用户成品实测为 720×1280。底片宽度已是 720 时原样使用,
# 否则缩放到 720 宽(高度按比例并取偶),避免出现非标准分辨率。
TARGET_W = 720

JOBS = {}
JOBS_LOCK = threading.Lock()


# ------------------------------------------------------------ 工具
def jdump(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|]', "_", s or "")
    return s.strip()[:60] or "project"


def ascii_slug(s, fallback="project"):
    """生成 ASCII 安全的目录名。

    实测踩过:直接用中文标题当目录名,ffmpeg 会报
    「Illegal byte sequence」—— Windows 下非 ASCII 路径的经典坑。
    所以目录名一律转成 ASCII,中文信息保留在 spec.json 的「项目名」里。
    做法:ASCII 字母数字保留;中文字符拼到摘要上做一个短哈希,保证唯一可辨。
    """
    s = (s or "").strip()
    ascii_part = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:28]
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    tail = "%08x" % h
    if ascii_part:
        return "%s_%s" % (ascii_part.lower(), tail[:6])
    return "%s_%s" % (fallback, tail[:8])


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=K.CREATE_NO_WINDOW)
    return ((p.stdout or b"").decode("utf-8", "replace"),
            (p.stderr or b"").decode("utf-8", "replace"), p.returncode)


# ------------------------------------------------------------ multipart 解析
def parse_multipart(headers, rfile, out_dir, field_files):
    """流式解析 multipart/form-data。

    为什么要自己写:cgi 模块在 3.13 被移除,而且我们要**把上传的视频
    直接写盘**(视频几十 MB,不能整个读进内存)。
    做法:每读满一块就在缓冲区里找 boundary,找到就把前面的内容定下来。
    """
    ctype = headers.get("Content-Type", "")
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
    if not m:
        return None, None
    boundary = (m.group(1) or m.group(2)).strip().encode()
    delim = b"--" + boundary
    fields, files = {}, {}

    # 关键:必须限制总读取量。否则一旦 boundary 解析出偏差,
    # rfile.read() 会一直等后续数据(客户端在等响应),双方互等 -> 死锁。
    # 实测踩过:curl 与 urllib 都在上传后超时。
    try:
        clen = int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        clen = 0
    budget = clen if clen > 0 else None

    buf = b""
    state = "preamble"          # preamble -> headers -> body -> done
    cur_name, cur_file, cur_is_file = None, None, False
    fh = None

    def close_file():
        nonlocal fh, cur_file
        if fh:
            fh.close()
            fh = None
        cur_file = None

    while True:
        if budget is not None and budget <= 0:
            break
        chunk = rfile.read(64 * 1024 if budget is None else min(64 * 1024, budget))
        if not chunk:
            break
        if budget is not None:
            budget -= len(chunk)
        buf += chunk
        while True:
            if state == "preamble":
                idx = buf.find(delim)
                if idx < 0:
                    buf = buf[-len(delim):]
                    break
                buf = buf[idx + len(delim):]
                state = "headers"
                continue
            if state == "headers":
                idx = buf.find(b"\r\n\r\n")
                if idx < 0:
                    break
                head = buf[:idx].decode("utf-8", "replace")
                buf = buf[idx + 4:]
                cur_name, cur_file = None, None
                for line in head.split("\r\n"):
                    if line.lower().startswith("content-disposition"):
                        nm = re.search(r'name="([^"]*)"', line)
                        fn = re.search(r'filename="([^"]*)"', line)
                        if nm:
                            cur_name = nm.group(1)
                        if fn:
                            cur_file = fn.group(1)
                cur_is_file = bool(cur_file) and cur_name in field_files
                if cur_is_file:
                    safe = safe_name(os.path.splitext(cur_file)[0])
                    ext = os.path.splitext(cur_file)[1] or ".mp4"
                    path = os.path.join(out_dir, safe + ext)
                    fh = open(path, "wb")
                    files[cur_name] = path
                else:
                    fields[cur_name] = b""
                state = "body"
                continue
            # state == body:找下一个 boundary
            idx = buf.find(delim)
            if idx < 0:
                # 保留可能跨块的 boundary 前缀
                keep = len(delim) + 4
                if len(buf) > keep:
                    payload, buf = buf[:-keep], buf[-keep:]
                    if cur_is_file and fh:
                        fh.write(payload)
                    elif cur_name:
                        fields[cur_name] = fields.get(cur_name, b"") + payload
                break
            payload = buf[:idx]
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            if cur_is_file and fh:
                fh.write(payload)
            elif cur_name:
                fields[cur_name] = fields.get(cur_name, b"") + payload
            close_file()
            buf = buf[idx + len(delim):]
            if buf.startswith(b"--"):        # 结束
                state = "done"
                buf = b""
                break
            if buf.startswith(b"\r\n"):
                buf = buf[2:]
            state = "headers"
            continue
        if state == "done":
            break
    close_file()
    return ({k: v.decode("utf-8", "replace") for k, v in fields.items()}, files)


# ------------------------------------------------------------ 出片流程
def make_job():
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {"state": "pending", "percent": 0, "message": "排队中",
                     "log": "", "result": None}
    return jid


def set_job(jid, **kw):
    with JOBS_LOCK:
        JOBS[jid].update(kw)


def append_log(jid, text):
    with JOBS_LOCK:
        JOBS[jid]["log"] = (JOBS[jid].get("log") or "") + text


def do_generate(jid, video_path, title, lines):
    """后台线程:生成字幕 + 渲染成片。"""
    try:
        set_job(jid, state="running", percent=5, message="读取底片信息…")
        info = K.probe_media(video_path)
        if not info:
            raise RuntimeError("无法读取视频信息")
        dur = info["dur"] or 0
        if dur <= 0.5:
            raise RuntimeError("视频时长异常:%.2fs" % dur)

        pairs = [(K._s(l.get("en")), K._s(l.get("zh"))) for l in lines]
        pairs = [(a, b) for a, b in pairs if a or b]
        if not pairs:
            raise RuntimeError("没有可用的歌词行")
        no_zh = [a for a, b in pairs if a and not b]
        if no_zh:
            raise RuntimeError("还有 %d 句没填中文译文(第一句:%s)"
                               % (len(no_zh), no_zh[0][:50]))

        proj = ascii_slug(title or ("project_" + time.strftime("%m%d_%H%M")),
                          fallback="project")
        pdir = os.path.join(PROJECTS, proj)
        if os.path.isdir(pdir):
            proj = proj + "_" + time.strftime("%H%M%S")
            pdir = os.path.join(PROJECTS, proj)
        os.makedirs(os.path.join(pdir, "raw"), exist_ok=True)
        os.makedirs(os.path.join(pdir, "subs"), exist_ok=True)
        os.makedirs(os.path.join(pdir, "out"), exist_ok=True)

        # ---- 决定输出分辨率 ----
        src_w, src_h = info["w"], info["h"]
        if src_w == TARGET_W:
            out_w, out_h = src_w, src_h
            need_scale = False
        else:
            out_w = TARGET_W
            out_h = int(round(src_h * TARGET_W / float(src_w)))
            if out_h % 2:
                out_h += 1
            need_scale = True
        fps = 30

        set_job(jid, percent=15, message="准备底片(%dx%d)…" % (out_w, out_h))
        dst = os.path.join(pdir, "raw", "clean.mp4")
        if need_scale:
            _, err, code = run([FFMPEG, "-hide_banner", "-loglevel", "error",
                                "-y", "-i", video_path,
                                "-vf", "scale=%d:%d" % (out_w, out_h),
                                "-c:v", "libx264", "-preset", "veryfast",
                                "-crf", "18", "-pix_fmt", "yuv420p",
                                "-c:a", "copy", dst])
            if code != 0:      # 音轨复制失败就重编
                _, err, code = run([FFMPEG, "-hide_banner", "-loglevel", "error",
                                    "-y", "-i", video_path,
                                    "-vf", "scale=%d:%d" % (out_w, out_h),
                                    "-c:v", "libx264", "-preset", "veryfast",
                                    "-crf", "18", "-pix_fmt", "yuv420p",
                                    dst])
            if code != 0:
                raise RuntimeError("缩放底片失败:%s" % err[-400:])
        else:
            shutil.copy2(video_path, dst)

        # ---- 时间轴 + 字幕 ----
        set_job(jid, percent=30, message="分配时间轴并生成字幕…")
        times = K.allocate_timing(len(pairs), dur)
        srt_text = K.build_srt(pairs, times)
        srt_rel = "subs/bilingual.srt"
        with open(os.path.join(pdir, srt_rel), "w", encoding="utf-8") as fh:
            fh.write(srt_text)

        spec = K.build_spec(proj, "raw/clean.mp4", out_w, out_h, fps,
                            srt_rel, title, dur)
        with open(os.path.join(pdir, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False, indent=2)

        # ---- 渲染 ----
        set_job(jid, percent=40, message="渲染中(字幕+标题)…")
        asm = os.path.join(BIN, "assemble.py")
        out, err, code = run([sys.executable, asm, "--spec",
                             os.path.join(pdir, "spec.json")])
        append_log(jid, out + "\n" + err)
        if code != 0:
            raise RuntimeError("渲染失败(退出码 %s):%s" % (code, (err or out)[-400:]))
        # 把文字层几何写进日志,便于排查排版(标题/字幕的实际落点)
        for line in (out or "").splitlines():
            if "文字层几何" in line or line.strip().startswith(("字幕 y=", "标题 y=")):
                append_log(jid, "[几何] " + line.strip() + "\n")

        mp4 = os.path.join(pdir, "out", "%s.mp4" % proj)
        if not os.path.isfile(mp4):
            raise RuntimeError("渲染结束但没找到成片")
        out_srt = os.path.join(pdir, "out", "%s.srt" % proj)
        real = K.probe_media(mp4)

        set_job(jid, state="done", percent=100, message="完成",
                result={"video": os.path.relpath(mp4, ROOT),
                        "srt": os.path.relpath(
                            out_srt if os.path.isfile(out_srt)
                            else os.path.join(pdir, srt_rel), ROOT),
                        "dir": os.path.relpath(pdir, ROOT),
                        "duration": round(real["dur"], 2) if real else round(dur, 2),
                        "count": len(pairs),
                        "res": "%dx%d" % (out_w, out_h)})
    except Exception as exc:                                  # noqa: BLE001
        set_job(jid, state="error", message=str(exc), percent=0)


# ------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "LyricsStudio/1.0"

    def log_message(self, fmt, *args):
        pass                                    # 静音,别刷屏

    # ---------- 响应工具 ----------
    def read_fields(self):
        """统一读取请求字段,同时支持两种 Content-Type。

        为什么需要:浏览器用 FormData 发 multipart,而脚本/测试常发 JSON。
        早期版本只认 multipart,脚本调用时报"数据格式错误"或 KeyError,
        排查起来很费时间。统一在这里兼容。
        返回 (fields_dict, files_dict)
        """
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                return (data if isinstance(data, dict) else {}), {}
            except (ValueError, OSError):
                return {}, {}
        return parse_multipart(self.headers, self.rfile, TMP, set())

    def send_json(self, obj, code=200):
        body = jdump(obj)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # ---------- GET ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            f = os.path.join(WEB, "index.html")
            if not os.path.isfile(f):
                return self.send_json({"error": "缺少 web/index.html"}, 500)
            with open(f, "rb") as fh:
                return self.send_bytes(fh.read(), "text/html; charset=utf-8")
        if p == "/api/ping":
            # 给前端探活用:页面打开时先确认后端是否在跑。
            # 存在的意义:服务停掉后前端只会显示 "Failed to fetch",
            # 信息量太低,用户不知道该干什么。
            return self.send_json({"ok": True, "service": "lyrics-studio"})
        if p == "/api/job":
            jid = urllib.parse.parse_qs(u.query).get("id", [""])[0]
            with JOBS_LOCK:
                j = JOBS.get(jid)
            if not j:
                return self.send_json({"error": "job 不存在"}, 404)
            return self.send_json(dict(j))
        if p.startswith("/api/job/"):
            jid = p.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                j = JOBS.get(jid)
            if not j:
                return self.send_json({"error": "job 不存在"}, 404)
            return self.send_json(j)
        if p == "/api/file":
            q = urllib.parse.parse_qs(u.query)
            rel = q.get("name", [""])[0]
            full = os.path.normpath(os.path.join(ROOT, rel))
            # 路径必须落在工程根目录内(加分隔符,防止前缀绕过)
            guard = os.path.normpath(ROOT) + os.sep
            if not (full + os.sep).startswith(guard) and full != os.path.normpath(ROOT):
                return self.send_json({"error": "路径非法"}, 403)
            if not os.path.isfile(full):
                return self.send_json({"error": "文件不存在"}, 404)
            ctype = ("video/mp4" if q.get("type", ["mp4"])[0] == "mp4"
                     else "text/plain; charset=utf-8")
            extra = {}
            if q.get("dl"):
                extra["Content-Disposition"] = 'attachment; filename="%s"' \
                    % os.path.basename(full)
            with open(full, "rb") as fh:
                return self.send_bytes(fh.read(), ctype, extra=extra)
        return self.send_json({"error": "not found"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        try:
            self._do_post()
        except Exception as exc:                              # noqa: BLE001
            import traceback
            print("[POST %s] 未捕获异常:%s" % (self.path, exc), flush=True)
            traceback.print_exc()
            try:
                self.send_json({"error": "服务器内部错误:%s" % exc}, 500)
            except Exception:                                 # noqa: BLE001
                pass

    def _do_post(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/upload":
            dbg = os.environ.get("LYRICS_DEBUG")
            if dbg:
                print("[upload] 进入,CT=%s,CL=%s"
                      % (self.headers.get("Content-Type", "")[:70],
                         self.headers.get("Content-Length")), flush=True)
            os.makedirs(TMP, exist_ok=True)
            fields, files = parse_multipart(self.headers, self.rfile,
                                            TMP, {"video"})
            if dbg:
                print("[upload] 解析完:fields=%s files=%s"
                      % (list((fields or {}).keys()),
                         {k: os.path.basename(v)
                          for k, v in (files or {}).items()}), flush=True)
            if not files:
                return self.send_json({"error": "没有收到视频文件"})
            path = list(files.values())[0]
            info = K.probe_media(path)
            if dbg:
                print("[upload] 探测完:%s" % (info,), flush=True)
            if not info:
                return self.send_json({"error": "无法读取视频,确认是视频文件"})
            return self.send_json({
                "name": os.path.basename(path), "path": path,
                "width": info["w"], "height": info["h"],
                "duration": round(info["dur"] or 0, 2)})

        if u.path == "/api/parse":
            fields, _ = self.read_fields()
            f = fields or {}
            # 同时接受 lyrics / lines 两个字段名(前端与脚本习惯不同)
            text = K._s(f.get("lyrics")) or K._s(f.get("lines")) or K._s(f.get("text"))
            if not text:
                return self.send_json(
                    {"error": "没有收到歌词内容(字段名用 lyrics 或 lines)"})
            pairs = K.parse_lyrics_block(text)
            raw = [{"en": a, "zh": b} for a, b in pairs]
            try:
                import translate_assist as TA
                analyzed = TA.analyze(raw)
            except Exception as exc:                       # noqa: BLE001
                print("[parse] translate_assist 失败:%s" % exc, flush=True)
                analyzed = [dict(r, 来源="需翻译", 规则=[], 提示=[],
                                 需要人工=True) for r in raw]
            return self.send_json({"lines": analyzed})

        if u.path == "/api/translate":
            # 用 DeepSeek API 按配方翻译(推理模型,可能要十几秒)
            fields, _ = self.read_fields()
            raw = (fields or {}).get("lines", "[]")
            model = (fields or {}).get("model") or "deepseek-v4-pro"
            if isinstance(raw, str):
                try:
                    items = json.loads(raw)
                except ValueError:
                    return self.send_json({"error": "数据格式错误"})
            else:
                items = raw
            if not isinstance(items, list) or not items:
                return self.send_json({"error": "没有收到待译内容"})
            pairs = [(K._s(i.get("en")), K._s(i.get("zh")))
                     for i in items if isinstance(i, dict)]
            try:
                import llm_translate as LT
            except Exception as exc:                       # noqa: BLE001
                return self.send_json({"error": "加载翻译模块失败:%s" % exc})
            if not LT.load_key():
                return self.send_json(
                    {"error": "没有 API key。请把 key 写入 video-skill/.deepseek_key"})
            res, stat = LT.translate_lines(pairs, model=model)
            return self.send_json({"lines": res, "stat": stat, "model": model})

        if u.path == "/api/corpus":
            # 让用户在网页里补语料 —— 越用越准的关键入口。
            fields, _ = self.read_fields()
            text = K._s((fields or {}).get("lines"))
            pairs = K.parse_lyrics_block(text)
            added, skipped = [], 0
            cdir = os.path.join(ROOT, "profiles", "_语料")
            os.makedirs(cdir, exist_ok=True)
            cpath = os.path.join(cdir, "用户补充.json")
            data = {"名称": "用户补充", "对照": []}
            if os.path.isfile(cpath):
                try:
                    with open(cpath, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    pass
            known = {(c.get("英文") or "").strip().lower()
                     for c in data.get("对照", [])}
            for en, zh in pairs:
                if not en or not zh:
                    skipped += 1
                    continue
                if en.strip().lower() in known:
                    skipped += 1
                    continue
                data["对照"].append({"英文": en, "中文": zh})
                added.append(en)
                known.add(en.strip().lower())
            with open(cpath, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            # 语料变了,清掉缓存下次重载
            try:
                import translate_assist as TA
                TA._corpus_cache = None
            except Exception:                              # noqa: BLE001
                pass
            return self.send_json({"added": len(added), "skipped": skipped,
                                   "total": len(data["对照"]),
                                   "file": os.path.relpath(cpath, ROOT)})


        if u.path == "/api/check":
            # 规则体检:按配方 v4 检查译文是否违规
            fields, _ = self.read_fields()
            raw = (fields or {}).get("lines", [])
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    return self.send_json({"error": "数据格式错误"})
            if not isinstance(raw, list) or not raw:
                return self.send_json({"error": "没有可检查的内容"})
            pairs = [(K._s(i.get("en")), K._s(i.get("zh")))
                     for i in raw if isinstance(i, dict)]
            try:
                import check_rules as CR
            except Exception as exc:                       # noqa: BLE001
                return self.send_json({"error": "加载体检模块失败:%s" % exc})
            issues, persons = CR.check(pairs)
            try:
                fixed = CR.auto_fix(pairs)
            except Exception:                              # noqa: BLE001
                fixed = pairs
            sev = [x for x in issues if x[0] == "严重"]
            return self.send_json({
                "issues": [{"级别": a, "句": b, "说明": c} for a, b, c in issues],
                "严重": len(sev), "总问题": len(issues),
                "第一人称": persons,
                "fixed": [{"en": a, "zh": b} for a, b in fixed],
            })

        if u.path == "/api/generate":
            fields, _ = self.read_fields()
            title = K._s((fields or {}).get("title"))
            raw = (fields or {}).get("lines", "[]")
            # JSON 请求时 raw 已经是 list;multipart 时是字符串。两种都要认。
            # 实测踩过:只处理字符串会在 JSON 调用时抛 TypeError,
            # 表现为"服务端直接断连、前端看不到错误"。
            if isinstance(raw, str):
                try:
                    lines = json.loads(raw)
                except ValueError:
                    return self.send_json({"error": "歌词数据格式错误"})
            elif isinstance(raw, list):
                lines = raw
            else:
                return self.send_json({"error": "歌词数据格式错误"})
            if not isinstance(lines, list) or not lines:
                return self.send_json({"error": "没有收到歌词"})
            if not title:
                return self.send_json({"error": "标题为空"})
            # 找最近一次上传的底片
            vids = []
            if os.path.isdir(TMP):
                vids = [os.path.join(TMP, f) for f in os.listdir(TMP)
                        if f.lower().endswith((".mp4", ".mov", ".mkv"))]
            if not vids:
                return self.send_json({"error": "还没上传底片"})
            video = max(vids, key=os.path.getmtime)
            jid = make_job()
            t = threading.Thread(target=do_generate,
                                 args=(jid, video, title, lines), daemon=True)
            t.start()
            return self.send_json({"job": jid})

        return self.send_json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    os.makedirs(PROJECTS, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)
    if not os.path.isfile(os.path.join(WEB, "index.html")):
        print("缺少 web/index.html")
        return 2
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d" % (args.host, args.port)
    print("=" * 54)
    print("  歌词翻译成片工具已启动")
    print("  请在浏览器打开:%s" % url)
    print("  工程输出目录:%s" % PROJECTS)
    print("  按 Ctrl+C 停止")
    print("=" * 54)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
