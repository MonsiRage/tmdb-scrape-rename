# TMDB 电影命名刮削工具

一个 Windows 下的本地影片整理工具：识别文件夹 / 视频文件名，去 [TMDB](https://www.themoviedb.org/) 查询，把影片文件夹统一重命名为

```
片名 (年份) [tmdbid=编号]
```

并为 Emby 等媒体库补齐海报、背景图、logo 和 nfo。

## 功能

- **识别影片**：按以下顺序确定 TMDB 编号——文件夹名里的 `[tmdbid=N]` → 纯数字文件夹名（排除 1900–2099 的年份）→ 名字里其他类似编号的标记 → 已有 nfo 里的 tmdb uniqueid → 用清洗后的标题 + 年份搜索 TMDB。多个候选又没有年份时跳过，不瞎猜。
- **重命名**：只重命名最底层的影片文件夹；系列 / 合集父文件夹只会深入扫描，不会被改名。蓝光 / DVD 原盘（BDMV / VIDEO_TS）和 `.iso` 视为一个整体。
- **整理散落视频**：根目录下零散的视频会先各自放进独立文件夹（上/下、CD1/CD2 等分段合并到同一个文件夹）。
- **电影 + 剧集**：GUI 每次同时搜索电影与剧集，只命中一边的进入对应标签页，冲突或刮削不到的进入「未能匹配」。
- **下载图片（仅当 TMDB 上有时）**：`poster.jpg`（海报）、`fanart.jpg`（背景）、`clearlogo.png`（logo）。不会伪造 folder.jpg / banner / landscape 等 TMDB 没有的图。
- **写 Emby nfo**：文件夹里**还没有任何 nfo** 时才写（命名为 `视频文件名.nfo`，没有视频时为 `movie.nfo`）；已有的 nfo 绝不覆盖或删除。
- **离线网页快照**：在每个影片文件夹生成 `tmdb.html`，一个类似 TMDB 详情页的离线页面（海报、logo、标题、简介、类型、片长、导演、制片、国家、系列、演员、TMDB / IMDb 链接），图片引用同文件夹里的 poster / fanart / clearlogo。
- **预览 / 正式两种模式**：预览模式只显示计划（改名、下载、写 nfo、写网页），不改任何文件；正式模式才真正执行。

## 使用方法

### 1. GUI（推荐）

```
pythonw tmdb_刮削命名.pyw
```

选择扫描根目录 → 点「仅预览（不改名、不写文件）」或「确认刮削（正式更改）」。结果按「概览 / 电影识别 / 剧集识别 / 未能匹配 / 更改」分标签显示。

不想装 Python 的话，可以去 [Releases](../../releases) 页面下载打包好的 Windows exe（`TMDB-scrape-rename.exe`）。exe 内置引擎，修改旁边的 .py 不会生效；同样需要在 exe 同目录放 `tmdb_api_key.txt` 或设置环境变量 `TMDB_API_KEY`。

### 2. bat 一键运行

把 `tmdb_命名预览.bat` / `tmdb_命名.bat` 和 `tmdb_format_rename.py` 放进要整理的影片根目录，双击：

- `tmdb_命名预览.bat`：只预览，不改文件
- `tmdb_命名.bat`：正式改名 + 下载图片 + 写 nfo + 生成 tmdb.html

### 3. 命令行

```
python tmdb_format_rename.py "D:\影片"              # 正式执行
python tmdb_format_rename.py "D:\影片" --preview    # 仅预览
python tmdb_format_rename.py "D:\影片" --no-poster --no-nfo
python tmdb_format_rename.py "D:\影片" --media=tv   # 按剧集处理（也可 --media=movie / auto）
```

## 环境要求

- Windows + Python 3（建议 3.9 及以上）
- **无需 pip 安装任何第三方包**：只用标准库（`urllib`、`json`、`tkinter` 等）。GUI 需要 Python 自带的 tkinter。
- 自己打包 exe 时才需要 `pip install pyinstaller`，然后 `pyinstaller TMDB刮削命名.spec`。

## TMDB API key（必需）

程序**不内置 key**，请使用你自己的 TMDB API key（在 TMDB 账号设置 → API 里免费申请，使用 v3 的 API Key）：

1. 在脚本（或 exe）**同一目录**新建 `tmdb_api_key.txt`，第一行写入你的 key；或者
2. 设置环境变量 `TMDB_API_KEY`。

⚠️ `tmdb_api_key.txt` 已写入 `.gitignore`，**千万不要提交到仓库或分享出去**。

## 生成的文件

运行时会在脚本目录生成若干 `*.json` 缓存 / 日志（搜索缓存、上次结果等），均已被 `.gitignore` 忽略。
