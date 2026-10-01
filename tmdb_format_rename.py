# -*- coding: utf-8 -*-
"""tmdb格式命名 — rename movie leaf folders under a root.

Usage:
  python tmdb_format_rename.py "D:\\path\\to\\folder"
  python tmdb_format_rename.py "D:\\path\\to\\folder" --preview
  python tmdb_format_rename.py "D:\\path\\to\\folder" --no-poster --no-nfo

Target name: {title} ({year}) [tmdbid={id}]

ID resolution order per leaf:
  1) [tmdbid=N] in folder name
  2) bare numeric folder name (e.g. 15859) — treated as TMDB id
     (4-digit years 1900-2099 excluded)
  3) other id-like tokens in name (tmdbid=N / [N] 5+ digits)
  4) movie-level nfo tmdb id (uniqueid type=tmdb; never actor tmdbid)
  5) TMDB search by cleaned folder title + year

Only renames leaf video folders; series/collection parents are dug into, not renamed.
Blu-ray/DVD disc folders (BDMV/VIDEO_TS) and .iso leaves are treated as one unit; internals skipped.
Loose videos at root/dump dirs are wrapped into folders first (not inside collection folders; not re-wrapping dedicated leaves; multi-part 上/下/CD1/2 merge into one folder). NFO named {video}.nfo.
"""
from __future__ import annotations

import difflib
import http.client
import json
import os
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.sax.saxutils
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

def _app_dir() -> Path:
    """Folder of the exe or this script. Not the PyInstaller temp extract."""
    me = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    try:
        return me.resolve().parent
    except OSError:
        # e.g. the exe sits on a network / NAS drive Windows cannot resolve (WinError 1005)
        return Path(os.path.abspath(me)).parent


def _roaming_dir() -> Path:
    """Key, JSON caches, and run logs. Not beside the exe (OneDrive would sync them)."""
    base = (os.environ.get("APPDATA") or "").strip()
    if not base:
        base = str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "TMDB刮削命名"


# Files that used to be written next to the exe / script.
_SIDECAR_NAMES = (
    "tmdb_api_key.txt",
    "tmdb_movie_title_cache.json",
    "tmdb_search_cache.json",
    "tmdb_format_rename_last.json",
    "tmdb_format_rename_apply.json",
    "tmdb_format_rename_posters.json",
)


def _adopt_sidecar(dest: Path, src: Path) -> None:
    """Move a key/json left beside the exe into Roaming. Roaming wins if both exist."""
    try:
        if dest.resolve() == src.resolve():
            return
    except Exception:
        return
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    for name in _SIDECAR_NAMES:
        old = src / name
        new = dest / name
        try:
            if not old.is_file():
                continue
            if (not new.exists()) or new.stat().st_size == 0:
                if new.exists():
                    new.unlink()
                shutil.move(str(old), str(new))
            else:
                old.unlink()
        except Exception:
            try:
                if old.is_file() and not new.exists():
                    shutil.copy2(str(old), str(new))
                    old.unlink()
            except Exception:
                pass


def _data_dir() -> Path:
    """Roaming by default. TMDB_TOOLS_DIR still overrides (tests / explicit path)."""
    override = (os.environ.get("TMDB_TOOLS_DIR") or "").strip()
    if override:
        d = Path(override)
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            return _app_dir()
    d = _roaming_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return _app_dir()
    _adopt_sidecar(d, _app_dir())
    return d


_SCRIPT_DIR = _app_dir()
TOOLS = _data_dir()

UA = "tmdb-format-rename/1.5"


def _load_api_key() -> str:
    k = (os.environ.get("TMDB_API_KEY") or "").strip()
    if k:
        return k
    for cand in (TOOLS / "tmdb_api_key.txt", _SCRIPT_DIR / "tmdb_api_key.txt"):
        try:
            if cand.exists():
                v = cand.read_text(encoding="utf-8", errors="ignore").strip()
                if v and not v.startswith("#"):
                    return v.splitlines()[0].strip()
        except Exception:
            pass
    return ""


API_KEY = _load_api_key()

# --- begin media helpers (movie/tv) ---
MEDIA_KIND = "auto"


def media_is_tv() -> bool:
    return MEDIA_KIND == "tv"


def media_is_auto() -> bool:
    return MEDIA_KIND == "auto"



_TV_SEASON_RE = re.compile(r"(?i)(?:^|[\s._\-\[\(])S\d{1,2}(?:E\d{1,3})?(?:$|[\s._\-\]\)])|Season\s*\d+|第\s*\d+\s*季")


def looks_like_tv_name(name: str) -> bool:
    """Heuristic: season markers strongly suggest a TV folder/file."""
    return bool(_TV_SEASON_RE.search(name or ""))


# Optional separators between title and type token: space, -, _, ., ·, —, –, etc.
# Type / year / id wrappers: spaces, dashes, underscores, dots, brackets
_HINT_WRAP = r"[\s\-_.·•〜～—–\[\]\(\)（）【】]*"

_MEDIA_HINT_TV = re.compile(
    rf"(?i)(?:{_HINT_WRAP})(?:"
    r"电视剧|電視劇|电视连续剧|電視連續劇|剧集|劇集|网剧|網劇|连续剧|連續劇|"
    r"TV\s*Series|TV\s*Show|(?<![A-Za-z])TV(?![A-Za-z])|(?<![A-Za-z])Series(?![A-Za-z])|(?<![A-Za-z])Show(?![A-Za-z])|"
    r"第\s*[0-9一二三四五六七八九十百]+\s*季|"
    r"(?<![A-Za-z])S\d{{1,2}}(?![A-Za-z0-9])|(?<![A-Za-z])Season\s*\d+(?![A-Za-z0-9])"
    rf")(?:{_HINT_WRAP})"
)
_MEDIA_HINT_MOVIE = re.compile(
    rf"(?i)(?:{_HINT_WRAP})(?:"
    r"电影|電影|影片|剧场版|劇場版|"
    r"(?<![A-Za-z])Movie(?![A-Za-z])|(?<![A-Za-z])Film(?![A-Za-z])|(?<![A-Za-z])Cinema(?![A-Za-z])"
    rf")(?:{_HINT_WRAP})"
)
_MEDIA_HINT_STRIP = re.compile(
    rf"(?i){_HINT_WRAP}(?:"
    r"电视剧|電視劇|电视连续剧|電視連續劇|剧集|劇集|网剧|網劇|连续剧|連續劇|"
    r"电影|電影|影片|剧场版|劇場版|"
    r"TV\s*Series|TV\s*Show|(?<![A-Za-z])TV(?![A-Za-z])|(?<![A-Za-z])Series(?![A-Za-z])|(?<![A-Za-z])Show(?![A-Za-z])|"
    r"(?<![A-Za-z])Movie(?![A-Za-z])|(?<![A-Za-z])Film(?![A-Za-z])|(?<![A-Za-z])Cinema(?![A-Za-z])"
    rf"){_HINT_WRAP}"
)
_HINT_ONLY = re.compile(
    r"^(?:电视剧|電視劇|剧集|劇集|网剧|網劇|连续剧|連續劇|电影|電影|影片|剧场版|劇場版|"
    r"TV|Series|Show|Movie|Film|Cinema)$",
    re.I,
)


# Season / episode markers that are not part of a title: S01, S01-S02, S01E02,
# Season 1, 第一季, 全3季. (Only 季: 第2部 can be a movie sequel.)
_SEASON_TOKENS = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:S\d{1,2}(?:[\s._]*[-–~][\s._]*S?\d{1,2})?"
    r"(?:[\s._]*E\d{1,3}(?:[\s._]*[-–~]?[\s._]*E?\d{1,3})?)?"
    r"|Seasons?[\s._]*\d{1,2}(?:[\s._]*[-–~][\s._]*\d{1,2})?|Complete[\s._]*Series)(?![A-Za-z0-9])"
    r"|第\s*[一二三四五六七八九十百零两\d]+(?:\s*[-–~至到]\s*[一二三四五六七八九十百零两\d]+)?\s*季"
    r"|全\s*[一二三四五六七八九十\d]+\s*季"
)


# Site / quality / language tags that wrap real titles in Chinese release names:
# 【高清电影】阿尔卑斯[2011][1080P][国英双语]
_SITE_WORDS = (
    r"www\.|\.com\b|\.net\b|\.cc\b|\.org\b|\.tv\b|高清|电影|剧集|电视剧|蓝光|原盘|国语|粤语|英语|国英|国粤|双语|中字|中英|"
    r"字幕|发布|压制|下载|迅雷|全\d+集|共\d+集|更新|完结|超清|字幕组|论坛|影视|无删减|未删减|导演剪辑|加长|修复|"
    r"\bBT\b|1080|720|2160|4K|BluRay|WEB-?DL|REMUX|x26[45]|HEVC"
)
_SITE_TAG_RE = re.compile(r"[\[【（(][^\]】）)]*?(?:" + _SITE_WORDS + r")[^\]】）)]*?[\]】）)]", re.I)
_CN_TAGS_RE = re.compile(
    r"国英双语|国粤双语|国粤英|中英双语|中英双字|中英字幕|国语中字|粤语中字|国语配音|简繁英?字幕|简体中文|繁体中文|"
    r"内嵌字幕|外挂字幕|官方中字|高清中字|蓝光原盘|蓝光版|高清版|无删减版|未删减版|导演剪辑版|加长版|修复版|完整版"
)


_EDITION_CUT_RE = re.compile(r"(?i)(?<![A-Za-z])(?:director'?s?|theatrical|extended|final|unrated|ultimate|special|uncut)[\s._\-]+cut(?![A-Za-z])")
# "...x265-RARBG" / "...WEB-DL-GRP" / "...DTS-HD.MA.5.1-FGT": a release group after a known tech token.
_TRAILING_GROUP_RE = re.compile(
    r"(?i)((?:1080p|2160p|720p|480p|bluray|web-?dl|webrip|hdtv|remux|x26[45]|h\.?26[45]|hevc|avc|hdr|\d\.\d)[\s._]*)-[A-Za-z0-9]{2,15}$"
)


def strip_site_tags(s: str) -> str:
    out = _CN_TAGS_RE.sub(" ", _SITE_TAG_RE.sub(" ", s or ""))
    out = _TRAILING_GROUP_RE.sub(r"\1", _EDITION_CUT_RE.sub(" ", out))
    return out if out.strip(" ._-") else (s or "")


def strip_season_tokens(s: str) -> str:
    return _SEASON_TOKENS.sub(" ", s or "")


_SEASON_DIR_RE = re.compile(
    r"(?i)^(?:s\d{1,2}|season[\s._-]*\d{1,2}|series[\s._-]*\d{1,2}|第\s*[一二三四五六七八九十百零两\d]+\s*季|specials?|特别篇|特典)$"
)
_EPISODE_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[\(])(?:S\d{1,2}[\s._]*E\d{1,3}|\d{1,2}x\d{2,3}|EP?[\s._]*\d{2,3}|第\s*\d{1,3}\s*[集话話])(?:$|[\s._\-\]\)])"
)


def is_season_dir_name(name: str) -> bool:
    return bool(_SEASON_DIR_RE.match((name or "").strip()))


def looks_like_episode_file(stem: str) -> bool:
    return bool(_EPISODE_RE.search(stem or ""))


def folder_media_hint(name: str) -> str | None:
    """Folder-name type hint: 'tv' / 'movie' / None.

    Works with any glue/separator/brackets, including digits in between:
      宝贝星球电视剧 / 宝贝星球--电视剧 / 宝贝星球——电视剧 / 宝贝星球_电视剧
      宝贝星球.电视剧 / 宝贝星球(电视剧) / 宝贝星球（剧集） / 宝贝星球【电视剧】
      宝贝星球_2025_电视剧 / 宝贝星球(2025)电视剧
    Season markers also count as tv. If both movie and tv tokens appear → None.
    """
    s = name or ""
    tv = bool(_MEDIA_HINT_TV.search(s)) or looks_like_tv_name(s)
    movie = bool(_MEDIA_HINT_MOVIE.search(s))
    if tv and not movie:
        return "tv"
    if movie and not tv:
        return "movie"
    return None


def strip_media_hint_tokens(s: str) -> str:
    """Remove 电视剧/电影/TV/Movie tokens (and surrounding separators/brackets)."""
    s = _MEDIA_HINT_STRIP.sub(" ", s or "")
    return re.sub(r"\s+", " ", s).strip(" ._-—–·•〜～()（）[]【】")





# Does id N exist on the movie / TV endpoint? Asked for every [tmdbid=N] folder to tell a
# movie from a show that shares the number. The answer is kept on disk (an id that is
# there stays there), so a rescan of a big library does not ask again. Only a real
# "not found" counts as "no"; a network error raises instead of being taken for one.
_EXISTS: dict = {"movie": {}, "tv": {}}
_EXISTS_STATE = {"loaded": False, "dirty": False}
_EXISTS_TTL = 60 * 86400


def _exists_path() -> Path:
    return TOOLS / "tmdb_id_exists.json"


def _load_exists() -> None:
    if _EXISTS_STATE["loaded"]:
        return
    _EXISTS_STATE["loaded"] = True
    now = time.time()
    data = load_json(_exists_path())
    for kind in ("movie", "tv"):
        for tid, v in (data.get(kind) or {}).items():
            if isinstance(v, list) and len(v) == 2 and now - float(v[1]) < _EXISTS_TTL:
                _EXISTS[kind][tid] = (bool(v[0]), float(v[1]))


def save_exists_cache() -> None:
    if not _EXISTS_STATE["dirty"]:
        return
    _EXISTS_STATE["dirty"] = False
    save_json(_exists_path(), {k: {t: [v[0], v[1]] for t, v in d.items()} for k, d in _EXISTS.items()})


def _id_exists(kind: str, tid: str) -> bool:
    tid = str(tid or "").strip()
    if not tid.isdigit():
        return False
    _load_exists()
    hit = _EXISTS[kind].get(tid)
    if hit is not None:
        return hit[0]
    data = api_get(f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}&language=zh-CN")
    if isinstance(data, dict) and not data.get("_error") and data.get("id") is not None:
        ok = True
    elif isinstance(data, dict) and data.get("_error") == "HTTP 404":
        ok = False
    else:
        raise RuntimeError(f"cannot tell whether {kind} {tid} exists: {(data or {}).get('_error')}")
    _EXISTS[kind][tid] = (ok, time.time())
    _EXISTS_STATE["dirty"] = True
    return ok


def tmdb_tv_exists(tid: str) -> bool:
    """True if id resolves on TV endpoint."""
    return _id_exists("tv", tid)


def tmdb_movie_exists(tid: str) -> bool:
    return _id_exists("movie", tid)


def _title_tokens(s: str) -> set[str]:
    s = (s or "").lower()
    s = re.sub(r"[\[\]【】()（）._\-]+", " ", s)
    parts = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{1,}", s)
    return set(parts)


def is_tv_id_for_folder(tid: str, folder_name: str) -> bool:
    """In TV mode: keep id only if it is a TV show for this folder.

    TMDB movie/tv ids share the same numeric space, so id 278 can be both
    a movie and an unrelated TV entry. Prefer TV when:
      - TV endpoint works AND movie does not, or
      - folder looks like a series (S01/Season), or
      - folder name overlaps TV title/original_name more than movie title.
    Otherwise treat as movie folder and ignore in TV mode.
    """
    tid = str(tid or "").strip()
    if not tid.isdigit():
        return False
    tv = None
    movie = None
    try:
        tv = api_get(f"https://api.themoviedb.org/3/tv/{tid}?api_key={API_KEY}&language=zh-CN")
        if not (isinstance(tv, dict) and not tv.get("_error") and tv.get("id") is not None):
            tv = None
    except Exception:
        tv = None
    try:
        movie = api_get(f"https://api.themoviedb.org/3/movie/{tid}?api_key={API_KEY}&language=zh-CN")
        if not (isinstance(movie, dict) and not movie.get("_error") and movie.get("id") is not None):
            movie = None
    except Exception:
        movie = None

    if not tv and not movie:
        return False
    if tv and not movie:
        return True
    if movie and not tv:
        return False

    # both exist
    if looks_like_tv_name(folder_name):
        return True

    ft = _title_tokens(folder_name)
    tv_titles = " ".join(
        str(x or "")
        for x in (
            (tv or {}).get("name"),
            (tv or {}).get("original_name"),
        )
    )
    mv_titles = " ".join(
        str(x or "")
        for x in (
            (movie or {}).get("title"),
            (movie or {}).get("original_title"),
        )
    )
    tv_score = len(ft & _title_tokens(tv_titles))
    mv_score = len(ft & _title_tokens(mv_titles))
    if tv_score > mv_score:
        return True
    if mv_score > tv_score:
        return False
    # tie: movie-style "Title (year) [tmdbid=]" without season → treat as movie
    if re.search(r"\(\d{4}\)\s*\[tmdbid=", folder_name or "", re.I):
        return False
    return False


def _normalize_tv_payload(d):
    """Map TV API fields onto movie-like keys so title pickers reuse the same path."""
    if not isinstance(d, dict) or d.get("_error"):
        return d
    out = dict(d)
    if not out.get("title") and out.get("name"):
        out["title"] = out.get("name")
    if not out.get("original_title") and out.get("original_name"):
        out["original_title"] = out.get("original_name")
    if not out.get("release_date") and out.get("first_air_date"):
        out["release_date"] = out.get("first_air_date")
    if not out.get("runtime"):
        ert = out.get("episode_run_time")
        if isinstance(ert, list) and ert:
            try:
                out["runtime"] = int(ert[0])
            except Exception:
                pass
    return out


def fit_folder_name(title: str, year, tid, parent: Path, longest_child_name: str = "") -> str:
    """Keep full title when path budget allows; shorten only as needed."""
    title = win_safe(title or "") or "untitled"
    year_s = str(year) if year and re.fullmatch(r"\d{4}", str(year)) else ""
    parent_s = str(parent)
    child = longest_child_name or "video.mkv"
    budget = 260 - len(parent_s) - 1 - len(child) - 1
    budget = max(12, min(240, budget))

    def ok(name: str) -> bool:
        return len(name) <= budget

    cands = []
    if tid and year_s:
        cands.append(f"{title} ({year_s}) [tmdbid={tid}]")
    if year_s:
        cands.append(f"{title} ({year_s})")
    cands.append(title)
    short = re.split(r"[:：]", title, maxsplit=1)[0].strip() or title
    if short != title:
        if tid and year_s:
            cands.append(f"{short} ({year_s}) [tmdbid={tid}]")
        if year_s:
            cands.append(f"{short} ({year_s})")
        cands.append(short)
    for c in cands:
        c = win_safe(c)
        if ok(c):
            return c
    if tid and year_s:
        suf = f" ({year_s}) [tmdbid={tid}]"
    elif year_s:
        suf = f" ({year_s})"
    elif tid:
        suf = f" [tmdbid={tid}]"
    else:
        suf = ""
    room = max(4, budget - len(suf))
    head = title if len(title) <= room else (title[: room - 1].rstrip() + "…")
    return win_safe(head + suf)


def long_path(p) -> str:
    s = str(p)
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\"):
        return s
    try:
        s = str(Path(s).resolve())
    except Exception:
        pass
    if re.match(r"^[A-Za-z]:\\", s) or re.match(r"^[A-Za-z]:/", s):
        s = "\\\\?\\" + s.replace("/", "\\")
    return s

# --- end media helpers ---


CACHE_PATH = TOOLS / "tmdb_movie_title_cache.json"
SEARCH_CACHE_PATH = TOOLS / "tmdb_search_cache.json"

# Separators / wrappers around year & tmdbid: space, - _ . · — – and () [] 【】（）
_META_SEP = r"[\s\-_.·•〜～—–]*"
_META_OPEN = r"[\[\(（【]"
_META_CLOSE = r"[\]\)）】]"

# [tmdbid=123] / (tmdbid=123) / 【tmdbid=123】 / tmdbid=123 / --tmdbid_123
TMDB_RE = re.compile(
    rf"(?:{_META_OPEN}){_META_SEP}tmdbid{_META_SEP}[=:_\-]?{_META_SEP}(\d+){_META_SEP}(?:{_META_CLOSE})",
    re.I,
)
TMDB_EQ_RE = re.compile(
    rf"(?:^|(?<=\W)|(?<=[\u4e00-\u9fff])){_META_SEP}(?:tmdb(?:id)?|id){_META_SEP}[=:_\-]?{_META_SEP}(\d{{3,}})(?=$|\W|[\u4e00-\u9fff]|{_META_CLOSE})",
    re.I,
)
BRACKET_ID_RE = re.compile(rf"{_META_OPEN}{_META_SEP}(\d{{5,}}){_META_SEP}{_META_CLOSE}")  # [15859] / (15859)
BARE_ID_RE = re.compile(r"^\d{3,}$")  # folder name is only digits
# Year with optional wrappers: 2025 / (2025) / 【2025】 / --2025 / _2025 / .2025
YEAR_RE = re.compile(
    rf"(?:^|[^\d]|{_META_OPEN}){_META_SEP}((?:19|20)\d{{2}})(?:{_META_SEP}(?:{_META_CLOSE})|(?=[^\d]|$))"
)
def strip_tmdb_markers(s: str) -> str:
    """Remove [tmdbid=N] / tmdbid=N / [12345] from a name. An id between 1900 and 2099
    (怒火攻心 (2006) [tmdbid=1948]) must never be read as a release year."""
    s = TMDB_RE.sub(" ", s or "")
    s = TMDB_EQ_RE.sub(" ", s)
    return BRACKET_ID_RE.sub(" ", s)


HAN_RE = re.compile(r"[\u4e00-\u9fff]")
KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
LATIN_RE = re.compile(r"[A-Za-z]")
ILLEGAL = str.maketrans({
    "\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？",
    '"': "＂", "<": "＜", ">": "＞", "|": "｜",
})
PRUNE = {"@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information"}
VIDEO = {
    ".mkv", ".mp4", ".avi", ".wmv", ".m2ts", ".ts", ".iso",
    ".mpg", ".mpeg", ".mov", ".m4v", ".rmvb", ".flv", ".webm",
}
EXTRAS_DIR = {
    "extras", "extra", "bonus", "featurettes", "interviews", "deleted scenes",
    "trailers", "trailer", "samples", "sample", "subs", "subtitles", "subtitle",
    "behind the scenes", "other", "misc",
}
SERIES = re.compile(r"(系列|Collection|Trilogy|合集)", re.I)
JUNK = re.compile(
    r"(?i)\b("
    r"bluray|blu\-?ray|web\-?dl|webrip|hdtv|dvdrip|bdrip|remux|hdr|dv|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|aac|dts(?:-?hd)?|truehd|atmos|"
    r"1080p|2160p|720p|480p|4k|uhd|hdr10|sdr|dolby|vision|"
    r"chinese|chs|cht|eng|jpn|multi|proper|repack|extended|uncut|dc|"
    r"complete|limited|internal|sample|"
    r"telesync|telecine|hdts|hdcam|hd\-?cam|screener|dvdscr|workprint|"
    r"ts|tc|cam|scr|r5|wp"
    r")\b"
)

_DISC_ART_WARNED = False


def movie_tmdb_id(text: str) -> str:
    """Movie-level TMDB id only — never actor/crew <tmdbid>."""
    m = re.search(
        r"<uniqueid[^>]*type=['\"]tmdb(?:id)?['\"][^>]*>\s*([^<]+)\s*</uniqueid>",
        text,
        re.I,
    )
    if m:
        return re.sub(r"\D", "", m.group(1))
    cleaned = re.sub(
        r"<(actor|director|writer|producer|gueststar)\b[^>]*>.*?</\1>",
        "",
        text,
        flags=re.I | re.S,
    )
    m = re.search(r"<tmdbid>\s*([^<]+)\s*</tmdbid>", cleaned, re.I)
    return re.sub(r"\D", "", m.group(1)) if m else ""


def grab_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*([^<]+)\s*</{tag}>", text, re.I)
    return m.group(1).strip() if m else ""


def parse_nfo(text: str) -> dict:
    if re.search(r"<episodedetails\b", text, re.I):
        return {}
    tmdb = movie_tmdb_id(text)
    year = grab_tag(text, "year")
    if not year:
        m = re.search(r"<premiered>(\d{4})", text, re.I)
        year = m.group(1) if m else ""
    if not year:
        m = re.search(r"<releasedate>(\d{4})", text, re.I)
        year = m.group(1) if m else ""
    return {
        "tmdb": tmdb or None,
        "title": grab_tag(text, "title") or None,
        "originaltitle": grab_tag(text, "originaltitle") or None,
        "year": (year[:4] if year else None),
    }


def win_safe(s: str) -> str:
    """Windows-illegal chars -> fullwidth. Keep middle-dot and ellipsis."""
    s = (s or "").replace("\u00a0", " ").replace("\u3000", " ")
    s = s.translate(ILLEGAL)
    s = re.sub(r"[\t\r\n]+", " ", s)
    return re.sub(r" +", " ", s).strip(" .")


def xml_escape(s: str) -> str:
    return xml.sax.saxutils.escape(s or "", {"'": "&apos;", '"': "&quot;"})


# One run asks for the same search/detail URL many times (classify, then
# score, then the next folder with the same title). Cache the body.
# Stay under TMDB's rate limit and wait out HTTP 429 instead of recording the
# movie as a failure.
_URL_CACHE: dict[str, dict] = {}
_REQ_TIMES: list[float] = []


_PACE_LOCK = threading.Lock()


def _pace_requests() -> None:
    """Sliding window, safe to call from several threads.

    TMDB allows roughly 50 requests/s; stay at 40/s. HTTP 429 is still waited out
    in api_get.
    """
    window = 1.0
    limit = 40
    while True:
        with _PACE_LOCK:
            now = time.monotonic()
            while _REQ_TIMES and now - _REQ_TIMES[0] >= window:
                _REQ_TIMES.pop(0)
            if len(_REQ_TIMES) < limit:
                _REQ_TIMES.append(now)
                return
            wait = window - (now - _REQ_TIMES[0]) + 0.01
        time.sleep(max(wait, 0.01))


# Worker threads for network-bound steps (search, detail fetch, artwork).
try:
    WORKERS = max(1, int(os.environ.get("TMDB_WORKERS") or 8))
except ValueError:
    WORKERS = 8


def _pmap(fn, items):
    """fn over items on WORKERS threads, results in input order."""
    items = list(items)
    if WORKERS <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(items))) as ex:
        return list(ex.map(fn, items))


# HTTP with connection reuse. urllib opens a new TLS connection (and rebuilds the CA
# store) for every request, which was most of the time of a scan. Each worker thread
# keeps one connection per host; an HTTP proxy from the environment is tunnelled
# through. Anything unusual (authenticated proxy, ...) falls back to urllib.
_SSL_CTX: list = []
_HTTP_TLS = threading.local()
_FALLBACK_OPENER: list = []


def _ssl_context() -> ssl.SSLContext:
    if not _SSL_CTX:
        _SSL_CTX.append(ssl.create_default_context())
    return _SSL_CTX[0]


def _new_connection(host: str):
    """A connection object for host, or None when urllib has to be used."""
    try:
        proxy = urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http")
        if proxy and urllib.request.proxy_bypass(host):
            proxy = None
        if not proxy:
            return http.client.HTTPSConnection(host, timeout=60, context=_ssl_context())
        pr = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
        if pr.scheme != "http" or pr.username or not pr.hostname:
            return None
        c = http.client.HTTPSConnection(pr.hostname, pr.port or 80, timeout=60, context=_ssl_context())
        c.set_tunnel(host, 443)
        return c
    except Exception:
        return None


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    """GET url and return the body. HTTP errors raise urllib.error.HTTPError, as urlopen did."""
    pu = urllib.parse.urlparse(url)
    host = pu.netloc
    path = pu.path + ("?" + pu.query if pu.query else "")
    conns = getattr(_HTTP_TLS, "conns", None)
    if conns is None:
        conns = _HTTP_TLS.conns = {}
    headers = {"User-Agent": UA}
    for attempt in (1, 2):
        c = conns.get(host)
        if c is None and host not in getattr(_HTTP_TLS, "no_pool", set()):
            c = _new_connection(host)
            if c is None:
                _HTTP_TLS.no_pool = getattr(_HTTP_TLS, "no_pool", set()) | {host}
            else:
                conns[host] = c
        if c is None:
            if not _FALLBACK_OPENER:
                _FALLBACK_OPENER.append(urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context())))
            req = urllib.request.Request(url, headers=headers)
            with _FALLBACK_OPENER[0].open(req, timeout=timeout) as r:
                return r.read()
        try:
            c.timeout = timeout
            c.request("GET", path, headers=headers)
            r = c.getresponse()
            body = r.read()
            if r.will_close:
                conns.pop(host, None)
                c.close()
        except Exception:
            # A kept-alive connection the server has closed: retry once on a new one.
            conns.pop(host, None)
            try:
                c.close()
            except Exception:
                pass
            if attempt == 2:
                raise
            continue
        if r.status >= 400:
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, None)
        return body
    raise RuntimeError("unreachable")


def api_get(url: str, retries: int = 4):
    cached = _URL_CACHE.get(url)
    if isinstance(cached, dict):
        return cached
    attempt = 0
    rate_tries = 0
    while attempt < retries:
        _pace_requests()
        try:
            data = json.loads(_http_get(url, timeout=30).decode("utf-8", "ignore"))
            if not isinstance(data, dict):
                data = {"_error": "bad_response", "raw": data}
            _URL_CACHE[url] = data
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429 and rate_tries < 6:
                rate_tries += 1
                ra = 0.0
                try:
                    ra = float(e.headers.get("Retry-After") or 0)
                except Exception:
                    ra = 0.0
                time.sleep(ra if ra > 0 else min(8.0, 1.5 * rate_tries))
                continue
            attempt += 1
            if e.code in (401, 404):
                # Definitive answers (bad key / no such id): no point retrying.
                err = {"_error": f"HTTP {e.code}"}
                _URL_CACHE[url] = err
                return err
            if attempt >= retries:
                # Transient (5xx, ...): report, but do not cache for the run.
                return {"_error": f"HTTP {e.code}"}
            time.sleep(0.6 * attempt)
        except Exception as e:
            attempt += 1
            if attempt >= retries:
                return {"_error": str(e)}
            time.sleep(1.5 * attempt)
    return {"_error": "unknown"}


def check_api_key() -> tuple[bool, str]:
    """Ask TMDB once whether the key works. Returns (ok, message in Chinese)."""
    if not API_KEY:
        return False, "没有 TMDB API Key。请在程序首页填写，或设置环境变量 TMDB_API_KEY。"
    data = api_get(f"https://api.themoviedb.org/3/configuration?api_key={API_KEY}")
    err = str((data or {}).get("_error") or "")
    if not err:
        return True, ""
    if err == "HTTP 401":
        return False, "TMDB API Key 无效（HTTP 401）。请检查是否使用 v3 的 API Key。"
    return False, f"连不上 TMDB（{err}）。请检查网络 / 代理后重试。"


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json(path: Path, data: dict) -> None:
    """Write via a temp file and rename, so a crash never leaves a half-written cache.
    The big caches are written compact; logs stay readable."""
    try:
        TOOLS.mkdir(parents=True, exist_ok=True)
        big = path.name in ("tmdb_movie_title_cache.json", "tmdb_search_cache.json", "tmdb_id_exists.json")
        text = json.dumps(data, ensure_ascii=False, indent=None if big else 2, separators=(",", ":") if big else None)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception as e:
        print(f"  warn: save failed {path.name}: {e}", flush=True)


def readable_han_title(title: str) -> bool:
    """True for Chinese titles: has Han, and not Japanese (no kana)."""
    t = (title or "").strip()
    if not t or not HAN_RE.search(t):
        return False
    if KANA_RE.search(t):
        return False
    return True


def is_japanese_title(title: str) -> bool:
    t = (title or "").strip()
    return bool(t and KANA_RE.search(t))


def is_english_title(title: str) -> bool:
    """Latin/digit titles like Top Gun: Maverick or 1917 — keep as-is from zh-CN page."""
    t = (title or "").strip()
    if not t:
        return False
    if HAN_RE.search(t) or KANA_RE.search(t):
        return False
    # Must have a letter or digit; allow punctuation/spaces
    if not (LATIN_RE.search(t) or re.search(r"\d", t)):
        return False
    return True


def pick_from_langs(zh_cn, zh_tw, zh_hk, en, base):
    """Title preference:
    1) zh-CN if real Chinese (Han, not kana)
    2) zh-CN if already English/Latin (Top Gun, 1917) — keep, do NOT replace with zh-TW
    3) zh-TW / zh-HK if real Chinese (when zh-CN is Japanese etc.)
    4) original_title (English movies naturally stay English here)
    """
    def title_of(d):
        if not isinstance(d, dict) or d.get("_error"):
            return ""
        return (d.get("title") or "").strip()

    zh_t = title_of(zh_cn)
    tw_t = title_of(zh_tw)
    hk_t = title_of(zh_hk)
    en_t = title_of(en)
    orig = ""
    for d in (zh_cn, en, zh_tw, zh_hk, base):
        if isinstance(d, dict) and not d.get("_error"):
            ot = (d.get("original_title") or "").strip()
            if ot:
                orig = ot
                break
    release = ""
    for d in (zh_cn, en, zh_tw, zh_hk):
        if isinstance(d, dict) and d.get("release_date"):
            release = d["release_date"]
            break
    year = release[:4] if release and len(release) >= 4 and release[:4].isdigit() else None

    # 1) Proper Chinese on zh-CN (Han, not Japanese)
    if readable_han_title(zh_t):
        return zh_t, year, "zh-CN"
    # 2) zh-CN page already English — keep
    if is_english_title(zh_t):
        return zh_t, year, "zh-CN-en"
    # 3) Other Chinese when zh-CN was Japanese / empty / unusable
    if readable_han_title(tw_t):
        return tw_t, year, "zh-TW"
    if readable_han_title(hk_t):
        return hk_t, year, "zh-HK"
    # 4) Original title directly (English movies naturally stay English)
    if orig:
        return orig, year, "original"
    if en_t:
        return en_t, year, "en"
    if zh_t and not is_japanese_title(zh_t):
        return zh_t, year, "zh-CN-fallback"
    if zh_t:
        return zh_t, year, "zh-CN-fallback"
    return (tw_t or hk_t or ""), year, "fallback"


def _pick_overview(zh, tw, hk, en, base) -> str:
    for d in (zh, tw, hk, en, base):
        if isinstance(d, dict) and not d.get("_error"):
            ov = (d.get("overview") or "").strip()
            if ov:
                return ov
    return ""


def _pick_best_image(images: list, prefer_langs=("zh", "zh-CN", "zh-TW", "zh-HK", None)) -> str:
    """Pick best image path: prefer language then highest vote_average / vote_count."""
    if not images:
        return ""
    ranked = []
    for img in images:
        path = img.get("file_path") or ""
        if not path:
            continue
        lang = img.get("iso_639_1")
        try:
            lang_rank = prefer_langs.index(lang)
        except ValueError:
            lang_rank = 99
        vote = float(img.get("vote_average") or 0)
        votes = float(img.get("vote_count") or 0)
        ranked.append((lang_rank, -vote, -votes, path))
    if not ranked:
        return ""
    ranked.sort()
    return ranked[0][3]


def fetch_movie_images(tid: str, kind: str | None = None) -> dict:
    """GET /movie/{id}/images — best poster, backdrop, logo."""
    data = api_get(
        f"https://api.themoviedb.org/3/{('tv' if (str(kind or '').lower()=='tv' or (str(kind or '').lower() not in ('movie','tv') and media_is_tv())) else 'movie')}/{tid}/images?api_key={API_KEY}"
        f"&include_image_language=zh,zh-CN,zh-TW,zh-HK,en,null"
    )
    return _images_from_payload(data)


def _images_from_payload(data) -> dict:
    """Best poster / backdrop / logo from an /images payload (or its appended copy)."""
    if not isinstance(data, dict) or data.get("_error"):
        return {"poster_path": "", "backdrop_path": "", "logo_path": ""}
    poster = _pick_best_image(data.get("posters") or [])
    backdrop = _pick_best_image(data.get("backdrops") or [], prefer_langs=(None, "zh", "zh-CN", "en"))
    logo = _pick_best_image(data.get("logos") or [])
    return {
        "poster_path": poster or "",
        "backdrop_path": backdrop or "",
        "logo_path": logo or "",
    }


def cache_needs_refetch(entry: dict) -> bool:
    """True if cache ok but missing poster_path or art dict (fixes no_poster_path bug)."""
    if not entry or not entry.get("ok") or not entry.get("picked_title"):
        return True
    if not entry.get("poster_path"):
        return True
    art = entry.get("art")
    if not isinstance(art, dict):
        return True
    # Force refetch for richer Emby NFO metadata (nfo_ver >= 3)
    nfo_ver = entry.get("nfo_ver")
    try:
        if nfo_ver is None or int(nfo_ver) < 3:
            return True
    except (TypeError, ValueError):
        return True
    # art present is enough; empty paths inside are fine (movie may lack logo)
    return False




def ckey(tid, kind: str | None = None) -> str:
    """Metadata cache key. A movie and a TV show can share one numeric id, so a
    show is stored as 'tv:<id>'; movies (and unknown kinds) keep the bare id."""
    return f"tv:{tid}" if kind == "tv" else str(tid)


def fetch_movie(tid: str, cache: dict, force: bool = False, kind: str | None = None) -> dict:
    tid = str(tid)
    ck = ckey(tid, kind)
    if not force and ck in cache and not cache_needs_refetch(cache[ck]):
        c = cache[ck]
        if c.get("title_zh") is not None or c.get("title_en") is not None:
            return c

    if kind not in ("movie", "tv"):
        if media_is_auto():
            try:
                kind = classify_tmdb_id_kind(str(tid), "") or "movie"
            except Exception:
                kind = "movie"
            if kind not in ("movie", "tv"):
                kind = "movie"
        else:
            kind = "tv" if media_is_tv() else "movie"
    ck = ckey(tid, kind)
    append = (
        "&append_to_response=credits,external_ids,content_ratings,videos,images"
        if kind == "tv"
        else "&append_to_response=credits,release_dates,videos,external_ids,images"
    ) + "&include_image_language=zh,zh-CN,zh-TW,zh-HK,en,null"
    zh = api_get(
        f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}"
        f"&language=zh-CN{append}"
    )
    # zh-CN already carries original_title, credits, and release dates.
    # zh-TW / zh-HK are only for the case where zh-CN is Japanese or empty.
    zh_title = ""
    zh_ok = isinstance(zh, dict) and not zh.get("_error")
    if zh_ok:
        zh_title = (zh.get("title") or zh.get("name") or "").strip()
    tw = None
    hk = None
    if zh_ok and (readable_han_title(zh_title) or is_english_title(zh_title)):
        en = zh
    elif zh_ok:
        tw = api_get(f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}&language=zh-TW")
        tw_title = ""
        if isinstance(tw, dict) and not tw.get("_error"):
            tw_title = (tw.get("title") or tw.get("name") or "").strip()
        if not readable_han_title(tw_title):
            hk = api_get(f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}&language=zh-HK")
        en = zh
    else:
        en = api_get(
            f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}"
            f"&language=en-US{append}"
        )
        en_title = ""
        if isinstance(en, dict) and not en.get("_error"):
            en_title = (en.get("title") or en.get("name") or "").strip()
        if not (readable_han_title(en_title) or is_english_title(en_title)):
            tw = api_get(f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}&language=zh-TW")
            tw_title = ""
            if isinstance(tw, dict) and not tw.get("_error"):
                tw_title = (tw.get("title") or tw.get("name") or "").strip()
            if not readable_han_title(tw_title):
                hk = api_get(f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}&language=zh-HK")
    if kind == "tv":
        zh = _normalize_tv_payload(zh)
        tw = _normalize_tv_payload(tw)
        hk = _normalize_tv_payload(hk)
        en = _normalize_tv_payload(en)
    base = zh if isinstance(zh, dict) and not zh.get("_error") else en
    if kind == "tv":
        base = _normalize_tv_payload(base)
    if not isinstance(base, dict) or base.get("_error"):
        cache[ck] = {"ok": False, "error": (base or {}).get("_error")}
        if isinstance(cache.get(ck), dict) and kind in ("movie", "tv"):
            cache[ck]["kind"] = kind
        return cache[ck]

    picked, year, lang = pick_from_langs(zh, tw, hk, en, base)
    poster = ""
    backdrop = ""
    for d in (zh, en, tw, hk, base):
        if isinstance(d, dict):
            if not poster and d.get("poster_path"):
                poster = d.get("poster_path") or ""
            if not backdrop and d.get("backdrop_path"):
                backdrop = d.get("backdrop_path") or ""

    # images normally ride along in the detail call (append_to_response)
    appended = base.get("images") if isinstance(base, dict) else None
    imgs = _images_from_payload(appended) if isinstance(appended, dict) else fetch_movie_images(tid, kind)
    if imgs.get("poster_path"):
        poster = imgs["poster_path"]
    if imgs.get("backdrop_path"):
        backdrop = imgs["backdrop_path"]
    logo = imgs.get("logo_path") or ""

    overview = _pick_overview(zh, tw, hk, en, base)
    genres = []
    for d in (zh, tw, hk, en, base):
        if isinstance(d, dict) and d.get("genres"):
            genres = [g.get("name") for g in d["genres"] if isinstance(g, dict) and g.get("name")]
            if genres:
                break
    runtime = None
    for d in (zh, en, tw, hk, base):
        if isinstance(d, dict) and d.get("runtime"):
            runtime = d.get("runtime")
            break
    imdb_id = ""
    for d in (en, zh, base):
        if isinstance(d, dict) and d.get("imdb_id"):
            imdb_id = d.get("imdb_id") or ""
            break
    release_date = ""
    for d in (zh, en, tw, hk):
        if isinstance(d, dict) and d.get("release_date"):
            release_date = d["release_date"]
            break

    # Credits / cast / crew from zh-CN (fallback en)
    credits_payload = None
    for d in (zh, en):
        if isinstance(d, dict) and isinstance(d.get("credits"), dict):
            credits_payload = d["credits"]
            break
    cast = []
    directors = []
    writers = []
    if isinstance(credits_payload, dict):
        raw_cast = [c for c in (credits_payload.get("cast") or []) if isinstance(c, dict)]
        raw_cast.sort(key=lambda c: c.get("order") if c.get("order") is not None else 9999)
        # No actor cap — match Emby fullness (only-more-not-less)
        for c in raw_cast:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            role = (c.get("character") or "").strip()
            cid = c.get("id")
            entry = {"name": name, "type": "Actor"}
            if role:
                entry["role"] = role
                entry["character"] = role
            if cid is not None:
                entry["tmdbid"] = str(cid)
                entry["id"] = cid
            cast.append(entry)
        seen_crew = set()
        for c in credits_payload.get("crew") or []:
            if not isinstance(c, dict):
                continue
            job = (c.get("job") or "").strip()
            name = (c.get("name") or "").strip()
            if not name:
                continue
            cid = c.get("id")
            person = {"name": name}
            if cid is not None:
                person["tmdbid"] = str(cid)
                person["id"] = cid
            key = (str(cid) if cid is not None else name, job)
            if key in seen_crew:
                continue
            seen_crew.add(key)
            if job == "Director":
                directors.append(person)
            elif job in ("Writer", "Screenplay", "Story"):
                writers.append(person)
            elif job in ("Producer", "Executive Producer", "Co-Producer", "Associate Producer"):
                # Emby often lists producers inside <actor type=Producer>
                prod = {"name": name, "type": "Producer", "role": job}
                if cid is not None:
                    prod["tmdbid"] = str(cid)
                    prod["id"] = cid
                cast.append(prod)
            elif job in (
                "Conductor",
                "Original Music Composer",
                "Composer",
                "Music",
                "Music Director",
            ):
                # Emby may list conductors/composers as <actor type=Conductor|...>
                atype = "Conductor" if "Conductor" in job or "Music Director" in job else "Composer"
                mus = {"name": name, "type": atype, "role": job}
                if cid is not None:
                    mus["tmdbid"] = str(cid)
                    mus["id"] = cid
                cast.append(mus)

    rating = None
    for d in (zh, en, base):
        if isinstance(d, dict) and d.get("vote_average") is not None:
            try:
                rating = float(d.get("vote_average"))
            except (TypeError, ValueError):
                rating = d.get("vote_average")
            break

    countries = []
    for d in (zh, en, base):
        if isinstance(d, dict) and d.get("production_countries"):
            countries = [
                (c.get("name") or "").strip()
                for c in d["production_countries"]
                if isinstance(c, dict) and (c.get("name") or "").strip()
            ]
            if countries:
                break

    studios = []
    for d in (zh, en, base):
        if isinstance(d, dict) and d.get("production_companies"):
            studios = [
                (c.get("name") or "").strip()
                for c in d["production_companies"]
                if isinstance(c, dict) and (c.get("name") or "").strip()
            ]
            if studios:
                break

    collection = None
    for d in (zh, en, base):
        if not isinstance(d, dict):
            continue
        btc = d.get("belongs_to_collection")
        if isinstance(btc, dict) and btc.get("id") is not None:
            col_id = btc.get("id")
            col_name = (btc.get("name") or "").strip()
            if not col_name:
                col_data = api_get(
                    f"https://api.themoviedb.org/3/collection/{col_id}"
                    f"?api_key={API_KEY}&language=zh-CN"
                )
                if isinstance(col_data, dict) and not col_data.get("_error"):
                    col_name = (col_data.get("name") or "").strip()
            collection = {"id": col_id, "name": col_name}
            break

    # tagline (prefer zh then en)
    tagline = ""
    for d in (zh, tw, hk, en, base):
        if isinstance(d, dict) and (d.get("tagline") or "").strip():
            tagline = (d.get("tagline") or "").strip()
            break

    # MPAA / certification from release_dates (prefer US then CN then any)
    mpaa = ""
    for d in (en, zh, base):
        if not isinstance(d, dict):
            continue
        rd = d.get("release_dates") or {}
        results = rd.get("results") if isinstance(rd, dict) else None
        if not results:
            continue
        by_cc = {r.get("iso_3166_1"): r for r in results if isinstance(r, dict)}
        for cc in ("US", "CN", "GB", "HK", "TW"):
            block = by_cc.get(cc) or {}
            for item in block.get("release_dates") or []:
                if not isinstance(item, dict):
                    continue
                cert = (item.get("certification") or "").strip()
                if cert:
                    mpaa = cert
                    break
            if mpaa:
                break
        if mpaa:
            break

    # Trailer YouTube URL from videos
    trailer = ""
    for d in (zh, en, base):
        if not isinstance(d, dict):
            continue
        vids = d.get("videos") or {}
        results = vids.get("results") if isinstance(vids, dict) else None
        if not results:
            continue
        # prefer official Trailer YouTube
        ranked = []
        for v in results:
            if not isinstance(v, dict):
                continue
            if (v.get("site") or "").lower() != "youtube":
                continue
            key = (v.get("key") or "").strip()
            if not key:
                continue
            typ = (v.get("type") or "").lower()
            score = 0
            if typ == "trailer":
                score += 10
            if v.get("official"):
                score += 5
            ranked.append((score, key))
        if ranked:
            ranked.sort(key=lambda x: -x[0])
            trailer = f"https://www.youtube.com/watch?v={ranked[0][1]}"
            break

    # tvdb id from external_ids
    tvdb_id = ""
    for d in (en, zh, base):
        if not isinstance(d, dict):
            continue
        ext = d.get("external_ids") if isinstance(d.get("external_ids"), dict) else None
        if not ext:
            # sometimes top-level
            if d.get("tvdb_id"):
                tvdb_id = str(d.get("tvdb_id"))
                break
            continue
        if ext.get("tvdb_id"):
            tvdb_id = str(ext.get("tvdb_id"))
            break

    cache[ck] = {
        "ok": bool(picked),

        "tmdb": tid,
        "title_zh": (zh or {}).get("title") if isinstance(zh, dict) else None,
        "title_tw": (tw or {}).get("title") if isinstance(tw, dict) else None,
        "title_hk": (hk or {}).get("title") if isinstance(hk, dict) else None,
        "title_en": (en or {}).get("title") if isinstance(en, dict) else None,
        "original_title": (base or {}).get("original_title"),
        "year": year,
        "picked_title": picked,
        "picked_lang": lang,
        "release_date": release_date,
        "poster_path": poster,
        "backdrop_path": backdrop,
        "logo_path": logo,
        "art": {
            "poster": poster,
            "backdrop": backdrop,
            "logo": logo,
        },
        "overview": overview,
        "plot": overview,
        "genres": genres,
        "runtime": runtime,
        "imdb_id": imdb_id,
        "cast": cast,
        "directors": directors,
        "writers": writers,
        "rating": rating,
        "countries": countries,
        "studios": studios,
        "collection": collection,
        "credits": writers,
        "tagline": tagline,
        "mpaa": mpaa,
        "trailer": trailer,
        "tvdb_id": tvdb_id,
        "nfo_ver": 3,
    }
    if not picked:
        cache[ck]["ok"] = False
        cache[ck]["error"] = "empty_title"
    if isinstance(cache.get(ck), dict) and kind in ("movie", "tv"):
        cache[ck]["kind"] = kind
    return cache[ck]


def extract_year(name: str):
    """Release year for TMDB search.

    Year *ranges* in the folder name (e.g. 1940–1958 in an anthology title) are
    NOT a release year — the Blu-ray may be 2025. Ignore them so search is not
    filtered to the wrong decade.
    """
    s = name or ""
    if re.search(r"(?:19|20)\d{2}\s*[–—\-]\s*(?:19|20)\d{2}", s):
        return None
    s = strip_tmdb_markers(s)
    # A year in brackets is the release year; a bare 4-digit number next to it
    # (怒火攻心 (2006) 1948) may be an id or part of the title.
    bracketed = re.findall(r"[\(\[（【]\s*((?:19|20)\d{2})\s*[\)\]）】]", s)
    if bracketed:
        return bracketed[-1]
    years = YEAR_RE.findall(s)
    if not years:
        return None
    return years[-1]


def other_year_candidates(name: str, year: str | None) -> list:
    """Other year-looking numbers in a name, last first: tried when the chosen year finds nothing."""
    s = strip_tmdb_markers(name or "")
    if re.search(r"(?:19|20)\d{2}\s*[–—\-]\s*(?:19|20)\d{2}", s):
        return []
    out = []
    for y in reversed(YEAR_RE.findall(s)):
        if y != year and y not in out:
            out.append(y)
    return out


def strip_year_range_tokens(s: str) -> str:
    """Remove anthology spans like 1940-1958 / 1940–1958 from a search string."""
    s = s or ""
    s = re.sub(r"(?:^|[^\d])(?:19|20)\d{2}\s*[–—\-]\s*(?:19|20)\d{2}(?=[^\d]|$)", " ", s)
    return re.sub(r"\s+", " ", s).strip(" ._-")


_RELEASE_JUNK = re.compile(
    r"(?ix)(?:"
    r"\b(?:(?:360|480|720|1080|2160|4320)p|uhd|fhd|hd|sd|4k|8k|hdr10\+?|hdr|dv|dovi|dolby|vision)\b|"
    r"\b(?:bluray|blu\-?ray|bdrip|brrip|bdremux|web\-?dl|webrip|hdtv|remux|encode|bdmv)\b|"
    r"\b(?:x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx)\b|"
    r"\b(?:aac|ac3|eac3|dts(?:\-?hd)?(?:\-?ma)?(?:\-?x)?|truehd|atmos|flac|lpcm|mp3|ddp?|dd)\b|"
    r"\b(?:10bit|8bit|main\.?10|hi10p)\b|"
    r"\b(?:ma|truehd)\s*\d(?:\.\d)?\b|"
    r"\b\d(?:\.\d)?\s*(?:ch|channels?)\b|"
    r"\b(?:disc|disk|cd|dvd|bd)\s*\d+\b|"
    r"\b(?:proper|repack|extended|theatrical|directors?\.?cut|unrated|limited|hybrid)\b|"
    r"\b(?:itunes|web)\b|"
    r"\b(?:p[0-9])\b|"
    r"[\-_.]?pt\d+|"
    r"@\w+|"
    r"\b(?:hdsky|chd|wiki|frds|pter|hds|ttg|cmct|ourbits|mteam|ntb|flux|amzn|nf|dsnp|atvp|fgt)\b"
    r")"
)


def strip_release_junk(s: str) -> str:
    """Strip codec/resolution/audio/group junk from a release-style name.

    Keeps sequel / title digits (Paddington 2, Spider-Man 3, 2 Fast 2 Furious).
    Keeps title dimension tags glued to CJK (蜜桃成熟時33D).
    Language tags only when ALL-CAPS release markers (GERMAN), not title words (Italian Job).
    Does not strip mid-title words that collide with group names (Beast vs beAst).
    """
    s = s or ""
    # Drop bracketed release teams early: [Ben The Men], 【FRDS】
    def _br_drop(m):
        inner = (m.group(1) or "").strip()
        if re.fullmatch(r"(?:19|20)\d{2}", inner):
            return m.group(0)
        if re.search(r"(?i)tmdbid\s*=", inner):
            return m.group(0)
        # keep short pure CJK bracket titles
        if re.fullmatch(r"[\u4e00-\u9fff·・]{2,40}", inner):
            return m.group(0)
        return " "
    s = re.sub(r"[\[【\(（]([^\]】\)）]+)[\]】\)）]", _br_drop, s)
    # Trailing -Group / .Group (Atmos-jennaortega, h264-watcher)
    s = re.sub(r"(?i)[\-.][A-Za-z][A-Za-z0-9]{1,24}$", " ", s)
    s = re.sub(r"[\[【].*$", " ", s)  # unclosed [Ben T...
    s = re.sub(r"(?i)\bmulti\b", " ", s)
    s = re.sub(r"(?i)(?<=\w\s)DC(?=\s|$|[\._\-])", " ", s)  # mid Director's Cut only; keep leading DC League

    # Glued resolution: ongbak1080p / Title2160p
    # H.264 / x264 / H.265 before leftover lone H / 264
    s = re.sub(r"(?i)[hx]\.?264", " ", s)
    s = re.sub(r"(?i)[hx]\.?265", " ", s)
    s = re.sub(r"(?i)\bhevc\b", " ", s)
    s = re.sub(r"(?i)\bavc\b", " ", s)
    s = re.sub(r"(?i)(?<![0-9])(?:360|480|720|1080|2160|4320)p", " ", s)
    # HDR10+ / DoVi / BDRemux / iTunes before dot-split (avoid leftover 10+)
    s = re.sub(r"(?i)hdr10\+?", " ", s)
    s = re.sub(r"(?i)\b10\+", " ", s)
    s = re.sub(r"(?i)\b(?:bd)?remux\b", " ", s)
    s = re.sub(r"(?i)\bdovi\b", " ", s)
    s = re.sub(r"(?i)\bhybrid\b", " ", s)
    s = re.sub(r"(?i)\bitunes\b", " ", s)
    s = re.sub(r"(?i)(?:^|[\s._\-])iT(?=$|[\s._\-])", " ", s)
    s = re.sub(r"(?i)\bweb[\-_.]?dl\b", " ", s)
    s = re.sub(r"(?i)\bweb[\-_.]?rip\b", " ", s)
    s = re.sub(r"(?i)(?:^|[\s._\-])web(?=$|[\s._\-](?:h\.?26|x26|dl|rip))", " ", s)
    s = re.sub(r"(?i)\bddp?\s*\d(?:\.\d)?\b", " ", s)
    s = re.sub(r"(?i)\bp[0-9]\b", " ", s)
    s = re.sub(r"(?i)\bby\b", " ", s)
    # Work on dotted form first so DTS-X / 7.1 / DTS-HD.MA stay glued
    s = re.sub(r"(?i)dts[\-_.]?hd[\-_.]?ma", " ", s)
    s = re.sub(r"(?i)dts[\-_.]?x\b", " ", s)
    s = re.sub(r"(?i)dts[\-_.]?hd\b", " ", s)
    s = re.sub(r"(?i)\btruehd\b", " ", s)
    s = re.sub(r"(?i)\batmos\b", " ", s)
    s = re.sub(r"(?i)\b\d(?:\.\d)\b", " ", s)  # 5.1 / 7.1 before dot-split
    s = re.sub(
        r"(?i)\b(?:deluxe|special|ultimate|collector'?s?|limited|anniversary|remastered)[\s._\-]*edition\b",
        " ",
        s,
    )
    s = re.sub(r"(?i)\b(?:director'?s?\s*cut|theatrical\s*cut|extended\s*cut)\b", " ", s)
    s = re.sub(
        r"(?<![A-Za-z])(?:GERMAN|FRENCH|ITALIAN|SPANISH|JAPANESE|KOREAN|RUSSIAN|HINDI|CHINESE|"
        r"ICELANDIC|SWEDISH|DANISH|NORWEGIAN|FINNISH|DUTCH|POLISH|CZECH|HUNGARIAN|TURKISH|"
        r"THAI|VIETNAMESE|ARABIC|HEBREW|GREEK|PORTUGUESE|BRAZILIAN|LATIN|NORDIC|MULTI|"
        r"PROPER|REPACK|EXTENDED|UNRATED|LIMITED|INTERNAL|THEATRICAL|IMAX|OAR|SDR|HDR|INTEGRAL|"
        r"FRA|GER|ITA|JPN|KOR|RUS|CHI|ENG|SPA)(?![A-Za-z])",
        " ",
        s,
    )
    s = re.sub(
        r"(?i)(?:^|[\s._\-])(?:ru|fr|de|es|it|jp|ja|ko|cn|zh|nl|se|sv|no|dk|da|fi|pl|cz|hu|th|vn|ar|he|tr|el|pt|br|ua|uk)(?=$|[\s._\-](?:\d{3,4}p|Blu|WEB|REMUX|HEVC|x26|FGT))",
        " ",
        s,
    )
    s = re.sub(r"(?i)[\s._\-](?:ru|fr|de|es|it|jp|ja|ko|cn|zh)(?=$)", " ", s)
    s = s.replace(".", " ").replace("_", " ").replace("-", " ")
    s = _RELEASE_JUNK.sub(" ", s)
    s = re.sub(r"(?i)\bdts\s*x\b", " ", s)
    s = re.sub(r"(?i)\b(?:ma|hd|truehd|atmos|ddp?|dd|aac|flac|lpcm|eac3|ac3)\b", " ", s)
    s = re.sub(r"(?i)\bblu\s*ray\b", " ", s)
    s = re.sub(r"(?i)\b(?:uhd|remux|encode|hybrid|dovi|itunes)\b", " ", s)
    s = re.sub(r"(?i)\b(?:web|dl)\b", " ", s)  # leftover WEB DL tokens after split
    s = re.sub(r"@\S+", " ", s)
    s = re.sub(r"(?i)\b\d+(?:\.\d+)?\s*(?:ch|channels?)\b", " ", s)
    s = re.sub(r"\b\d+\.\d+\b", " ", s)
    s = re.sub(r"(?i)(?:\s|^)([257])\s+1(?=\s|$)", " ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)
    s = re.sub(r"(?i)\b10\+", " ", s)
    s = re.sub(
        r"(?i)\b(?:fgt|sparks|rarbg|yts|yify|ctrlhd|ntb|flux|d3g|vxt|evo|cmrg|"
        r"hdsky|chd|wiki|frds|pter|hds|ttg|cmct|ourbits|mteam|ntg|dvt|cls|watcher|"
        r"jennaortega|spacehd\d*|hone|usury|pawel\d*)\b",
        " ",
        s,
    )
    s = re.sub(r"(?i)(?:\s|^)x(?=\s|$)", " ", s)
    s = re.sub(
        r"(?i)(?:\s|^)(sparks|rarbg|yts|yify|fgt|ntb|flux|ctrlhd)\s*$",
        " ",
        s,
    )
    s = re.sub(r"(?:\s|^)beAst\s*$", " ", s)
    s = re.sub(r"(?i)[\-_.]beast\s*$", " ", s)
    # leftover lone + from HDR10+
    s = re.sub(r"(?:\s|^)\+(?=\s|$)", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ._-@+")
    return s


def clean_query_title(name: str) -> str:
    s = strip_season_tokens(strip_site_tags(name or ""))
    s = strip_media_hint_tokens(s)
    s = strip_release_junk(s)
    s = TMDB_RE.sub(" ", s)
    s = TMDB_EQ_RE.sub(" ", s)
    s = BRACKET_ID_RE.sub(" ", s)
    s = JUNK.sub(" ", s)
    # Drop year ranges first (1940-1958 / 1940–1958), then lone years
    s = re.sub(r"(?:^|[^\d])(?:19|20)\d{2}\s*[–—\-]\s*(?:19|20)\d{2}(?=[^\d]|$)", " ", s)
    s = re.sub(r"(?:^|[^\d])(?:19|20)\d{2}(?=[^\d]|$)", " ", s)
    s = s.replace(".", " ").replace("_", " ").replace("-", " ")
    s = re.sub(r"[\[\]\(\)\{\}【】（）]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ._-")
    # Drop edition fluff left after dotted Deluxe.Edition strip
    s = re.sub(
        r"(?i)\b(?:deluxe|special|ultimate|collector'?s?|limited|anniversary|remastered)\s*edition\b",
        " ",
        s,
    )
    s = re.sub(r"\s+", " ", s).strip(" ._-")
    parts = s.split()
    if len(parts) >= 2:
        last = parts[-1]
        # Keep Roman sequel markers (II, III, IV...) — do not treat as scene group
        _roman = bool(re.fullmatch(r"[IVXLCDM]{1,6}", last))
        if (not _roman) and re.fullmatch(r"[A-Za-z0-9]{2,8}", last) and (
            last.isupper()
            or re.fullmatch(r"[A-Z0-9]{2,6}", last)
            or last.lower() in {
                "dks", "sparks", "rarbg", "yts", "yify", "etrg", "evo", "ntb", "flux", "cmrg",
                "hdsky", "chd", "wiki", "frds", "pter", "hds", "ttg",
            }
        ):
            s = " ".join(parts[:-1]).strip(" ._-")
    return s



_QUALITY_TOKENS = re.compile(
    r"(?i)\b("
    r"(?:360|480|720|1080|2160|4320)p|uhd|fhd|hd|sd|4k|8k|hdr10\+?|hdr|dv|dolby|vision|"
    r"bluray|blu\-?ray|bdrip|brrip|web\-?dl|webrip|hdtv|remux|encode|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|aac|dts(?:\-?hd)?(?:\-?ma)?|truehd|atmos|flac|"
    r"10bit|8bit|hdr10|main.?10|"
    r"disc\s*\d+|disk\s*\d+|cd\s*\d+|dvd\s*\d+|d\d{1,2}|"
    r"s\d{1,2}e\d{1,3}|s\d{1,2}|season\s*\d+|complete|pack|"
    r"diy|remux|hybrid|repack|proper|extended|directors\.?cut|"
    r"简繁|双语|字幕|原盘|英版|美版|日版|港版|台版|国语|粤语|中字|"
    r"truehy?brid|imax"
    r")\b"
)
_GROUP_AT = re.compile(r"(?i)[@\-_](?:HDSky|HDS|WiKi|FRDS|CHD|ADWeb|Pter|OurBits|MTeam|TTG|CMCT|beAst|Dream|CtrlHD|NTb|AMZN|NF|DSNP|ATVP|iT|HMAX|VXT|FLUX|EA|d3g|NTG)\b.*$")
_SIZE_TOKEN = re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:GB|GiB|MB|MiB)\b")
_BRACKET = re.compile(r"[\[【\(（]([^\]】\)）]+)[\]】\)）]")


def extract_search_queries(name: str) -> list[str]:
    """Pull likely titles from release-style folder/file names.

    Tries Chinese bracket titles, Latin show/movie names, then a cleaned full string.
    """
    name = strip_season_tokens(strip_site_tags(name or ""))
    # collapse_dotted_acronym
    # leading_num_title_query: 3.from.Hell -> 3 from Hell
    name = re.sub(r"^(\d{1,2})[._\- ]+(?=[A-Za-z])", r"\1 ", (name or "").strip())

    name = re.sub(r"\b(?:[A-Za-z]\.){2,}[A-Za-z]\.?\b", lambda m: m.group(0).replace(".",""), name or "")
    name = (name or "").replace("&", " and ")
    # split_acronym_year
    name = re.sub(r"(?i)\b([A-Z]{2,10})((?:19|20)\d{2})\b", r"\1 \2", name or "")

    raw = name or ""
    # strip extension-ish
    raw = re.sub(r"\.(mkv|mp4|iso|ts|m2ts|avi|mov|wmv)$", "", raw, flags=re.I)
    queries: list[str] = []

    # Leading Chinese title (徒手攀岩.Free.Solo.2018...); keep 33D/3D/2D glued to title
    _cjk_pat = r"^([\u4e00-\u9fff·・：:]{2,40}(?:\d{1,2}[Dd])?)"
    m_cjk = re.match(_cjk_pat, raw.replace("_", " ").replace(".", " "))
    if not m_cjk:
        m_cjk = re.match(_cjk_pat, raw)
    if m_cjk:
        queries.append(m_cjk.group(1).strip(" ·・"))
    # Latin title before year/quality: Free.Solo / Paddington.2 / 2.Fast.2.Furious
    # Words must start with a letter (years like 2016 are not title tokens).
    # Sequel digits use (?!\d) so 2160p cannot contribute a fake "2".
    m_lat = re.search(
        r"(?i)(?:^|[\s._\-])(("
        r"(?:\d{1,2}[\s._\-]+)?"  # optional leading num (2 Fast...)
        r"[A-Za-z][A-Za-z0-9']*"
        r"(?:[\s._\-]+(?:[A-Za-z][A-Za-z0-9']*|(?:[2-9]|1[0-9])(?!\d))){0,8}"
        r"))(?=[\s._\-]*(?:(?:19|20)\d{2}|(?:360|480|720|1080|2160|4320)p|Blu|WEB|REMUX|PROPER|CHINESE|GERMAN))",
        raw,
    )
    if m_lat:
        queries.append(re.sub(r"[._\-]+", " ", m_lat.group(1)).strip())

    # 1) bracket / 【】 segments that look like titles (not pure quality)
    for seg in _BRACKET.findall(raw):
        s = seg.strip()
        if not s or _SIZE_TOKEN.fullmatch(s):
            continue
        # skip if mostly quality tokens
        cleaned = _QUALITY_TOKENS.sub(" ", s)
        cleaned = _SIZE_TOKEN.sub(" ", cleaned)
        cleaned = re.sub(r"[\._\-]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) >= 2 and not re.fullmatch(r"[\d\s]+", cleaned):
            # Prefer short Chinese / plain titles
            if re.search(r"[\u4e00-\u9fff]", cleaned) or re.search(r"[A-Za-z]{3,}", cleaned):
                queries.append(cleaned)

    # 2) Latin title run: e.g. Parenthood.S01.2025...
    latin = re.sub(_BRACKET, " ", raw)
    latin = _GROUP_AT.sub(" ", latin)
    latin = _SIZE_TOKEN.sub(" ", latin)
    # take chunk before season/year/quality
    m = re.search(
        r"(?i)(^|[\s\._\-])([A-Za-z][A-Za-z0-9'&: ]{1,80}?)(?=[\s\._\-]*(?:S\d{1,2}|Season\b|20\d{2}|19\d{2}|\d{3,4}p|UHD|Blu|WEB|REMUX))",
        latin,
    )
    if m:
        cand = re.sub(r"[\._]+", " ", m.group(2)).strip(" -_")
        cand = re.sub(r"\s+", " ", cand)
        if len(cand) >= 2:
            queries.append(cand)

    # 3) fallback cleaned whole name
    whole = _BRACKET.sub(r" \1 ", raw)
    whole = _GROUP_AT.sub(" ", whole)
    whole = _SIZE_TOKEN.sub(" ", whole)
    whole = _QUALITY_TOKENS.sub(" ", whole)
    whole = re.sub(r"[\._\-]+", " ", whole)
    whole = re.sub(r"\s+", " ", whole).strip()
    # drop leftover lone digits
    whole = re.sub(r"\b\d\b", " ", whole)
    whole = re.sub(r"\s+", " ", whole).strip()
    if whole:
        queries.append(whole)

    # Dedup preserve order; also add first Chinese-only and first Latin-only
    out: list[str] = []
    seen = set()
    for q in queries:
        q = win_safe(q) if "win_safe" in globals() else q
        q = strip_media_hint_tokens(q)
        q = strip_release_junk(q)
        q = strip_year_range_tokens(q)
        q = re.sub(r"(?i)\b(?:web|dl|itunes|dovi|remux|hybrid|uhd)\b", " ", q)
        q = re.sub(r"(?i)\b10\+", " ", q)
        q = re.sub(r"(?:\s|^)\+(?=\s|$)", " ", q)
        q = re.sub(r"\s+", " ", (q or "").strip(" ._+-"))
        if not q or q.lower() in seen:
            continue
        if _HINT_ONLY.fullmatch(q):
            continue  # skip bare 「电视剧」「电影」 as a title
        seen.add(q.lower())
        out.append(q)
    # Always offer fully cleaned title (drops lone years + ranges)
    cq = clean_query_title(raw)
    if cq and cq.lower() not in seen:
        out.insert(0, cq)
        seen.add(cq.lower())
    # Titles that contain a number ("2046.2004", "Blade Runner 2049 (2017)"):
    # everything before the release year, if it holds a lone number token.
    _num_title = ""
    _yms = list(YEAR_RE.finditer(raw))
    if _yms and TMDB_RE.search(raw):
        _yms = []  # an id like [tmdbid=1948] is not a year
    if _yms:
        _pre = re.sub(r"[._]+", " ", raw[: _yms[-1].start(1)])
        _pre = re.sub(r"[\s\(\[【（\-]+$", "", _pre).strip()
        if _pre and re.search(r"(?<!\d)\d{1,4}(?![\dpPiIkKxX])", _pre) and re.search(r"[A-Za-z\u4e00-\u9fff\d]", _pre):
            _num_title = win_safe(_pre)

    # Glued title+resolution stubs only: ongbak1080p -> also try "ong bak"
    if re.search(r"(?i)[A-Za-z](?:360|480|720|1080|2160|4320)p", raw or ""):
        for base in list(out):
            if re.fullmatch(r"[a-z]{5,20}", base or ""):
                for cut in (3, 4):
                    if cut < len(base) - 2:
                        spaced = (base[:cut] + " " + base[cut:]).strip()
                        if spaced.lower() not in seen:
                            out.append(spaced)
                            seen.add(spaced.lower())
    # Prefer shorter Chinese title and clear Latin title first
    def rank(q: str):
        has_cjk = 1 if re.search(r"[\u4e00-\u9fff]", q) else 0
        has_latin = 1 if re.search(r"[A-Za-z]{3,}", q) else 0
        # prefer pure-ish titles
        pure = 0 if re.search(r"(?i)diy|字幕|原盘|bluray|hevc|dts", q) else 1
        return (-pure, -has_cjk, -has_latin, len(q))
    out.sort(key=rank)

    # Drop leftover release junk queries; prefer pure titles
    _JUNK_ONLY = re.compile(
        r"(?i)^(proper|repack|extended|theatrical|uncut|limited|internal|remastered|"
        r"chinese|chs|cht|eng|jpn|multi|german|french|italian|spanish|japanese|korean|"
        r"russian|hindi|ma|hd|uhd|remux|bluray|webdl|webrip|"
        r"[ivxlcdm]{1,6}|edition|deluxe|special)$"
    )
    def _is_dirty(q: str) -> bool:
        q = (q or "").strip()
        if not q:
            return True
        if _JUNK_ONLY.fullmatch(q):
            return True
        # Roman numerals alone (II, III, IV...)
        if re.fullmatch(r"(?i)[ivxlcdm]{1,6}", q):
            return True
        # leftover year / scene group crumbs
        if re.search(r"(?i)\b(?:19|20)\d{2}\b", q) and re.search(r"(?i)\b(?:fgt|x|proper)\b", q):
            return True
        if re.search(r"(?i)\b(?:ma|pt\d+|bluray|blu\s*ray|1080p|2160p|dts|hdsky|remux|fgt|itunes|dovi|hybrid|ddp\d*|web|watcher|hone)\b|@|\b(?:360|480|720|1080|2160|4320)p\b|\+|\bben\s+the\s+men\b", q):
            return True
        # lone codec crumbs: "Title H" / "Title 264" left from H.264
        parts = [w for w in q.split() if w]
        if parts and re.fullmatch(r"(?i)(?:h|x)?264|265|hevc|avc|h|x", parts[-1]):
            return True
        if len(parts) >= 2 and re.fullmatch(r"(?i)[a-z]", parts[-1]):
            return True
        return False
    out = [q for q in out if q and not _is_dirty(q)]
    def _q_pref(q: str):
        has_cjk = 1 if re.search(r"[\u4e00-\u9fff]", q) else 0
        pure_cjk = 1 if re.fullmatch(r"[\u4e00-\u9fff\u3040-\u30ff·・：:\s]{2,}", q) else 0
        words = [w for w in q.split() if w]
        title_tokens = sum(1 for w in words if re.fullmatch(r"[A-Za-z][A-Za-z0-9']*|\d{1,2}", w))
        weak = 0
        if not has_cjk:
            if title_tokens <= 1:
                weak += 3
            elif title_tokens == 2 and len(q) < 10:
                weak += 1
            if re.fullmatch(r"(?i)[ivxlcdm]{1,6}", q):
                weak += 5
        # prefer queries that keep a sequel mark / dimension tag (33D)
        seq = 1 if sequel_mark(q) or re.search(r"[\u4e00-\u9fffA-Za-z][2-9]\b|[2-9]$", q) else 0
        dim = 1 if dimension_mark(q) else 0
        return (-dim, -seq, -pure_cjk, -has_cjk, -title_tokens, weak, -len(q))

    # Add apostrophe variants: Worlds End -> World's End (TMDB often has ')
    extra = []
    for q in out:
        words = q.split()
        if len(words) >= 2:
            for i, w in enumerate(words):
                if len(w) >= 3 and w.lower().endswith("s") and "'" not in w and not w[-2:].lower() in {"ss", "us", "is", "os"}:
                    # Only known contractions / World's End. Never Patriots Day / War of the Worlds.
                    allow = False
                    wl = w.lower()
                    nxt = words[i + 1].lower() if i + 1 < len(words) else ""
                    if wl in {"dont", "cant", "wont", "its", "shes", "hes", "im", "oceans", "charlies", "rosemarys"}:
                        allow = True
                    elif wl == "worlds" and nxt == "end":
                        allow = True
                    elif wl == "devils" and nxt == "advocate":
                        allow = True
                    # possessive patterns: Name's Wife/Son/... but NOT plural demonyms/titles + Day
                    elif nxt in {"wife", "husband", "daughter", "son", "ghost", "honour", "honor"} and wl not in {
                        "patriots", "parents", "kids", "friends", "lovers", "brothers", "sisters", "mothers", "fathers"
                    }:
                        allow = True
                    if allow:
                        ww = list(words)
                        ww[i] = w[:-1] + "'" + w[-1]
                        extra.append(" ".join(ww))
    for q in extra:
        ql = q.lower()
        if ql not in {x.lower() for x in out}:
            out.insert(0, q)

    out.sort(key=_q_pref)
    # If source title carries 33D/3D/2D, drop queries that lost that mark (avoid 蜜桃成熟時 → wrong id)
    _dim_src = dimension_mark(name)
    if _dim_src:
        kept = [q for q in out if dimension_mark(q)]
        if kept:
            out = kept
    # Never keep calendar years inside query text (year is passed separately to search_tmdb).
    # Fixes "The Torture Club 2014" / "Patriots Day 2016" false no_results.
    cleaned_q = []
    seen_cq = set()
    for q in out:
        q2 = re.sub(r"\b(?:19|20)\d{2}\b", " ", q or "")
        q2 = re.sub(r"\s+", " ", q2).strip(" ._-")
        if not q2 or len(q2) < 2:
            continue
        if q2.lower() in seen_cq:
            continue
        seen_cq.add(q2.lower())
        cleaned_q.append(q2)
    out = cleaned_q
    out.sort(key=_q_pref)
    # If we already have a multi-token title, drop weak 1-token latin leftovers (End/Job/Captain/II)
    multi = [q for q in out if len([w for w in q.split() if w]) >= 2 or re.search(r"[\u4e00-\u9fff]", q)]
    if multi:
        def _weak_one(q: str) -> bool:
            if re.search(r"[\u4e00-\u9fff]", q):
                return False
            words = [w for w in q.split() if w]
            return len(words) <= 1
        out = [q for q in out if not _weak_one(q)] or multi

    # The year-stripping above also strips years that are part of the title.
    # Offer the full pre-year text as well: first when the title is only a
    # number (2046, 1917), otherwise as a last resort (Blade Runner 2049).
    if _num_title and re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", _num_title) or (_num_title and re.fullmatch(r"\d{1,4}", _num_title)):
        if _num_title.lower() not in {x.lower() for x in out}:
            if re.fullmatch(r"\d{1,4}", _num_title):
                out.insert(0, _num_title)
            else:
                out.append(_num_title)

    return with_dot_variants(out)[:8]



def dimension_mark(text: str) -> str:
    """Return 2D/3D/33D-style tag if present in title (incl. glued to CJK)."""
    s = (text or "").strip()
    if not s:
        return ""
    m = re.search(r"(?i)(?:[\u4e00-\u9fff]|\b)(\d{1,2}D)\b", s)
    return m.group(1).upper() if m else ""


def sequel_mark(text: str) -> str:
    """Return sequel token if name looks numbered (2/II/第二部), else "".

    Never treats calendar years (19xx/20xx) or resolution (1080p/2160p) as sequels.
    """
    s = (text or "").strip()
    if not s:
        return ""
    s0 = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)
    s0 = re.sub(r"(?i)\b\d{3,4}p\b", " ", s0)
    s0 = re.sub(r"\s+", " ", s0).strip(" ._-")
    # Title2 / 强奸男2 / Title 2
    m = re.search(r"(?:(?<=[A-Za-z\u4e00-\u9fff])|[\s\-_.·・])([2-9]|1[0-9])\s*$", s0)
    if m:
        return m.group(1)
    m = re.search(r"(?i)(?:^|[\s\-_.])(II|III|IV|V|VI|VII|VIII|IX|X)\s*$", s0)
    if m:
        return m.group(1).upper()
    m = re.search(r"第\s*([2-9一二三四五六七八九十]+)\s*[部章节集話话]?", s0)
    if m:
        return m.group(1)
    return ""


def query_has_sequel(q: str, mark: str) -> bool:
    if not mark:
        return True
    q = q or ""
    if mark.isdigit():
        return bool(re.search(rf"(?:^|[^\d]){re.escape(mark)}(?:[^\d]|$)", q)) or q.endswith(mark)
    return mark.lower() in q.lower() or mark in q



def is_finished_scrape(leaf: dict) -> bool:
    """A previous run already renamed this folder to include [tmdbid=N]."""
    if (leaf or {}).get("id_from") != "bracket_tmdbid":
        return False
    if not str((leaf or {}).get("tmdb") or "").strip().isdigit():
        return False
    # Named but no poster yet (the artwork download failed last time, or the
    # folder was renamed by hand): not finished, so it is checked again.
    try:
        return (Path(leaf.get("path") or "") / "poster.jpg").is_file()
    except Exception:
        return True


def extract_id_from_name(name: str):
    """Return (tmdb_id, how) or (None, None)."""
    n = (name or "").strip()
    m = TMDB_RE.search(n)
    if m:
        return m.group(1), "bracket_tmdbid"
    if BARE_ID_RE.fullmatch(n):
        if len(n) == 4 and 1900 <= int(n) <= 2099:
            return None, None
        return n, "bare_numeric"
    m = TMDB_EQ_RE.search(n)
    if m:
        return m.group(1), "name_tmdb_eq"
    m = BRACKET_ID_RE.search(n)
    if m:
        return m.group(1), "bracket_numeric"
    return None, None


# Bumped whenever a TMDB search request ends in an API/network error, so a
# failed lookup can be told apart from a genuine "no results".
_SEARCH_TLS = threading.local()


def _search_fails() -> int:
    return getattr(_SEARCH_TLS, "n", 0)


def _search_tmdb_one_kind(kind: str, q: str, year: str | None):
    """Search one TMDB endpoint; return scored list of (score, tid, result).

    Latin release names query en-US so English titles match (The Captain).
    CJK queries keep zh-CN (宝贝星球 etc.).
    """
    q = q or ""
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", q))
    lang = "zh-CN" if has_cjk else "en-US"
    params = {"api_key": API_KEY, "query": q, "include_adult": "true", "language": lang}
    if year and re.fullmatch(r"\d{4}", str(year)):
        if kind == "tv":
            params["first_air_date_year"] = str(year)
        else:
            params["year"] = str(year)
    url = f"https://api.themoviedb.org/3/search/{kind}?" + urllib.parse.urlencode(params)
    data = api_get(url)
    if not isinstance(data, dict) or data.get("_error"):
        if year:
            params.pop("year", None)
            params.pop("first_air_date_year", None)
            data = api_get(f"https://api.themoviedb.org/3/search/{kind}?" + urllib.parse.urlencode(params))
    if not isinstance(data, dict) or data.get("_error"):
        _SEARCH_TLS.n = _search_fails() + 1
        return []
    results = data.get("results") or []
    scored = []
    ql = (q or "").strip().lower()
    qn = _norm_match_title(q)
    for r in results:
        rid = r.get("id")
        if not rid:
            continue
        rd = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        score = float(r.get("popularity") or 0)
        if year and rd == str(year):
            score += 1000
        # year±1 soft bonus (filename year often off by one)
        if year and rd.isdigit() and abs(int(rd) - int(year)) == 1:
            score += 200
        title = (r.get("title") or r.get("name") or "").strip()
        orig = (r.get("original_title") or r.get("original_name") or "").strip()
        rt = f"{title} {orig}".strip()
        tn = _norm_match_title(title)
        on = _norm_match_title(orig)
        if qn and (tn == qn or on == qn):
            score += 500  # exact title match
        elif ql and (title.lower() == ql or orig.lower() == ql):
            score += 500
        elif ql and (ql in rt.lower() or rt.lower() in ql):
            score += 50
        scored.append((score, str(rid), r))
    scored.sort(key=lambda x: -x[0])
    return scored



def _norm_match_title(s: str) -> str:
    s = (s or "").casefold()
    # World's End == Worlds End ; strip quotes/apostrophes
    s = s.replace("'", "").replace("’", "").replace("‘", "").replace("`", "")
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    # Fantastic Four == Fantastic 4 (and similar Four/4 title spelling)
    s = re.sub(r"\bfour\b", "4", s)
    # WALLE == WALL-E == WALL·E: spacing and punctuation never tell titles apart
    return s.replace(" ", "")



# Version / origin words in a folder name ("无耻之徒 美版", "Wallander (SE)", "维兰德（瑞典版）"):
# (regex, origin countries, original languages). Used to pick between same-title candidates.
_REGION_DEFS = [
    (r"美版|美剧|美国版|(?i:American[\s._-]*(?:version|ver|remake))|(?<![A-Za-z0-9])US(?:A)?(?![A-Za-z0-9])", {"US"}, {"en"}),
    (r"英版|英剧|英国版|(?i:British[\s._-]*(?:version|ver))|(?<![A-Za-z0-9])(?:UK|GB)(?![A-Za-z0-9])", {"GB"}, {"en"}),
    (r"日版|日剧|日本版|(?i:Japanese[\s._-]*(?:version|ver|remake))|(?<![A-Za-z0-9])JP(?![A-Za-z0-9])", {"JP"}, {"ja"}),
    (r"韩版|韩剧|韩国版|(?i:Korean[\s._-]*(?:version|ver|remake))|(?<![A-Za-z0-9])KR(?![A-Za-z0-9])", {"KR"}, {"ko"}),
    (r"港版|港剧|香港版|(?<![A-Za-z0-9])HK(?![A-Za-z0-9])", {"HK"}, {"cn", "zh"}),
    (r"台版|台剧|台湾版|(?<![A-Za-z0-9])TW(?![A-Za-z0-9])", {"TW"}, {"zh"}),
    (r"国产版|国剧|大陆版|内地版", {"CN"}, {"zh", "cn"}),
    (r"泰版|泰剧|泰国版|(?i:Thai[\s._-]*(?:version|ver))", {"TH"}, {"th"}),
    (r"瑞典版|(?i:Swedish[\s._-]*(?:version|ver))|(?<![A-Za-z0-9])SE(?![A-Za-z0-9])", {"SE"}, {"sv"}),
    (r"丹麦版|(?i:Danish[\s._-]*(?:version|ver))|(?<![A-Za-z0-9])DK(?![A-Za-z0-9])", {"DK"}, {"da"}),
    (r"挪威版|(?i:Norwegian[\s._-]*(?:version|ver))", {"NO"}, {"no", "nb"}),
    (r"芬兰版|(?i:Finnish[\s._-]*(?:version|ver))", {"FI"}, {"fi"}),
    (r"荷兰版|(?i:Dutch[\s._-]*(?:version|ver))|(?<![A-Za-z0-9])NL(?![A-Za-z0-9])", {"NL"}, {"nl"}),
    (r"德版|德国版|德剧|(?i:German[\s._-]*(?:version|ver))", {"DE"}, {"de"}),
    (r"法版|法国版|法剧|(?i:French[\s._-]*(?:version|ver))", {"FR"}, {"fr"}),
    (r"意大利版|意版|(?i:Italian[\s._-]*(?:version|ver))", {"IT"}, {"it"}),
    (r"西班牙版|西版|(?i:Spanish[\s._-]*(?:version|ver))", {"ES"}, {"es"}),
    (r"俄罗斯版|俄版|俄剧|(?i:Russian[\s._-]*(?:version|ver))", {"RU"}, {"ru"}),
    (r"土耳其版|土剧|(?i:Turkish[\s._-]*(?:version|ver))", {"TR"}, {"tr"}),
    (r"巴西版|(?i:Brazilian[\s._-]*(?:version|ver))", {"BR"}, {"pt"}),
    (r"印度版|印剧|(?i:Indian[\s._-]*(?:version|ver))", {"IN"}, {"hi", "ta", "te", "ml", "bn", "mr", "kn"}),
    (r"以色列版|(?i:Israeli[\s._-]*(?:version|ver))", {"IL"}, {"he"}),
    (r"澳版|澳大利亚版|(?i:Australian[\s._-]*(?:version|ver))|(?<![A-Za-z0-9])AU(?![A-Za-z0-9])", {"AU"}, {"en"}),
    (r"加拿大版|(?i:Canadian[\s._-]*(?:version|ver))", {"CA"}, {"en", "fr"}),
]
_REGION_RES = [(re.compile(pat), c, l) for pat, c, l in _REGION_DEFS]


def region_hint(*names: str) -> dict | None:
    """Origin implied by version words in the given names (folder name, first video names).

    Returns {"countries": set, "langs": set, "key": "GB"} or None. Two different
    origins in one name cancel out (nothing to decide on).
    """
    found = []
    for pat, c, l in _REGION_RES:
        if any(pat.search(n or "") for n in names):
            found.append((pat, c, l))
    if len(found) != 1:
        return None
    pat, c, l = found[0]
    return {"countries": set(c), "langs": set(l), "key": "+".join(sorted(c)), "pat": pat}


def strip_region_words(q: str, region: dict | None) -> str:
    """'The Office US' -> 'The Office' (the version word is used as a filter instead)."""
    if not region:
        return q
    return re.sub(r"\s+", " ", region["pat"].sub(" ", q or "")).strip(" ._-()[]（）")


def with_dot_variants(queries: list) -> list:
    """WALL E / WALL-E -> also WALL·E: TMDB does not find the middle-dot titles from those."""
    out = list(queries)
    for q0 in queries:
        m0 = re.fullmatch(r"([A-Za-z]{2,})[ \-]([A-Za-z])", q0 or "")
        if m0 and f"{m0.group(1)}·{m0.group(2)}" not in out:
            out.append(f"{m0.group(1)}·{m0.group(2)}")
    return out


def _region_match(kind: str, r: dict, region: dict) -> bool:
    oc = {str(x).upper() for x in (r.get("origin_country") or [])}
    if kind == "tv" and oc:
        return bool(oc & region["countries"])
    return (r.get("original_language") or "").lower() in region["langs"] or bool(oc & region["countries"])


def _cand_summary(cand_lists: list, q: str, limit: int = 6) -> list:
    """The candidates a person would choose between: best title match first."""
    rows = []
    for kind, scored in cand_lists:
        for item in scored[:12]:
            r = item[2]
            if isinstance(r, dict):
                rows.append((_title_similarity(q, r), item[0], kind, str(item[1]), r))
    rows.sort(key=lambda x: (-x[0], -x[1]))
    out = []
    for sim, _sc, kind, tid, r in rows[:limit]:
        out.append({
            "media": kind,
            "tmdb": tid,
            "title": r.get("title") or r.get("name") or "",
            "original": r.get("original_title") or r.get("original_name") or "",
            "year": str(r.get("release_date") or r.get("first_air_date") or "")[:4],
            "country": "/".join(r.get("origin_country") or []),
            "lang": r.get("original_language") or "",
            "sim": round(sim, 2),
        })
    return out


def describe_candidate(c: dict) -> str:
    where = "·".join(x for x in (c.get("year"), c.get("country") or c.get("lang")) if x)
    rt = f" {c['runtime']}分钟" if c.get("runtime") else ""
    return f"{'电影' if c.get('media') == 'movie' else '剧集'}《{c.get('title') or c.get('original') or ''}》({where or '?'}{rt}) [tmdbid={c.get('tmdb')}]"


def _search_cache_key(q: str, year, prefer_tid: str = "", prefer_kind: str = "", region_key: str = "") -> str:
    """Same key search_tmdb stores. Callers must use this to read the entry back."""
    mode = "auto" if media_is_auto() else ("tv" if media_is_tv() else "movie")
    prefer_tid = str(prefer_tid or "").strip()
    if not prefer_tid.isdigit():
        prefer_tid = ""
    prefer_kind = str(prefer_kind or "").strip().lower()
    if prefer_kind not in ("movie", "tv"):
        prefer_kind = ""
    return f"{q}|{year or ''}|{mode}|{prefer_tid}|{prefer_kind}|{region_key or ''}|u5"



def _title_similarity(q: str, r: dict, region: dict | None = None) -> float:
    """1.0 for an exact (normalised) title match, else the best fuzzy ratio over the TMDB titles.
    With a region, a title's own version word ("维兰德（瑞典版）") does not count against it."""
    qn = _norm_match_title(q or "")
    if not qn or not isinstance(r, dict):
        return 0.0
    # A bilingual folder name ("切尔诺贝利 Chernobyl") is exact when either half is exactly a TMDB title.
    exact = {qn}
    cjk = _norm_match_title(re.sub(r"[A-Za-z0-9 ._\-:'’&]+", " ", q or ""))
    latin = _norm_match_title(re.sub(r"[^\x00-\x7f]+", " ", q or ""))
    if cjk and latin and len(cjk) >= 2 and len(latin) >= 2:
        exact.update((cjk, latin))
    best = 0.0
    for k in ("title", "name", "original_title", "original_name"):
        raw = r.get(k) or ""
        for cand in ((raw, strip_region_words(raw, region)) if region else (raw,)):
            tn = _norm_match_title(cand)
            if not tn:
                continue
            if tn in exact:
                return 1.0
            best = max(best, difflib.SequenceMatcher(None, qn, tn).ratio())
    return best


# Below this a hit that only shares a year (or a word) with the folder name is a guess.
SIMILAR_ENOUGH = 0.8


_TECH_CUT_RE = re.compile(
    r"(?i)[\s._\-\[\(（【](?:2160p|1080p|720p|480p|4k|uhd|blu-?ray|web-?dl|webrip|hdtv|remux|x26[45]|h\.?26[45]|hevc|avc|hdr)(?![A-Za-z0-9])"
)


def folder_title_components(name: str) -> list[str]:
    """The title text a folder name carries, before year/quality: one entry per script.

    'It.Boy.2013.Extended.Cut.1080p…' -> ['It Boy'];  '飓风营救.Taken.2008.1080p' -> ['飓风营救', 'Taken'].
    Nothing is dropped as "junk" here, so a word the query builder removed still counts.
    """
    n = strip_season_tokens(strip_site_tags(name or ""))
    n = re.sub(r"\.(mkv|mp4|iso|ts|m2ts|avi|mov|wmv)$", "", n, flags=re.I)
    n = strip_tmdb_markers(n)
    years = list(YEAR_RE.finditer(n))
    cut = years[-1].start(1) if years else len(n)
    bracketed = list(re.finditer(r"[\(\[（【]\s*((?:19|20)\d{2})\s*[\)\]）】]", n))
    if bracketed:  # (2006) is the year even when another number follows it
        cut = bracketed[-1].start(1)
    tm = _TECH_CUT_RE.search(n)
    if tm and tm.start() < cut:
        cut = tm.start()
    ref = re.sub(r"[\[\]【】（）()_.\-]+", " ", n[:cut])
    ref = re.sub(r"\s+", " ", ref).strip()
    if not ref:
        return []
    cjk = " ".join(re.findall(r"[\u4e00-\u9fff·・]+", ref))
    latin = re.sub(r"\s+", " ", re.sub(r"[\u4e00-\u9fff·・]+", " ", ref)).strip()
    comps = [ref]
    if cjk and latin:
        comps += [cjk, latin]
    return comps


def folder_title_mismatch_note(name: str, titles: list, region: dict | None = None) -> str:
    """"" when some part of the folder's title text matches a matched TMDB title; else why not."""
    comps = [c for c in (strip_region_words(c, region) for c in folder_title_components(name)) if c]
    titles = [t for t in (titles or []) if t]
    if region:
        titles = titles + [strip_region_words(t, region) for t in titles]
    if not comps or not titles:
        return ""
    best = 0.0
    for c in comps:
        cn = _norm_match_title(c)
        if not cn:
            continue
        for t in titles:
            tn = _norm_match_title(t)
            if not tn:
                continue
            best = max(best, 1.0 if tn == cn else difflib.SequenceMatcher(None, cn, tn).ratio())
    if best >= SIMILAR_ENOUGH:
        return ""
    return f"文件夹名里的标题「{comps[0]}」与搜到的《{titles[0]}》不一致（{best:.0%}）"


def _uncertain_note(q: str, year, chosen_tid, chosen_kind: str, cand_lists: list, prefer_kind: str = "", region: dict | None = None) -> str:
    """Why a match is a guess and must not be renamed unconfirmed; "" if it is not.

    cand_lists: [(kind, scored)] with scored = [(score, tid, result)].
    Uncertain when:
      - the chosen title is not (nearly) the searched title, even if the year fits;
      - the folder looks like a series (S01, 第一季, episode files) but only a movie matched, or the reverse;
      - the folder has no year and another candidate has the same title.
    """
    qn = _norm_match_title(q or "")
    if not qn:
        return ""
    chosen_r = None
    others = []
    for kind, scored in cand_lists:
        for item in scored:
            tid, r = str(item[1]), item[2]
            if not isinstance(r, dict):
                continue
            if tid == str(chosen_tid) and kind == chosen_kind:
                chosen_r = r
            elif _title_similarity(q, r, region) >= 1.0 and (not prefer_kind or kind == prefer_kind):
                yr = str(r.get("release_date") or r.get("first_air_date") or "")[:4]
                others.append(f"{'电影' if kind == 'movie' else '剧集'}《{r.get('title') or r.get('name') or ''}》({yr or '?'}) [tmdbid={tid}]")
    sim = 1.0
    if chosen_r is not None:
        sim = _title_similarity(q, chosen_r, region)
        if sim < SIMILAR_ENOUGH:
            return f"搜到的标题《{chosen_r.get('title') or chosen_r.get('name') or ''}》与文件夹名不相似（{sim:.0%}），只是年份或个别词对上"
    if prefer_kind in ("movie", "tv") and chosen_kind in ("movie", "tv") and chosen_kind != prefer_kind:
        return f"文件夹像{'剧集' if prefer_kind == 'tv' else '电影'}，但只匹配到{'剧集' if chosen_kind == 'tv' else '电影'}"
    if year and re.fullmatch(r"\d{4}", str(year)):
        return ""
    if others:
        return "无年份，仅凭片名匹配；另有同名候选：" + "、".join(others[:3])
    if chosen_r is not None and sim < 1.0:
        return "无年份，且搜到的标题与文件夹名不完全一致"
    return ""


def _chosen_titles(tid, kind: str, cand_lists: list) -> list:
    """Title variants (title/name/original_*) of the chosen search result."""
    for k, scored in cand_lists:
        for item in scored:
            if k == kind and str(item[1]) == str(tid) and isinstance(item[2], dict):
                r = item[2]
                return [r.get(x) for x in ("title", "name", "original_title", "original_name") if r.get(x)]
    return []


def _scored_has_exact(scored: list, q: str) -> bool:
    qn = _norm_match_title(q or "")
    if not qn:
        return False
    for s in scored or []:
        r0 = s[2] if len(s) > 2 and isinstance(s[2], dict) else {}
        for k in ("title", "name", "original_title", "original_name"):
            if _norm_match_title(r0.get(k) or "") == qn:
                return True
    return False


def _merge_scored(dest: list, extra: list, penalty: int = 0) -> None:
    have = {x[1] for x in dest}
    for s in extra:
        if s[1] in have:
            continue
        if penalty:
            dest.append((s[0] - penalty, s[1], s[2]))
        else:
            dest.append(s)
        have.add(s[1])


def search_tmdb(query: str, year: str | None, search_cache: dict, prefer_tid: str | None = None, prefer_kind: str | None = None, region: dict | None = None):
    """Return (tmdb_id, how). A search that hit API/network errors and found
    nothing is reported as "search_error" (and not cached), never "no_results".
    The candidates considered are attached to the cache entry ("candidates").
    """
    fails0 = _search_fails()
    _SEARCH_TLS.cands = None
    tid, how = _search_tmdb_impl(query, year, search_cache, prefer_tid, prefer_kind, region)
    pt = str(prefer_tid or "").strip()
    pk = str(prefer_kind or "").strip().lower()
    ckey_ = _search_cache_key((query or "").strip(), year, pt if pt.isdigit() else "", pk if pk in ("movie", "tv") else "", (region or {}).get("key", ""))
    if not tid and how == "no_results" and _search_fails() > fails0:
        search_cache.pop(ckey_, None)
        return None, "search_error"
    rec = search_cache.get(ckey_)
    cands = getattr(_SEARCH_TLS, "cands", None)
    if isinstance(rec, dict) and cands and "candidates" not in rec:
        rec["candidates"] = cands
    return tid, how


def _search_tmdb_impl(query: str, year: str | None, search_cache: dict, prefer_tid: str | None = None, prefer_kind: str | None = None, region: dict | None = None):
    """Return (tmdb_id, how).

    Auto mode cascade when title collides:
      1) title search both movie+tv
      2) if both (or multi) hit → filter by year
      3) if still both → prefer folder tmdbid if it matches one candidate
      4) still duplicate → ambiguous_movie_and_tv (fail tab)
    A unique title-only hit on one side is accepted as correct.
    """
    q = (query or "").strip()
    if not q:
        return None, "empty_query"
    prefer_tid = str(prefer_tid or "").strip()
    if prefer_tid and not prefer_tid.isdigit():
        prefer_tid = ""
    prefer_kind = str(prefer_kind or "").strip().lower()
    if prefer_kind not in ("movie", "tv"):
        prefer_kind = ""
    key = _search_cache_key(q, year, prefer_tid, prefer_kind, (region or {}).get("key", ""))
    has_year = bool(year and re.fullmatch(r"\d{4}", str(year)))
    cached = search_cache.get(key) if isinstance(search_cache.get(key), dict) else None
    if cached:
        if cached.get("error") in ("ambiguous_no_year", "ambiguous_movie_and_tv"):
            cached = None
        elif cached.get("tmdb") and (has_year or cached.get("unique") is True or prefer_tid):
            return str(cached["tmdb"]), "search_cache"

    def _year_of(r: dict) -> str:
        return ((r.get("release_date") or r.get("first_air_date") or "")[:4])

    def _filter_year(scored: list) -> list:
        if not has_year or not scored:
            return scored
        yh = [s for s in scored if _year_of(s[2]) == str(year)]
        return yh if yh else []

    def _off_by_one_exact(scored: list):
        """Unique exact-title hit one year off (filename year vs TMDB)."""
        if not has_year:
            return None
        try:
            y0 = int(year)
        except Exception:
            return None
        qn = _norm_match_title(q or "")
        if not qn:
            return None
        near = []
        for s in scored:
            r0 = s[2] if isinstance(s[2], dict) else {}
            rd = _year_of(r0)
            if not rd.isdigit() or abs(int(rd) - y0) != 1:
                continue
            for k in ("title", "name", "original_title", "original_name"):
                if _norm_match_title(r0.get(k) or "") == qn:
                    near.append(s)
                    break
        if len({str(s[1]) for s in near}) == 1:
            return near[0]
        return None

    def _from_year_hits(yh: list):
        """Several or one candidate(s) in the folder's year: (tid, how, use)."""
        if len(yh) == 1:
            return yh[0][1], "search_year", yh
        if prefer_tid:
            hit = [s for s in yh if str(s[1]) == prefer_tid]
            if len(hit) == 1:
                return hit[0][1], "search_tmdbid", hit
        # Exact title among year hits (NOT TMDB vote scores).
        qn = _norm_match_title(q or "")
        exact = []
        for s in yh:
            r0 = s[2] if isinstance(s[2], dict) else {}
            for k in ("title", "name", "original_title", "original_name"):
                t = _norm_match_title(r0.get(k) or "")
                if t and t == qn:
                    exact.append(s)
                    break
        if exact and len({str(s[1]) for s in exact}) == 1:
            return exact[0][1], "search_year_exact", exact
        # Prefer abstain over wrong match: score gaps are NOT unique enough
        # (The Nun / Ringu / Lamb same-year collisions). Unmatched tab is OK.
        return None, "ambiguous_no_year", (exact if len(exact) > 1 else yh)

    def _pick_unique(scored: list):
        """Return (tid, how, scored) after title→year→tmdbid cascade within one side."""
        if not scored:
            return None, "no_results", scored
        # title-level uniqueness
        if len(scored) == 1 or scored[0][0] >= scored[1][0] + 80:
            # still apply year if present and top doesn't match year — escalate
            if has_year:
                yh = _filter_year(scored)
                if yh:
                    return _from_year_hits(yh)
                # top title unique but wrong year → try year filter empty → no year match
                if not yh:
                    hit = _off_by_one_exact(scored)
                    if hit:
                        return hit[1], "search_year", [hit]
                    return None, "no_results", scored
            return scored[0][1], "search", scored
        # title collision within side → escalate year
        if has_year:
            yh = _filter_year(scored)
            if yh:
                return _from_year_hits(yh)
            hit = _off_by_one_exact(scored)
            if hit:
                return hit[1], "search_year", [hit]
            return None, "no_results", scored
        # no year: try prefer_tid among title candidates
        if prefer_tid:
            hit = [s for s in scored if str(s[1]) == prefer_tid]
            if len(hit) == 1:
                return hit[0][1], "search_tmdbid", hit
        return None, "ambiguous_no_year", scored

    if media_is_auto():
        # Title search both sides. If BOTH return any candidates, that is a
        # collision — year / folder tmdbid must resolve; otherwise unmatched.
        # Do NOT accept "unique on one side" while the other side also hit.
        #
        # With a year, search that year first (2 calls). The old ±5 sweep
        # fired 20 more searches for every folder even after an exact hit,
        # and a big library then tripped TMDB's rate limit.
        movie_scored: list = []
        tv_scored: list = []
        # A folder that says what it is (S01E01 files, 电视剧, 电影) is searched on that
        # side first; the other side is only asked when that gives no exact title.
        _order = ("tv", "movie") if prefer_kind == "tv" else ("movie", "tv")
        _lists = {"movie": movie_scored, "tv": tv_scored}

        def _search_side(side_year):
            for k in _order:
                if k != _order[0] and prefer_kind and _scored_has_exact(_lists[_order[0]], q):
                    break
                if side_year:
                    _lists[k][:] = _search_tmdb_one_kind(k, q, side_year)
                else:
                    _merge_scored(_lists[k], _search_tmdb_one_kind(k, q, None))

        if has_year:
            _search_side(year)
        if not _scored_has_exact(movie_scored, q) and not _scored_has_exact(tv_scored, q):
            _search_side(None)
        if has_year and not _scored_has_exact(movie_scored, q) and not _scored_has_exact(tv_scored, q):
            # Filename year off by one (The Captain 2017 vs 2018).
            try:
                y0 = int(year)
                qn = _norm_match_title(q or "")
                for y2 in (y0 - 1, y0 + 1):
                    if y2 < 1900 or y2 > 2099:
                        continue
                    for kind, dest in (("movie", movie_scored), ("tv", tv_scored)):
                        extra = []
                        for s in _search_tmdb_one_kind(kind, q, str(y2)):
                            r0 = s[2] if isinstance(s[2], dict) else {}
                            titles = [
                                _norm_match_title(r0.get(k) or "")
                                for k in ("title", "name", "original_title", "original_name")
                            ]
                            if qn and qn in titles:
                                extra.append(s)
                        _merge_scored(dest, extra, penalty=30)
            except Exception:
                pass
        movie_scored.sort(key=lambda x: -x[0])
        tv_scored.sort(key=lambda x: -x[0])
        if region:
            # "美版" / "（瑞典版）": keep the candidates made in that place, if any are.
            m_keep = [x for x in movie_scored if _region_match("movie", x[2], region)]
            t_keep = [x for x in tv_scored if _region_match("tv", x[2], region)]
            if m_keep or t_keep:
                movie_scored, tv_scored = m_keep, t_keep
        _SEARCH_TLS.cands = _cand_summary([("movie", movie_scored), ("tv", tv_scored)], q)

        movie_has = bool(movie_scored)
        tv_has = bool(tv_scored)

        def _store_hit(tid, media, how):
            search_cache[key] = {
                "ok": True,
                "tmdb": str(tid),
                "media": media,
                "unique": True,
                "query": q,
                "year": year,
                "via": how,
                "titles": _chosen_titles(tid, media, [("movie", movie_scored), ("tv", tv_scored)]),
                "uncertain": _uncertain_note(
                    q, year, tid, media,
                    # A folder hint (S01, 电视剧, ...) settles the side: only rivals on it count.
                    [(k, sc) for k, sc in (("movie", movie_scored), ("tv", tv_scored)) if prefer_kind not in ("movie", "tv") or k == prefer_kind],
                    prefer_kind,
                    region,
                ),
            }
            return str(tid), how if str(how).startswith("search") else "search"

        def _store_amb(err, movie_tid=None, tv_tid=None, movie_use=None, tv_use=None):
            rec = {"ok": False, "error": err, "query": q, "year": year}
            if movie_tid:
                rec["movie_tmdb"] = str(movie_tid)
            if tv_tid:
                rec["tv_tmdb"] = str(tv_tid)
            if movie_use:
                r0 = movie_use[0][2]
                rec["movie_title"] = r0.get("title") or r0.get("name") or ""
            if tv_use:
                r0 = tv_use[0][2]
                rec["tv_title"] = r0.get("name") or r0.get("title") or ""
            search_cache[key] = rec
            return None, err

        if movie_has and tv_has:
            m_list = _filter_year(movie_scored) if has_year else list(movie_scored)
            t_list = _filter_year(tv_scored) if has_year else list(tv_scored)
            # Keep exact-title year±1 candidates that _filter_year dropped
            if has_year:
                try:
                    y0 = int(year)
                    qn = _norm_match_title(q or "")
                    def _keep_near(scored, dest):
                        for s in scored:
                            if any(s[1] == x[1] for x in dest):
                                continue
                            r0 = s[2] if isinstance(s[2], dict) else {}
                            rd = str(r0.get("release_date") or r0.get("first_air_date") or "")[:4]
                            if not rd.isdigit() or abs(int(rd) - y0) != 1:
                                continue
                            titles = [_norm_match_title(r0.get(k) or "") for k in ("title", "name", "original_title", "original_name")]
                            if qn and qn in titles:
                                dest.append(s)
                    _keep_near(movie_scored, m_list)
                    _keep_near(tv_scored, t_list)
                except Exception:
                    pass
            if has_year and not m_list and not t_list:
                search_cache[key] = {"ok": False, "error": "no_results", "query": q, "year": year}
                return None, "no_results"
            if m_list and not t_list:
                tid, how, use = _pick_unique(m_list)
                if tid:
                    return _store_hit(tid, "movie", how)
                return _store_amb("ambiguous_no_year", movie_use=use)
            if t_list and not m_list:
                tid, how, use = _pick_unique(t_list)
                if tid:
                    return _store_hit(tid, "tv", how)
                return _store_amb("ambiguous_no_year", tv_use=use)
            movie_tid, movie_how, movie_use = _pick_unique(m_list)
            tv_tid, tv_how, tv_use = _pick_unique(t_list)
            # Folder-name hint (e.g. 「宝贝星球 电视剧」) — resolve on that side only
            if prefer_kind in ("tv", "movie"):
                side = t_list if prefer_kind == "tv" else m_list
                if side:
                    if prefer_tid:
                        hit = [s for s in side if str(s[1]) == prefer_tid]
                        if len(hit) == 1:
                            return _store_hit(prefer_tid, prefer_kind, "search_tmdbid")
                    tid, how, use = _pick_unique(side)
                    if tid:
                        return _store_hit(tid, prefer_kind, how if str(how).startswith("search") else "search_kind_hint")
                    # Exact title only — never force by score or newest date (wrong match > unmatched)
                    qn = _norm_match_title(q or "")
                    exact = []
                    for sc, tid0, r0 in side:
                        for k in ("title", "name", "original_title", "original_name"):
                            if qn and _norm_match_title(r0.get(k) or "") == qn:
                                exact.append((sc, tid0, r0))
                                break
                    if len(exact) == 1 or (exact and len({str(x[1]) for x in exact}) == 1):
                        return _store_hit(exact[0][1], prefer_kind, "search_kind_hint")
                    return _store_amb(
                        "ambiguous_no_year",
                        movie_tid=(side[0][1] if prefer_kind == "movie" and side else None),
                        tv_tid=(side[0][1] if prefer_kind == "tv" and side else None),
                        movie_use=(side if prefer_kind == "movie" else None),
                        tv_use=(side if prefer_kind == "tv" else None),
                    )
            if prefer_tid:
                mh = [s for s in m_list if str(s[1]) == prefer_tid]
                th = [s for s in t_list if str(s[1]) == prefer_tid]
                if len(mh) == 1 and not th:
                    return _store_hit(prefer_tid, "movie", "search_tmdbid")
                if len(th) == 1 and not mh:
                    return _store_hit(prefer_tid, "tv", "search_tmdbid")
            # cross_side_exact_title
            qn = _norm_match_title(q or "")
            def _exact_side(side):
                out = []
                for item in side:
                    r0 = item[2]
                    for k in ("title", "name", "original_title", "original_name"):
                        if qn and _norm_match_title(r0.get(k) or "") == qn:
                            out.append(item)
                            break
                return out
            mex = _exact_side(m_list)
            tex = _exact_side(t_list)
            if mex and not tex:
                if len({str(x[1]) for x in mex}) == 1 or (len(mex) == 1) or (len(mex) > 1 and mex[0][0] >= mex[1][0] + 80):
                    return _store_hit(mex[0][1], "movie", "search_year_exact")
            if tex and not mex:
                if len({str(x[1]) for x in tex}) == 1 or (len(tex) == 1) or (len(tex) > 1 and tex[0][0] >= tex[1][0] + 80):
                    return _store_hit(tex[0][1], "tv", "search_year_exact")
            if mex and tex:
                if mex[0][0] >= tex[0][0] + 40:
                    return _store_hit(mex[0][1], "movie", "search_year_exact")
                if tex[0][0] >= mex[0][0] + 40:
                    return _store_hit(tex[0][1], "tv", "search_year_exact")
                # Do NOT use season/episode markers (or their absence) to pick a side:
                # movie multi-disc names (CD1/CD2) must not tip TV/movie. Abstain instead.
            if movie_tid and tv_tid:
                return _store_amb(
                    "ambiguous_movie_and_tv",
                    movie_tid=movie_tid,
                    tv_tid=tv_tid,
                    movie_use=movie_use,
                    tv_use=tv_use,
                )
            return _store_amb(
                "ambiguous_movie_and_tv",
                movie_tid=movie_tid or (m_list[0][1] if m_list else None),
                tv_tid=tv_tid or (t_list[0][1] if t_list else None),
                movie_use=movie_use or m_list,
                tv_use=tv_use or t_list,
            )

        if movie_has and not tv_has:
            tid, how, use = _pick_unique(movie_scored)
            if tid:
                return _store_hit(tid, "movie", how)
            search_cache[key] = {"ok": False, "error": how or "no_results", "query": q, "year": year}
            return None, how or "no_results"
        if tv_has and not movie_has:
            tid, how, use = _pick_unique(tv_scored)
            if tid:
                return _store_hit(tid, "tv", how)
            search_cache[key] = {"ok": False, "error": how or "no_results", "query": q, "year": year}
            return None, how or "no_results"

        search_cache[key] = {"ok": False, "error": "no_results", "query": q, "year": year}
        return None, "no_results"


    kind = "tv" if media_is_tv() else "movie"
    scored = _search_tmdb_one_kind(kind, q, year)
    if region:
        keep = [x for x in scored if _region_match(kind, x[2], region)]
        scored = keep or scored
    _SEARCH_TLS.cands = _cand_summary([(kind, scored)], q)
    tid, how, scored2 = _pick_unique(scored)
    if how == "ambiguous_no_year":
        search_cache[key] = {"ok": False, "error": "ambiguous_no_year", "query": q, "year": year}
        return None, "ambiguous_no_year"
    if not tid:
        search_cache[key] = {"ok": False, "error": "no_results"}
        return None, "no_results"
    search_cache[key] = {
        "ok": True, "tmdb": tid, "media": kind,
        "unique": (has_year or len(scored) == 1 or bool(prefer_tid)),
        "query": q, "year": year,
        "titles": _chosen_titles(tid, kind, [(kind, scored)]),
        "uncertain": _uncertain_note(q, year, tid, kind, [(kind, scored)], prefer_kind, region),
    }
    return tid, how if how.startswith("search") else "search"



def build_name(title: str, year, tid: str) -> str:
    title = win_safe(title)
    if year and re.fullmatch(r"\d{4}", str(year)):
        name = f"{title} ({year}) [tmdbid={tid}]"
    else:
        name = f"{title} [tmdbid={tid}]"
    if len(name) > 240:
        tail = f" ({year}) [tmdbid={tid}]" if year and re.fullmatch(r"\d{4}", str(year)) else f" [tmdbid={tid}]"
        name = win_safe(title)[: 240 - len(tail) - 1].rstrip(" .") + tail
    return name


def should_prune(name: str) -> bool:
    return name in PRUNE or name.startswith(".")


def is_extras_dir(name: str) -> bool:
    return name.lower().strip() in EXTRAS_DIR


DISC_DIR_NAMES = {
    "bdmv", "certificate", "certificatea", "certificateb",
    "video_ts", "audio_ts",
}


def _entries(folder: Path, strict: bool = False) -> list:
    """[(name, is_dir, is_file)] in one directory read. os.scandir gets the type with the
    listing, where Path.iterdir()+is_dir() costs one extra call per entry (slow on a
    network share)."""
    out = []
    try:
        with os.scandir(str(folder)) as it:
            for e in it:
                try:
                    out.append((e.name, e.is_dir(), e.is_file()))
                except OSError:
                    pass
    except Exception:
        if strict:
            raise
    return out


def is_disc_structure(folder: Path) -> bool:
    """Blu-ray / DVD folder tree — treat outer folder as one leaf; never dig in."""
    names = {n.lower() for n, isd, isf in _entries(folder) if isd or isf}
    if "bdmv" in names:
        return True
    if "video_ts" in names:
        return True
    # ISO sitting with companion folders still counts as disc-ish leaf if .iso present
    # (handled separately); here structure-only.
    return False


def has_video(folder: Path) -> bool:
    for n, isd, isf in _entries(folder):
        if isf and Path(n).suffix.lower() in VIDEO:
            return True
        if isd and n.lower() in ("bdmv", "video_ts"):
            return True
    return False


def list_videos_in_dir(folder: Path) -> list[Path]:
    return [folder / n for n, _isd, isf in _entries(folder) if isf and Path(n).suffix.lower() in VIDEO]


def interesting_subdirs(folder: Path) -> list[Path]:
    """Non-prune, non-extras, non-BDMV subdirs."""
    out = []
    for n, isd, _isf in _entries(folder):
        if not isd or should_prune(n):
            continue
        if n.lower() in DISC_DIR_NAMES:
            continue
        if is_extras_dir(n):
            continue
        out.append(folder / n)
    return out


def find_nfo_meta(folder: Path):
    nfos = [folder / n for n, _isd, isf in _entries(folder) if isf and n.lower().endswith(".nfo") and not n.startswith(".")]
    if not nfos:
        return {}
    nfos.sort(key=lambda p: (0 if p.name.lower() == "movie.nfo" else 1, p.name.lower()))
    for nfo in nfos[:6]:
        try:
            meta = parse_nfo(nfo.read_text(encoding="utf-8", errors="ignore"))
            if meta and (meta.get("tmdb") or meta.get("title") or meta.get("originaltitle")):
                return meta
        except Exception:
            continue
    return {}


def _unique_stem_folder(parent: Path, stem: str) -> Path:
    safe = win_safe(stem) or "video"
    candidate = parent / safe
    if not candidate.exists():
        return candidate
    n = 2
    while n <= 99:
        c = parent / f"{safe} #{n}"
        if not c.exists():
            return c
        n += 1
    return parent / f"{safe} #{int(time.time())}"


def multipart_base(stem: str) -> str:
    """Strip 上/下集、Part/CD/Disc/D01 markers so multi-part files group together.

    Markers may appear at the end or in the middle (e.g. Title.D01.2024.1080p).
    Episode codes like S01E02 are kept so different episodes stay separate.
    """
    s = stem or ""
    # Middle or edge: Disc/Disk/DVD/CD/Part/Pt + number (do not touch SxxExx)
    s = re.sub(
        r"(?i)(?<![A-Za-z0-9])(?:disc|disk|dvd|cd|part|pt)[.\s_\-]*0*\d+(?![A-Za-z0-9])",
        "",
        s,
    )
    # Middle or edge: D01 / D1 style disc markers (common on Blu-ray dumps)
    s = re.sub(r"(?i)(?<![A-Za-z0-9])d[.\s_\-]*0*\d{1,2}(?![A-Za-z0-9])", "", s)
    # Chinese part markers as a token
    s = re.sub(
        r"(?:^|[.\s_\-])(?:上集|下集|中集|上篇|下篇|前篇|后篇|完整版)(?=$|[.\s_\-])",
        "",
        s,
    )
    # trailing 上/下 alone (after separator)
    s = re.sub(r"(?i)([.\s_\-]+(?:上|下))$", "", s)
    # trailing part/cd/disc (legacy)
    s = re.sub(
        r"(?i)([.\s_\-]*(?:part|pt|cd|disc|disk)[.\s_\-]*\d+)$",
        "",
        s,
    )
    s = re.sub(r"(?i)([.\s_\-]+(?:p|part)?[12])$", "", s)
    # collapse leftover separators
    s = re.sub(r"[.\s_\-]{2,}", ".", s)
    s = re.sub(r"^[.\s_\-]+|[.\s_\-]+$", "", s).strip()
    return s or stem


def looks_like_disc_folder(name: str) -> bool:
    """True for Disc1 / D01 / CD2 / Part2 style folder names."""
    n = name or ""
    if re.search(r"(?i)(?:^|[.\s_\-])(?:disc|disk|dvd|cd|part|pt)[.\s_\-]*0*\d+", n):
        return True
    if re.search(r"(?i)(?:^|[.\s_\-])d[.\s_\-]*0*\d{1,2}(?:$|[.\s_\-])", n):
        return True
    if re.search(r"(?i)_d\d{1,2}$", n):
        return True
    base = multipart_base(n)
    return base != n and bool(re.search(r"(?i)(?:disc|disk|dvd|cd|part|\bd\d{1,2}\b)", n))


def _same_multidisc_siblings(a: dict, b: dict) -> bool:
    """Same parent + same tmdb + disc-like names (or identical multipart base)."""
    if not a or not b:
        return False
    if str(a.get("tmdb") or "") != str(b.get("tmdb") or ""):
        return False
    if (a.get("parent") or "").lower() != (b.get("parent") or "").lower():
        return False
    na, nb = a.get("name") or "", b.get("name") or ""
    ba, bb = multipart_base(na), multipart_base(nb)
    if ba and ba.lower() == bb.lower():
        return True
    return looks_like_disc_folder(na) and looks_like_disc_folder(nb)


def collapse_multidisc_leaves(leaves: list, root: Path) -> tuple[list, list]:
    """Collapse sibling disc folders that share parent+tmdb.

    Prefer renaming the *parent* once, then flatten Disc/D01 subfolders into it.
    If parent is the scan root, keep the first disc as primary and mark the
    rest for merge-into (no Title #2 / #3 folders).
    Returns (effective_leaves, skipped_member_recs).
    """
    from collections import defaultdict

    by: dict[tuple, list] = defaultdict(list)
    passthrough = []
    for L in leaves:
        tid = str(L.get("tmdb") or "").strip()
        if not tid:
            passthrough.append(L)
            continue
        by[((L.get("parent") or "").lower(), tid)].append(L)

    skip_extra: list[dict] = []
    replace_paths: dict[str, dict] = {}
    consumed: set[str] = set()

    try:
        root_res = root.resolve()
    except Exception:
        root_res = root

    for (_pkey, tid), group in by.items():
        if len(group) < 2:
            continue
        bases = {multipart_base(g.get("name") or "") for g in group}
        discish = all(looks_like_disc_folder(g.get("name") or "") for g in group)
        if not ((len(bases) == 1 and next(iter(bases))) or discish):
            continue
        group_sorted = sorted(group, key=lambda g: (g.get("name") or "").lower())
        parent_path = Path(group_sorted[0]["parent"])
        try:
            parent_is_root = parent_path.resolve() == root_res
        except Exception:
            parent_is_root = str(parent_path) == str(root)

        if not parent_is_root:
            synth = {
                "path": str(parent_path),
                "name": parent_path.name,
                "parent": str(parent_path.parent),
                "tmdb": tid,
                "id_from": "multidisc_parent",
                "multidisc_members": [g["path"] for g in group_sorted],
                "kind": "multidisc_parent",
                "flatten_discs": True,
            }
            for g in group_sorted:
                consumed.add(g["path"])
                skip_extra.append(
                    {
                        **g,
                        "reason": "multidisc_under_parent",
                        "merge_into": str(parent_path),
                    }
                )
            replace_paths[str(parent_path)] = synth
            continue

        primary = dict(group_sorted[0])
        primary["multidisc_merge_from"] = [g["path"] for g in group_sorted[1:]]
        primary["kind"] = "multidisc_primary"
        replace_paths[primary["path"]] = primary
        consumed.add(primary["path"])
        for g in group_sorted[1:]:
            consumed.add(g["path"])
            skip_extra.append(
                {
                    **g,
                    "reason": "multidisc_merge",
                    "merge_into_primary": primary["path"],
                }
            )

    out: list[dict] = []
    seen: set[str] = set()
    for L in leaves:
        p = L["path"]
        if p in replace_paths and p not in seen:
            out.append(replace_paths[p])
            seen.add(p)
            continue
        if p in consumed:
            continue
        out.append(L)
    for p, synth in replace_paths.items():
        if p not in seen and synth.get("kind") == "multidisc_parent":
            out.append(synth)
            seen.add(p)
    out.extend(passthrough)
    return out, skip_extra


def _unique_child_path(dest_dir: Path, name: str) -> Path:
    cand = dest_dir / name
    if not cand.exists():
        return cand
    stem = Path(name).stem
    suffix = Path(name).suffix
    n = 2
    while n <= 99:
        c = dest_dir / f"{stem} #{n}{suffix}"
        if not c.exists():
            return c
        n += 1
    return dest_dir / f"{stem} #{int(time.time())}{suffix}"


def merge_folder_into(src: Path, dest: Path) -> None:
    """Move all children of src into dest, then remove empty src."""
    if not src.exists():
        return
    if not dest.exists():
        _log_op("mkdir", path=str(dest))
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if src.resolve() == dest.resolve():
            return
    except Exception:
        if str(src) == str(dest):
            return
    for child in list(src.iterdir()):
        target = _unique_child_path(dest, child.name)
        _rename_one(child, target)
    try:
        if src.exists() and not any(src.iterdir()):
            src.rmdir()
            _log_op("rmdir", path=str(src))
    except Exception:
        pass





def flatten_disc_subfolders(folder: Path) -> list[str]:
    """Move media out of Disc/D01-style subfolders into folder; remove empties.

    Makes multi-disc ISOs/videos sit side-by-side so players can advance to the
    next disc without digging into subfolders.
    """
    moved: list[str] = []
    if not folder.exists() or not folder.is_dir():
        return moved
    for sub in list(folder.iterdir()):
        if not sub.is_dir():
            continue
        if not looks_like_disc_folder(sub.name):
            continue
        # If it is a real BDMV/VIDEO_TS disc tree, keep the folder (player needs structure)
        if is_disc_structure(sub):
            continue
        for child in list(sub.iterdir()):
            target = _unique_child_path(folder, child.name)
            _rename_one(child, target)
            moved.append(str(target))
        try:
            # drop tiny leftover nfo/txt junk then rmdir
            for leftover in list(sub.iterdir()):
                if leftover.is_file() and leftover.suffix.lower() in {
                    ".nfo", ".txt", ".jpg", ".png", ".jpeg", ".url"
                } and leftover.stat().st_size < 200_000:
                    try:
                        leftover.unlink()
                    except Exception:
                        pass
            if sub.exists() and not any(sub.iterdir()):
                sub.rmdir()
                _log_op("rmdir", path=str(sub))
        except Exception:
            pass
    return moved


def is_collection_dir(name: str) -> bool:
    return bool(SERIES.search(name or ""))


def is_drive_root(path: Path) -> bool:
    """True for drive roots like E:/ — never a movie leaf."""
    try:
        p = Path(path)
        if p.parent == p:
            return True
        s = str(p).rstrip("\\/")
        if len(s) == 2 and s[1] == ":":
            return True
        if not (p.name or "").strip():
            return True
    except Exception:
        return False
    return False


def is_dedicated_movie_leaf(folder: Path) -> bool:
    """True if folder is already an independent movie folder — never re-wrap."""
    # Drive roots are never dedicated leaves — a lone loose video on E:\ must still wrap.
    try:
        if is_drive_root(folder):
            return False
    except Exception:
        pass
    tid, _ = extract_id_from_name(folder.name)
    if tid:
        return True
    # Blu-ray / DVD disc folder (BDMV / VIDEO_TS) — outer folder is the leaf
    if is_disc_structure(folder):
        return True
    videos = list_videos_in_dir(folder)
    if len(videos) == 0:
        return False
    # Exactly one video or one ISO (extras/subs ok) => dedicated leaf
    if len(videos) == 1:
        return True
    # Multiple videos but same multipart base => already a multi-part leaf
    bases = {multipart_base(v.stem) for v in videos}
    if len(bases) == 1:
        return True
    return False


def root_is_single_movie(folder: Path) -> bool:
    """True when the chosen folder is one film, not a library that also has subfolders.

    One stray video sitting next to many movie folders must not hide the library.
    Subs / Featurettes do not count as those subfolders.
    """
    try:
        if is_drive_root(folder):
            return False
        if not is_dedicated_movie_leaf(folder):
            return False
        return not interesting_subdirs(folder)
    except Exception:
        return False


_SIDECAR_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi", ".sbv", ".nfo", ".jpg", ".jpeg", ".png", ".tbn"}


def sidecar_files(video: Path) -> list[Path]:
    """Files next to a video that belong to it: Name.chs.srt, Name.nfo, Name-poster.jpg."""
    out = []
    stem = video.stem
    for n, _isd, isf in _entries(video.parent):
        if not isf or n == video.name:
            continue
        ext = Path(n).suffix.lower()
        if ext not in _SIDECAR_EXT or not n.startswith(stem):
            continue
        rest = n[len(stem):]
        if rest[:1] in (".", "-", "_", " "):
            out.append(video.parent / n)
    return out


def wrap_loose_videos(root: Path, preview: bool = False) -> list[dict]:
    """Wrap loose videos only when TMDB can identify the group.

    - Multi-part / multi-disc of same title share one folder.
    - If TMDB search fails: do NOT create per-video folders.
    - Folder name keeps full title when path budget allows; otherwise shortens.
    - Uses long-path prefix on Windows; removes empty folder if move fails.
    """

    # skip_wrap_if_root_dedicated_leaf (never for drive roots)
    try:
        if root_is_single_movie(root):
            return []
    except Exception:
        pass
    wrapped = []
    search_cache = load_json(SEARCH_CACHE_PATH)
    meta_cache = load_json(CACHE_PATH)  # loaded once, saved once: it can be large
    meta_cache_dirty = False
    stack = [root]
    seen = set()
    while stack:
        d = stack.pop()
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        kids = [d / n for n, isd, _isf in _entries(d) if isd]

        for child in kids:
            try:
                if should_prune(child.name):
                    continue
                if is_collection_dir(child.name):
                    stack.append(child)
                    continue
                if is_dedicated_movie_leaf(child):
                    continue
                stack.append(child)
            except Exception:
                continue

        videos = list_videos_in_dir(d)
        if not videos:
            continue
        if d.name.lower() in ("bdmv", "stream", "playlist", "clipinf", "certificate", "video_ts", "audio_ts"):
            continue
        if is_disc_structure(d):
            continue
        if is_collection_dir(d.name):
            continue
        # TV: episode files (S01E02, 第3集) and Season folders are not loose movies.
        if is_season_dir_name(d.name) or any(looks_like_episode_file(v.stem) for v in videos):
            continue
        # Dedicated movie leaf (incl. multi-ISO anthology): never treat as loose,
        # even when the scan root *is* that folder.
        try:
            is_root = d.resolve() == root.resolve()
        except Exception:
            is_root = str(d) == str(root)
        # A scan root with movie folders in it is a library: one stray video next to
        # them is a loose video, not "the" movie of that folder.
        if is_dedicated_movie_leaf(d) and not (is_root and interesting_subdirs(d)):
            continue
        if not is_root and len(videos) < 2:
            continue

        groups: dict[str, list] = {}
        for vf in videos:
            base = multipart_base(vf.stem)
            groups.setdefault(base, []).append(vf)

        for base, files in groups.items():
            if len(files) == 1 and not is_root and len(videos) == 1:
                continue

            # Prefer parent/folder title over disc codes like TJ_GOLDEN_ERA_ANTHOLOGY_D1
            year = extract_year(base) or (None if (is_drive_root(d) or is_root) else extract_year(d.name))
            queries = []
            sources = [base]
            if not is_drive_root(d) and not is_root:
                sources.insert(0, d.name)
            for src_name in sources:
                if not (src_name or "").strip():
                    continue
                queries.extend(extract_search_queries(src_name))
                cq = clean_query_title(src_name)
                if cq:
                    queries.append(cq)
            seen_q, uniq = set(), []
            for q in queries:
                q = (q or "").strip()
                if not q or q.lower() in seen_q:
                    continue
                seen_q.add(q.lower())
                uniq.append(q)
            # Prefer queries without year-range / cryptic ALLCAPS codes
            def _wq(q: str):
                junk = 0
                if re.search(r"\d{4}\s*[–—\-]\s*\d{4}", q):
                    junk += 2
                if q.isupper() and " " in q:
                    junk += 1
                return (junk, len(q))
            uniq.sort(key=_wq)
            tid, how = (None, "empty_query")
            query = uniq[0] if uniq else ""
            guessed = False
            for qtry in uniq:
                tid, how = search_tmdb(qtry, year, search_cache, prefer_kind=folder_media_hint(d.name))
                query = qtry
                if tid:
                    _cent = search_cache.get(_search_cache_key(qtry, year, "", folder_media_hint(d.name) or "")) or {}
                    if _cent.get("uncertain") or (
                        folder_title_mismatch_note(d.name, _cent.get("titles") or [])
                        and folder_title_mismatch_note(base, _cent.get("titles") or [])
                    ):
                        # A guess: keep looking for a certain hit with another query.
                        guessed = True
                        tid, how = None, "uncertain_title_only"
                        continue
                if tid:
                    break
            if not tid and guessed:
                how = "uncertain_title_only"

            if not tid:
                for vf in files:
                    wrapped.append({
                        "video": str(vf),
                        "folder": "",
                        "parent": str(d),
                        "stem": base,
                        "group_size": len(files),
                        "action": "skip_no_tmdb",
                        "reason": how or "no_tmdb_id",
                        "search_query": query,
                        "search_year": year,
                    })
                    print(f"  散落跳过(刮不到): {vf.name} | {how} 搜={query!r} 年={year}", flush=True)
                continue

            title_for_folder = query
            year_for_folder = year
            try:
                _cent = search_cache.get(_search_cache_key(query, year, "", folder_media_hint(d.name) or "")) or {}
                _mk = _cent.get("media") if _cent.get("media") in ("movie", "tv") else None
                meta = fetch_movie(str(tid), meta_cache, kind=_mk)
                meta_cache_dirty = True
                if isinstance(meta, dict) and meta.get("ok") and meta.get("picked_title"):
                    title_for_folder = meta.get("picked_title") or title_for_folder
                    year_for_folder = meta.get("year") or year_for_folder
            except Exception:
                pass

            longest = max((f.name for f in files), key=len)
            folder_name = fit_folder_name(title_for_folder, year_for_folder, str(tid), d, longest)
            dest_dir = d / folder_name
            if dest_dir.exists() and not dest_dir.is_dir():
                dest_dir = _unique_stem_folder(d, folder_name)

            for vf in files:
                dest_file = dest_dir / vf.name
                rec = {
                    "video": str(vf),
                    "folder": str(dest_dir),
                    "parent": str(d),
                    "stem": base,
                    "group_size": len(files),
                    "tmdb": str(tid),
                    "title": title_for_folder,
                }
                side = sidecar_files(vf)
                if side:
                    rec["sidecars"] = [x.name for x in side]
                if preview:
                    rec["action"] = "would_wrap"
                    wrapped.append(rec)
                    print(f"  计划收入文件夹: {vf.name} -> {dest_dir.name}/" + (f"（连同 {len(side)} 个字幕/附属文件）" if side else ""), flush=True)
                else:
                    created = False
                    try:
                        if not dest_dir.exists():
                            Path(long_path(dest_dir)).mkdir(parents=True, exist_ok=True)
                            created = True
                            _log_op("mkdir", path=str(dest_dir))
                        final_dest = dest_file
                        if Path(long_path(dest_file)).exists():
                            final_dest = dest_dir / f"{vf.stem}_moved{vf.suffix}"
                        shutil.move(long_path(vf), long_path(final_dest))
                        _log_op("rename", src=str(vf), dst=str(final_dest))
                        for sc in side:  # subtitles / nfo that belong to the video go with it
                            try:
                                target = dest_dir / sc.name
                                if not Path(long_path(target)).exists():
                                    shutil.move(long_path(sc), long_path(target))
                                    _log_op("rename", src=str(sc), dst=str(target))
                            except Exception:
                                pass
                        rec["action"] = "wrapped"
                        rec["video"] = str(final_dest)
                        wrapped.append(rec)
                        print(f"  已收入文件夹: {vf.name} -> {dest_dir.name}/", flush=True)
                    except Exception as e:
                        rec["action"] = "wrap_fail"
                        rec["error"] = str(e)
                        wrapped.append(rec)
                        print(f"  收入文件夹失败: {vf.name}: {e}", flush=True)
                        if created:
                            try:
                                if dest_dir.exists() and not any(dest_dir.iterdir()):
                                    dest_dir.rmdir()
                                    print(f"  已清理空文件夹: {dest_dir.name}", flush=True)
                            except Exception:
                                pass
    try:
        save_json(SEARCH_CACHE_PATH, search_cache)
        if meta_cache_dirty:
            save_json(CACHE_PATH, meta_cache)
    except Exception:
        pass
    # wrap_stamp_media_kind
    for _w in wrapped:
        if not isinstance(_w, dict):
            continue
        _tid = str(_w.get("tmdb") or "").strip()
        if not _tid:
            continue
        _m = (_w.get("media") or _w.get("kind") or _w.get("id_kind") or "").strip().lower()
        if _m not in ("movie", "tv"):
            try:
                _m = classify_tmdb_id_kind(_tid, str(_w.get("stem") or _w.get("folder") or _w.get("title") or "")) or ""
            except Exception:
                _m = ""
        if _m in ("movie", "tv"):
            _w["media"] = _m
            _w["kind"] = _m
            _w["id_kind"] = _m
            _w["is_tv"] = _m == "tv"
            _w["is_movie"] = _m == "movie"
    return wrapped


def collect_candidate_dirs(root: Path):
    """Walk tree: tagged/bare-id leaves collected; dig through parents/series."""
    tagged = []
    untagged_video = []
    # If user selected a single movie folder as root, treat it as a leaf.
    try:
        if root_is_single_movie(root):
            tid, how = extract_id_from_name(root.name)
            rec = {
                "path": str(root),
                "name": root.name,
                "parent": str(root.parent),
            }
            if tid:
                rec["tmdb"] = tid
                rec["id_from"] = how
                tagged.append(rec)
            else:
                untagged_video.append(rec)
            return tagged, untagged_video
    except Exception:
        pass
    stack = [root]
    seen = set()
    while stack:
        d = stack.pop()
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        try:
            kids = [d / n for n, isd, _isf in _entries(d, strict=True) if isd]
        except Exception as e:
            print(f"  skip unreadable: {d} ({e})", flush=True)
            continue
        for child in kids:
            try:
                if should_prune(child.name):
                    continue
                # Never enter Blu-ray/DVD internal dirs as their own movies
                if child.name.lower() in DISC_DIR_NAMES or child.name.lower() in {
                    "stream", "playlist", "clipinf", "backup", "auxdata", "meta",
                }:
                    continue
                tid, how = extract_id_from_name(child.name)
                if tid:
                    tagged.append({
                        "path": str(child),
                        "name": child.name,
                        "parent": str(child.parent),
                        "tmdb": tid,
                        "id_from": how,
                    })
                    continue
                # Whole disc folder (contains BDMV/VIDEO_TS) = one leaf; do not dig in
                if is_disc_structure(child):
                    untagged_video.append({
                        "path": str(child),
                        "name": child.name,
                        "parent": str(child.parent),
                        "kind": "disc",
                    })
                    continue
                try:
                    subdirs = [child / n for n, isd, _isf in _entries(child, strict=True) if isd and not should_prune(n)]
                except Exception:
                    subdirs = []
                # A show folder whose subfolders are Season 01 / S02 / 第一季 ...: the show
                # folder is the leaf, not each season.
                season_subs = [x for x in subdirs if is_season_dir_name(x.name)]
                if season_subs and not has_video(child) and len(season_subs) == len([x for x in subdirs if not is_extras_dir(x.name)]):
                    untagged_video.append({
                        "path": str(child),
                        "name": child.name,
                        "parent": str(child.parent),
                        "kind": "tv_show",
                        "media": "tv",
                    })
                    continue
                if SERIES.search(child.name) or (subdirs and not has_video(child)):
                    stack.append(child)
                    continue
                if has_video(child):
                    untagged_video.append({
                        "path": str(child),
                        "name": child.name,
                        "parent": str(child.parent),
                    })
                elif subdirs:
                    stack.append(child)
            except Exception:
                continue
    return tagged, untagged_video



def _try_unique_title_search(leaf: dict, search_cache: dict) -> bool:
    """If title(+year) search hits exactly one side uniquely, stamp leaf and return True.

    Used when an existing tmdbid is unclear/collides: a unique title hit is trusted.
    Both-sides or ambiguous results leave the leaf untouched (caller may fail-tab).
    """
    name0 = leaf.get("name") or ""
    year = None
    try:
        year = extract_year(name0)
    except Exception:
        year = None
    # Do not fall back to the first year inside a range (1940–1958); extract_year
    # already returns None for ranges on purpose.
    if not year and not re.search(r"(?:19|20)\d{2}\s*[–—\-]\s*(?:19|20)\d{2}", name0):
        m = re.search(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)", name0)
        if m:
            year = m.group(1)
    queries: list[str] = []
    try:
        queries.extend(extract_search_queries(name0) or [])
    except Exception:
        pass
    try:
        cq = clean_query_title(name0)
        if cq:
            queries.append(cq)
    except Exception:
        pass
    # de-dupe
    seen = set()
    uniq = []
    for q in queries:
        q = (q or "").strip()
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        uniq.append(q)
    prefer = str(
        leaf.get("prefer_tmdb")
        or leaf.get("movie_tmdb")
        or leaf.get("tv_tmdb")
        or leaf.get("tmdb")
        or ""
    ).strip()
    for query in uniq:
        tid, how = search_tmdb(query, year, search_cache, prefer_tid=prefer, prefer_kind=folder_media_hint(name0))
        if not tid:
            continue
        cent = search_cache.get(
            _search_cache_key(query, year, prefer, folder_media_hint(name0))
        ) or {}
        media_hit = cent.get("media") if isinstance(cent, dict) else ""
        if media_hit not in ("movie", "tv"):
            try:
                media_hit = classify_tmdb_id_kind(str(tid), name0) or ""
            except Exception:
                media_hit = ""
        if media_hit not in ("movie", "tv"):
            continue
        leaf["tmdb"] = str(tid)
        leaf["media"] = media_hit
        leaf["id_from"] = "title_search_unique"
        leaf["search_query"] = query
        leaf["search_year"] = year
        leaf.pop("ambiguous", None)
        leaf.pop("reason", None)
        return True
    return False


# The folder the scan started from. Its name is the library's, never a film's title.
SCAN_ROOT: Path | None = None

# Choices made in the GUI for folders the tool could not decide: {path: {"tmdb", "media"}}.
USER_CHOICES: dict = {}


def _path_key(path) -> str:
    return str(path).replace("/", "\\").rstrip("\\").lower()


def load_user_choices() -> dict:
    """Drop choices whose folder no longer exists (it was renamed after the choice was used)."""
    path = TOOLS / "tmdb_user_choices.json"
    data = load_json(path)
    out = {}
    for k, v in (data or {}).items():
        if not (isinstance(v, dict) and str(v.get("tmdb") or "").isdigit() and v.get("media") in ("movie", "tv")):
            continue
        if v.get("path") and not Path(v["path"]).exists():
            continue  # the folder was renamed: the choice has been used
        out[k] = v
    if out != (data or {}):
        try:
            save_json(path, out)
        except Exception:
            pass
    return out


def _leaf_video_stems(folder: Path, limit: int = 40) -> list[str]:
    """Stems of the videos in a folder and in its Season subfolders."""
    vids = []
    try:
        vids = list(list_videos_in_dir(folder))
        for sub in interesting_subdirs(folder)[:8]:
            vids += list_videos_in_dir(sub)
    except Exception:
        pass
    return [v.stem for v in vids[:limit]]


def year_from_stems(stems: list) -> str | None:
    """The release year the video file names agree on (None if none, or they disagree)."""
    years = []
    for st in stems:
        y = extract_year(strip_season_tokens(st))
        if y:
            years.append(y)
    if not years:
        return None
    top = Counter(years).most_common()
    return top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else None


def _cand_region_ok(c: dict, region: dict) -> bool:
    if c.get("country"):
        return bool({x.upper() for x in c["country"].split("/")} & region["countries"])
    return (c.get("lang") or "").lower() in region["langs"]


def candidates_note(cands: list, limit: int = 4) -> str:
    return "候选：" + "；".join(describe_candidate(c) for c in (cands or [])[:limit]) if cands else ""


def _cjk_qualifier(name: str) -> tuple[str, str]:
    """'维兰德（瑞典版）.Wallander…' -> ('维兰德', '瑞典版'): a version tag after a Chinese title."""
    n = strip_season_tokens(strip_site_tags(name or ""))
    m = re.match(r"^\s*([\u4e00-\u9fff·・]{2,30})[（(]([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9 ·・]{0,14})[）)]", n)
    return (m.group(1), m.group(2)) if m else ("", "")


def _leaf_media_hint(leaf: dict) -> str | None:
    """'tv'/'movie'/None for a leaf: name hint, a Season-folder show, or episode files."""
    hint = folder_media_hint(leaf.get("name") or "")
    if hint:
        return hint
    if leaf.get("kind") == "tv_show":
        return "tv"
    try:
        if any(looks_like_episode_file(v.stem) for v in list_videos_in_dir(Path(leaf.get("path") or ""))):
            return "tv"
    except Exception:
        pass
    return None


def resolve_leaf_id(leaf: dict, search_cache: dict):
    """Fill tmdb id via nfo or search. Mutates leaf."""
    choice = USER_CHOICES.get(_path_key(leaf.get("path") or ""))
    if choice:
        leaf["tmdb"] = str(choice["tmdb"])
        leaf["media"] = choice["media"]
        leaf["id_from"] = "user_choice"
        return True
    if leaf.get("tmdb"):
        tid0 = str(leaf.get("tmdb") or "").strip()
        if media_is_auto():
            name0 = leaf.get("name") or ""
            try:
                kind = classify_tmdb_id_kind(tid0, name0)
            except Exception:
                kind = ""
            if kind in ("movie", "tv"):
                leaf["media"] = kind
                leaf["id_from"] = leaf.get("id_from") or "bracket_tmdbid"
                return True
            # Title collide → year → tmdbid cascade via search; unique hit is correct
            leaf["prefer_tmdb"] = tid0
            if _try_unique_title_search(leaf, search_cache):
                return True
            # still duplicate / unclear — fail tab for user confirm
            if tmdb_id_both_kinds(tid0):
                try:
                    tv_s, mv_s, tv_t, mv_t = _title_scores_for_id(tid0, name0)
                except Exception:
                    tv_s = mv_s = 0
                    tv_t = mv_t = ""
                leaf["ambiguous"] = True
                leaf["id_from"] = "ambiguous_movie_and_tv"
                leaf["reason"] = "ambiguous_movie_and_tv"
                leaf["movie_tmdb"] = tid0
                leaf["tv_tmdb"] = tid0
                leaf["note"] = "标题未能确认：电影「%s」(分=%s) / 剧集「%s」(分=%s)" % (
                    mv_t or "?",
                    mv_s,
                    tv_t or "?",
                    tv_s,
                )
                # Both are offered in the picker (double-click in 未能匹配).
                leaf["candidates"] = [
                    {"media": "movie", "tmdb": str(tid0), "title": mv_t, "original": "", "year": "", "country": "", "lang": ""},
                    {"media": "tv", "tmdb": str(tid0), "title": tv_t, "original": "", "year": "", "country": "", "lang": ""},
                ]
                leaf["tmdb"] = None
                return False
            leaf["id_from"] = "no_tmdb_id"
            leaf["tmdb"] = None
            return False
        if media_is_tv():
            if is_tv_id_for_folder(tid0, leaf.get("name") or ""):
                leaf["media"] = "tv"
                leaf["id_from"] = leaf.get("id_from") or "bracket_tmdbid_tv"
                return True
            leaf["skip_as_movie"] = True
            leaf["id_from"] = "ignored_movie_tmdbid"
            leaf["tmdb"] = None
            return False
        leaf["media"] = "movie"
        return True

    folder = Path(leaf["path"])
    meta = find_nfo_meta(folder)
    tid = str(meta.get("tmdb") or "").strip()
    if tid.isdigit():
        if media_is_auto():
            name0 = leaf.get("name") or ""
            try:
                kind = classify_tmdb_id_kind(tid, name0)
            except Exception:
                kind = ""
            if kind in ("movie", "tv"):
                leaf["tmdb"] = tid
                leaf["media"] = kind
                leaf["id_from"] = "nfo"
                return True
            leaf["prefer_tmdb"] = tid
            if _try_unique_title_search(leaf, search_cache):
                return True
            if tmdb_id_both_kinds(tid):
                try:
                    tv_s, mv_s, tv_t, mv_t = _title_scores_for_id(tid, name0)
                except Exception:
                    tv_s = mv_s = 0
                    tv_t = mv_t = ""
                leaf["ambiguous"] = True
                leaf["id_from"] = "ambiguous_movie_and_tv"
                leaf["reason"] = "ambiguous_movie_and_tv"
                leaf["movie_tmdb"] = tid
                leaf["tv_tmdb"] = tid
                leaf["note"] = "标题未能确认：电影「%s」(分=%s) / 剧集「%s」(分=%s)" % (
                    mv_t or "?",
                    mv_s,
                    tv_t or "?",
                    tv_s,
                )
                # Both are offered in the picker (double-click in 未能匹配).
                leaf["candidates"] = [
                    {"media": "movie", "tmdb": str(tid), "title": mv_t, "original": "", "year": "", "country": "", "lang": ""},
                    {"media": "tv", "tmdb": str(tid), "title": tv_t, "original": "", "year": "", "country": "", "lang": ""},
                ]
                leaf["tmdb"] = None
                return False
            leaf["id_from"] = "no_tmdb_id"
            return False
        if media_is_tv() and not is_tv_id_for_folder(tid, leaf.get("name") or ""):
            leaf["skip_as_movie"] = True
            leaf["id_from"] = "ignored_movie_nfo"
            return False
        leaf["tmdb"] = tid
        leaf["media"] = "tv" if media_is_tv() else "movie"
        leaf["id_from"] = "nfo"
        return True
    year = None if leaf.get("_ignore_year") else (leaf.get("_year_override") or extract_year(leaf["name"]))
    if not year and not leaf.get("_ignore_year") and meta.get("year"):
        y = str(meta.get("year"))[:4]
        if y.isdigit():
            year = y
    stems = _leaf_video_stems(folder)
    if not year and not leaf.get("_ignore_year"):
        year = year_from_stems(stems)
        if year:
            leaf["year_from"] = "files"
    region = region_hint(leaf.get("name") or "", *stems[:3])
    region_key = (region or {}).get("key", "")
    leaf["media_hint"] = _leaf_media_hint(leaf) or ""
    # Search names: leaf itself, cleaned leaf, and parent folder (multi-disc D1/D2
    # often have short codes like TJ_GOLDEN_ERA_ANTHOLOGY_D1 while the parent has
    # the real title).
    parent_name = Path(leaf.get("parent") or folder.parent).name
    queries: list[str] = []
    parent_queries: list[str] = []

    def _add_queries(src: str, bucket: list) -> None:
        bucket.extend(extract_search_queries(src))
        cq = clean_query_title(src)
        if cq:
            bucket.append(cq)

    _add_queries(leaf.get("name") or "", queries)
    if meta.get("title"):
        queries.append(clean_query_title(str(meta.get("title"))))
    if meta.get("originaltitle"):
        queries.append(clean_query_title(str(meta.get("originaltitle"))))
    # The parent folder is only a fallback for a leaf with no usable title of its
    # own or a disc part (D1/CD2). Otherwise a library root such as "lib" or
    # "Films" would be searched too and could outrank or hijack the real title.
    has_own_queries = any(q and q.strip() for q in queries)
    parent_own = {q.strip().lower() for q in queries if q}
    parent_is_root = SCAN_ROOT is not None and _path_key(leaf.get("parent") or folder.parent) == _path_key(SCAN_ROOT)
    if parent_name and not parent_is_root and parent_name.lower() not in {"movies", "movie", "tv", "tvs", "电视剧", "电影", "动漫", "动画"}:
        if not [q for q in queries if q and q.strip()] or looks_like_disc_folder(leaf.get("name") or ""):
            _add_queries(parent_name, parent_queries)
            if not year:
                year = extract_year(parent_name)
    parent_only = {q.strip().lower() for q in parent_queries if q} - parent_own
    queries.extend(parent_queries)
    # "维兰德（瑞典版）": also search with the version tag, which may be part of the TMDB title.
    qual_core, qual = _cjk_qualifier(leaf.get("name") or "")
    if qual:
        queries.append(f"{qual_core} {qual}")
    # Prefer sequel-bearing queries first; never lock onto a base-title hit when
    # the source has a sequel mark (强奸男2 / 聚会的目的2). Then prefer longer.
    # The parent's trailing digit (a library root called "Movies2") is not a sequel
    # mark of this leaf; only count it when the parent is what we are searching.
    # Season markers (S01, 第一季) are not sequel marks of a title.
    src_mark = sequel_mark(strip_season_tokens(leaf.get("name") or "")) or (
        sequel_mark(strip_season_tokens(parent_name or "")) if parent_queries and not has_own_queries else ""
    )
    def _q_rank(q: str):
        junk = 1 if re.search(r"\d{4}\s*[–—\-]\s*\d{4}", q) or (re.search(r"\b(?:19|20)\d{2}\b", q) and not re.fullmatch(r"\d{1,4}", q.strip())) else 0
        has_seq = 0 if (src_mark and query_has_sequel(q, src_mark)) or sequel_mark(q) else 1
        return (1 if q.strip().lower() in parent_only else 0, junk, has_seq, -len(q))
    # de-dupe
    seen_q = set()
    uniq = []
    for q in sorted(queries, key=_q_rank):
        q = (q or "").strip()
        if not q or q.lower() in seen_q:
            continue
        seen_q.add(q.lower())
        uniq.append(q)
    if region:
        uniq = [q for q in dict.fromkeys(strip_region_words(q, region) for q in uniq) if q]
    queries = with_dot_variants(uniq)
    last_how = "empty_query"
    last_query = ""
    last_cent = None
    saw_search_error = False
    # An uncertain or ambiguous answer to one query must not stop the others: a
    # cleaner query may still give a certain hit. Keep the first such answer and
    # report it only if no query does.
    pending: dict | None = None
    for query in queries:
        # If source is clearly a sequel, skip base-title-only queries (wrong-match risk)
        if src_mark and not query_has_sequel(query, src_mark) and not sequel_mark(query):
            continue
        _prefer_tid = str(leaf.get("prefer_tmdb") or leaf.get("tmdb") or "") or None
        _prefer_kind = _leaf_media_hint(leaf)
        tid, how = search_tmdb(query, year, search_cache, prefer_tid=_prefer_tid, prefer_kind=_prefer_kind, region=region)
        last_how, last_query = how, query
        last_cent = search_cache.get(_search_cache_key(query, year, _prefer_tid or "", _prefer_kind or "", region_key))
        if how == "search_error":
            saw_search_error = True
        if tid:
            cent = last_cent
            note = (cent or {}).get("uncertain") if isinstance(cent, dict) else ""
            if not note and region and isinstance(cent, dict):
                chosen_c = next((c for c in cent.get("candidates") or [] if str(c.get("tmdb")) == str(tid)), None)
                if chosen_c is not None and not _cand_region_ok(chosen_c, region):
                    note = f"文件夹名标了「{'/'.join(sorted(region['countries']))}」版，但匹配到的是 {chosen_c.get('country') or chosen_c.get('lang') or '?'} 制作"
            if not note and qual and not region and _norm_match_title(qual) not in _norm_match_title(query):
                note = f"文件夹名里的「{qual}」没有出现在搜索词里，可能是另一个版本"
            if not note and isinstance(cent, dict) and not (_prefer_tid and str(_prefer_tid) == str(tid)):
                # The query builder may have dropped a word of the real title (It Boy -> Boy).
                note = folder_title_mismatch_note(leaf.get("name") or "", cent.get("titles") or [], region)
            if note:
                # A guess: report it, do not rename unless confirmed.
                if pending is None:
                    pending = {
                        "id_from": "uncertain_title_only",
                        "reason": "uncertain_title_only",
                        "note": f"{note} | {candidates_note((cent or {}).get('candidates'))}",
                        "search_query": query,
                        "candidate_tmdb": str(tid),
                        "candidates": (cent or {}).get("candidates") or [],
                        "uncertain": note,
                    }
                continue
            leaf["tmdb"] = tid
            leaf["id_from"] = how
            leaf["search_query"] = query
            leaf["search_year"] = year
            media_hit = ""
            if isinstance(cent, dict) and cent.get("media") in ("movie", "tv"):
                media_hit = cent["media"]
            if not media_hit and how == "search_kind_hint" and _prefer_kind in ("movie", "tv"):
                media_hit = _prefer_kind
            if not media_hit and _prefer_kind in ("movie", "tv"):
                media_hit = _prefer_kind
            if not media_hit:
                try:
                    media_hit = classify_tmdb_id_kind(str(tid), leaf.get("name") or "") or ""
                except Exception:
                    media_hit = ""
            if not media_hit:
                media_hit = "tv" if media_is_tv() else "movie"
            leaf["media"] = media_hit
            return True
        if how == "ambiguous_movie_and_tv":
            cent = last_cent or {}
            if pending is None or pending.get("reason") != "uncertain_title_only":
                pending = {
                    "ambiguous": True,
                    "id_from": "ambiguous_movie_and_tv",
                    "reason": "ambiguous_movie_and_tv",
                    "search_query": query,
                    "movie_tmdb": cent.get("movie_tmdb"),
                    "tv_tmdb": cent.get("tv_tmdb"),
                    "candidates": cent.get("candidates") or [],
                    "note": ("电影候选=%s %s / 剧集候选=%s %s" % (
                        cent.get("movie_tmdb"), cent.get("movie_title") or "",
                        cent.get("tv_tmdb"), cent.get("tv_title") or "",
                    )) + (" | " + candidates_note(cent.get("candidates")) if cent.get("candidates") else ""),
                }
    if year and not leaf.get("_ignore_year") and _SEASON_TOKENS.search(leaf.get("name") or ""):
        # The year in a season release name is that season's year (Friends.S07.2000), not the
        # show's first-air year (1994), so a year-filtered search can miss the show. Ask again
        # without the year; keep the first outcome if that does not settle it.
        probe = dict(leaf)
        probe["_ignore_year"] = True
        if resolve_leaf_id(probe, search_cache):
            leaf.update(probe)
            return True
    if pending is not None:
        leaf.update(pending)
        leaf["search_year"] = year
        return False
    # A later "no results" must not hide that an earlier query failed to reach TMDB.
    if not saw_search_error and not leaf.get("_year_retry"):
        # Nothing found for this year: the name may carry another year-looking number.
        alts = other_year_candidates(leaf.get("name") or "", year)
        if alts:
            leaf["_year_retry"] = True
            leaf["_year_override"] = alts[0]
            if resolve_leaf_id(leaf, search_cache):
                return True
            if leaf.get("id_from") in ("uncertain_title_only", "ambiguous_movie_and_tv", "ambiguous_no_year"):
                return False
    leaf["id_from"] = "search_error" if saw_search_error else (last_how or "unresolved")
    leaf["search_query"] = last_query or (queries[0] if queries else "")
    leaf["search_year"] = year
    lc = last_cent if isinstance(last_cent, dict) else {}
    if lc.get("candidates"):
        leaf["candidates"] = lc["candidates"]
        leaf["note"] = candidates_note(lc["candidates"])
    return False


IMG_POSTER = "https://image.tmdb.org/t/p/w780"
IMG_BACKDROP = "https://image.tmdb.org/t/p/w1280"
IMG_ORIGINAL = "https://image.tmdb.org/t/p/original"


def _file_ok(path: Path, min_size: int = 200) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_size
    except Exception:
        return False


def _download_bytes(url: str, dest: Path, min_size: int = 500) -> str:
    if _file_ok(dest, min_size=1):
        return "already_exists"
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err = ""
    for attempt in range(1, 5):
        try:
            data = _http_get(url, timeout=60)
            if not data or len(data) < min_size:
                return "too_small"
            tmp.write_bytes(data)
            if dest.exists():
                try:
                    dest.unlink()
                except Exception:
                    pass
            tmp.replace(dest)
            _log_op("create", path=str(dest))
            return "ok"
        except Exception as e:
            last_err = str(e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            if isinstance(e, urllib.error.HTTPError) and e.code in (400, 403, 404):
                break  # definitive: this file is not there
            time.sleep(1.5 * attempt)
    return f"err:{last_err}"


def download_artwork(folder: Path, meta: dict, preview: bool = False) -> dict:
    """Download artwork only when TMDB has the asset.

    poster / fanart(background) / clearlogo. landscape 不生成（避免 fanart 复制改名）。
    No fabricated folder.jpg, banner, or discart.
    Returns dict of {kind: status}.
    """
    global _DISC_ART_WARNED
    results = {}
    art = meta.get("art") if isinstance(meta.get("art"), dict) else {}
    poster = meta.get("poster_path") or art.get("poster") or ""
    backdrop = meta.get("backdrop_path") or art.get("backdrop") or ""
    logo = meta.get("logo_path") or art.get("logo") or ""

    jobs = []
    if poster:
        jobs.append(("poster", IMG_POSTER + poster, folder / "poster.jpg", 500))
    else:
        results["poster"] = "no_poster_path"
    # Only download when TMDB actually provides the asset (never invent).
    # fanart = backdrop only. Do NOT copy fanart to landscape.jpg (Emby thumb duplicate).
    if backdrop:
        jobs.append(("fanart", IMG_BACKDROP + backdrop, folder / "fanart.jpg", 500))
    else:
        results["fanart"] = "no_backdrop_path"
    # TMDB has no separate landscape asset; skip rather than renaming fanart.
    results["landscape"] = "skipped_not_on_tmdb"
    if logo:
        jobs.append(("clearlogo", IMG_ORIGINAL + logo, folder / "clearlogo.png", 200))
    else:
        results["clearlogo"] = "no_logo_path"

    if not _DISC_ART_WARNED:
        print("  说明：光盘图（discart）TMDB没有提供，已跳过", flush=True)
        _DISC_ART_WARNED = True

    for kind, url, dest, min_sz in jobs:
        if preview:
            if _file_ok(dest, min_size=1):
                results[kind] = "already_exists"
            else:
                results[kind] = f"would_download:{url.split('/')[-1]}"
        else:
            st = _download_bytes(url, dest, min_size=min_sz)
            results[kind] = st
            time.sleep(0.015)

    # Do not fabricate folder.jpg / banner.jpg / discart — only real TMDB assets above.
    return results


def _cdata_plot(plot: str) -> str:
    """Wrap plot in CDATA; split safely if it contains the CDATA terminator."""
    s = plot or ""
    if "]]>" in s:
        # Split across terminator: foo]]>bar -> <![CDATA[foo]]]]><![CDATA[>bar]]>
        parts = s.split("]]>")
        return "<![CDATA[" + "]]]]><![CDATA[>".join(parts) + "]]>"
    return f"<![CDATA[{s}]]>"


def build_nfo_xml(meta: dict, tid: str, root: str = "movie") -> str:
    title = meta.get("picked_title") or meta.get("title_zh") or meta.get("title_en") or ""
    original = meta.get("original_title") or ""
    year = meta.get("year") or ""
    if year:
        year = str(year)[:4]
    plot = meta.get("plot") or meta.get("overview") or ""
    runtime = meta.get("runtime")
    runtime_s = str(int(runtime)) if runtime else ""
    premiered = meta.get("release_date") or ""
    genres = meta.get("genres") or []
    imdb = meta.get("imdb_id") or ""
    rating = meta.get("rating")
    cast = meta.get("cast") or []
    directors = meta.get("directors") or []
    writers = meta.get("writers") or []
    credits = meta.get("credits") if meta.get("credits") is not None else writers
    countries = meta.get("countries") or []
    studios = meta.get("studios") or []
    collection = meta.get("collection") if isinstance(meta.get("collection"), dict) else None

    tagline = (meta.get("tagline") or "").strip()
    mpaa = (meta.get("mpaa") or "").strip()
    trailer = (meta.get("trailer") or "").strip()
    tvdb = (meta.get("tvdb_id") or "").strip()
    dateadded = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        f"<{root}>",
        f"  <plot>{_cdata_plot(plot)}</plot>",
        "  <outline />",
        "  <lockdata>false</lockdata>",
        f"  <dateadded>{xml_escape(dateadded)}</dateadded>",
        f"  <title>{xml_escape(title)}</title>",
        f"  <originaltitle>{xml_escape(original)}</originaltitle>",
    ]

    for a in cast:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "").strip()
        if not name:
            continue
        role = (a.get("role") or a.get("character") or "").strip()
        atid = a.get("tmdbid") if a.get("tmdbid") is not None else a.get("id")
        atype = (a.get("type") or "Actor").strip() or "Actor"
        lines.append("  <actor>")
        lines.append(f"    <name>{xml_escape(name)}</name>")
        if role:
            lines.append(f"    <role>{xml_escape(role)}</role>")
        lines.append(f"    <type>{xml_escape(atype)}</type>")
        if atid is not None and str(atid).strip() != "":
            lines.append(f"    <tmdbid>{xml_escape(str(atid))}</tmdbid>")
        lines.append("  </actor>")

    for d in directors:
        if not isinstance(d, dict):
            continue
        name = (d.get("name") or "").strip()
        if not name:
            continue
        dtid = d.get("tmdbid") if d.get("tmdbid") is not None else d.get("id")
        if dtid is not None and str(dtid).strip() != "":
            lines.append(f'  <director tmdbid="{xml_escape(str(dtid))}">{xml_escape(name)}</director>')
        else:
            lines.append(f"  <director>{xml_escape(name)}</director>")

    for w in writers:
        if not isinstance(w, dict):
            continue
        name = (w.get("name") or "").strip()
        if not name:
            continue
        wtid = w.get("tmdbid") if w.get("tmdbid") is not None else w.get("id")
        if wtid is not None and str(wtid).strip() != "":
            lines.append(f'  <writer tmdbid="{xml_escape(str(wtid))}">{xml_escape(name)}</writer>')
        else:
            lines.append(f"  <writer>{xml_escape(name)}</writer>")

    for w in credits:
        if not isinstance(w, dict):
            continue
        name = (w.get("name") or "").strip()
        if not name:
            continue
        wtid = w.get("tmdbid") if w.get("tmdbid") is not None else w.get("id")
        if wtid is not None and str(wtid).strip() != "":
            lines.append(f'  <credits tmdbid="{xml_escape(str(wtid))}">{xml_escape(name)}</credits>')
        else:
            lines.append(f"  <credits>{xml_escape(name)}</credits>")

    if rating is not None and rating != "":
        lines.append(f"  <rating>{xml_escape(str(rating))}</rating>")
    if year:
        lines.append(f"  <year>{xml_escape(year)}</year>")
    lines.append(f"  <sorttitle>{xml_escape(title)}</sorttitle>")
    if mpaa:
        lines.append(f"  <mpaa>{xml_escape(mpaa)}</mpaa>")
    if tagline:
        lines.append(f"  <tagline>{xml_escape(tagline)}</tagline>")
    if runtime_s:
        lines.append(f"  <runtime>{xml_escape(runtime_s)}</runtime>")
    if imdb:
        lines.append(f"  <imdbid>{xml_escape(imdb)}</imdbid>")
    if tvdb:
        lines.append(f"  <tvdbid>{xml_escape(tvdb)}</tvdbid>")
    lines.append(f"  <tmdbid>{xml_escape(str(tid))}</tmdbid>")
    if premiered:
        lines.append(f"  <premiered>{xml_escape(premiered)}</premiered>")
        lines.append(f"  <releasedate>{xml_escape(premiered)}</releasedate>")
    if trailer:
        lines.append(f"  <trailer>{xml_escape(trailer)}</trailer>")
    for c in countries:
        if c:
            lines.append(f"  <country>{xml_escape(str(c))}</country>")
    for g in genres:
        if g:
            lines.append(f"  <genre>{xml_escape(str(g))}</genre>")
    for s in studios:
        if s:
            lines.append(f"  <studio>{xml_escape(str(s))}</studio>")
    if collection and (collection.get("id") is not None or (collection.get("name") or "").strip()):
        col_id = collection.get("id")
        col_name = (collection.get("name") or "").strip()
        if col_id is not None:
            lines.append(f'  <set tmdbcolid="{xml_escape(str(col_id))}">')
        else:
            lines.append("  <set>")
        if col_name:
            lines.append(f"    <name>{xml_escape(col_name)}</name>")
        lines.append("  </set>")
    lines.append(f'  <uniqueid type="tmdb">{xml_escape(str(tid))}</uniqueid>')
    if imdb:
        lines.append(f'  <uniqueid type="imdb">{xml_escape(imdb)}</uniqueid>')
    if tvdb:
        lines.append(f'  <uniqueid type="tvdb">{xml_escape(tvdb)}</uniqueid>')
    id_val = imdb if imdb else str(tid)
    lines.append(f"  <id>{xml_escape(id_val)}</id>")
    lines.append("  <fileinfo>")
    lines.append("    <streamdetails />")
    lines.append("  </fileinfo>")
    lines.append(f"</{root}>")
    lines.append("")
    return "\n".join(lines)


def folder_has_nfo(folder: Path) -> bool:
    """True if folder already has any .nfo (Emby or prior scrape)."""
    return any(isf and n.lower().endswith(".nfo") and not n.startswith(".") for n, _isd, isf in _entries(folder))



def _html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_tmdb_html(meta: dict, tid: str, folder: Path) -> str:
    """Offline TMDB-like detail page; images use same-folder filenames."""
    title = meta.get("picked_title") or meta.get("title_zh") or meta.get("title_en") or ""
    original = meta.get("original_title") or ""
    year = str(meta.get("year") or "")[:4]
    plot = meta.get("plot") or meta.get("overview") or ""
    rating = meta.get("rating")
    runtime = meta.get("runtime")
    genres = meta.get("genres") or []
    cast = meta.get("cast") or []
    directors = meta.get("directors") or []
    studios = meta.get("studios") or []
    countries = meta.get("countries") or []
    tagline = (meta.get("tagline") or "").strip()
    premiered = meta.get("release_date") or ""
    imdb = meta.get("imdb_id") or ""
    collection = meta.get("collection") if isinstance(meta.get("collection"), dict) else None
    _kind = (meta.get("kind") or meta.get("media") or "").strip().lower()
    if _kind not in ("movie", "tv"):
        try:
            _kind = classify_tmdb_id_kind(str(tid), "") or "movie"
        except Exception:
            _kind = "movie"
        if _kind not in ("movie", "tv"):
            _kind = "movie"
    tmdb_url = f"https://www.themoviedb.org/{_kind}/{tid}"

    def has(name: str) -> bool:
        try:
            f = folder / name
            return f.is_file() and f.stat().st_size > 200
        except Exception:
            return False

    poster = "poster.jpg" if has("poster.jpg") else ""
    fanart = "fanart.jpg" if has("fanart.jpg") else ("landscape.jpg" if has("landscape.jpg") else "")
    logo = "clearlogo.png" if has("clearlogo.png") else ""

    try:
        rating_s = f"{float(rating):.1f}" if rating not in (None, "") else "—"
    except (TypeError, ValueError):
        rating_s = "—"
    runtime_s = f"{int(runtime)} min" if runtime else ""
    genre_s = " · ".join(_html_escape(str(g)) for g in genres if g)

    cast_actors = [
        a for a in cast
        if isinstance(a, dict) and (a.get("type") or "Actor") == "Actor"
    ][:24]
    cast_html = []
    for a in cast_actors:
        name = _html_escape((a.get("name") or "").strip())
        role = _html_escape((a.get("role") or a.get("character") or "").strip())
        if not name:
            continue
        cast_html.append(
            f'<div class="card"><div class="name">{name}</div>'
            f'<div class="role">{role}</div></div>'
        )
    directors_s = ", ".join(
        _html_escape((d.get("name") or "").strip())
        for d in directors if isinstance(d, dict) and d.get("name")
    )
    studios_s = ", ".join(_html_escape(str(s)) for s in studios if s)
    countries_s = ", ".join(_html_escape(str(c)) for c in countries if c)
    set_s = _html_escape((collection.get("name") or "").strip()) if collection else ""

    if fanart:
        bg_style = (
            'background-image:linear-gradient(90deg,rgba(3,37,65,.92) 0%,'
            'rgba(3,37,65,.75) 45%,rgba(3,37,65,.35) 100%),'
            f'url("{fanart}");'
        )
    else:
        bg_style = "background:#032541;"

    poster_html = (
        f'<img class="poster" src="{poster}" alt="poster">'
        if poster else
        '<div class="poster ph">No Poster</div>'
    )
    logo_html = f'<img class="logo" src="{logo}" alt="logo">' if logo else ""
    orig_html = (
        f'<p class="orig">{_html_escape(original)}</p>'
        if original and original != title else ""
    )
    tag_html = f'<p class="tagline">{_html_escape(tagline)}</p>' if tagline else ""
    runtime_html = f"<span>{_html_escape(runtime_s)}</span>" if runtime_s else ""
    genre_html = f"<span>{genre_s}</span>" if genre_s else ""
    date_html = f"<span>{_html_escape(premiered)}</span>" if premiered else ""
    dir_html = f"<b>导演</b> {directors_s}<br>" if directors_s else ""
    stu_html = f"<b>制片</b> {studios_s}<br>" if studios_s else ""
    cty_html = f"<b>国家</b> {countries_s}<br>" if countries_s else ""
    set_html = f"<b>系列</b> {set_s}<br>" if set_s else ""
    imdb_html = f" · <b>IMDb</b> {_html_escape(imdb)}" if imdb else ""
    cast_block = "".join(cast_html) if cast_html else '<div class="facts">暂无演员信息</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)} ({_html_escape(year)}) — TMDB offline</title>
<style>
:root {{ --bg:#032541; --card:#0b3a5b; --text:#fff; --muted:#a8b5c4; --accent:#01b4e4; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif; background:#0a1520; color:var(--text); }}
.hero {{ min-height:420px; background-size:cover; background-position:center; {bg_style} }}
.wrap {{ max-width:1100px; margin:0 auto; padding:28px 20px 48px; display:grid; grid-template-columns:220px 1fr; gap:28px; }}
.poster {{ width:220px; border-radius:10px; box-shadow:0 8px 28px rgba(0,0,0,.45); display:block; background:#123; }}
.poster.ph {{ height:330px; display:flex; align-items:center; justify-content:center; color:var(--muted); }}
.logo {{ max-width:280px; max-height:72px; margin:8px 0 12px; }}
h1 {{ margin:0 0 6px; font-size:2rem; font-weight:700; }}
.orig {{ color:var(--muted); margin:0 0 12px; }}
.tagline {{ font-style:italic; color:#d7e3ef; margin:0 0 14px; }}
.meta {{ display:flex; flex-wrap:wrap; gap:10px 16px; color:#dce7f2; margin:0 0 16px; font-size:.95rem; }}
.badge {{ background:rgba(1,180,228,.18); border:1px solid var(--accent); color:#b8ecfa; padding:2px 10px; border-radius:999px; }}
.plot {{ line-height:1.65; color:#e8eef5; max-width:70ch; white-space:pre-wrap; }}
section {{ margin-top:28px; }}
section h2 {{ font-size:1.25rem; margin:0 0 12px; border-left:4px solid var(--accent); padding-left:10px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:12px; }}
.card {{ background:var(--card); border-radius:8px; padding:12px; min-height:72px; }}
.card .name {{ font-weight:600; }}
.card .role {{ color:var(--muted); font-size:.85rem; margin-top:4px; }}
.facts {{ color:var(--muted); font-size:.92rem; line-height:1.7; }}
.facts b {{ color:#e7eef6; font-weight:600; }}
a {{ color:var(--accent); }}
.footer {{ margin-top:36px; color:var(--muted); font-size:.85rem; }}
@media (max-width:760px) {{ .wrap {{ grid-template-columns:1fr; }} .poster {{ width:160px; }} }}
</style>
</head>
<body>
<div class="hero">
  <div class="wrap">
    <div>{poster_html}</div>
    <div>
      {logo_html}
      <h1>{_html_escape(title)} <span style="font-weight:500;opacity:.85">({_html_escape(year)})</span></h1>
      {orig_html}
      {tag_html}
      <div class="meta">
        <span class="badge">★ {rating_s}</span>
        {runtime_html}
        {genre_html}
        {date_html}
      </div>
      <p class="plot">{_html_escape(plot)}</p>
      <p class="facts" style="margin-top:16px">
        {dir_html}{stu_html}{cty_html}{set_html}
        <b>TMDB</b> <a href="{tmdb_url}">{tid}</a>{imdb_html}
      </p>
    </div>
  </div>
</div>
<div class="wrap" style="display:block; max-width:1100px;">
  <section>
    <h2>演职员</h2>
    <div class="grid">
      {cast_block}
    </div>
  </section>
  <p class="footer">Offline snapshot by tmdb_format_rename · deps in this folder: poster.jpg / fanart.jpg / clearlogo.png · online: <a href="{tmdb_url}">{tmdb_url}</a></p>
</div>
</body>
</html>
"""


def write_tmdb_html(folder: Path, meta: dict, tid: str, preview: bool = False) -> str:
    """Write tmdb.html into movie folder (deps: local artwork files)."""
    if preview:
        return "would_write_html:tmdb.html"
    try:
        html = build_tmdb_html(meta, str(tid), folder)
        dest = folder / "tmdb.html"
        dest.write_text(html, encoding="utf-8")
        _log_op("create", path=str(dest))
        return f"ok:{dest.name}"
    except Exception as e:
        return f"err:{e}"


def write_movie_nfo(folder: Path, meta: dict, tid: str, preview: bool = False, kind: str = "movie") -> str:
    """Write Emby nfo as {video_filename}.nfo only when folder has no existing nfo.

    Existing Emby/other nfo is never overwritten or deleted.
    A TV show gets one tvshow.nfo (written unless one exists); episode nfos are left alone.
    """
    videos = list_videos_in_dir(folder)
    if kind == "tv":
        if (folder / "tvshow.nfo").exists():
            return "already_has_nfo"
        if preview:
            return "would_write_nfo:tvshow.nfo"
        try:
            (folder / "tvshow.nfo").write_text(build_nfo_xml(meta, tid, root="tvshow"), encoding="utf-8")
            _log_op("create", path=str(folder / "tvshow.nfo"))
            return "ok:tvshow.nfo"
        except Exception as e:
            return f"err:{e}"
    if folder_has_nfo(folder):
        if preview:
            return "already_has_nfo"
        return "already_has_nfo"

    if preview:
        if videos:
            return f"would_write_nfo:{videos[0].stem}.nfo"
        return "would_write_nfo"

    xml = build_nfo_xml(meta, tid)
    written = []
    try:
        if not videos:
            dest = folder / "movie.nfo"
            dest.write_text(xml, encoding="utf-8")
            _log_op("create", path=str(dest))
            written.append(dest.name)
        else:
            for vf in videos:
                dest = folder / f"{vf.stem}.nfo"
                dest.write_text(xml, encoding="utf-8")
                _log_op("create", path=str(dest))
                written.append(dest.name)
            # Do NOT delete existing movie.nfo — Emby primary; we only write when none existed.
        return "ok:" + ",".join(written)
    except Exception as e:
        return f"err:{e}"


def apply_artwork_and_nfo(
    folders_and_ids,
    cache: dict,
    preview: bool = False,
    do_poster: bool = True,
    do_nfo: bool = True,
    preview_nfo: bool = False,
):
    """folders_and_ids: list of (folder_path_str, tmdb_id[, 'movie'|'tv'])."""
    art_stats = {"ok": 0, "skip": 0, "fail": 0}
    nfo_stats = {"ok": 0, "skip": 0, "fail": 0}
    html_stats = {"ok": 0, "skip": 0, "fail": 0}
    samples = []
    write_nfo_now = do_nfo and (not preview or preview_nfo)

    # Downloads are the slow part of a real run: fetch them on worker threads
    # first, then let the loop below report what happened.
    art_prefetched: dict = {}
    if do_poster and not preview:
        def _dl(item):
            folder_s, tid = item[0], item[1]
            meta0 = cache.get(ckey(tid, item[2] if len(item) > 2 else None)) or {}
            if meta0.get("picked_title") and meta0.get("poster_path"):
                return folder_s, download_artwork(Path(folder_s), meta0, preview=False)
            return folder_s, None
        art_prefetched = {k: v for k, v in _pmap(_dl, folders_and_ids) if v is not None}

    for item in folders_and_ids:
        folder_s, tid = item[0], item[1]
        known_kind = item[2] if len(item) > 2 and item[2] in ("movie", "tv") else None
        folder = Path(folder_s)
        meta = cache.get(ckey(tid, known_kind)) or {}
        # Per-item movie/tv — do not rely on global mode alone.
        _k = (known_kind or meta.get("kind") or meta.get("media") or "").strip().lower()
        if _k not in ("movie", "tv"):
            try:
                _k = classify_tmdb_id_kind(str(tid), folder.name) or ""
            except Exception:
                _k = ""
        if _k not in ("movie", "tv"):
            _k = "tv" if media_is_tv() else "movie"
        if meta.get("kind") != _k or not meta.get("picked_title") or not meta.get("poster_path"):
            try:
                meta = fetch_movie(str(tid), cache, force=True, kind=_k)
            except Exception:
                meta = cache.get(ckey(tid, _k)) or meta
        if isinstance(meta, dict):
            meta["kind"] = _k
            cache[ckey(tid, _k)] = meta
        art_summary = ""
        nfo_st = ""

        if do_poster:
            results = art_prefetched.get(folder_s) or download_artwork(folder, meta, preview=preview)
            # Count poster primarily for stats (keep similar to old behavior)
            pst = results.get("poster", "")
            if preview:
                if pst.startswith("would_download"):
                    art_stats["ok"] += 1
                elif pst in ("already_exists", "already_has_poster"):
                    art_stats["skip"] += 1
                else:
                    art_stats["fail"] += 1
            else:
                any_ok = any(v == "ok" for v in results.values())
                any_exist = any(v == "already_exists" for v in results.values())
                if any(str(v).startswith("err") for v in results.values()):
                    # A poster/fanart/logo that failed to download must not hide
                    # behind another file that succeeded.
                    art_stats["fail"] += 1
                    print(
                        f"  图片下载失败: {folder.name} | "
                        + ";".join(f"{k}={v}" for k, v in results.items() if str(v).startswith("err")),
                        flush=True,
                    )
                elif any_ok:
                    art_stats["ok"] += 1
                elif any_exist and not any(str(v).startswith("err") for v in results.values()):
                    art_stats["skip"] += 1
                elif pst == "no_poster_path" and not any_ok:
                    art_stats["fail"] += 1
                elif any(str(v).startswith("err") for v in results.values()):
                    art_stats["fail"] += 1
                else:
                    art_stats["skip"] += 1
            art_summary = ";".join(f"{k}={v}" for k, v in results.items())

        if write_nfo_now:
            nfo_st = write_movie_nfo(folder, meta, str(tid), preview=preview and not preview_nfo, kind=_k)
            # if preview_nfo during preview: actually write; if apply: write
            if preview and not preview_nfo:
                nfo_st = "would_write_nfo"
                nfo_stats["ok"] += 1
            elif nfo_st == "already_has_nfo":
                nfo_stats["skip"] += 1
            elif nfo_st == "ok" or str(nfo_st).startswith("ok:"):
                nfo_stats["ok"] += 1
            elif nfo_st == "would_write_nfo" or str(nfo_st).startswith("would_write_nfo"):
                nfo_stats["ok"] += 1
            else:
                nfo_stats["fail"] += 1
        elif do_nfo and preview:
            nfo_st = "would_write_nfo"
            nfo_stats["ok"] += 1

        # Offline TMDB-like HTML (deps: poster/fanart/logo in same folder)
        html_st = write_tmdb_html(folder, meta, str(tid), preview=preview)
        if str(html_st).startswith("would_") or str(html_st).startswith("ok"):
            html_stats["ok"] += 1
        else:
            html_stats["fail"] += 1

        if len(samples) < 12:
            samples.append(
                f"{folder.name[:50]} | 图片={_zh_art_status(art_summary)} NFO={_zh_art_status(nfo_st)} 网页={_zh_art_status(html_st)}"
            )

    return art_stats, nfo_stats, html_stats, samples


# ---------------------------------------------------------------------------
# Change log: every move / rename / created file / removed empty folder of a real
# run is recorded, so "undo last run" can put things back.
# ---------------------------------------------------------------------------
CHANGE_LOG: list = []
_RUN_STAMP: list = [None]
_CHANGE_LOCK = threading.Lock()


def _log_op(op: str, **kw) -> None:
    with _CHANGE_LOCK:
        CHANGE_LOG.append({"op": op, **kw})


def history_dir() -> Path:
    return TOOLS / "history"


def save_history(root) -> None:
    """Write (overwrite) this run's history file. Called after each phase of a real run."""
    with _CHANGE_LOCK:
        ops = list(CHANGE_LOG)
    if not ops:
        return
    if _RUN_STAMP[0] is None:
        _RUN_STAMP[0] = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        history_dir().mkdir(parents=True, exist_ok=True)
        path = history_dir() / f"{_RUN_STAMP[0]}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"root": str(root), "time": _RUN_STAMP[0], "ops": ops}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
    except Exception as e:
        print(f"  warn: could not save the change history: {e}", flush=True)


def list_history() -> list:
    d = history_dir()
    try:
        return sorted((p for p in d.glob("*.json") if not p.name.endswith(".undone.json")), reverse=True)
    except Exception:
        return []


def undo_last_run() -> int:
    """Reverse the newest recorded real run. Returns the number of problems."""
    files = list_history()
    if not files:
        print("没有可以撤销的改名记录。", flush=True)
        return 0
    path = files[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读不了改名记录 {path.name}: {e}", flush=True)
        return 1
    ops = data.get("ops") or []
    print(f"撤销 {data.get('time')} 的那一次（{data.get('root')}），共 {len(ops)} 步 ...", flush=True)
    done = skipped = 0
    for o in reversed(ops):
        try:
            kind = o.get("op")
            if kind == "create":
                f = Path(o["path"])
                if f.is_file():
                    f.unlink()
                    done += 1
            elif kind == "rename":
                src, dst = Path(o["src"]), Path(o["dst"])
                if dst.exists() and not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(dst), str(src))
                    done += 1
                else:
                    skipped += 1
                    print(f"  跳过（现在的状态和记录不符）: {dst.name} -> {src.name}", flush=True)
            elif kind == "mkdir":
                d = Path(o["path"])
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                    done += 1
            elif kind == "rmdir":
                d = Path(o["path"])
                if not d.exists():
                    d.mkdir(parents=True, exist_ok=True)
                    done += 1
        except Exception as e:
            skipped += 1
            print(f"  撤销失败: {o} ({e})", flush=True)
    try:
        os.replace(str(path), str(path.with_name(path.stem + ".undone.json")))
    except Exception:
        pass
    print(f"已撤销 {done} 步，跳过 {skipped} 步。", flush=True)
    return skipped


def _rename_one(src: Path, dest: Path) -> None:
    """Rename folder; fall back to cmd ren on WinError 50 (some cloud mounts)."""
    try:
        src.rename(dest)
        _log_op("rename", src=str(src), dst=str(dest))
        return
    except OSError as e:
        if getattr(e, "winerror", None) != 50 and "not supported" not in str(e).lower():
            raise
    import subprocess
    if src.parent != dest.parent:
        raise OSError(f"cross-dir rename not supported via cmd: {src} -> {dest}")
    r = subprocess.run(
        ["cmd", "/c", "ren", str(src), dest.name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if r.returncode != 0 or not dest.exists():
        err = (r.stderr or r.stdout or "").strip() or f"cmd ren failed code={r.returncode}"
        raise OSError(err)
    _log_op("rename", src=str(src), dst=str(dest))


def apply_renames(plan):
    ok, fail = [], []
    ordered = sorted(
        plan,
        key=lambda r: 1 if (r.get("action") in ("merge_into", "merge_version", "merge_season")) else 0,
    )

    def _maybe_flatten(rec, dest: Path):
        if rec.get("flatten_discs") or rec.get("kind") == "multidisc_parent":
            try:
                moved = flatten_disc_subfolders(dest)
                return moved
            except Exception:
                return []
        # Also flatten when we just merged disc siblings into dest
        if rec.get("action") in {"merge_into", "rename_then_merge"} or rec.get("multidisc_merge_from"):
            try:
                return flatten_disc_subfolders(dest)
            except Exception:
                return []
        return []

    for i, rec in enumerate(ordered, 1):
        src = Path(rec["path"])
        dest = Path(rec["dest"])
        action = (rec.get("action") or "rename").strip()
        try:
            if action == "flatten_discs":
                if not dest.exists():
                    # after rename path may be src==dest already_ok
                    dest = src if src.exists() else dest
                flat = flatten_disc_subfolders(dest if dest.exists() else src)
                ok.append({**rec, "final": str(dest if dest.exists() else src), "note": "flattened", "flattened": len(flat)})
            elif action == "merge_season":
                if not src.exists():
                    fail.append({**rec, "error": "source_missing"})
                else:
                    if rec.get("season_no") is not None:
                        move_into_season(src, dest, rec["season_no"])
                    else:
                        merge_folder_into(src, dest)
                    ok.append({**rec, "final": str(dest), "note": "season_merged"})
            elif action == "merge_version":
                if not src.exists():
                    fail.append({**rec, "error": "source_missing"})
                else:
                    apply_version_names(src, rec.get("version_base") or dest.name,
                                        rec.get("version_label") or "", rec.get("version_videos") or [])
                    merge_folder_into(src, dest)
                    ok.append({**rec, "final": str(dest), "note": "version_merged"})
            elif action == "merge_into":
                if not src.exists():
                    ok.append({**rec, "final": str(dest), "note": "merge_src_missing"})
                else:
                    merge_folder_into(src, dest)
                    flat = _maybe_flatten(rec, dest)
                    ok.append({**rec, "final": str(dest), "note": "merged", "flattened": len(flat)})
            elif action == "rename_then_merge" or rec.get("multidisc_merge_from"):
                if not src.exists():
                    fail.append({**rec, "error": "source_missing"})
                else:
                    if str(src) != str(dest):
                        if not dest.exists():
                            _rename_one(src, dest)
                        else:
                            try:
                                if src.resolve() != dest.resolve():
                                    merge_folder_into(src, dest)
                            except Exception:
                                merge_folder_into(src, dest)
                    for sib in rec.get("multidisc_merge_from") or []:
                        sp = Path(sib)
                        if sp.exists():
                            merge_folder_into(sp, dest)
                    flat = _maybe_flatten(rec, dest)
                    ok.append({**rec, "final": str(dest), "flattened": len(flat)})
            else:
                if not src.exists():
                    fail.append({**rec, "error": "source_missing"})
                else:
                    if str(src) != str(dest):
                        _rename_one(src, dest)
                    if rec.get("season_wrap") is not None:
                        wrap_into_season(dest, rec["season_wrap"])
                    if rec.get("version_label"):
                        apply_version_names(dest, rec.get("version_base") or dest.name,
                                            rec["version_label"], rec.get("version_videos") or [])
                    flat = _maybe_flatten(rec, dest)
                    ok.append({**rec, "final": str(dest), "flattened": len(flat)})
        except Exception as e:
            fail.append({**rec, "error": str(e)})
        if i % 50 == 0:
            print(f"  heartbeat {i}/{len(ordered)} ok={len(ok)} fail={len(fail)}", flush=True)
    still = []
    for rec in fail:
        src = Path(rec["path"])
        dest = Path(rec["dest"])
        action = (rec.get("action") or "rename").strip()
        try:
            time.sleep(0.12)
            if action == "merge_into":
                if not src.exists():
                    ok.append({**rec, "final": str(dest), "note": "merge_src_missing"})
                    continue
                merge_folder_into(src, dest)
                _maybe_flatten(rec, dest)
                ok.append({**rec, "final": str(dest), "note": "merged_retry"})
                continue
            if dest.exists() and not src.exists():
                ok.append({**rec, "final": str(dest), "note": "already_dest"})
                continue
            if not src.exists():
                still.append(rec)
                continue
            _rename_one(src, dest)
            _maybe_flatten(rec, dest)
            ok.append({**rec, "final": str(dest), "note": "retry_ok"})
        except Exception as e:
            still.append({**rec, "error": str(e)})
    return ok, still




def picked_title_from_cache(meta: dict):
    if any(meta.get(k) for k in ("title_zh", "title_tw", "title_hk", "title_en")):
        fake_zh = {"title": meta.get("title_zh")}
        fake_tw = {"title": meta.get("title_tw")}
        fake_hk = {"title": meta.get("title_hk")}
        fake_en = {
            "title": meta.get("title_en"),
            "original_title": meta.get("original_title"),
            "release_date": meta.get("release_date"),
        }
        title, year, lang = pick_from_langs(fake_zh, fake_tw, fake_hk, fake_en, fake_en)
        if not year:
            year = meta.get("year")
        return title, year, lang
    return meta.get("picked_title"), meta.get("year"), meta.get("picked_lang")


def normalize_root_arg(raw: str) -> Path:
    """Clean a root given on the command line: stray quotes, 'E:' -> 'E:\\', 'E:\\.' -> 'E:\\'."""
    s = (raw or "").strip().strip('"').strip()
    if s.endswith("\\.") or s.endswith("/."):
        s = s[:-1]
    while len(s) > 3 and s[-1] in "\\/":
        s = s[:-1]
    if len(s) == 2 and s[1] == ":":
        s += "\\"
    return Path(s)




def _zh_reason(code: str) -> str:
    return {
        "already_ok": "已经是正确名字",
        "no_tmdb_id": "找不到TMDB编号",
        "ambiguous_no_year": "多个候选且无年份（已跳过不猜）",
        "no_usable_title": "没有可用标题",
        "dest_exists": "目标文件夹名已存在",
        "rename_failed": "改名失败",
        "no_results": "搜索无结果",
        "search_error": "搜索失败",
        "empty_query": "标题为空",
        "uncertain_title_only": "不确定：需要确认（未改名）",
        "duplicate_movie": "重复影片（请决定保留哪个）",
        "season_merge_unclear": "同一部剧的多个文件夹无法合并（请确认）",
    }.get(code or "", code or "?")


def _zh_id_from(code: str) -> str:
    return {
        "bracket_tmdbid": "文件夹名里的[tmdbid=]",
        "nfo": "来自nfo文件",
        "search": "TMDB搜索",
        "bare_id": "纯数字文件夹名",
        "token_id": "名字里的编号",
        "ambiguous_no_year": "多个候选且无年份",
        "no_results": "搜索无结果",
        "search_error": "搜索失败",
        "unresolved": "未能识别",
    }.get(code or "", code or "?")



def _zh_art_status(s: str) -> str:
    if not s:
        return "-"
    return (
        s.replace("already_exists", "已有")
        .replace("no_poster_path", "无封面")
        .replace("no_logo_path", "无logo")
        .replace("skipped_not_on_tmdb", "不生成(非独立资源)")
        .replace("no_backdrop_path", "无背景")
        .replace("would_download", "将下载")
        .replace("would_write_nfo", "将写nfo")
        .replace("already_has_nfo", "已有nfo跳过")
        .replace("would_write_html:tmdb.html", "将写网页")
        .replace("ok:", "完成:")
        .replace("poster=", "封面=")
        .replace("fanart=", "背景=")
        .replace("landscape=", "横图=")
        .replace("clearlogo=", "logo=")
    )




def tmdb_id_both_kinds(tid: str) -> bool:
    tid = str(tid or "").strip()
    if not tid.isdigit():
        return False
    try:
        return bool(tmdb_tv_exists(tid) and tmdb_movie_exists(tid))
    except Exception:
        return True  # cannot tell (network): treat as unclear so the folder is not renamed


def _title_scores_for_id(tid: str, folder_name: str) -> tuple[int, int, str, str]:
    """Return (tv_score, movie_score, tv_title, movie_title) for folder vs both endpoints.

    Scoring uses folder title tokens plus year (from folder name) against
    TMDB release / first_air year — enough to confirm most titled+year folders.
    """
    tid = str(tid or "").strip()
    folder_name = folder_name or ""
    tv = movie = None
    try:
        tv = api_get(f"https://api.themoviedb.org/3/tv/{tid}?api_key={API_KEY}&language=zh-CN")
        if not (isinstance(tv, dict) and not tv.get("_error") and tv.get("id") is not None):
            tv = None
    except Exception:
        tv = None
    try:
        movie = api_get(f"https://api.themoviedb.org/3/movie/{tid}?api_key={API_KEY}&language=zh-CN")
        if not (isinstance(movie, dict) and not movie.get("_error") and movie.get("id") is not None):
            movie = None
    except Exception:
        movie = None
    tv_title = " ".join(str(x or "") for x in ((tv or {}).get("name"), (tv or {}).get("original_name")))
    mv_title = " ".join(
        str(x or "") for x in ((movie or {}).get("title"), (movie or {}).get("original_title"))
    )
    ft = _title_tokens(folder_name)
    tv_score = len(ft & _title_tokens(tv_title)) if tv else -1
    mv_score = len(ft & _title_tokens(mv_title)) if movie else -1
    # Year from folder (Title (2010) [tmdbid=...]) strongly confirms the matching side
    folder_year = None
    try:
        folder_year = extract_year(folder_name)
    except Exception:
        folder_year = None
    if not folder_year:
        m = re.search(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)", strip_tmdb_markers(folder_name))
        if m:
            folder_year = m.group(1)
    if folder_year:
        if tv:
            ty = ((tv.get("first_air_date") or "")[:4])
            if ty == str(folder_year):
                tv_score = (tv_score if tv_score >= 0 else 0) + 5
            elif ty and tv_score >= 0:
                tv_score = max(0, tv_score - 1)
        if movie:
            my = ((movie.get("release_date") or "")[:4])
            if my == str(folder_year):
                mv_score = (mv_score if mv_score >= 0 else 0) + 5
            elif my and mv_score >= 0:
                mv_score = max(0, mv_score - 1)
    return tv_score, mv_score, tv_title.strip(), mv_title.strip()


def classify_tmdb_id_kind(tid: str, folder_name: str = "") -> str:
    """Return 'tv' / 'movie' / '' for a TMDB numeric id.

    When the same id exists on both movie and tv endpoints, decide by folder
    title overlap with TMDB titles (not library path names). In auto mode,
    return '' only if the title cannot clearly confirm either side.
    """
    tid = str(tid or "").strip()
    if not tid.isdigit():
        return ""
    has_tv = tmdb_tv_exists(tid)
    has_movie = tmdb_movie_exists(tid)
    if has_tv and not has_movie:
        return "tv"
    if has_movie and not has_tv:
        return "movie"
    if has_tv and has_movie:
        if not folder_name:
            return "" if media_is_auto() else ("tv" if media_is_tv() else "movie")
        tv_score, mv_score, _, _ = _title_scores_for_id(tid, folder_name)
        # Title and/or year match: higher side wins (year alone is enough for most cases)
        if tv_score > mv_score and tv_score > 0:
            return "tv"
        if mv_score > tv_score and mv_score > 0:
            return "movie"
        # Never tip by season/episode markers (CD1/CD2 etc. are not TV proof).
        if media_is_auto():
            # title+year cannot confirm — leave for fail tab
            return ""
        if folder_name and is_tv_id_for_folder(tid, folder_name):
            return "tv"
        return "movie"
    if media_is_auto():
        return ""
    return "tv" if media_is_tv() else "movie"


def stamp_item_media(item: dict) -> dict:
    """Stamp media/id_kind for GUI tabs.

    Prefer TMDB id type; on movie/tv id collision use folder title (+ year).
    No tmdbid yet (title/year only): season markers → tv, else current scrape mode.
    Never trust library path names like 电影/电视剧.
    """
    if not isinstance(item, dict):
        return item
    tid = str(item.get("tmdb") or "").strip()
    name = item.get("name") or item.get("path") or ""
    kind = ""
    prev = (item.get("media") or "").strip().lower()
    if prev in ("movie", "tv") and tid.isdigit():
        # Already decided while matching; do not spend two more requests on it.
        kind = prev
    elif tid.isdigit():
        try:
            kind = classify_tmdb_id_kind(tid, name)
        except Exception:
            kind = ""
    if not kind:
        prev = (item.get("media") or "").strip().lower()
        if prev in ("movie", "tv"):
            kind = prev
        elif media_is_auto():
            kind = "movie"
        else:
            kind = "tv" if media_is_tv() else "movie"
    item["media"] = kind
    item["id_kind"] = kind
    item["is_tv"] = kind == "tv"
    item["is_movie"] = kind == "movie"
    return item


# ---------------------------------------------------------------------------
# Video length (movies only). Read from the file header in pure Python:
# MP4/MOV/M4V (mvhd) and MKV/WEBM (Info/Duration). Anything else, or any
# error, gives None and the check is simply skipped.
# ---------------------------------------------------------------------------
def _mp4_info(f, size: int) -> dict | None:
    """{"sec", "w", "h"} from an MP4/MOV: mvhd for the length, the widest tkhd for the picture size."""
    def walk(start: int, end: int, want: bytes):
        pos = start
        while pos + 8 <= end:
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                return
            bsize = int.from_bytes(hdr[:4], "big")
            btype = hdr[4:8]
            hlen = 8
            if bsize == 1 and len(hdr) >= 16:
                bsize = int.from_bytes(hdr[8:16], "big")
                hlen = 16
            elif bsize == 0:
                bsize = end - pos
            if bsize < hlen:
                return
            if btype == want:
                yield pos + hlen, min(pos + bsize, end)
            pos += bsize

    for m_start, m_end in walk(0, size, b"moov"):
        out = {"sec": None, "w": 0, "h": 0}
        for h_start, _h_end in walk(m_start, m_end, b"mvhd"):
            f.seek(h_start)
            body = f.read(32)
            if len(body) >= 20:
                if body[0] == 1 and len(body) >= 32:
                    timescale = int.from_bytes(body[20:24], "big")
                    dur = int.from_bytes(body[24:32], "big")
                else:
                    timescale = int.from_bytes(body[12:16], "big")
                    dur = int.from_bytes(body[16:20], "big")
                out["sec"] = dur / timescale if timescale else None
            break
        for t_start, t_end in walk(m_start, m_end, b"trak"):
            for k_start, _k_end in walk(t_start, t_end, b"tkhd"):
                f.seek(k_start)
                body = f.read(96)
                off = 88 if (body[:1] == b"\x01") else 76
                if len(body) >= off + 8:
                    w = int.from_bytes(body[off:off + 4], "big") >> 16
                    h = int.from_bytes(body[off + 4:off + 8], "big") >> 16
                    if w * h > out["w"] * out["h"]:
                        out["w"], out["h"] = w, h
                break
        return out
    return None


def _ebml_vint(f, keep_marker: bool):
    b = f.read(1)
    if not b:
        return None, 0
    first = b[0]
    length = 1
    mask = 0x80
    while length <= 8 and not (first & mask):
        length += 1
        mask >>= 1
    if length > 8:
        return None, 0
    rest = f.read(length - 1)
    if len(rest) != length - 1:
        return None, 0
    val = first if keep_marker else (first & (mask - 1))
    for byte in rest:
        val = (val << 8) | byte
    unknown = (not keep_marker) and val == (1 << (7 * length)) - 1
    return (None if unknown else val), length


def _mkv_info(f, size: int) -> dict | None:
    """{"sec", "w", "h"} from an MKV/WEBM: Info/Duration and the video track's pixel size."""
    import struct

    def children(start: int, end: int):
        pos = start
        for _ in range(4000):
            if pos >= end:
                return
            f.seek(pos)
            eid, il = _ebml_vint(f, True)
            if eid is None:
                return
            esize, sl = _ebml_vint(f, False)
            data = pos + il + sl
            stop = end if esize is None else min(data + esize, end)
            yield eid, data, stop
            if stop <= pos:
                return
            pos = stop

    def uint(data: int, stop: int) -> int:
        f.seek(data)
        return int.from_bytes(f.read(stop - data), "big")

    for eid, data, stop in children(0, size):
        if eid != 0x18538067:  # Segment
            continue
        out = {"sec": None, "w": 0, "h": 0}
        got_info = False
        for cid, cdata, cstop in children(data, stop):
            if cid == 0x1F43B675:  # first Cluster: header part is over
                break
            if cid == 0x1549A966:  # Info
                scale, dur = 1000000, None
                for iid, idata, istop in children(cdata, cstop):
                    f.seek(idata)
                    raw = f.read(istop - idata)
                    if iid == 0x2AD7B1:
                        scale = int.from_bytes(raw, "big") or 1000000
                    elif iid == 0x4489 and len(raw) in (4, 8):
                        dur = struct.unpack(">f" if len(raw) == 4 else ">d", raw)[0]
                out["sec"] = dur * scale / 1e9 if dur else None
                got_info = True
            elif cid == 0x1654AE6B:  # Tracks
                for tid_, tdata, tstop in children(cdata, cstop):
                    if tid_ != 0xAE:  # TrackEntry
                        continue
                    for vid, vdata, vstop in children(tdata, tstop):
                        if vid != 0xE0:  # Video
                            continue
                        w = h = 0
                        for pid, pdata, pstop in children(vdata, vstop):
                            if pid == 0xB0:
                                w = uint(pdata, pstop)
                            elif pid == 0xBA:
                                h = uint(pdata, pstop)
                        if w * h > out["w"] * out["h"]:
                            out["w"], out["h"] = w, h
        return out if got_info else None
    return None


def video_info(path: Path) -> dict | None:
    """{"sec", "w", "h"} for MP4/MOV/M4V/MKV/WEBM; None when unreadable (any other format, damage)."""
    try:
        ext = path.suffix.lower()
        if ext not in (".mp4", ".m4v", ".mov", ".mkv", ".webm"):
            return None
        size = path.stat().st_size
        with open(long_path(path), "rb") as f:
            info = _mp4_info(f, size) if ext in (".mp4", ".m4v", ".mov") else _mkv_info(f, size)
        if not info or not info.get("sec") or not (0 < info["sec"] < 48 * 3600):
            return None
        return info
    except Exception:
        return None


def video_duration_seconds(path: Path) -> float | None:
    info = video_info(path)
    return info["sec"] if info else None


_EDITION_RE = re.compile(
    r"(?i)extended|director'?s?[\s._-]*cut|uncut|unrated|final[\s._-]*cut|redux|special[\s._-]*edition|ultimate|"
    r"collector|theatrical|complete[\s._-]*edition|加长|导演剪辑|未删减|完整版|终极版|特别版|院线版|戏院"
)


def leaf_duration_minutes(folder: Path) -> float | None:
    """Length of the movie in a folder: the longest file, or the sum of its parts (CD1/CD2)."""
    try:
        videos = list_videos_in_dir(folder)
    except Exception:
        return None
    groups: dict = {}
    for v in videos:
        d = video_duration_seconds(v)
        if d and d >= 60:
            groups.setdefault(multipart_base(v.stem), []).append(d)
    if not groups:
        return None
    return max(sum(ds) for ds in groups.values()) / 60.0


def runtime_fits(duration_min: float, runtime_min: float, edition: bool = False) -> bool:
    """Is the file length plausible for a film TMDB lists at runtime_min?

    Cuts differ: The Last Emperor is 163 min in cinemas and about 219 min extended,
    so the window is wide on the long side (x1.45; x2.3 when the name says extended /
    director's cut / 加长版) and a little narrow on the short side."""
    if not runtime_min or runtime_min <= 0:
        return True
    lo = runtime_min * (0.7 if edition else 0.88) - 4
    hi = runtime_min * (2.3 if edition else 1.45) + 10
    return lo <= duration_min <= hi


MERGE_SEASONS = True   # on by default; --no-merge-seasons turns it off


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_int(t: str) -> int | None:
    t = (t or "").strip()
    if t.isdigit():
        return int(t)
    if t == "十":
        return 10
    if t.startswith("十") and len(t) == 2 and t[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[t[1]]
    if len(t) == 2 and t[1] == "十" and t[0] in _CN_DIGITS:
        return _CN_DIGITS[t[0]] * 10
    if len(t) == 3 and t[1] == "十" and t[0] in _CN_DIGITS and t[2] in _CN_DIGITS:
        return _CN_DIGITS[t[0]] * 10 + _CN_DIGITS[t[2]]
    return _CN_DIGITS.get(t) if len(t) == 1 else None


def season_number_from_name(name: str) -> int | None:
    """One season number from a folder name (S02, Season 2, 第二季); None for none / ranges / specials."""
    n = name or ""
    if re.search(r"(?i)S\d{1,2}\s*[-–~]\s*S?\d{1,2}|Seasons?[\s._]*\d{1,2}\s*[-–~]|Complete[\s._]*Series|全\s*[一二三四五六七八九十\d]+\s*季|第\s*[一二三四五六七八九十\d]+\s*[-–~至到]", n):
        return None
    found = set()
    for m in re.finditer(r"(?i)(?<![A-Za-z0-9])S(\d{1,2})(?![0-9])", n):
        found.add(int(m.group(1)))
    for m in re.finditer(r"(?i)Season[\s._]*(\d{1,2})(?![0-9])", n):
        found.add(int(m.group(1)))
    for m in re.finditer(r"第\s*([零一二三四五六七八九十两\d]+)\s*季", n):
        v = _cn_int(m.group(1))
        if v is not None:
            found.add(v)
    return found.pop() if len(found) == 1 and 0 not in found else None


def season_folder_name(n: int) -> str:
    return f"Season {n:02d}"


def move_into_season(src: Path, dest: Path, season: int) -> None:
    """Move everything in src (loose episodes) into dest/Season NN, then drop empty src."""
    sd = dest / season_folder_name(season)
    merge_folder_into(src, sd)


def wrap_into_season(folder: Path, season: int) -> None:
    """A show folder that holds one season's episodes loose: put them into Season NN inside it."""
    sd = folder / season_folder_name(season)
    kids = [c for c in list(folder.iterdir())
            if c != sd and not (c.is_dir() and is_season_dir_name(c.name))]
    if not kids:
        return
    if not sd.exists():
        _log_op("mkdir", path=str(sd))
    sd.mkdir(exist_ok=True)
    for c in kids:
        _rename_one(c, _unique_child_path(sd, c.name))


def resolve_season_group(prim: dict, others: list) -> tuple[list, list]:
    """Folders of the same TV show: put each season under one show folder.

    Returns (plan_records, skip_records). A group is only merged when every folder's season
    can be read from its name (or it already contains Season subfolders) and no two folders
    claim the same season; otherwise nothing in the group is moved."""
    group = [prim] + others
    seasons = []
    for r in group:
        p = Path(r["path"])
        if r.get("kind") == "tv_show":
            subs = {x.name.lower() for x in p.iterdir() if x.is_dir() and is_season_dir_name(x.name)} if p.exists() else set()
            seasons.append(("show", subs))
        else:
            n = season_number_from_name(r.get("name") or "")
            seasons.append(("season", n))
    bad = [i for i, (k, v) in enumerate(seasons) if k == "season" and v is None]
    claimed: dict = {}
    clash = False
    for k, v in seasons:
        if k == "season" and v is not None:
            clash |= v in claimed
            claimed[v] = True
    if bad or clash:
        why = "看不出是第几季" if bad else "有两个文件夹是同一季"
        return [], [{**r, "reason": "season_merge_unclear", "note": f"同一部剧有多个文件夹，但{why}，未合并"} for r in group]
    plan = []
    for i, (r, (k, v)) in enumerate(zip(group, seasons)):
        r = dict(r)
        if i == 0:
            r["reason"] = "season_primary"
            if k == "season":
                r["season_wrap"] = v
            plan.append(r)
        else:
            r["action"] = "merge_season"
            r["dest"] = prim["dest"]
            r["target"] = prim["target"]
            r["reason"] = "season_merge"
            if k == "season":
                r["season_no"] = v
            plan.append(r)
    return plan, []


_VERSION_EDITIONS = [
    (r"director'?s?[\s._-]*cut|导演剪辑", "Director's Cut"),
    (r"extended|加长", "Extended"),
    (r"uncut|unrated|未删减", "Unrated"),
    (r"theatrical|院线版|戏院", "Theatrical"),
    (r"final[\s._-]*cut", "Final Cut"),
    (r"redux", "Redux"),
    (r"remaster|重制|修复版", "Remastered"),
    (r"imax", "IMAX"),
    (r"special[\s._-]*edition|特别版", "Special Edition"),
    (r"ultimate|终极版", "Ultimate"),
]
_VERSION_SOURCES = [
    (r"remux", "Remux"),
    (r"blu[\s._-]?ray|bdrip|brrip|bd25|bd50", "BluRay"),
    (r"web[\s._-]?dl|webrip", "WEB-DL"),
    (r"hdtv", "HDTV"),
    (r"dvdrip|dvd9|dvd5", "DVD"),
]


def version_label(text: str, info: dict | None) -> str:
    """Edition + resolution + source + HDR for one video, e.g. 'Extended 4K Remux'. '' = nothing notable."""
    t = text or ""
    parts = []
    for pat, lab in _VERSION_EDITIONS:
        if re.search(pat, t, re.I):
            parts.append(lab)
            break
    res = ""
    w, h = (info or {}).get("w") or 0, (info or {}).get("h") or 0
    if w and h:
        if w >= 3200 or h >= 1900:
            res = "4K"
        elif w >= 1800 or h >= 1000:
            res = "1080p"
        elif w >= 1200 or h >= 700:
            res = "720p"
        else:
            res = "SD"
    else:
        if re.search(r"2160p|(?<![a-z0-9])4k(?![a-z0-9])|uhd", t, re.I):
            res = "4K"
        elif re.search(r"1080[pi]", t, re.I):
            res = "1080p"
        elif re.search(r"720p", t, re.I):
            res = "720p"
        elif re.search(r"480p|576p", t, re.I):
            res = "SD"
    if res:
        parts.append(res)
    for pat, lab in _VERSION_SOURCES:
        if re.search(pat, t, re.I):
            parts.append(lab)
            break
    if re.search(r"dolby[\s._-]*vision|(?<![a-z0-9])dovi(?![a-z0-9])|(?<![a-z0-9])dv(?![a-z0-9])", t, re.I):
        parts.append("DV")
    elif re.search(r"hdr", t, re.I):
        parts.append("HDR")
    return " ".join(parts)


def version_profile(folder: Path) -> dict:
    """The main movie of a folder (largest video, or the multi-part set): size, length, picture size, label."""
    try:
        videos = list_videos_in_dir(folder)
    except Exception:
        videos = []
    groups: dict = {}
    for v in videos:
        try:
            sz = v.stat().st_size
        except Exception:
            sz = 0
        groups.setdefault(multipart_base(v.stem), []).append((v, sz))
    if not groups:
        return {"videos": [], "size": 0, "sec": None, "w": 0, "h": 0, "label": ""}
    members = max(groups.values(), key=lambda g: sum(x[1] for x in g))
    members.sort(key=lambda x: x[0].name.lower())
    info = None
    secs = []
    for v, _sz in members:
        i = video_info(v)
        if i:
            secs.append(i["sec"])
            if info is None or i["w"] * i["h"] > info["w"] * info["h"]:
                info = i
    text = folder.name + " " + " ".join(v.stem for v, _ in members)
    return {
        "videos": [v.name for v, _ in members],
        "size": sum(x[1] for x in members),
        "sec": sum(secs) if secs else None,
        "w": (info or {}).get("w") or 0,
        "h": (info or {}).get("h") or 0,
        "label": version_label(text, info),
    }


def describe_profile(p: dict) -> str:
    bits = []
    if p.get("w") and p.get("h"):
        bits.append(f"{p['w']}x{p['h']}")
    if p.get("sec"):
        bits.append(f"{round(p['sec'] / 60)}分钟")
    if p.get("size"):
        bits.append(f"{p['size'] / 1024 ** 3:.2f}GB" if p["size"] >= 1024 ** 3 else f"{p['size'] / 1024 ** 2:.0f}MB")
    return " ".join(bits) or "无法读取"


def decide_duplicate_group(group: list) -> tuple[str, list]:
    """group: records of folders that all claim the same movie folder name.

    Returns ("versions", labels) when every folder is a different version (labels are
    unique), else ("duplicate", labels): at least two look identical, the user decides."""
    profs = [version_profile(Path(r["path"])) for r in group]
    labels = [p["label"] for p in profs]
    for i, r in enumerate(group):
        r["version_profile"] = profs[i]
    # same label but clearly different length (another cut) → tell them apart by minutes
    for lab in set(labels):
        idx = [i for i, x in enumerate(labels) if x == lab]
        if len(idx) < 2:
            continue
        secs = [profs[i]["sec"] for i in idx]
        if all(secs) and max(secs) > min(secs) * 1.06:
            for i in idx:
                labels[i] = (lab + " " if lab else "") + f"{round(profs[i]['sec'] / 60)}min"
    unique = len(set(labels)) == len(labels) and all(p["videos"] for p in profs)
    return ("versions" if unique else "duplicate"), labels


def apply_version_names(folder: Path, base: str, label: str, video_names: list) -> None:
    """Rename a folder's main video (and its subtitles / nfo) to '<base> - <label>.ext'."""
    if not label or not video_names:
        return
    for i, vn in enumerate(sorted(video_names), 1):
        v = folder / vn
        if not v.exists():
            continue
        stem = f"{base} - {label}" + (f" - part{i}" if len(video_names) > 1 else "")
        if v.stem == stem:
            continue
        sides = sidecar_files(v)
        old_stem = v.stem
        dst = folder / (stem + v.suffix)
        if dst.exists():
            continue
        _rename_one(v, dst)
        for sc in sides:
            sdst = folder / (stem + sc.name[len(old_stem):])
            if sc.exists() and not sdst.exists():
                _rename_one(sc, sdst)


def _is_tv_leaf(L: dict) -> bool:
    """A TV show folder: stamped tv in automatic mode, or any folder in a TV-only run."""
    return L.get("media") == "tv" or (media_is_tv() and L.get("media") != "movie")


def tv_counts(tid: str, cache: dict) -> dict | None:
    """{"seasons": n, "episodes": n, "per": {season: episodes}} of a TMDB show (cached 14 days)."""
    ck = f"tvc:{tid}"
    c = cache.get(ck)
    if isinstance(c, dict) and time.time() - c.get("ts", 0) < 14 * 86400 and c.get("per") is not None:
        return c
    d = api_get(f"https://api.themoviedb.org/3/tv/{tid}?api_key={API_KEY}&language=zh-CN")
    if not isinstance(d, dict) or d.get("_error"):
        return None
    per = {}
    for se in d.get("seasons") or []:
        if isinstance(se, dict) and se.get("season_number") is not None:
            per[str(se["season_number"])] = int(se.get("episode_count") or 0)
    out = {
        "ts": time.time(),
        "seasons": int(d.get("number_of_seasons") or len([k for k in per if k != "0"])),
        "episodes": int(d.get("number_of_episodes") or 0),
        "per": per,
    }
    cache[ck] = out
    return out


_EP_SEASON_RE = re.compile(r"(?i)(?<![A-Za-z0-9])S(\d{1,2})[\s._]*E\d{1,3}")


def local_season_counts(folder: Path) -> dict:
    """{season_no: episode_file_count} of the video files under a show folder (extras skipped)."""
    out: dict = {}
    stack = [(folder, None)]
    seen = 0
    while stack and seen < 5000:
        d, dir_season = stack.pop()
        for n, isd, isf in _entries(d):
            if isd:
                if should_prune(n) or is_extras_dir(n) or n.lower() in DISC_DIR_NAMES:
                    continue
                sn = season_number_from_name(n) if is_season_dir_name(n) else None
                stack.append((d / n, sn if sn is not None else dir_season))
            elif isf and Path(n).suffix.lower() in VIDEO:
                seen += 1
                m = _EP_SEASON_RE.search(n)
                sn = int(m.group(1)) if m else dir_season
                if sn is not None:
                    out[sn] = out.get(sn, 0) + 1
    return out


def check_tv_episodes(leaves: list, cache: dict) -> tuple[list, list]:
    """Cross-check TV leaves with TMDB's season / episode counts. Returns (kept, flagged).

    More seasons (or clearly more episodes in a season) on disk than the matched show has is
    impossible; if another show with the same title does fit, the leaf is listed as uncertain.
    Fewer than TMDB lists is normal (partial libraries) and never flagged."""
    kept, flagged = [], []

    def fits(counts, loc):
        if not counts:
            return True
        for sn, n in loc.items():
            if sn == 0:
                continue
            if sn > counts["seasons"]:
                return False
            per = counts["per"].get(str(sn))
            if per and n > per * 1.3 + 3:
                return False
        return True

    def one(L):
        media = L.get("media") or ("tv" if media_is_tv() else "movie")
        if media != "tv" or not L.get("tmdb") or L.get("id_from") == "user_choice":
            return L, None
        loc = local_season_counts(Path(L["path"]))
        if not loc:
            return L, None
        tid = str(L["tmdb"])
        counts = tv_counts(tid, cache)
        if not counts:
            return L, None
        if fits(counts, loc):
            L["episode_check"] = "ok"
            return L, None
        name = L.get("name") or ""
        q = L.get("search_query") or clean_query_title(strip_season_tokens(name))
        try:
            found = _search_tmdb_one_kind("tv", q, None)
        except Exception:
            found = []
        pool = [(t2, r) for (_sc, t2, r) in found if str(t2) != tid and isinstance(r, dict) and _title_similarity(q, r) >= 1.0]
        rivals, fit = [], []
        for t2, r in pool[:5]:
            c2 = tv_counts(str(t2), cache)
            cand = {
                "media": "tv", "tmdb": str(t2), "title": r.get("name") or r.get("title") or "",
                "original": r.get("original_name") or "", "year": str(r.get("first_air_date") or "")[:4],
                "country": "", "lang": r.get("original_language") or "",
                "seasons": c2["seasons"] if c2 else None,
            }
            rivals.append(cand)
            if c2 and fits(c2, loc):
                fit.append(cand)
        meta = cache.get(ckey(tid, "tv")) or {}
        own = {
            "media": "tv", "tmdb": tid, "title": meta.get("picked_title") or "", "original": meta.get("original_title") or "",
            "year": str(meta.get("year") or "")[:4], "country": "", "lang": "", "seasons": counts["seasons"],
        }
        have = "、".join(f"第{k}季{v}集" for k, v in sorted(loc.items()))
        L["episode_check"] = "mismatch"
        if fit:
            best = fit[0]
            note = (f"文件夹里有 {have}，超出所选《{own['title']}》（共{counts['seasons']}季）的范围，"
                    f"但《{best['title']}》({best['year']}，{best['seasons']}季) 吻合")
            return L, {"note": note, "candidates": [own] + fit + [c for c in rivals if c not in fit]}
        L["episode_note"] = f"文件夹里有 {have}，但 TMDB 只有 {counts['seasons']} 季（可能是分季编号不同）"
        return L, None

    for L, flag in _pmap(one, leaves):
        if flag:
            L2 = dict(L)
            L2.pop("tmdb", None)
            L2.update({
                "id_from": "uncertain_title_only",
                "reason": "uncertain_title_only",
                "note": flag["note"] + (" | " + candidates_note(flag["candidates"], 4) if flag["candidates"] else ""),
                "candidates": flag["candidates"],
                "candidate_tmdb": str(L.get("tmdb")),
            })
            flagged.append(L2)
        else:
            kept.append(L)
    return kept, flagged


def check_movie_durations(leaves: list, cache: dict) -> tuple[list, list]:
    """Cross-check movie leaves with the length of their video. Returns (kept, flagged).

    A leaf is flagged (listed as uncertain, not renamed) when the video length does not
    fit the chosen film but fits another film with the same title, or does not fit and
    the folder year was already off. A length that merely differs on a film whose title
    and year match is only noted (it may be another cut)."""
    kept, flagged = [], []

    def one(L):
        if (L.get("media") or "movie") != "movie" or not L.get("tmdb") or L.get("id_from") == "user_choice":
            return L, None
        folder = Path(L["path"])
        d = leaf_duration_minutes(folder)
        if d is None:
            return L, None
        tid = str(L["tmdb"])
        meta = cache.get(ckey(tid, "movie")) or {}
        rt = meta.get("runtime")
        rt = int(rt) if rt else None
        name = L.get("name") or ""
        edition = bool(_EDITION_RE.search(name + " " + " ".join(_leaf_video_stems(folder, 6))))
        L["duration_min"] = round(d)
        L["runtime_min"] = rt
        if rt and runtime_fits(d, rt, edition):
            L["duration_check"] = "ok"
            return L, None
        fy = extract_year(name) or L.get("search_year")
        chosen_year = str(meta.get("year") or meta.get("release_date") or "")[:4]
        weak = bool(fy and chosen_year and fy != chosen_year)
        # Same-title films with a length that does fit.
        q = L.get("search_query") or clean_query_title(name)
        rivals = []
        try:
            found = _search_tmdb_one_kind("movie", q, None)
        except Exception:
            found = []
        pool = [(tid2, r) for (_sc, tid2, r) in found if str(tid2) != tid and isinstance(r, dict) and _title_similarity(q, r) >= 1.0]
        def _gap(item):
            y = str(item[1].get("release_date") or "")[:4]
            return abs(int(y) - int(fy)) if (y.isdigit() and fy and str(fy).isdigit()) else 99
        pool.sort(key=_gap)
        for tid2, r in pool[:4]:
            try:
                m2 = fetch_movie(str(tid2), cache, kind="movie")
            except Exception:
                continue
            rt2 = m2.get("runtime") if isinstance(m2, dict) else None
            rivals.append({
                "media": "movie", "tmdb": str(tid2), "title": r.get("title") or "", "original": r.get("original_title") or "",
                "year": str(r.get("release_date") or "")[:4], "country": "", "lang": r.get("original_language") or "",
                "runtime": int(rt2) if rt2 else None,
            })
        fit = [c for c in rivals if c["runtime"] and runtime_fits(d, c["runtime"], edition)]
        own = {
            "media": "movie", "tmdb": tid, "title": meta.get("picked_title") or "", "original": meta.get("original_title") or "",
            "year": chosen_year, "country": "", "lang": "", "runtime": rt,
        }
        mins = f"{int(d)}分钟"
        if fit:
            fit.sort(key=lambda c: abs(c["runtime"] - d))
            best = fit[0]
            L["duration_check"] = "rival_fits"
            note = (
                f"视频时长 {mins}，与所选《{own['title']}》({chosen_year}，{('片长%d分钟' % rt) if rt else '无片长记录'}) 不符，"
                f"但和《{best['title']}》({best['year']}，{best['runtime']}分钟) 吻合"
            )
            return L, {"note": note, "candidates": [own] + fit + [c for c in rivals if c not in fit]}
        if rt and weak:
            L["duration_check"] = "mismatch_weak"
            return L, {
                "note": f"视频时长 {mins}，与《{own['title']}》片长 {rt} 分钟差得多，而且文件夹年份 {fy} 与它的 {chosen_year} 也不一致",
                "candidates": [own] + rivals,
            }
        L["duration_check"] = "mismatch_noted" if rt else "unknown_runtime"
        if rt:
            L["duration_note"] = f"视频时长 {mins}，TMDB 片长 {rt} 分钟（可能是不同剪辑版）"
        return L, None

    for L, flag in _pmap(one, leaves):
        if flag:
            L2 = dict(L)
            L2.pop("tmdb", None)
            L2.update({
                "id_from": "uncertain_title_only",
                "reason": "uncertain_title_only",
                "note": flag["note"] + (" | " + candidates_note(flag["candidates"], 4) if flag["candidates"] else ""),
                "candidates": flag["candidates"],
                "candidate_tmdb": str(L.get("tmdb")),
            })
            flagged.append(L2)
        else:
            kept.append(L)
    return kept, flagged


def main():
    global MEDIA_KIND, API_KEY, TOOLS, CACHE_PATH, SEARCH_CACHE_PATH
    # GUI may set TMDB_API_KEY / TMDB_TOOLS_DIR after this module was first imported.
    TOOLS = _data_dir()
    CACHE_PATH = TOOLS / "tmdb_movie_title_cache.json"
    SEARCH_CACHE_PATH = TOOLS / "tmdb_search_cache.json"
    API_KEY = _load_api_key()
    args = [a for a in sys.argv[1:] if a]
    if "--undo" in args:
        return 1 if undo_last_run() else 0
    with _CHANGE_LOCK:
        CHANGE_LOG.clear()
    _RUN_STAMP[0] = None
    preview = "--preview" in args or "-n" in args
    no_poster = "--no-poster" in args
    no_nfo = "--no-nfo" in args
    preview_nfo = "--preview-nfo" in args
    only_new = "--only-new" in args
    global MERGE_SEASONS
    MERGE_SEASONS = "--no-merge-seasons" not in args
    media = "auto"
    if "--tv" in args or "--media=tv" in args:
        media = "tv"
    if "--media=movie" in args:
        media = "movie"
    for a in list(args):
        if a.startswith("--media="):
            media = (a.split("=", 1)[1] or "auto").strip().lower()
    if media not in ("movie", "tv", "auto"):
        media = "auto"
    MEDIA_KIND = media

    args = [
        a
        for a in args
        if a not in ("--preview", "-n", "--no-poster", "--no-nfo", "--preview-nfo", "--only-new", "--no-merge-seasons", "--tv", "--media=tv", "--media=movie", "--media=auto")
        and not a.startswith("--media=")
    ]
    if not args:
        root = Path(os.environ.get("TMDB_RENAME_ROOT") or os.getcwd())
    else:
        root = normalize_root_arg(args[0])
    try:
        root = root.resolve()
    except Exception:
        pass
    global SCAN_ROOT
    SCAN_ROOT = root

    # Reset rename-success log each run so UI won't show stale "改名成功" from older sessions
    try:
        (TOOLS / "tmdb_format_rename_apply.json").write_text(
            json.dumps(
                {
                    "renamed_ok": 0,
                    "renamed_fail": 0,
                    "ok": [],
                    "fail": [],
                    "media": MEDIA_KIND,
                    "root": str(root),
                    "cleared": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    print("=" * 60, flush=True)
    print("TMDB 剧集命名刮削" if media_is_tv() else "TMDB 电影命名刮削", flush=True)
    print(f"★★ 媒体类型 = 【{'剧集' if media_is_tv() else '电影'}】 ★★", flush=True)
    print(f"扫描目录 = {root}", flush=True)
    print(f"运行模式 = {'仅预览（不改文件）' if preview else '正式更改'}", flush=True)
    if no_poster:
        poster_mode = "关闭"
    elif preview:
        poster_mode = "预览（不下载）"
    else:
        poster_mode = "下载到电影文件夹"
    print(f"封面图片 = {poster_mode}", flush=True)
    if no_nfo:
        nfo_mode = "关闭"
    elif preview and not preview_nfo:
        nfo_mode = "预览（不写入）"
    elif preview_nfo:
        nfo_mode = "预览并写入"
    else:
        nfo_mode = "写入（仅当文件夹还没有 nfo）"
    print(f"NFO文件 = {nfo_mode}", flush=True)
    print("网页快照 = 生成 tmdb.html（正式更改时写入；预览只显示计划）", flush=True)
    print(
        "增量刮削 = 只处理新文件夹（跳过名字里已有 [tmdbid=] 的）" if only_new else "增量刮削 = 关闭（已刮削的也会再检查）",
        flush=True,
    )
    print("=" * 60, flush=True)
    if not root.exists() or not root.is_dir():
        print("错误：扫描目录不存在，或不是文件夹", flush=True)
        return 2
    key_ok, key_msg = check_api_key()
    if not key_ok:
        print(f"错误：{key_msg}", flush=True)
        return 2

    print("正在整理散落的视频（预览时只显示计划）...", flush=True)
    wrapped = wrap_loose_videos(root, preview=preview)
    print(f"  散落视频整理数 = {len(wrapped)}", flush=True)
    if not preview:
        save_history(root)

    print("正在扫描剧集文件夹..." if media_is_tv() else "正在扫描电影文件夹...", flush=True)
    tagged, untagged = collect_candidate_dirs(root)
    print(f"  文件夹名已带 TMDB编号: {len(tagged)}", flush=True)
    print(f"  尚未带编号的视频文件夹: {len(untagged)}", flush=True)
    skipped_done: list = []
    if only_new:
        stay = []
        for leaf in tagged:
            if is_finished_scrape(leaf):
                skipped_done.append(leaf)
            else:
                stay.append(leaf)
        tagged = stay
        print(f"  跳过已刮削（名字已含 [tmdbid=]）: {len(skipped_done)}", flush=True)

    search_cache = load_json(SEARCH_CACHE_PATH)
    USER_CHOICES.clear()
    USER_CHOICES.update(load_user_choices())
    leaves = list(tagged)
    unresolved = []
    if media_is_auto() and tagged:
        # [tmdbid=N] folders: a movie and a TV show can share N, so decide which one
        # this folder is (by its title) instead of assuming.
        tag_ok = _pmap(lambda L: resolve_leaf_id(L, search_cache), tagged)
        leaves = [L for L, ok in zip(tagged, tag_ok) if ok]
        for L, ok in zip(tagged, tag_ok):
            if not ok:
                if not L.get("reason"):
                    L["reason"] = L.get("id_from") or "unresolved"
                unresolved.append(L)
    resolved = _pmap(lambda u: resolve_leaf_id(u, search_cache), untagged)
    save_exists_cache()
    for u, ok in zip(untagged, resolved):
        if ok:
            leaves.append(u)
        else:
            if not u.get("reason"):
                u["reason"] = u.get("id_from") or "unresolved"
            unresolved.append(u)

    # Drop movie-id leaves in TV mode; also drop unresolved that were flagged as movie
    if media_is_tv() and not media_is_auto():
        kept_leaves = []
        for L in leaves:
            tid = str(L.get("tmdb") or "").strip()
            if not tid:
                continue
            if is_tv_id_for_folder(tid, L.get("name") or ""):
                kept_leaves.append(L)
            else:
                print(f"  剧集模式跳过电影文件夹: {(L.get('name') or '')[:70]} [tmdbid={tid}]", flush=True)
        leaves = kept_leaves
        unresolved = [u for u in unresolved if not u.get("skip_as_movie")]
        # Prefer series-like names among remaining unresolved (still try all, but log)
        tvish = [u for u in unresolved if looks_like_tv_name(u.get("name") or "")]
        other = [u for u in unresolved if not looks_like_tv_name(u.get("name") or "")]
        if tvish or other:
            print(
                f"  剧集模式待识别: 像剧集的 {len(tvish)}，其他 {len(other)}（仍会尝试，但优先剧集特征）",
                flush=True,
            )
            unresolved = tvish + other

    save_json(SEARCH_CACHE_PATH, search_cache)
    print(f"  通过 nfo/搜索补到编号: {len(leaves) - len(tagged)}", flush=True)
    print(f"  未能识别编号: {len(unresolved)}", flush=True)
    for u in unresolved[:15]:
        print(
            f"    ? {(u.get('name') or '')[:70]} | {_zh_id_from(u.get('id_from') or 'unresolved')}"
            f" (搜={u.get('search_query')!r} 年={u.get('search_year')})",
            flush=True,
        )

    cache = load_json(CACHE_PATH)
    # A movie and a TV show can share one numeric id, so metadata is cached and
    # fetched per (id, kind); the kind is the one the folder was matched on.
    def _leaf_kind(L):
        return L.get("media") if L.get("media") in ("movie", "tv") else None

    ids = sorted({(str(L["tmdb"]), _leaf_kind(L)) for L in leaves if L.get("tmdb")}, key=lambda x: (x[0], x[1] or ""))
    need = [(i, k) for i, k in ids if cache_needs_refetch(cache.get(ckey(i, k)) or {})]
    print(f"不重复的电影编号 = {len(ids)}，需要联网获取资料 = {len(need)}", flush=True)
    _done = [0]
    _done_lock = threading.Lock()

    def _fetch_one(pair):
        fetch_movie(pair[0], cache, kind=pair[1])
        with _done_lock:
            _done[0] += 1
            if _done[0] % 25 == 0:
                print(f"  fetched {_done[0]}/{len(need)}", flush=True)

    _pmap(_fetch_one, need)
    if need:
        save_json(CACHE_PATH, cache)
    for tid, kind in ids:
        key = ckey(tid, kind)
        meta = cache.get(key) or {}
        # Re-pick title with current language rules (cache may be stale)
        if meta.get("ok") and any(meta.get(k) for k in ("title_zh", "title_tw", "title_hk", "title_en")):
            fake_zh = {"title": meta.get("title_zh"), "release_date": meta.get("release_date")}
            fake_tw = {"title": meta.get("title_tw")}
            fake_hk = {"title": meta.get("title_hk")}
            fake_en = {
                "title": meta.get("title_en"),
                "original_title": meta.get("original_title"),
                "release_date": meta.get("release_date"),
            }
            title, year, lang = pick_from_langs(fake_zh, fake_tw, fake_hk, fake_en, fake_en)
            if title:
                meta = dict(meta)
                meta["picked_title"] = title
                meta["picked_lang"] = lang
                if year:
                    meta["year"] = year
                cache[key] = meta
        if cache_needs_refetch(meta):
            fetch_movie(tid, cache, kind=kind)
    save_json(CACHE_PATH, cache)

    # Movies: does the video's length fit the film we matched? (cheap header read)
    if not media_is_tv():
        leaves, dur_flagged = check_movie_durations(leaves, cache)
        for L in leaves:
            if L.get("duration_note"):
                print(f"  时长提示: {(L.get('name') or '')[:60]} | {L['duration_note']}", flush=True)
        for L in dur_flagged:
            print(f"  时长对不上: {(L.get('name') or '')[:60]} | {L.get('note')}", flush=True)
        unresolved.extend(dur_flagged)
        save_json(CACHE_PATH, cache)

    # TV: do the seasons / episodes on disk fit the show we matched?
    leaves, ep_flagged = check_tv_episodes(leaves, cache)
    for L in leaves:
        if L.get("episode_note"):
            print(f"  集数提示: {(L.get('name') or '')[:60]} | {L['episode_note']}", flush=True)
    for L in ep_flagged:
        print(f"  集数对不上: {(L.get('name') or '')[:60]} | {L.get('note')}", flush=True)
    unresolved.extend(ep_flagged)
    save_json(CACHE_PATH, cache)

    # Multi-disc sibling folders (D1/D2…) sharing parent+tmdb → one folder
    leaves, multidisc_skips = collapse_multidisc_leaves(leaves, root)
    plan, skip = [], []
    skip.extend(multidisc_skips)
    multidisc_dest_by_primary = {}
    used = {}
    dupgroups: dict = {}
    for L in leaves:
        tid = L["tmdb"]
        meta = cache.get(ckey(tid, _leaf_kind(L))) or {}
        title = year = lang = source = None
        if meta.get("ok") and meta.get("picked_title"):
            title, year, lang = picked_title_from_cache(meta)
            source = "tmdb"
        if not title:
            nfo = find_nfo_meta(Path(L["path"]))
            t = (nfo.get("title") or nfo.get("originaltitle") or "").strip()
            y = str(nfo.get("year") or "")[:4]
            if t:
                title, year, lang, source = t, (y if y.isdigit() else None), "nfo", "nfo"
        if not title:
            skip.append({**L, "reason": "no_usable_title"})
            continue
        parent = L["parent"]
        try:
            target = fit_folder_name(title, year, tid, Path(parent))
        except Exception:
            target = build_name(title, year, tid)
        key = (parent.lower(), target.lower())
        dest = str(Path(parent) / target)
        # Same target already claimed: merge multi-disc siblings instead of Title #2
        if key in used and used[key] != L["path"]:
            prev_path = used[key]
            prev_leaf = next((x for x in leaves if x.get("path") == prev_path), None)
            if prev_leaf and _same_multidisc_siblings(prev_leaf, L):
                skip.append({
                    **L,
                    "target": target,
                    "dest": dest,
                    "title": title,
                    "year": year,
                    "lang": lang,
                    "source": source,
                    "reason": "multidisc_merge",
                    "merge_into": dest,
                    "action": "merge_into",
                })
                for prec in plan:
                    if prec.get("path") == prev_path:
                        prec.setdefault("multidisc_merge_from", []).append(L["path"])
                        break
                else:
                    multidisc_dest_by_primary[prev_path] = dest
                continue
            if not _is_tv_leaf(L):
                dupgroups.setdefault(key, [prev_path]).append({
                    **L, "target": target, "dest": dest, "title": title,
                    "year": year, "lang": lang, "source": source,
                })
                continue
            if _is_tv_leaf(L) and MERGE_SEASONS:
                dupgroups.setdefault(key, [prev_path]).append({
                    **L, "target": target, "dest": dest, "title": title,
                    "year": year, "lang": lang, "source": source,
                })
                continue
            n = 2
            while key in used and n <= 30:
                try:
                    target = fit_folder_name(f"{title} #{n}", year, tid, Path(parent))
                except Exception:
                    target = build_name(f"{title} #{n}", year, tid)
                key = (parent.lower(), target.lower())
                n += 1
            dest = str(Path(parent) / target)
        used[key] = L["path"]
        rec = {
            **L,
            "target": target,
            "dest": dest,
            "title": title,
            "year": year,
            "lang": lang,
            "source": source,
        }
        if L.get("multidisc_merge_from"):
            rec["multidisc_merge_from"] = list(L.get("multidisc_merge_from") or [])
            rec["action"] = "rename_then_merge"
        if L["name"] == target:
            if rec.get("multidisc_merge_from"):
                multidisc_dest_by_primary[L["path"]] = dest
                plan.append({**rec, "reason": "already_ok_merge_siblings"})
            elif rec.get("flatten_discs") or rec.get("kind") == "multidisc_parent":
                # Name already correct, but still flatten D1/D2 media into this folder
                plan.append({**rec, "action": "flatten_discs", "reason": "flatten_discs_only"})
            else:
                skip.append({**rec, "reason": "already_ok"})
            continue
        dp, sp = Path(dest), Path(L["path"])
        try:
            if dp.exists() and dp.resolve() != sp.resolve():
                skip.append({**rec, "reason": "dest_exists"})
                continue
        except Exception:
            if dp.exists() and str(dp) != str(sp):
                skip.append({**rec, "reason": "dest_exists"})
                continue
        plan.append(rec)

    # Several folders for the same movie: different versions → one folder, labelled files;
    # identical-looking ones → reported as duplicates, nothing touched.
    for gkey, members in dupgroups.items():
        prim_path = members[0]
        prim = None
        for lst in (plan, skip):
            for r in lst:
                if r.get("path") == prim_path and r.get("reason") in (None, "already_ok", "flatten_discs_only"):
                    prim = r
                    lst.remove(r)
                    break
            if prim:
                break
        if prim is None:
            prim = next((r for r in skip if r.get("path") == prim_path), None)
            for m in members[1:]:
                skip.append({**m, "reason": "dest_exists"})
            continue
        if _is_tv_leaf(prim):
            p_recs, s_recs = resolve_season_group(prim, members[1:])
            plan.extend(p_recs)
            skip.extend(s_recs)
            continue
        group = [prim] + members[1:]
        verdict, labels = decide_duplicate_group(group)
        base = re.sub(r"\s*\[tmdbid=\d+\]\s*$", "", prim["target"])
        if verdict == "versions":
            for r, lab in zip(group, labels):
                r["version_label"] = lab
                r["version_videos"] = list(r["version_profile"]["videos"])
                r["version_base"] = base
                r["version_info"] = describe_profile(r["version_profile"])
            prim["reason"] = "version_primary"
            plan.append(prim)
            for r in group[1:]:
                plan.append({**r, "action": "merge_version", "dest": prim["dest"],
                             "target": prim["target"], "reason": "version_merge"})
        else:
            summary = "；".join(f"{Path(r['path']).name} → {describe_profile(r['version_profile'])}"
                               for r in group)
            for r in group:
                skip.append({**r, "reason": "duplicate_movie", "note": "重复：" + summary})

    # Promote multidisc merge skips into plan so apply actually moves them
    primary_dest = {p.get("path"): p.get("dest") for p in plan if p.get("dest")}
    primary_dest.update(multidisc_dest_by_primary)
    merge_plan = []
    still_skip = []
    for s in skip:
        if s.get("reason") == "multidisc_merge" and s.get("merge_into"):
            merge_plan.append({
                **s,
                "path": s["path"],
                "dest": s["merge_into"],
                "target": Path(s["merge_into"]).name,
                "action": "merge_into",
            })
        else:
            still_skip.append(s)
    expanded = []
    for rec in plan:
        expanded.append(rec)
        for sib in rec.get("multidisc_merge_from") or []:
            if any(m.get("path") == sib for m in merge_plan):
                continue
            merge_plan.append({
                "path": sib,
                "name": Path(sib).name,
                "parent": rec.get("parent"),
                "tmdb": rec.get("tmdb"),
                "dest": rec.get("dest"),
                "target": rec.get("target"),
                "title": rec.get("title"),
                "year": rec.get("year"),
                "action": "merge_into",
                "reason": "multidisc_merge",
            })
    plan = expanded + merge_plan
    skip = still_skip

    for u in unresolved:
        # Prefer specific failure (ambiguous / no_results) over generic no_tmdb_id.
        # Note: GUI must still dedupe — unresolved is listed separately for detail.
        how = (u.get("id_from") or u.get("reason") or "").strip()
        reason = how if how in (
            "ambiguous_no_year",
            "ambiguous_movie_and_tv",
            "ambiguous",
            "no_results",
            "search_error",
            "empty_query",
            "uncertain_title_only",
        ) else "no_tmdb_id"
        skip.append({**u, "reason": reason})

    print("-" * 60, flush=True)
    label = "剧集文件夹" if media_is_tv() else "电影文件夹"
    print(f"{label} = {len(leaves)}，待改名 = {len(plan)}，跳过 = {len(skip)}，散落整理 = {len(wrapped)}", flush=True)
    print(f"编号来源统计 = { { _zh_id_from(k): v for k, v in Counter(L.get('id_from') for L in leaves).items() } }", flush=True)
    print(f"跳过原因统计 = { { _zh_reason(k): v for k, v in Counter(s.get('reason') for s in skip).items() } }", flush=True)
    print(f"改名标题语言统计 = {dict(Counter(p.get('lang') for p in plan))}", flush=True)
    for p in plan[:30]:
        print(f"  [{p.get('id_from')}/{p.get('lang')}]: {(p.get('name') or '')[:65]}", flush=True)
        print(f"    -> {p['target'][:80]}", flush=True)
    if len(plan) > 30:
        print(f"  … 另外还有 {len(plan) - 30} 条", flush=True)

    # Stamp media for GUI: classify each TMDB id (cached; title-match on movie/tv id collisions).
    def _quick_stamp(rows):
        try:
            return [stamp_item_media(dict(x)) for x in rows]
        except Exception:
            return rows

    leaves, skip, plan, unresolved = (_quick_stamp(x) for x in (leaves, skip, plan, unresolved))

    log = {
        "root": str(root),
        "leaf_count": len(leaves),
        "plan_count": len(plan),
        "skip_count": len(skip),
        "unresolved_count": len(unresolved),
        "wrapped_count": len(wrapped),
        "skipped_done_count": len(skipped_done),
        "only_new": only_new,
        "leaves": leaves,
        "unresolved": unresolved,
        "wrapped": wrapped,
        "plan": plan,
        "skip": skip,
        "preview": preview,
        "media": MEDIA_KIND,
    }
    save_exists_cache()
    if not preview:
        save_history(root)
    log_path = TOOLS / "tmdb_format_rename_last.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"详细日志文件: {log_path}", flush=True)

    ok, fail = [], []
    if preview:
        print("当前是「仅预览」：不会改名，也不会写入文件。", flush=True)
    elif not plan:
        print("没有需要改名的文件夹（可能都已是正确名称，或未能识别）。", flush=True)
    else:
        print(f"开始正式改名，共 {len(plan)} 个文件夹...", flush=True)
        ok, fail = apply_renames(plan)
        save_history(root)
        apply_path = TOOLS / "tmdb_format_rename_apply.json"
        apply_path.write_text(
            json.dumps(
                {
                    "renamed_ok": len(ok),
                    "renamed_fail": len(fail),
                    "ok": ok,
                    "fail": fail,
                    "media": MEDIA_KIND,
                    "root": str(root),
                    "ts": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"DONE rename ok={len(ok)} fail={len(fail)}", flush=True)
        print(f"改名结果日志: {apply_path}", flush=True)
        for f in fail[:10]:
            print(f"  失败: {f.get('name')} | {f.get('error')}", flush=True)

    do_poster = not no_poster
    do_nfo = not no_nfo
    if do_poster or do_nfo:
        targets = []
        seen = set()

        def add_target(folder, tid, kind=None):
            if not folder or not tid:
                return
            key = str(folder).lower()
            if key in seen:
                return
            seen.add(key)
            targets.append((str(folder), str(tid), kind))

        if preview:
            for rec in plan:
                add_target(rec["path"], rec.get("tmdb"), rec.get("media"))
            for s in skip:
                if s.get("reason") == "already_ok":
                    add_target(s.get("dest") or s.get("path"), s.get("tmdb"), s.get("media"))
        else:
            for rec in ok:
                add_target(rec.get("final") or rec.get("dest"), rec.get("tmdb"), rec.get("media"))
            for s in skip:
                if s.get("reason") == "already_ok":
                    add_target(s.get("dest") or s.get("path"), s.get("tmdb"), s.get("media"))

        if targets:
            print(f"{'预览' if preview else '处理'} 封面/NFO/网页，共 {len(targets)} 个文件夹...", flush=True)
            art_st, nfo_st, html_st, samples = apply_artwork_and_nfo(
                targets,
                cache,
                preview=preview,
                do_poster=do_poster,
                do_nfo=do_nfo,
                preview_nfo=preview_nfo,
            )
            if do_poster:
                print(f"封面图片：成功={art_st['ok']} 已有跳过={art_st['skip']} 失败={art_st['fail']}", flush=True)
            if do_nfo:
                print(f"NFO文件：将写/已写={nfo_st['ok']} 失败={nfo_st['fail']}（已有nfo不会覆盖）", flush=True)
            print(f"网页快照 tmdb.html：将写/已写={html_st['ok']} 失败={html_st['fail']}", flush=True)
            for s in samples:
                print(f"  结果: {_zh_art_status(s)}", flush=True)
            (TOOLS / "tmdb_format_rename_posters.json").write_text(
                json.dumps(
                    {
                        "art_stats": art_st,
                        "nfo_stats": nfo_st,
                        "html_stats": html_st,
                        "samples": samples,
                        "count": len(targets),
                        "wrapped_count": len(wrapped),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            print("没有需要处理封面/NFO/网页的文件夹。", flush=True)

    already_ok_n = sum(1 for s in skip if s.get("reason") == "already_ok")
    unmatched_n = len(skip) - already_ok_n
    print("-" * 60, flush=True)
    print("汇总（分类）：", flush=True)
    print(f"  已识别电影文件夹 = {len(leaves)}", flush=True)
    print(f"  待改名 = {len(plan)}", flush=True)
    print(f"  已正确命名（无需改） = {already_ok_n}", flush=True)
    if only_new:
        print(f"  跳过已刮削（未联网） = {len(skipped_done)}", flush=True)
    print(f"  未能匹配（已跳过） = {unmatched_n}", flush=True)
    print(f"  散落视频整理 = {len(wrapped)}", flush=True)
    print(f"  改名成功 = {len(ok)}", flush=True)
    print(f"  改名失败 = {len(fail)}", flush=True)
    print(
        f"（旧格式对照：电影文件夹={len(leaves)} 待改名={len(plan)} 跳过={len(skip)} "
        f"散落整理={len(wrapped)} 改名成功={len(ok)} 改名失败={len(fail)}）",
        flush=True,
    )

    print("-" * 60, flush=True)
    print("说明：", flush=True)
    print("  · 仅预览 = 只看计划，不改名、不下图、不写文件", flush=True)
    print("  · 正式更改 = 按计划改文件夹名，并下载缺的图片、写缺的nfo、生成tmdb.html", flush=True)
    print("  · 已经是正确名字 = 文件夹名已符合「中文名 (年份) [tmdbid=编号]」", flush=True)
    print("  · 找不到TMDB编号 = 名称太乱或是剧集/合集盘，未能自动匹配", flush=True)
    print("  · 多个候选且无年份 = 搜到多部同名/相近片，文件夹没写年份，已跳过不猜", flush=True)
    print("  · 已有跳过 = 封面等文件已存在，不会重复下载", flush=True)
    if not preview:
        save_history(root)
        if CHANGE_LOG:
            print(f"改动已记录，可以用「撤销上次改名」还原（{len(CHANGE_LOG)} 步）。", flush=True)
    return 0 if not fail else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", flush=True)
        raise SystemExit(130)
