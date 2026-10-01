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
- **电影 + 剧集**：GUI 每次同时搜索电影与剧集，只命中一边的进入对应标签页，冲突或刮削不到的进入「需要确认」。
- **下载图片（仅当 TMDB 上有时）**：`poster.jpg`（海报）、`fanart.jpg`（背景）、`clearlogo.png`（logo）。不会伪造 folder.jpg / banner / landscape 等 TMDB 没有的图。
- **写 Emby nfo**：文件夹里**还没有任何 nfo** 时才写（命名为 `视频文件名.nfo`，没有视频时为 `movie.nfo`）；已有的 nfo 绝不覆盖或删除。
- **离线网页快照**：在每个影片文件夹生成 `tmdb.html`，一个类似 TMDB 详情页的离线页面（海报、logo、标题、简介、类型、片长、导演、制片、国家、系列、演员、TMDB / IMDb 链接），图片引用同文件夹里的 poster / fanart / clearlogo。
- **同名电影/多版本**：同一部电影有多个文件夹时，不同版本（Extended、4K、Remux 等，按文件名和视频头里的分辨率判断）合并成一个文件夹，视频改名为 `片名 (年份) - 4K Remux.mkv`；看起来完全一样的列为「重复影片」，写明分辨率、片长、大小，由你决定保留哪个。
- **合并季文件夹**（默认开启，`--no-merge-seasons` 关闭）：同一部剧各季的独立文件夹并入 `剧名/Season NN`；看不出第几季或两个文件夹是同一季时整组不动，列进「需要确认」。
- **剧集集数检查**：本地的季数/集数超出所选剧集时，如果有同名剧集吻合，就列进「需要确认」。
- **标题语言 / 命名格式**：首页「命名与语言」可选标题语言（自动/繁体/英文）和文件夹格式（二选一：`标题 (年份) [tmdbid=编号]` 或 `原名 (年份) [tmdbid=编号]`）；命令行 `--title-lang=en`、`--name-template="{original} ({year}) [tmdbid={tid}]"`。
- **撤销上次改名**：每次正式更改都会记录，首页或结果页的「撤销上次改名」（命令行 `--undo`）可还原。「导出表格」把结果存成 CSV。
- **不确定的匹配默认不改名**：文件夹名没写年份、没有 `[tmdbid=]` / nfo，纯靠片名搜出来时，只要还有别的同名候选（或片名不完全一致），就列进「需要确认」并写明候选，不改名。确认没问题后勾选「同时改名不确定的」（命令行 `--accept-uncertain`）再跑。
- **出错不冒充“没结果”**：开跑前先验证 API Key；搜索时遇到网络 / API 错误会标为「搜索失败」，不会当成「搜索无结果」，也不写进搜索缓存，重跑即可。
- **预览 / 正式两种模式**：预览模式只显示计划（改名、下载、写 nfo、写网页），不改任何文件；正式模式才真正执行。
- **只刮削新添加的**：首页默认勾选。名字里已经有 `[tmdbid=编号]` 的文件夹直接跳过，不联网、不改名、不补图。取消勾选才会把整库再检查一遍。新片子请放成新文件夹，不要丢进已经带编号的旧文件夹。

## 使用方法

### 1. GUI（推荐）

```
pythonw tmdb_刮削命名.pyw
```

选择扫描根目录 → 点「仅预览（不改名、不写文件）」或「确认刮削（正式更改）」。结果按「概览 / 电影识别 / 剧集识别 / 需要确认 / 更改」分标签显示。

不想装 Python 的话，可以去 [Releases](../../releases) 页面下载打包好的 Windows exe（`TMDB-scrape-rename.exe`）。exe 内置引擎，修改旁边的 .py 不会生效。API Key 和 JSON 缓存在 `%AppData%\Roaming\TMDB刮削命名`，也可以用环境变量 `TMDB_API_KEY`。

### 2. 命令行

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

程序**不内置 key**（找不到 key 时会直接报错退出），请使用你自己的 TMDB API key（在 TMDB 账号设置 → API 里免费申请，使用 v3 的 API Key）：

1. 在程序首页的 **TMDB API Key** 输入框里粘贴。点「仅预览」或「确认刮削」时写入 `%AppData%\Roaming\TMDB刮削命名\tmdb_api_key.txt`，不会写进日志；已有该文件会自动填入。或者
2. 直接在那个目录新建 `tmdb_api_key.txt`，第一行写入你的 key；或者
3. 设置环境变量 `TMDB_API_KEY`。

以前放在 exe 旁边的 `tmdb_api_key.txt` 和 JSON 会在下次启动时挪进上述目录。

⚠️ `tmdb_api_key.txt` 已写入 `.gitignore`，**千万不要提交到仓库或分享出去**。

## 生成的文件

运行时在 `%AppData%\Roaming\TMDB刮削命名` 生成 JSON 缓存 / 日志（搜索缓存、上次结果等），不放在 exe 旁边。海报、nfo、`tmdb.html` 仍写在各影片文件夹里。
