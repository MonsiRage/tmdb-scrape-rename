# -*- coding: utf-8 -*-
"""TMDB 刮削命名 — 先选电影/剧集，再选预览或确认刮削"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk


def app_dir() -> Path:
    me = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    try:
        return me.resolve().parent
    except OSError:
        # e.g. the exe sits on a network / NAS drive Windows cannot resolve (WinError 1005)
        return Path(os.path.abspath(me)).parent


def find_tools_dir() -> Path:
    here = app_dir()
    if (here / "tmdb_format_rename.py").is_file():
        return here
    return here


def default_root(tools: Path) -> Path:
    name = tools.name
    if any(k in name for k in ("TMDB", "刮削", "tmdb")):
        parent = tools.parent
        if str(parent) not in ("", "."):
            return parent
    cwd = Path.cwd()
    return cwd if cwd != tools else tools


def normalize_root(raw: str) -> Path:
    s = (raw or "").strip().strip('"')
    if len(s) == 2 and s[1] == ":":
        s = s + "\\"
    return Path(s)


def find_python_for_script() -> str:
    """Only for non-frozen .pyw. Frozen exe must NOT use system Python."""
    if getattr(sys, "frozen", False):
        return sys.executable or ""
    return sys.executable or "python"


def engine_importable() -> bool:
    """True if tmdb_format_rename can be imported (sibling .py or frozen bundle)."""
    try:
        import importlib
        importlib.import_module("tmdb_format_rename")
        return True
    except Exception:
        return False


def engine_ready(script: Path) -> bool:
    """Frozen exe uses the bundled module. A loose .py is only for the .pyw."""
    if getattr(sys, "frozen", False):
        return engine_importable()
    return script.is_file() or engine_importable()


def data_dir() -> Path:
    """Same folder the engine uses: %AppData%\\Roaming\\TMDB刮削命名."""
    try:
        import tmdb_format_rename as eng
        return Path(eng.TOOLS)
    except Exception:
        base = (os.environ.get("APPDATA") or "").strip() or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "TMDB刮削命名"


def read_saved_api_key() -> str:
    """Key already stored in Roaming, else the environment."""
    path = data_dir() / "tmdb_api_key.txt"
    try:
        if path.is_file():
            v = path.read_text(encoding="utf-8", errors="ignore").strip()
            if v and not v.startswith("#"):
                line = v.splitlines()[0].strip()
                if line:
                    return line
    except Exception:
        pass
    return (os.environ.get("TMDB_API_KEY") or "").strip()


def last_history() -> dict | None:
    """The newest change-history file of a real run (what 撤销上次改名 would undo)."""
    d = data_dir() / "history"
    try:
        files = sorted(d.glob("*.json"))
    except Exception:
        return None
    for f in reversed(files):
        try:
            h = json.loads(f.read_text(encoding="utf-8"))
            if h.get("ops"):
                return h
        except Exception:
            continue
    return None


class _LineWriter:
    """Capture engine prints into GUI log line-by-line."""

    def __init__(self, emit):
        self._emit = emit
        self._buf = ""
        self._lock = threading.Lock()  # the engine prints from several worker threads

    def write(self, s):
        if not s:
            return 0
        lines = []
        with self._lock:
            self._buf += str(s)
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                lines.append(line)
        for line in lines:
            self._emit(line)
        return len(s)

    def flush(self):
        with self._lock:
            rest, self._buf = self._buf, ""
        if rest:
            self._emit(rest)


REASON_ZH = {
    "already_ok": "已经是正确名字",
    "dest_exists": "目标名已存在",
    "no_tmdb_id": "找不到TMDB编号",
    "no_results": "搜索无结果",
    "ambiguous_no_year": "多个候选且无年份",
    "ambiguous_movie_and_tv": "电影和剧集都匹配到了（请确认）",
    "search_error": "搜索出错",
    "uncertain_title_only": "不确定：需要确认（未改名）",
    "duplicate_movie": "重复影片（请决定保留哪个）",
    "season_merge_unclear": "同一部剧的多个文件夹无法合并（请确认）",
    "empty_query": "查询为空",
    "no_usable_title": "没有可用标题",
    "rename_failed": "改名失败",
    "skip_no_tmdb": "刮不到，未整理",
    "multidisc_merge": "多碟合并到同一文件夹",
    "multidisc_under_parent": "多碟保留在父文件夹内",
    "already_ok_merge_siblings": "名字已正确，合并其余碟片",
}


def zh_reason(code: str) -> str:
    return REASON_ZH.get(code or "", code or "未知")



def guess_lib_kind(item: dict, run_media: str = "movie") -> str:
    """Classify as movie/tv for result tabs.

    Do NOT trust user folder names like 电影/电视剧 — those are manual and may be wrong.
    Prefer per-item media stamped by the scraper; else TMDB id kind; else this run mode.
    """
    if not isinstance(item, dict):
        return "tv" if run_media == "tv" else "movie"
    for key in ("media", "media_kind", "kind_media", "media_hint"):
        v = (item.get(key) or "").strip().lower()
        if v in ("tv", "show", "series", "剧集"):
            return "tv"
        if v in ("movie", "film", "电影"):
            return "movie"
    ik = (item.get("id_kind") or item.get("tmdb_kind") or "").strip().lower()
    if ik in ("tv", "show", "series"):
        return "tv"
    if ik in ("movie", "film"):
        return "movie"
    # Explicit opposite-type flags from scraper
    if item.get("is_tv") is True:
        return "tv"
    if item.get("is_movie") is True:
        return "movie"
    # Classify by TMDB id when wrap/plan rows omit media (auto mode).
    tid = str(item.get("tmdb") or item.get("id") or "").strip()
    if tid.isdigit():
        try:
            # Prefer engine helper if importable (side-loaded next to exe).
            from tmdb_format_rename import classify_tmdb_id_kind as _clf
            k = _clf(tid, str(item.get("stem") or item.get("name") or item.get("folder") or item.get("title") or ""))
            if k in ("tv", "movie"):
                return k
        except Exception:
            pass
    return "tv" if run_media == "tv" else "movie"



class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("TMDB 刮削命名")
        self.geometry("760x600")
        self.minsize(680, 560)
        self.tools = find_tools_dir()
        self.script = self.tools / "tmdb_format_rename.py"
        self.proc = None
        self._scan_root: Path | None = None
        self._last_was_preview = False
        self._media = "auto"
        self._only_new = True
        self._build_chooser()
        self._stop_flag = False
        if not engine_ready(self.script):
            messagebox.showerror(
                "缺少引擎",
                "找不到 tmdb_format_rename。\n"
                "请使用 TMDB刮削命名.exe，或把 tmdb_format_rename.py 放在程序同目录。\n"
                f"目录: {self.tools}",
            )

    def _build_chooser(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)
        ttk.Label(top, text="TMDB 刮削命名", font=("Microsoft YaHei UI", 14, "bold")).pack(
            anchor="w", pady=(6, 2)
        )
        ttk.Label(
            top,
            text="① 选扫描目录  →  ② 预览或确认刮削（电影+剧集双搜，冲突进失败栏）",
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(0, 12))

        row = ttk.Frame(top)
        row.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(row, text="扫描根目录").pack(side=tk.LEFT)
        self.root_var = tk.StringVar(value=str(default_root(self.tools)))
        ttk.Entry(row, textvariable=self.root_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(row, text="浏览…", command=self._browse).pack(side=tk.LEFT)

        scope = ttk.LabelFrame(top, text="刮削范围")
        scope.pack(fill=tk.X, pady=(0, 10))
        self.only_new_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            scope,
            text="只刮削新添加的",
            variable=self.only_new_var,
        ).pack(anchor="w", padx=8, pady=(6, 0))
        ttk.Label(
            scope,
            text="勾选后，跳过名字里已有 [tmdbid=] 的文件夹。取消勾选，则整库重新检查。",
        ).pack(anchor="w", padx=28, pady=(0, 4))
        ttk.Label(
            scope,
            text="标题对不上、没写年份又有同名候选、或剧集/电影类型对不上的，默认不改名，列在「需要确认」里并写明原因和候选。在那个标签里双击一行，选对的那个再跑。",
        ).pack(anchor="w", padx=28, pady=(0, 8))

        fmt = ttk.LabelFrame(top, text="自动整理")
        fmt.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(
            fmt,
            text="同一部剧各季的独立文件夹会自动合并到「剧名/Season NN」；同一部电影的不同版本会合并并加版本标记。预览里逐条列出，改错可「撤销上次改名」。",
            foreground="#666", wraplength=700, justify=tk.LEFT,
        ).pack(anchor="w", padx=8, pady=(0, 6))

        key_row = ttk.Frame(top)
        key_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(key_row, text="TMDB API Key").pack(side=tk.LEFT)
        self.api_key_var = tk.StringVar(value=read_saved_api_key())
        self._api_key_entry = tk.Entry(key_row, textvariable=self.api_key_var, show="*")
        self._api_key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self._show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            key_row, text="显示", variable=self._show_key, command=self._toggle_api_key
        ).pack(side=tk.LEFT)
        ttk.Label(
            top,
            text="API Key 和 JSON 缓存在 %AppData%\\Roaming\\TMDB刮削命名，不会写进日志，也不会放在 exe 旁边。",
            foreground="#666",
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(
            top,
            text="每次同时搜索电影与剧集：只命中一边进对应标签；重复或刮削不到的都进「需要确认」（说明写在行内）。",
            foreground="#666",
        ).pack(anchor="w", pady=(0, 8))

        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, pady=(4, 8))
        ttk.Button(
            btns, text="仅预览（不改名、不写文件）", command=lambda: self._choose(True)
        ).pack(side=tk.LEFT, padx=(0, 12), ipadx=10, ipady=8)
        ttk.Button(
            btns, text="确认刮削（正式更改）", command=lambda: self._choose(False)
        ).pack(side=tk.LEFT, padx=(0, 12), ipadx=10, ipady=8)
        ttk.Button(btns, text="退出", command=self.destroy).pack(side=tk.RIGHT, ipadx=10, ipady=8)
        ttk.Button(btns, text="撤销上次改名", command=self._undo).pack(side=tk.RIGHT, padx=(0, 12), ipadx=10, ipady=8)
        ttk.Label(top, text=f"数据目录: {data_dir()}", foreground="#666").pack(
            anchor="w", pady=(10, 0)
        )

    def _browse(self) -> None:
        path = filedialog.askdirectory(initialdir=self.root_var.get() or str(Path.home()))
        if path:
            self.root_var.set(path)

    def _sync_only_new(self) -> None:
        var = getattr(self, "only_new_var", None)
        if var is None:
            return
        try:
            self._only_new = bool(var.get())
        except Exception:
            self._only_new = True

    def _toggle_api_key(self) -> None:
        self._api_key_entry.configure(show="" if self._show_key.get() else "*")

    def _persist_api_key(self) -> str:
        """Save the key beside the program and expose it to the engine."""
        var = getattr(self, "api_key_var", None)
        if var is not None:
            key = (var.get() or "").strip()
        else:
            key = read_saved_api_key()
        if not key:
            return ""
        path = data_dir() / "tmdb_api_key.txt"
        try:
            path.write_text(key + "\n", encoding="utf-8")
        except Exception as e:
            messagebox.showerror("无法保存 API Key", f"{path}\n{e}")
            return ""
        os.environ["TMDB_API_KEY"] = key
        return key

    def _choose(self, preview: bool) -> None:
        root = normalize_root(self.root_var.get())
        if not root.exists() or not root.is_dir():
            messagebox.showerror("目录无效", f"扫描根目录不存在:\n{root}")
            return
        if not engine_ready(self.script):
            messagebox.showerror(
                "缺少引擎",
                "找不到 tmdb_format_rename。\n"
                "请使用 TMDB刮削命名.exe，或把 tmdb_format_rename.py 放在程序同目录。\n"
                f"目录: {self.tools}",
            )
            return
        if not self._persist_api_key():
            messagebox.showerror(
                "缺少 API Key",
                "请填写 TMDB API Key。\n它会保存在 %AppData%\\Roaming\\TMDB刮削命名\\tmdb_api_key.txt。",
            )
            return
        media = "auto"
        self._only_new = bool(self.only_new_var.get()) if getattr(self, "only_new_var", None) is not None else bool(getattr(self, "_only_new", True))
        only_line = "只处理新文件夹，已有 [tmdbid=] 的会跳过。\n" if self._only_new else ""
        # 仅预览：直接开跑，不弹确认框；正式刮削才确认
        if not preview:
            ok = messagebox.askyesno(
                "确认刮削",
                (
                    f"媒体: 自动（电影+剧集双搜）\n模式: 正式更改\n目录: {root}\n\n"
                    f"{only_line}"
                    "两边都命中的会进失败栏，不改名。确定开始？"
                ),
            )
            if not ok:
                return

        self._media = media
        self._show_runner(root, preview, media)

    def _make_scrollable_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        cols = ("kind", "col1", "col2", "col3")
        tree = ttk.Treeview(wrap, columns=cols, show="headings", height=18)
        labels = {
            "kind": "类型",
            "col1": "名称",
            "col2": "说明",
            "col3": "完整路径 / 详情",
        }
        tree._sort_state = {}  # type: ignore[attr-defined]
        for col in cols:
            tree.heading(
                col,
                text=labels[col],
                command=lambda c=col, t=tree, lab=labels: self._sort_tree_by(t, c, lab),
            )
        tree.column("kind", width=56, minwidth=48, stretch=False, anchor="center")
        tree.column("col1", width=260, minwidth=120, stretch=False)
        tree.column("col2", width=140, minwidth=80, stretch=False)
        tree.column("col3", width=880, minwidth=240, stretch=False)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        tree.bind("<Double-1>", lambda e, t=tree: self._copy_tree_row(t))
        return tree

    @staticmethod
    def _sort_rows(tree: ttk.Treeview, col: str, reverse: bool) -> None:
        """Case-insensitive, natural order (第2季 before 第10季)."""
        def key(x):
            return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(x[0]).casefold())]
        rows = [(tree.set(iid, col), iid) for iid in tree.get_children("")]
        rows.sort(key=key, reverse=reverse)
        for idx, (_val, iid) in enumerate(rows):
            tree.move(iid, "", idx)

    def _sort_tree_by(self, tree: ttk.Treeview, col: str, labels: dict) -> None:
        """Click column header to toggle A→Z / Z→A sort (名称 & 路径最实用)."""
        state = getattr(tree, "_sort_state", {})
        reverse = bool(state.get(col, False))
        self._sort_rows(tree, col, reverse)
        tree._last_sort = (col, reverse)  # type: ignore[attr-defined]
        # Update heading marks; clear other columns' arrows
        for c, text in labels.items():
            if c == col:
                tree.heading(
                    c,
                    text=text + (" ▼" if reverse else " ▲"),
                    command=lambda cc=c, t=tree, lab=labels: self._sort_tree_by(t, cc, lab),
                )
            else:
                tree.heading(
                    c,
                    text=text,
                    command=lambda cc=c, t=tree, lab=labels: self._sort_tree_by(t, cc, lab),
                )
        state[col] = not reverse
        # Reset other columns so next click on them starts ascending
        for c in list(state):
            if c != col:
                state[c] = False
        tree._sort_state = state  # type: ignore[attr-defined]

    def _copy_tree_row(self, tree: ttk.Treeview) -> None:
        sel = tree.selection()
        if not sel:
            return
        vals = tree.item(sel[0], "values")
        text = " | ".join(str(v) for v in vals)
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.summary.configure(text="已复制当前行到剪贴板（方便查看完整长路径）")
        except Exception:
            pass

    def _show_runner(self, root: Path, preview: bool, media: str, cli_override: list | None = None) -> None:
        for w in self.winfo_children():
            w.destroy()
        self.geometry("1000x700")
        self.minsize(800, 540)
        self._scan_root = root
        self._last_was_preview = preview
        self._media = media
        media_zh = "自动识别" if media == "auto" else ("剧集" if media == "tv" else "电影")
        self.title("TMDB 刮削命名 — " + media_zh + ("预览中" if preview else "更改中"))

        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=12, pady=8)
        ttk.Label(
            bar,
            text=(("预览" if preview else "正式刮削") + f"  ·  【{media_zh}】  ·  {root}"),
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        self.only_new_var = tk.BooleanVar(value=bool(getattr(self, "_only_new", True)))
        ttk.Checkbutton(
            bar,
            text="只刮削新添加的",
            variable=self.only_new_var,
            command=self._sync_only_new,
        ).pack(side=tk.LEFT, padx=(16, 0))

        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop)
        self.btn_stop.pack(side=tk.RIGHT)
        self.btn_again = ttk.Button(bar, text="重新选择", command=self._restart, state=tk.DISABLED)
        self.btn_again.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_reload = ttk.Button(bar, text="刷新结果", command=self._load_results, state=tk.DISABLED)
        self.btn_reload.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_apply = ttk.Button(
            bar, text="确认并正式刮削", command=self._confirm_apply, state=tk.DISABLED
        )
        self.btn_apply.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_undo = ttk.Button(bar, text="撤销上次改名", command=self._undo)
        self.btn_undo.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_export = ttk.Button(bar, text="导出表格", command=self._export_csv, state=tk.DISABLED)
        self.btn_export.pack(side=tk.RIGHT, padx=(0, 8))

        self.summary = ttk.Label(
            self,
            text="运行中…完成后可在下方标签页查看；点表头可排序，长路径用横向滑动条，双击一行可复制。",
            foreground="#444",
        )
        self.summary.pack(fill=tk.X, padx=12, pady=(0, 6))

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        self.tab_trees: dict[str, ttk.Treeview] = {}

        log_frame = ttk.Frame(self.notebook)
        self.notebook.add(log_frame, text="运行日志")
        self.log = scrolledtext.ScrolledText(log_frame, height=20, wrap=tk.WORD, font=("Consolas", 10))
        self.log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Tab order: 概览 → 电影/剧集识别 → 需要确认(含重复/刮削失败) → 更改(含散落整理)
        # 「已正确命名」只写在概览/日志，不再单独开标签
        self._result_tab_defs = [
            ("overview", "概览"),
            ("leaves_movie", "电影识别"),
            ("leaves_tv", "剧集识别"),
            ("unmatched", "需要确认"),
            ("changes", "更改"),
        ]
        for key, title in self._result_tab_defs:
            fr = ttk.Frame(self.notebook)
            self.notebook.add(fr, text=f"{title} (0)")
            if key == "overview":
                self.overview_text = scrolledtext.ScrolledText(
                    fr, height=18, wrap=tk.WORD, font=("Microsoft YaHei UI", 10), state=tk.DISABLED
                )
                self.overview_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
            else:
                if key == "unmatched":
                    top_row = ttk.Frame(fr)
                    top_row.pack(fill=tk.X, padx=8, pady=(6, 0))
                    ttk.Label(
                        top_row,
                        text="双击一行：从候选里选一个（或填 TMDB 编号），选完再点「仅预览」/「确认刮削」就会按你的选择处理。",
                        foreground="#555",
                    ).pack(side=tk.LEFT)
                    self.um_filter = tk.StringVar(value="全部")
                    cb = ttk.Combobox(top_row, textvariable=self.um_filter, values=["全部", "电影", "剧集"], state="readonly", width=6)
                    cb.pack(side=tk.RIGHT)
                    ttk.Label(top_row, text="类型").pack(side=tk.RIGHT, padx=(0, 4))
                    cb.bind("<<ComboboxSelected>>", lambda e: self._refill_unmatched())
                self.tab_trees[key] = self._make_scrollable_tree(fr)
                if key == "unmatched":
                    self.tab_trees[key].bind("<Double-1>", lambda e, t=self.tab_trees[key]: self._pick_candidate(t))

        self._run(root, preview, media, cli_override)

    def _set_tab_title(self, key: str, title: str, n: int) -> None:
        order = [k for k, _ in self._result_tab_defs]
        if key not in order:
            return
        self.notebook.tab(1 + order.index(key), text=f"{title} ({n})")

    def _clear_tree(self, key: str) -> None:
        tree = self.tab_trees.get(key)
        if not tree:
            return
        if hasattr(self, "_row_items"):
            for k in [k for k in self._row_items if k[0] == key]:
                self._row_items.pop(k, None)
        for item in tree.get_children():
            tree.delete(item)

    def _fill_tree(self, key: str, rows: list) -> None:
        self._clear_tree(key)
        tree = self.tab_trees[key]
        kind = "剧集" if getattr(self, "_media", "movie") == "tv" else "电影"
        if not hasattr(self, "_row_items"):
            self._row_items = {}
        for row in rows:
            if len(row) >= 4:
                iid = tree.insert("", tk.END, values=(row[0], row[1], row[2], row[3]))
                if len(row) >= 5 and isinstance(row[4], dict):
                    self._row_items[(key, iid)] = row[4]
            else:
                a, b, c = row[0], row[1], row[2]
                tree.insert("", tk.END, values=(kind, a, b, c))
        last = getattr(tree, "_last_sort", None)
        if last:
            self._sort_rows(tree, last[0], last[1])

    def _pick_candidate(self, tree: ttk.Treeview) -> None:
        """Double-click on a row of 需要确认: choose the right TMDB entry for that folder."""
        sel = tree.selection()
        if not sel:
            return
        item = getattr(self, "_row_items", {}).get(("unmatched", sel[0]))
        if not item or not item.get("path"):
            self._copy_tree_row(tree)
            return
        cands = [c for c in (item.get("candidates") or []) if isinstance(c, dict) and str(c.get("tmdb") or "").isdigit()]

        top = tk.Toplevel(self)
        top.title("选择正确的影片")
        top.transient(self)
        top.geometry("760x460")
        ttk.Label(top, text=str(item.get("name") or ""), font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
        ttk.Label(top, text=str(item.get("path") or ""), foreground="#555").pack(anchor="w", padx=12)
        why = item.get("note") or ""
        ttk.Label(top, text=f"原因：{zh_reason(item.get('reason') or '')}", foreground="#a33").pack(anchor="w", padx=12, pady=(6, 0))
        if why:
            ttk.Label(top, text=str(why)[:400], wraplength=720, justify="left", foreground="#555").pack(anchor="w", padx=12)

        var = tk.StringVar(value="0" if cands else "manual")
        box = ttk.LabelFrame(top, text="候选")
        box.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        for i, c in enumerate(cands):
            where = "·".join(x for x in (c.get("year"), c.get("country") or c.get("lang"), (f"片长{c['runtime']}分钟" if c.get("runtime") else "")) if x)
            kind_zh = "电影" if c.get("media") == "movie" else "剧集"
            text = f"{kind_zh}《{c.get('title') or c.get('original') or ''}》({where or '?'})  原名：{c.get('original') or ''}  [tmdbid={c.get('tmdb')}]"
            ttk.Radiobutton(box, text=text, variable=var, value=str(i)).pack(anchor="w", padx=8, pady=2)
        if not cands:
            ttk.Label(box, text="没有候选，可以直接填 TMDB 编号。").pack(anchor="w", padx=8, pady=2)
        manual = ttk.Frame(box)
        manual.pack(anchor="w", padx=8, pady=(8, 2))
        ttk.Radiobutton(manual, text="自己填：", variable=var, value="manual").pack(side=tk.LEFT)
        mtype = tk.StringVar(value=(item.get("media") or "movie") if (item.get("media") in ("movie", "tv")) else "movie")
        ttk.Radiobutton(manual, text="电影", variable=mtype, value="movie").pack(side=tk.LEFT, padx=(8, 0))
        ttk.Radiobutton(manual, text="剧集", variable=mtype, value="tv").pack(side=tk.LEFT, padx=(8, 0))
        mid = tk.StringVar()
        ttk.Entry(manual, textvariable=mid, width=12).pack(side=tk.LEFT, padx=8)
        ttk.Label(manual, text="TMDB 编号（网页地址 /movie/ 或 /tv/ 后面的数字）", foreground="#555").pack(side=tk.LEFT)

        def ok() -> None:
            if var.get() == "manual":
                tid = mid.get().strip()
                if not tid.isdigit():
                    messagebox.showerror("编号无效", "请填数字编号。", parent=top)
                    return
                choice = {"tmdb": tid, "media": mtype.get()}
            else:
                c = cands[int(var.get())]
                choice = {"tmdb": str(c["tmdb"]), "media": c.get("media") or "movie"}
            self._save_choice(item, choice)
            top.destroy()

        bar = ttk.Frame(top)
        bar.pack(fill=tk.X, padx=12, pady=(0, 10))
        ttk.Button(bar, text="取消", command=top.destroy).pack(side=tk.RIGHT)
        ttk.Button(bar, text="确定", command=ok).pack(side=tk.RIGHT, padx=(0, 8))

    def _save_choice(self, item: dict, choice: dict) -> None:
        """Remember the choice for this folder; the engine reads it on the next run."""
        path = data_dir() / "tmdb_user_choices.json"
        data = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        key = str(item.get("path") or "").replace("/", "\\").rstrip("\\").lower()
        data[key] = {**choice, "name": item.get("name") or "", "path": str(item.get("path") or "")}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return
        messagebox.showinfo("已记住", "已记住你的选择。\n再点一次「仅预览」或「确认刮削」，这个文件夹就会按它处理。")

    @staticmethod
    def _change_label(rec: dict, base: str) -> str:
        act = rec.get("action") or ""
        if act == "merge_version":
            return f"{base}·合并版本 [{rec.get('version_label') or ''}]"
        if act == "merge_season":
            n = rec.get("season_no")
            return f"{base}·并入 Season {n:02d}" if n is not None else f"{base}·并入剧目录"
        if rec.get("version_label"):
            return f"{base}·版本 [{rec['version_label']}]"
        if rec.get("season_wrap") is not None:
            return f"{base}·整理为 Season {rec['season_wrap']:02d}"
        return base

    def _refill_unmatched(self) -> None:
        rows = getattr(self, "_um_all", [])
        want = self.um_filter.get() if getattr(self, "um_filter", None) is not None else "全部"
        shown = [r for r in rows if want == "全部" or r[0] == want]
        self._fill_tree("unmatched", shown)
        n = f"{len(shown)}/{len(rows)}" if want != "全部" else str(len(rows))
        order = [k for k, _ in self._result_tab_defs]
        self.notebook.tab(1 + order.index("unmatched"), text=f"需要确认 ({n})")

    def _undo(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("忙", "请先等待当前任务结束。")
            return
        h = last_history()
        if not h:
            messagebox.showinfo("没有可撤销的改动", "还没有正式更改过，或记录已被清除。")
            return
        t = str(h.get("time") or "")
        if len(t) >= 15:
            t = f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[9:11]}:{t[11:13]}"
        ok = messagebox.askyesno(
            "撤销上次改名",
            f"将把 {t} 那一次正式更改还原：\n目录：{h.get('root')}\n共 {len(h.get('ops') or [])} 步"
            "（改回原文件夹名、拆回合并的版本/季，删除新写入的图片和 nfo）。\n\n确定撤销？",
        )
        if not ok:
            return
        self._last_was_preview = True
        self._show_runner(Path(h.get("root") or "."), True, "auto", ["--undo"])
        self.btn_undo.configure(state=tk.DISABLED)

    def _export_csv(self) -> None:
        d = getattr(self, "_export_data", None)
        if not d:
            return
        path = filedialog.asksaveasfilename(
            title="导出表格", defaultextension=".csv", filetypes=[("CSV 表格", "*.csv")],
            initialfile="tmdb刮削结果.csv",
        )
        if not path:
            return
        import csv
        media = d["media"]
        rows = []
        done = {r.get("path"): r for r in d["ok"]}
        for p in d["plan"]:
            act = p.get("action") or ""
            if d["preview"]:
                status = {"merge_version": "待合并版本", "merge_season": "待合并到季"}.get(act, "待改名")
            else:
                status = {"merge_version": "已合并版本", "merge_season": "已合并到季"}.get(act, "已改名" if p.get("path") in done else "待改名")
            rows.append([status, "剧集" if guess_lib_kind(p, media) == "tv" else "电影", p.get("name") or "",
                         p.get("target") or "", p.get("tmdb") or "",
                         p.get("version_label") or (f"Season {p['season_no']:02d}" if p.get("season_no") is not None else ""),
                         p.get("version_info") or "", ""])
        for r in d["um"]:
            it = r[4] if len(r) >= 5 and isinstance(r[4], dict) else {}
            vp = it.get("version_profile") or {}
            info = ""
            if vp:
                bits = []
                if vp.get("w"):
                    bits.append(f"{vp['w']}x{vp['h']}")
                if vp.get("sec"):
                    bits.append(f"{round(vp['sec'] / 60)}分钟")
                if vp.get("size"):
                    bits.append(f"{vp['size'] / 1024 ** 3:.2f}GB" if vp["size"] >= 1024 ** 3 else f"{vp['size'] / 1024 ** 2:.0f}MB")
                info = " ".join(bits)
            rows.append(["需要确认", r[0], r[1], "", it.get("tmdb") or it.get("candidate_tmdb") or "", "", info, f"{r[2]} {r[3] if len(r) > 3 else ''}".strip()])
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["状态", "类型", "原文件夹", "新名称", "TMDB编号", "版本/季", "分辨率·片长·大小", "原因/说明"])
                w.writerows(rows)
            messagebox.showinfo("已导出", f"共 {len(rows)} 行：\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _confirm_apply(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("忙", "请先等待当前任务结束。")
            return
        root = self._scan_root
        if root is None:
            messagebox.showerror("无目录", "没有可更改的扫描目录。")
            return
        media_zh = "自动识别" if getattr(self, "_media", "auto") == "auto" else ("剧集" if self._media == "tv" else "电影")
        ok = messagebox.askyesno(
            "确认正式刮削",
            f"媒体: {media_zh}\n将按刚才预览的同一目录执行正式更改：\n{root}\n\n"
            "会改名、下载缺的图片、写缺的 nfo。\n确定开始？",
        )
        if not ok:
            return
        self._last_was_preview = False
        self.title(f"TMDB 刮削命名 — {media_zh}更改中")
        self.btn_apply.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.btn_again.configure(state=tk.DISABLED)
        self.btn_reload.configure(state=tk.DISABLED)
        self.summary.configure(text="正在正式刮削…")
        self.notebook.select(0)
        self._log("")
        self._log("——" * 20)
        self._log("用户确认：开始正式刮削（同一目录 / 同一媒体类型）")
        self._run(root, False, self._media)

    def _load_results(self) -> None:
        last_path = data_dir() / "tmdb_format_rename_last.json"
        apply_path = data_dir() / "tmdb_format_rename_apply.json"
        data: dict = {}
        apply: dict = {}
        if last_path.is_file():
            try:
                data = json.loads(last_path.read_text(encoding="utf-8"))
            except Exception as e:
                self._log(f"读取结果失败: {e}")
                return
        else:
            self.summary.configure(text="还没有结果文件。跑完一次预览或正式刮削后再刷新。")
            return
        if apply_path.is_file():
            try:
                apply = json.loads(apply_path.read_text(encoding="utf-8"))
            except Exception:
                apply = {}

        leaves = data.get("leaves") or []
        plan = data.get("plan") or []
        skip = data.get("skip") or []
        wrapped = data.get("wrapped") or []
        unresolved = data.get("unresolved") or []

        preview = bool(data.get("preview"))
        media = data.get("media") or getattr(self, "_media", "movie") or "movie"
        media_zh = "自动识别" if media == "auto" else ("剧集" if media == "tv" else "电影")

        ok_list = list(apply.get("ok") or [])
        rename_fail_list = list(apply.get("fail") or [])
        # 改名成功/失败里的「改名结果」只保留本次扫描同 root+media；预览不显示旧改名成功
        apply_media = (apply.get("media") or "").strip()
        apply_root = (apply.get("root") or "").rstrip("\\/")
        data_root = str(data.get("root") or "").rstrip("\\/")
        data_media = (media or "").strip()
        same_run = True
        if preview:
            same_run = False
        elif apply.get("cleared"):
            same_run = False
        elif apply_media and data_media and apply_media != data_media:
            same_run = False
        elif apply_root and data_root and apply_root.lower() != data_root.lower():
            same_run = False
        if not same_run:
            ok_list = []
            rename_fail_list = []

        already_ok = [s for s in skip if s.get("reason") == "already_ok"]
        unmatched = [s for s in skip if s.get("reason") != "already_ok"]

        leaf_count = int(data.get("leaf_count") or len(leaves) or len(already_ok))
        plan_count = int(data.get("plan_count") or len(plan))
        wrap_count = int(data.get("wrapped_count") or len(wrapped))
        skip_count = int(data.get("skip_count") or len(skip))
        ok_count = len(ok_list)

        mode = "仅预览" if preview else "正式刮削"
        tip = ""
        if preview and self._last_was_preview:
            tip = "  → 可点右上角「确认并正式刮削」"
        # Keep runner media in sync with result JSON (avoid mixing movie/tv reads)
        self._media = media
        self.title(f"TMDB 刮削命名 — 【{media_zh}】结果")

        lines = [
            f"★★ 本次模式：【{media_zh}】 ★★",
            f"请按【{media_zh}】整理到对应库，不要和另一类混放。",
            "",
            f"扫描根目录：{data.get('root', '')}",
            f"媒体类型：【{media_zh}】",
            f"模式：{mode}",
            "",
            "分类说明：",
            f"  · 电影识别 / 剧集识别（{leaf_count}）：已拿到 TMDB 编号",
            "  · 需要确认：搜不到 / 电影剧集都命中(重复) / 不确定 / 刮削或改名失败等，原因见列表说明列",
            f"  · 更改（含散落整理 {wrap_count}）：文件夹改名 + 散落建夹改标题；待改 {plan_count}，已改 {ok_count}",
            f"  · 跳过已刮削：{int(data.get('skipped_done_count') or 0)}（名字里已有 [tmdbid=]，本次不联网、不改文件）",
            "",
            f"跳过合计：{skip_count}",
            f"详细 JSON：{last_path}",
            "",
            "提示：点表头可按该列排序（再点一次倒序）；长路径用底部横向滑动条；双击一行可复制。",
        ]
        if preview:
            lines.append("预览满意后，点右上角「确认并正式刮削」即可，无需重新打开程序。")

        self.overview_text.configure(state=tk.NORMAL)
        self.overview_text.delete("1.0", tk.END)
        self.overview_text.insert(tk.END, "\n".join(lines))
        self.overview_text.configure(state=tk.DISABLED)
        self._set_tab_title("overview", "概览", 1)

        # Split by each item's TMDB media type (stamped by scraper), not run mode alone
        id_items = list(leaves) if leaves else list(already_ok)
        movie_rows = []
        tv_rows = []
        for L in id_items:
            path = L.get("path") or ""
            name = L.get("name") or path
            note = f"tmdb={L.get('tmdb') or '?'}"
            kind = guess_lib_kind(L, media)
            type_zh = "剧集" if kind == "tv" else "电影"
            row = (type_zh, name, note, path)
            if kind == "tv":
                tv_rows.append(row)
            else:
                movie_rows.append(row)
        self._fill_tree("leaves_movie", movie_rows)
        self._set_tab_title("leaves_movie", "电影识别", len(movie_rows))
        self._fill_tree("leaves_tv", tv_rows)
        self._set_tab_title("leaves_tv", "剧集识别", len(tv_rows))

        # 已正确命名：不单独建标签，数量见概览/日志

        # 「需要确认」在后面统一填入（含重复 / 刮削失败）

        # 「更改」：预览=待更改；确认后=已更改。同一批只显示一种，不重复两条。
        change_rows = []
        if preview:
            for p in plan:
                change_rows.append(
                    (
                        ("剧集" if guess_lib_kind(p, media) == "tv" else "电影"),
                        p.get("name") or p.get("path") or "",
                        self._change_label(p, "待更改"),
                        f"→ {p.get('target') or p.get('dest') or ''}",
                    )
                )
        else:
            # 正式刮削后优先显示本次已更改；若无改名结果则仍显示计划（例如无需改名）
            src_rows = ok_list if ok_list else plan
            label = "已更改" if ok_list else "待更改"
            for r in src_rows:
                change_rows.append(
                    (
                        ("剧集" if guess_lib_kind(r, media) == "tv" else "电影"),
                        r.get("name") or r.get("path") or "",
                        self._change_label(r, label),
                        r.get("final")
                        or r.get("dest")
                        or (f"→ {r.get('target')}" if r.get("target") else "")
                        or r.get("path")
                        or "",
                    )
                )
        # 散落整理并入「更改」：建夹/改标题与文件夹改名同属整理
        for w in wrapped:
            action = (w.get("action") or "").strip()
            if action in {"skip_no_tmdb", "no_tmdb", "unresolved"} or (
                action.startswith("skip") and not w.get("tmdb")
            ):
                continue
            if action == "wrap_fail":
                continue
            type_zh = "剧集" if guess_lib_kind(w, media) == "tv" else "电影"
            name = w.get("stem") or w.get("video") or w.get("name") or ""
            detail = w.get("folder") or w.get("video") or w.get("reason") or ""
            if preview or action in {"would_wrap", ""}:
                status = "散落·待整理"
            elif action == "wrapped":
                status = "散落·已整理"
            else:
                status = "散落·待整理" if preview else "散落·已整理"
            change_rows.append((type_zh, name, status, detail))
        # 散落整理也仍显示「更改」，不把状态写进标签名
        self._fill_tree("changes", change_rows)
        self._set_tab_title("changes", "更改", len(change_rows))

        def _um_row(item, default_reason=""):
            reason = item.get("reason") or item.get("id_from") or default_reason or "unmatched"
            note = item.get("note") or item.get("path") or item.get("search_query") or ""
            if item.get("movie_tmdb") or item.get("tv_tmdb"):
                note = (
                    f"电影={item.get('movie_tmdb') or '?'} / 剧集={item.get('tv_tmdb') or '?'} | "
                    + str(note)
                )
            kind = "剧集" if guess_lib_kind(item, media) == "tv" else "电影"
            name = (
                item.get("name")
                or item.get("path")
                or item.get("stem")
                or item.get("video")
                or ""
            )
            reason_zh = zh_reason(reason)
            if reason == "ambiguous_no_year" and item.get("search_year"):
                reason_zh = "多个候选（同名同年）"
            return (kind, name, reason_zh, note, item)

        um_rows = []
        for s in unmatched:
            um_rows.append(_um_row(s))
        for u in unresolved:
            um_rows.append(_um_row(u, "no_tmdb_id"))
        for r in rename_fail_list:
            um_rows.append(
                (
                    ("剧集" if (r.get("media") or r.get("kind") or media) == "tv" else "电影"),
                    r.get("name") or r.get("path") or "",
                    "改名失败",
                    r.get("error") or r.get("dest") or r.get("path") or "",
                )
            )
        for w in wrapped:
            action = w.get("action") or ""
            if action in {"skip_no_tmdb", "no_tmdb", "unresolved"} or (
                action.startswith("skip") and not w.get("tmdb")
            ):
                um_rows.append(
                    (
                        ("剧集" if (w.get("media") or w.get("kind") or media) == "tv" else "电影"),
                        w.get("stem") or w.get("video") or w.get("name") or "",
                        zh_reason(action if action != "skip_no_tmdb" else "skip_no_tmdb"),
                        w.get("folder") or w.get("video") or w.get("reason") or "",
                    )
                )

        # Same folder can appear in both skip (no_tmdb_id) and unresolved
        # (ambiguous_movie_and_tv). Keep one row; prefer conflict wording.
        _reason_rank = {
            "电影和剧集都匹配到了（请确认）": 0,
            "多个候选且无年份": 1,
            "多个候选": 2,
            "改名失败": 3,
            "搜索无结果": 4,
            "找不到TMDB编号": 9,
        }
        best = {}  # key -> row
        for row in um_rows:
            # Prefer path-ish detail when present; else name
            pathish = str(row[3] or "").strip()
            name = str(row[1] or "").strip()
            key = (name.casefold(), pathish.casefold() if pathish else "")
            prev = best.get(key)
            if prev is None:
                best[key] = row
                continue
            r_new = _reason_rank.get(str(row[2]), 5)
            r_old = _reason_rank.get(str(prev[2]), 5)
            if r_new < r_old:
                best[key] = row
            elif r_new == r_old and len(str(row[3] or "")) > len(str(prev[3] or "")):
                best[key] = row  # keep richer note
        um_rows = list(best.values())

        self._um_all = um_rows
        self._refill_unmatched()
        self._export_data = {"plan": plan, "ok": ok_list, "skip": skip, "unresolved": unresolved, "um": um_rows, "media": media, "preview": preview}
        try:
            self.btn_export.configure(state=tk.NORMAL)
        except Exception:
            pass

        self.summary.configure(
            text=(
                f"【{media_zh}】{mode}完成 · 已识别 {leaf_count} · 更改(待) {plan_count} · "
                f"已正确命名 {len(already_ok)} · 需要确认 {len(um_rows)} · "
                f"散落(入更改) {wrap_count} · 更改(已) {ok_count}"
                + tip
            )
        )

        if preview and self._last_was_preview and self._scan_root is not None:
            self.btn_apply.configure(state=tk.NORMAL)
        else:
            self.btn_apply.configure(state=tk.DISABLED)

        self.notebook.select(1)

    def _restart(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("忙", "请先停止当前任务。")
            return
        for w in self.winfo_children():
            w.destroy()
        self.geometry("760x600")
        self.title("TMDB 刮削命名")
        self._scan_root = None
        self._last_was_preview = False
        self._build_chooser()

    def _log(self, line: str) -> None:
        self.log.insert(tk.END, line.rstrip() + "\n")
        self.log.see(tk.END)

    def _run(self, root: Path, preview: bool, media: str, cli_override: list | None = None) -> None:
        if not cli_override and not self._persist_api_key():
            messagebox.showerror(
                "缺少 API Key",
                "请回到首页填写 TMDB API Key。\n它会保存在 %AppData%\\Roaming\\TMDB刮削命名\\tmdb_api_key.txt。",
            )
            return
        self._sync_only_new()
        os.environ["TMDB_TOOLS_DIR"] = str(data_dir())
        os.environ["PYTHONIOENCODING"] = "utf-8"
        frozen = bool(getattr(sys, "frozen", False))
        cli = [str(root), f"--media={media}"]
        if getattr(self, "_only_new", False):
            cli.append("--only-new")
        if preview:
            cli.append("--preview")
        if cli_override:
            cli = list(cli_override)
        self._log("=" * 60)
        if frozen:
            self._log(("PREVIEW " if preview else "APPLY ") + "(portable exe, in-process) " + " ".join(cli))
        else:
            py = find_python_for_script()
            args = [py, "-u", str(self.script), *cli]
            self._log(("PREVIEW " if preview else "APPLY ") + " ".join(args[2:]))
        self._log(f"数据目录: {data_dir()}")
        self.btn_again.configure(state=tk.DISABLED)
        self.btn_reload.configure(state=tk.DISABLED)
        self.btn_apply.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self._stop_flag = False

        def worker() -> None:
            try:
                if frozen or (not self.script.is_file() and engine_importable()):
                    import tmdb_format_rename as eng

                    old_argv = list(sys.argv)
                    old_out, old_err = sys.stdout, sys.stderr

                    def emit(line: str) -> None:
                        self.after(0, self._log, line)

                    sys.stdout = _LineWriter(emit)  # type: ignore[assignment]
                    sys.stderr = sys.stdout  # type: ignore[assignment]
                    sys.argv = ["tmdb_format_rename.py", *cli]
                    # Re-read the key in case this module was imported before the user typed one.
                    eng.TOOLS = Path(os.environ["TMDB_TOOLS_DIR"])
                    eng.CACHE_PATH = eng.TOOLS / "tmdb_movie_title_cache.json"
                    eng.SEARCH_CACHE_PATH = eng.TOOLS / "tmdb_search_cache.json"
                    eng.API_KEY = eng._load_api_key()
                    try:
                        code = int(eng.main() or 0)
                    finally:
                        sys.argv = old_argv
                        try:
                            sys.stdout.flush()
                        except Exception:
                            pass
                        sys.stdout, sys.stderr = old_out, old_err
                    self.after(0, self._log, f"exit={code}")
                else:
                    py = find_python_for_script()
                    args = [py, "-u", str(self.script), *cli]
                    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
                    self.proc = subprocess.Popen(
                        args,
                        cwd=str(self.tools),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env={**os.environ, "PYTHONIOENCODING": "utf-8", "TMDB_TOOLS_DIR": str(data_dir())},
                        creationflags=creation,
                    )
                    assert self.proc.stdout is not None
                    for line in self.proc.stdout:
                        self.after(0, self._log, line.rstrip("\n"))
                    code = self.proc.wait()
                    self.after(0, self._log, f"exit={code}")
            except Exception as e:
                self.after(0, self._log, f"ERROR: {e}")
            finally:
                self.proc = None
                self.after(0, self.btn_stop.configure, {"state": tk.DISABLED})
                self.after(0, self.btn_again.configure, {"state": tk.NORMAL})
                self.after(0, self.btn_reload.configure, {"state": tk.NORMAL})
                if cli_override:
                    self.after(0, self.summary.configure, {"text": "撤销完成，详情见「运行日志」。需要时点「重新选择」回到首页。"})
                else:
                    self.after(0, self._load_results)

        threading.Thread(target=worker, daemon=True).start()

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._log("已请求停止…")


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
