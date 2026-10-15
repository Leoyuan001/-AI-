# Lyrics Studio · 歌词翻译成片工具

把英文歌词翻译成中文,做成**中英双语字幕**烧进视频,并按公式加上顶部标题,一键出片。

面向做「复古画面 + 当代说唱」类内容的创作者:你不做翻唱,只写字幕 —— 这个工具把字幕这一环自动化。

---

## 它解决什么问题

手工流程是:识别原视频语音 → 逐句转字幕 → 搜歌词校正英文 → **把英文译成中文** → 写标题 → 导出。

其中**翻译**最费时间,而且每个人的译法都不一样。本工具的做法是:

1. **先学你的译法** —— 把你已发作品的英中对照喂进去,它记住
2. **三层翻译** —— 见过的句子直接复用你的原译;词条按你的术语表;剩下的交给 LLM,并带上你的风格规则
3. **自动体检** —— 出片前按你的规则检查译文,把违规项列出来

**核心思路**:确定性的活(渲染、排版、字幕合成)交给代码;
需要判断的活(翻译)交给 LLM + 你的规则;你只做验收。

---

## 快速开始

### 环境要求

| 依赖 | 说明 |
| --- | --- |
| Python 3.9+ | 只用标准库做 Web 服务,无第三方 Web 框架 |
| ffmpeg / ffprobe | 必须在 PATH 里(**核心依赖**) |
| Pillow + numpy | 渲染字幕与标题图片 |
| 中文字体 | Windows 自带 `msyhbd.ttc` 即可;其它系统见下方配置 |

```bash
pip install pillow numpy
```

ffmpeg 安装:Windows 用 `choco install ffmpeg` 或从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载后加进 PATH。

### 启动

```bash
# Windows:直接双击
启动网页工具.bat

# 或者手动
python serve.py --port 8770
```

浏览器会自动打开 <http://127.0.0.1:8770>

### 使用

1. **拖底片进去** —— 已经剪好、没有字幕的干净视频
2. **填艺人名 + 年代** —— 标题按公式自动生成(`如果{艺人}出生于{年代}s`),也可手写
3. **贴歌词** —— 纯英文、中英交错、Tab 分隔、SRT 都能识别
4. **点「解析歌词」** —— 自动切句,并标出哪些能复用你的旧译文
5. **点「AI 翻译」** —— 没译过的句子交给 LLM(需配 API key)
6. **点「规则体检」** —— 检查译文是否违反你的风格规则;机械问题可一键修正
7. **点「一键出片」** —— 渲染成 mp4,网页内预览 + 下载

---

## 配置

### API key(可选,不配就只能手动译或用旧译文)

写进项目根目录的 `.deepseek_key`(单行),或设环境变量 `DEEPSEEK_API_KEY`。

支持 `deepseek-v4-pro`(更准)与 `deepseek-flash`(更快)。

> ⚠️ `.deepseek_key` 已在 `.gitignore` 里,**不要把它提交上去**。

### 你的风格规则(最重要)

```bash
cp profiles/style_rules.template.txt profiles/style_rules.txt
```

编辑 `style_rules.txt`,按你的译法写规则。**写得越具体、越带反例,LLM 越像你。**

不写也行 —— 程序会用一份通用兜底规则,只是译文不会带个人风格。

### 你的术语表

```bash
cp profiles/glossary.template.txt profiles/glossary.txt
```

格式是一行一条 `英文=中文`。命中词条的句子,**LLM 必须用你指定的译法**。

### 你的语料(让工具越用越准)

把已核对好的英中对照放进 `profiles/_语料/`,或者直接在网页歌词区下方的
「把它学到的也存下来」里贴。之后遇到同样的句子会**直接复用你的原译,不调 API**。

语料 json 格式:

```json
{
  "对照": [
    {"英文": "some english line", "中文": "某句中文译文"}
  ]
}
```

### 排版参数

`profiles/layout-spec.md` 记录了标题框位置、字号、描边、字幕底距等参数,
对应 `bin/project_kit.py` 顶部的 `LAYOUT` 字典。改那里就能调版式。

**默认基准分辨率 720×1280**,其它分辨率会按比例缩放。

---

## 命令行用法

不想开网页也能用:

```bash
# 单条视频出片
python bin/assemble.py --spec <项目>/spec.json --check   # 先校验
python bin/assemble.py --spec <项目>/spec.json           # 再渲染

# 给译文做规则体检
python bin/check_rules.py --srt <字幕.srt>
python bin/check_rules.py --srt <字幕.srt> --fix         # 顺带自动修正

# 翻译一批英文歌词
python bin/llm_translate.py --lines <英文.txt> --out out.json

# 评测:拿你的语料当考题,量出模型有多像你
python bin/llm_translate.py --eval --limit 20

# 从成品视频里量排版参数
python bin/measure_layout.py --frames <帧目录>

# 从成品视频里读字幕(没有原始字幕文件时)
python bin/extract_subs.py --dir samples --out distill/_read
```

---

## 架构

```
serve.py + web/index.html      本地网页(零依赖,标准库 http.server)
        ↓
bin/project_kit.py             共用构建器:排版参数 / 标题公式 / 歌词解析 / 术语表
        ↓
bin/llm_translate.py           三层翻译:语料召回 → 术语表 → LLM
bin/translate_assist.py        无 API 时的降级:语料召回 + 术语骨架 + 规则提示
bin/check_rules.py             规则体检
        ↓
bin/assemble.py                按 spec.json 渲染成片
```

**关键设计:配方与渲染器之间隔一层 `spec.json`。**
风格规则怎么改都不用动渲染器。

### 翻译的三层优先级

| 顺序 | 机制 | 可靠性 | 花不花 API 钱 |
| --- | --- | --- | --- |
| ① | 语料召回(复用你以前的译文) | 最高 | 不花 |
| ② | 术语表替换 | 中 | 不花 |
| ③ | LLM 按风格规则翻译 | 取决于规则质量 | 花 |

---

## 已知限制

| 限制 | 原因 | 应对 |
| --- | --- | --- |
| 不能在线使用 | 要跑 ffmpeg、读写本地文件 | 只能在本机跑 |
| 时间轴是均分的 | 歌曲音频全程无静音,静音检测切不出句子边界 | 在剪辑软件里微调,或手改 SRT |
| LLM 不保证守规则 | 模型行为有随机性 | **出片前跑 `check_rules.py`** |
| 术语表只给词级提示 | 整句翻译需要判断力 | 术语表 ≠ 完整翻译 |
| 推理模型要留足 token | deepseek 带 `reasoning_content`,推理也吃预算 | 代码里已按每句 ≥400 tokens 给 |

---

## 版权提醒

本工具**只处理你自己提供的素材与文本**。

- 翻译他人词曲属于**演绎作品**,原著作权仍在词曲作者/唱片公司
- 翻唱、搬运、二次发布可能触发平台的版权投诉
- 请自行确认你有权使用所处理的素材

工具本身不附带任何歌词、音频或视频内容。

---

## 许可

MIT
