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

import json
import os
import re
import unicodedata
import shutil
import sys
import time
import urllib.parse
import urllib.request
import xml.sax.saxutils
from collections import Counter
from datetime import datetime
from pathlib import Path

def _app_dir() -> Path:
    """API key, caches, and logs live next to the exe or this script.

    A frozen build must not use the PyInstaller temp extract: that folder
    disappears when the process exits, and it is not where the user puts
    tmdb_api_key.txt.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_SCRIPT_DIR = _app_dir()
# Prefer a writable tools folder; fall back to the app directory (portable).
# Optional override: set TMDB_TOOLS_DIR only if you really want another folder.
if os.environ.get("TMDB_TOOLS_DIR"):
    TOOLS = Path(os.environ["TMDB_TOOLS_DIR"])
else:
    TOOLS = _SCRIPT_DIR
try:
    TOOLS.mkdir(parents=True, exist_ok=True)
except Exception:
    TOOLS = _SCRIPT_DIR

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
    return "8265bd1679663a7ea12ac168da84d2e8"


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





_TMDB_TV_EXISTS_CACHE: dict[str, bool] = {}
_TMDB_MOVIE_EXISTS_CACHE: dict[str, bool] = {}


def tmdb_tv_exists(tid: str) -> bool:
    """True if id resolves on TV endpoint."""
    tid = str(tid or "").strip()
    if not tid.isdigit():
        return False
    if tid in _TMDB_TV_EXISTS_CACHE:
        return _TMDB_TV_EXISTS_CACHE[tid]
    ok = False
    try:
        data = api_get(
            f"https://api.themoviedb.org/3/tv/{tid}?api_key={API_KEY}&language=zh-CN"
        )
        ok = isinstance(data, dict) and not data.get("_error") and data.get("id") is not None
    except Exception:
        ok = False
    _TMDB_TV_EXISTS_CACHE[tid] = ok
    return ok


def tmdb_movie_exists(tid: str) -> bool:
    tid = str(tid or "").strip()
    if not tid.isdigit():
        return False
    if tid in _TMDB_MOVIE_EXISTS_CACHE:
        return _TMDB_MOVIE_EXISTS_CACHE[tid]
    ok = False
    try:
        data = api_get(
            f"https://api.themoviedb.org/3/movie/{tid}?api_key={API_KEY}&language=zh-CN"
        )
        ok = isinstance(data, dict) and not data.get("_error") and data.get("id") is not None
    except Exception:
        ok = False
    _TMDB_MOVIE_EXISTS_CACHE[tid] = ok
    return ok


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


def api_get(url: str, retries: int = 3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Connection": "keep-alive"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            if i == retries - 1:
                return {"_error": str(e)}
            time.sleep(0.6 * (i + 1))
    return {"_error": "unknown"}


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json(path: Path, data: dict) -> None:
    try:
        TOOLS.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
    time.sleep(0.02)
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




def fetch_movie(tid: str, cache: dict, force: bool = False, kind: str | None = None) -> dict:
    if not force and tid in cache and not cache_needs_refetch(cache[tid]):
        c = cache[tid]
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
    append = (
        "&append_to_response=credits,external_ids,content_ratings,videos"
        if kind == "tv"
        else "&append_to_response=credits,release_dates,videos,external_ids"
    )
    zh = api_get(
        f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}"
        f"&language=zh-CN{append}"
    )
    time.sleep(0.02)
    tw = api_get(f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}&language=zh-TW")
    time.sleep(0.02)
    hk = api_get(f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}&language=zh-HK")
    time.sleep(0.02)
    en = api_get(
        f"https://api.themoviedb.org/3/{kind}/{tid}?api_key={API_KEY}"
        f"&language=en-US{append}"
    )
    time.sleep(0.02)
    if kind == "tv":
        zh = _normalize_tv_payload(zh)
        tw = _normalize_tv_payload(tw)
        hk = _normalize_tv_payload(hk)
        en = _normalize_tv_payload(en)
    base = zh if isinstance(zh, dict) and not zh.get("_error") else en
    if kind == "tv":
        base = _normalize_tv_payload(base)
    if not isinstance(base, dict) or base.get("_error"):
        cache[tid] = {"ok": False, "error": (base or {}).get("_error")}
        if isinstance(cache.get(tid), dict) and kind in ("movie", "tv"):
            cache[tid]["kind"] = kind
        if isinstance(cache.get(tid), dict) and kind in ("movie", "tv"):  # stamp_kind
            cache[tid]["kind"] = kind
        return cache[tid]

    picked, year, lang = pick_from_langs(zh, tw, hk, en, base)
    poster = ""
    backdrop = ""
    for d in (zh, en, tw, hk, base):
        if isinstance(d, dict):
            if not poster and d.get("poster_path"):
                poster = d.get("poster_path") or ""
            if not backdrop and d.get("backdrop_path"):
                backdrop = d.get("backdrop_path") or ""

    imgs = fetch_movie_images(tid, kind)
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
                time.sleep(0.02)
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

    cache[tid] = {
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
        "credits": writers,
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
        cache[tid]["ok"] = False
        cache[tid]["error"] = "empty_title"
    if isinstance(cache.get(tid), dict) and kind in ("movie", "tv"):
        cache[tid]["kind"] = kind
    if isinstance(cache.get(tid), dict) and kind in ("movie", "tv"):  # stamp_kind
        cache[tid]["kind"] = kind
    return cache[tid]


def extract_year(name: str):
    """Release year for TMDB search.

    Year *ranges* in the folder name (e.g. 1940–1958 in an anthology title) are
    NOT a release year — the Blu-ray may be 2025. Ignore them so search is not
    filtered to the wrong decade.
    """
    s = name or ""
    if re.search(r"(?:19|20)\d{2}\s*[–—\-]\s*(?:19|20)\d{2}", s):
        return None
    years = YEAR_RE.findall(s)
    if not years:
        return None
    return years[-1]


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
    s = name or ""
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

    return out[:8]



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
    time.sleep(0.03)
    if not isinstance(data, dict) or data.get("_error"):
        if year:
            params.pop("year", None)
            params.pop("first_air_date_year", None)
            data = api_get(f"https://api.themoviedb.org/3/search/{kind}?" + urllib.parse.urlencode(params))
            time.sleep(0.03)
    if not isinstance(data, dict) or data.get("_error"):
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
    return s



def search_tmdb(query: str, year: str | None, search_cache: dict, prefer_tid: str | None = None, prefer_kind: str | None = None):
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
    mode = "auto" if media_is_auto() else ("tv" if media_is_tv() else "movie")
    key = f"{q}|{year or ''}|{mode}|{prefer_tid or ''}|{prefer_kind or ''}"
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

    def _pick_unique(scored: list):
        """Return (tid, how, scored) after title→year→tmdbid cascade within one side."""
        if not scored:
            return None, "no_results", scored
        # title-level uniqueness
        if len(scored) == 1 or scored[0][0] >= scored[1][0] + 80:
            # still apply year if present and top doesn't match year — escalate
            if has_year:
                yh = _filter_year(scored)
                if len(yh) == 1:
                    return yh[0][1], "search_year", yh
                if len(yh) > 1:
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
                    if len(exact) == 1:
                        return exact[0][1], "search_year_exact", exact
                    if len({str(s[1]) for s in exact}) == 1 and exact:
                        return exact[0][1], "search_year_exact", exact
                    # Prefer abstain over wrong match: score gaps are NOT unique enough
                    # (The Nun / Ringu / Lamb same-year collisions). Unmatched tab is OK.
                    if len(exact) > 1:
                        return None, "ambiguous_no_year", exact
                    return None, "ambiguous_no_year", yh
                # top title unique but wrong year → try year filter empty → no year match
                if yh == [] and _year_of(scored[0][2]) and _year_of(scored[0][2]) != str(year):
                    # don't accept wrong-year unique title when year known
                    return None, "no_results", scored
            return scored[0][1], "search", scored
        # title collision within side → escalate year
        if has_year:
            yh = _filter_year(scored)
            if len(yh) == 1:
                return yh[0][1], "search_year", yh
            if len(yh) > 1:
                if prefer_tid:
                    hit = [s for s in yh if str(s[1]) == prefer_tid]
                    if len(hit) == 1:
                        return hit[0][1], "search_tmdbid", hit
                qn = _norm_match_title(q or "")
                exact = []
                for s in yh:
                    r0 = s[2] if isinstance(s[2], dict) else {}
                    for k in ("title", "name", "original_title", "original_name"):
                        t = _norm_match_title(r0.get(k) or "")
                        if t and t == qn:
                            exact.append(s)
                            break
                if len(exact) == 1:
                    return exact[0][1], "search_year_exact", exact
                if len({str(s[1]) for s in exact}) == 1 and exact:
                    return exact[0][1], "search_year_exact", exact
                # Prefer abstain over wrong match (no popularity/score tie-break)
                if len(exact) > 1:
                    return None, "ambiguous_no_year", exact
                return None, "ambiguous_no_year", yh
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
        movie_scored = _search_tmdb_one_kind("movie", q, None)
        tv_scored = _search_tmdb_one_kind("tv", q, None)
        if has_year:
            for s in _search_tmdb_one_kind("movie", q, year):
                if all(s[1] != x[1] for x in movie_scored):
                    movie_scored.append(s)
            for s in _search_tmdb_one_kind("tv", q, year):
                if all(s[1] != x[1] for x in tv_scored):
                    tv_scored.append(s)
            # year±1 exact-title rescue (filename year off-by-one, e.g. The Captain 2017 vs 2018)
            try:
                y0 = int(year)
                qn = _norm_match_title(q or "")
                for y2 in [y0 + d for d in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5)]:
                    if y2 < 1900 or y2 > 2099:
                        continue
                    for s in _search_tmdb_one_kind("movie", q, str(y2)):
                        r0 = s[2] if isinstance(s[2], dict) else {}
                        titles = [_norm_match_title(r0.get(k) or "") for k in ("title", "name", "original_title", "original_name")]
                        if qn and qn in titles and all(s[1] != x[1] for x in movie_scored):
                            # slight penalty vs exact year
                            movie_scored.append((s[0] - 30, s[1], s[2]))
                    for s in _search_tmdb_one_kind("tv", q, str(y2)):
                        r0 = s[2] if isinstance(s[2], dict) else {}
                        titles = [_norm_match_title(r0.get(k) or "") for k in ("title", "name", "original_title", "original_name")]
                        if qn and qn in titles and all(s[1] != x[1] for x in tv_scored):
                            tv_scored.append((s[0] - 30, s[1], s[2]))
            except Exception:
                pass
            movie_scored.sort(key=lambda x: -x[0])
            tv_scored.sort(key=lambda x: -x[0])

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
                            if not rd.isdigit() or abs(int(rd) - y0) > 5 or abs(int(rd) - y0) == 0:
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
                    sc, tid0, r0 = item[0], item[1], item[2]
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


def is_disc_structure(folder: Path) -> bool:
    """Blu-ray / DVD folder tree — treat outer folder as one leaf; never dig in."""
    try:
        names = {c.name.lower() for c in folder.iterdir() if c.is_dir() or c.is_file()}
    except Exception:
        return False
    if "bdmv" in names:
        return True
    if "video_ts" in names:
        return True
    # ISO sitting with companion folders still counts as disc-ish leaf if .iso present
    # (handled separately); here structure-only.
    return False


def has_video(folder: Path) -> bool:
    try:
        for c in folder.iterdir():
            if c.is_file() and c.suffix.lower() in VIDEO:
                return True
            if c.is_dir() and c.name.lower() in ("bdmv", "video_ts"):
                return True
    except Exception:
        return False
    return False


def list_videos_in_dir(folder: Path) -> list[Path]:
    out = []
    try:
        for c in folder.iterdir():
            if c.is_file() and c.suffix.lower() in VIDEO:
                out.append(c)
    except Exception:
        pass
    return out


def interesting_subdirs(folder: Path) -> list[Path]:
    """Non-prune, non-extras, non-BDMV subdirs."""
    out = []
    try:
        for x in folder.iterdir():
            if not x.is_dir() or should_prune(x.name):
                continue
            if x.name.lower() in DISC_DIR_NAMES:
                continue
            if is_extras_dir(x.name):
                continue
            out.append(x)
    except Exception:
        pass
    return out


def find_nfo_meta(folder: Path):
    try:
        nfos = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".nfo" and not p.name.startswith(".")]
    except Exception:
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


def wrap_loose_videos(root: Path, preview: bool = False) -> list[dict]:
    """Wrap loose videos only when TMDB can identify the group.

    - Multi-part / multi-disc of same title share one folder.
    - If TMDB search fails: do NOT create per-video folders.
    - Folder name keeps full title when path budget allows; otherwise shortens.
    - Uses long-path prefix on Windows; removes empty folder if move fails.
    """

    # skip_wrap_if_root_dedicated_leaf (never for drive roots)
    try:
        if (not is_drive_root(root)) and is_dedicated_movie_leaf(root):
            return []
    except Exception:
        pass
    wrapped = []
    search_cache = load_json(SEARCH_CACHE_PATH)
    stack = [root]
    seen = set()
    while stack:
        d = stack.pop()
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        try:
            kids = list(d.iterdir())
        except Exception:
            continue

        for child in kids:
            try:
                if not child.is_dir() or should_prune(child.name):
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
        # Dedicated movie leaf (incl. multi-ISO anthology): never treat as loose,
        # even when the scan root *is* that folder.
        if is_dedicated_movie_leaf(d):
            continue

        try:
            is_root = d.resolve() == root.resolve()
        except Exception:
            is_root = str(d) == str(root)
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
            year = extract_year(base) or (None if is_drive_root(d) else extract_year(d.name))
            queries = []
            sources = [base]
            if not is_drive_root(d):
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
            for qtry in uniq:
                tid, how = search_tmdb(qtry, year, search_cache, prefer_kind=folder_media_hint(d.name))
                query = qtry
                if tid:
                    break

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
                cache = load_json(CACHE_PATH)
                meta = fetch_movie(str(tid), cache, force=False)
                save_json(CACHE_PATH, cache)
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
                if preview:
                    rec["action"] = "would_wrap"
                    wrapped.append(rec)
                    print(f"  计划收入文件夹: {vf.name} -> {dest_dir.name}/", flush=True)
                else:
                    created = False
                    try:
                        if not dest_dir.exists():
                            Path(long_path(dest_dir)).mkdir(parents=True, exist_ok=True)
                            created = True
                        final_dest = dest_file
                        if Path(long_path(dest_file)).exists():
                            final_dest = dest_dir / f"{vf.stem}_moved{vf.suffix}"
                        shutil.move(long_path(vf), long_path(final_dest))
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
        if (not is_drive_root(root)) and is_dedicated_movie_leaf(root):
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
            kids = list(d.iterdir())
        except Exception as e:
            print(f"  skip unreadable: {d} ({e})", flush=True)
            continue
        for child in kids:
            try:
                if not child.is_dir() or should_prune(child.name):
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
                    subdirs = [x for x in child.iterdir() if x.is_dir() and not should_prune(x.name)]
                except Exception:
                    subdirs = []
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
        mode = "auto" if media_is_auto() else ("tv" if media_is_tv() else "movie")
        cent = search_cache.get("%s|%s|%s|" % (query, year or "", mode)) or {}
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


def resolve_leaf_id(leaf: dict, search_cache: dict):
    """Fill tmdb id via nfo or search. Mutates leaf."""
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
    year = extract_year(leaf["name"])
    if not year and meta.get("year"):
        y = str(meta.get("year"))[:4]
        if y.isdigit():
            year = y
    # Search names: leaf itself, cleaned leaf, and parent folder (multi-disc D1/D2
    # often have short codes like TJ_GOLDEN_ERA_ANTHOLOGY_D1 while the parent has
    # the real title).
    name_sources = [leaf.get("name") or ""]
    parent_name = Path(leaf.get("parent") or folder.parent).name
    if parent_name and parent_name.lower() not in {"movies", "movie", "tv", "tvs", "电视剧", "电影", "动漫", "动画"}:
        name_sources.append(parent_name)
        if not year:
            year = extract_year(parent_name)
    queries: list[str] = []
    for src in name_sources:
        queries.extend(extract_search_queries(src))
        cq = clean_query_title(src)
        if cq:
            queries.append(cq)
    if meta.get("title"):
        queries.append(clean_query_title(str(meta.get("title"))))
    if meta.get("originaltitle"):
        queries.append(clean_query_title(str(meta.get("originaltitle"))))
    # Prefer sequel-bearing queries first; never lock onto a base-title hit when
    # the source has a sequel mark (强奸男2 / 聚会的目的2). Then prefer longer.
    src_mark = sequel_mark(leaf.get("name") or "") or sequel_mark(parent_name or "")
    def _q_rank(q: str):
        junk = 1 if re.search(r"\d{4}\s*[–—\-]\s*\d{4}", q) or re.search(r"\b(?:19|20)\d{2}\b", q) else 0
        has_seq = 0 if (src_mark and query_has_sequel(q, src_mark)) or sequel_mark(q) else 1
        return (junk, has_seq, -len(q))
    # de-dupe
    seen_q = set()
    uniq = []
    for q in sorted(queries, key=_q_rank):
        q = (q or "").strip()
        if not q or q.lower() in seen_q:
            continue
        seen_q.add(q.lower())
        uniq.append(q)
    queries = uniq
    last_how = "empty_query"
    last_query = ""
    for query in queries:
        # If source is clearly a sequel, skip base-title-only queries (wrong-match risk)
        if src_mark and not query_has_sequel(query, src_mark) and not sequel_mark(query):
            continue
        _prefer_tid = str(leaf.get("prefer_tmdb") or leaf.get("tmdb") or "") or None
        _prefer_kind = folder_media_hint(leaf.get("name") or "")
        tid, how = search_tmdb(query, year, search_cache, prefer_tid=_prefer_tid, prefer_kind=_prefer_kind)
        last_how, last_query = how, query
        if tid:
            leaf["tmdb"] = tid
            leaf["id_from"] = how
            leaf["search_query"] = query
            leaf["search_year"] = year
            mode = "auto" if media_is_auto() else ("tv" if media_is_tv() else "movie")
            cent = search_cache.get(
                "%s|%s|%s|%s|%s" % (query, year or "", mode, _prefer_tid or "", _prefer_kind or "")
            )
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
            mode = "auto" if media_is_auto() else ("tv" if media_is_tv() else "movie")
            cent = search_cache.get("%s|%s|%s|" % (query, year or "", mode)) or {}
            leaf["ambiguous"] = True
            leaf["id_from"] = "ambiguous_movie_and_tv"
            leaf["reason"] = "ambiguous_movie_and_tv"
            leaf["search_query"] = query
            leaf["search_year"] = year
            leaf["movie_tmdb"] = cent.get("movie_tmdb")
            leaf["tv_tmdb"] = cent.get("tv_tmdb")
            leaf["note"] = "电影候选=%s %s / 剧集候选=%s %s" % (
                cent.get("movie_tmdb"), cent.get("movie_title") or "",
                cent.get("tv_tmdb"), cent.get("tv_title") or "",
            )
            return False
    leaf["id_from"] = last_how or "unresolved"
    leaf["search_query"] = last_query or (queries[0] if queries else "")
    leaf["search_year"] = year
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
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Connection": "keep-alive"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if not data or len(data) < min_size:
            return "too_small"
        tmp.write_bytes(data)
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        tmp.replace(dest)
        return "ok"
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return f"err:{e}"


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


def build_nfo_xml(meta: dict, tid: str) -> str:
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
        "<movie>",
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
    lines.append("</movie>")
    lines.append("")
    return "\n".join(lines)


def folder_has_nfo(folder: Path) -> bool:
    """True if folder already has any .nfo (Emby or prior scrape)."""
    try:
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() == ".nfo" and not p.name.startswith("."):
                return True
    except Exception:
        pass
    return False



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
        return f"ok:{dest.name}"
    except Exception as e:
        return f"err:{e}"


def write_movie_nfo(folder: Path, meta: dict, tid: str, preview: bool = False) -> str:
    """Write Emby nfo as {video_filename}.nfo only when folder has no existing nfo.

    Existing Emby/other nfo is never overwritten or deleted.
    """
    videos = list_videos_in_dir(folder)
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
            written.append(dest.name)
        else:
            for vf in videos:
                dest = folder / f"{vf.stem}.nfo"
                dest.write_text(xml, encoding="utf-8")
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
    """folders_and_ids: list of (folder_path_str, tmdb_id)."""
    art_stats = {"ok": 0, "skip": 0, "fail": 0}
    nfo_stats = {"ok": 0, "skip": 0, "fail": 0}
    html_stats = {"ok": 0, "skip": 0, "fail": 0}
    samples = []
    write_nfo_now = do_nfo and (not preview or preview_nfo)

    for folder_s, tid in folders_and_ids:
        folder = Path(folder_s)
        meta = cache.get(str(tid)) or {}
        # Per-item movie/tv — do not rely on global mode alone.
        _k = (meta.get("kind") or meta.get("media") or "").strip().lower()
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
                meta = cache.get(str(tid)) or meta
        if isinstance(meta, dict):
            meta["kind"] = _k
            cache[str(tid)] = meta
        art_summary = ""
        nfo_st = ""

        if do_poster:
            results = download_artwork(folder, meta, preview=preview)
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
                if any_ok:
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
            nfo_st = write_movie_nfo(folder, meta, str(tid), preview=preview and not preview_nfo)
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


def _rename_one(src: Path, dest: Path) -> None:
    """Rename folder; fall back to cmd ren on WinError 50 (some cloud mounts)."""
    try:
        src.rename(dest)
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


def apply_renames(plan):
    ok, fail = [], []
    ordered = sorted(
        plan,
        key=lambda r: 1 if (r.get("action") == "merge_into") else 0,
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


def normalize_root_arg(raw: str) -> tuple[Path, list[str]]:
    """Fix Windows bat quoting of drive roots (trailing backslash + quote).

    Bats should pass "%CD%." ; also recover mangled drive-root + --preview.
    """
    extra: list[str] = []
    s = (raw or "").strip().strip('"')
    for flag in ("--preview", "-n", "--no-poster", "--no-nfo", "--preview-nfo"):
        if flag in s:
            extra.append(flag)
            idx = s.find(flag)
            if idx >= 0:
                s = s[:idx]
            while s and s[-1] in ' "\\':
                s = s[:-1]
    s = s.strip().strip('"')
    while s.endswith("\\") or s.endswith("/"):
        # keep drive root as X:\  — strip only extras after we handle drive
        if len(s) == 3 and s[1] == ":" and s[2] in "\\/":
            break
        if len(s) <= 3 and len(s) >= 2 and s[1] == ":":
            break
        s = s[:-1]
    if len(s) == 2 and s[1] == ":":
        s = s + "\\"
    # "%CD%." -> E:\.
    if s.endswith("\\.") or s.endswith("/."):
        s = s[:-1]
    elif len(s) >= 3 and s[-1] == "." and s[-2] in "\\/":
        s = s[:-1]
    return Path(s), extra




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
    return bool(tmdb_tv_exists(tid) and tmdb_movie_exists(tid))


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
        m = re.search(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)", folder_name)
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
    if tid.isdigit():
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


def main():
    global MEDIA_KIND, API_KEY
    # GUI may set TMDB_API_KEY after this module was first imported.
    API_KEY = _load_api_key()
    args = [a for a in sys.argv[1:] if a]
    preview = "--preview" in args or "-n" in args
    no_poster = "--no-poster" in args
    no_nfo = "--no-nfo" in args
    preview_nfo = "--preview-nfo" in args
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
        if a not in ("--preview", "-n", "--no-poster", "--no-nfo", "--preview-nfo", "--tv", "--media=tv", "--media=movie", "--media=auto")
        and not a.startswith("--media=")
    ]
    if not args:
        root = Path(os.environ.get("TMDB_RENAME_ROOT") or os.getcwd())
    else:
        root, glued = normalize_root_arg(args[0])
        if "--preview" in glued or "-n" in glued:
            preview = True
        if "--no-poster" in glued:
            no_poster = True
        if "--no-nfo" in glued:
            no_nfo = True
        if "--preview-nfo" in glued:
            preview_nfo = True
    try:
        root = root.resolve()
    except Exception:
        pass

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
    print("=" * 60, flush=True)
    if not root.exists() or not root.is_dir():
        print("错误：扫描目录不存在，或不是文件夹", flush=True)
        return 2

    print("正在整理散落的视频（预览时只显示计划）...", flush=True)
    wrapped = wrap_loose_videos(root, preview=preview)
    print(f"  散落视频整理数 = {len(wrapped)}", flush=True)

    print("正在扫描剧集文件夹..." if media_is_tv() else "正在扫描电影文件夹...", flush=True)
    tagged, untagged = collect_candidate_dirs(root)
    print(f"  文件夹名已带 TMDB编号: {len(tagged)}", flush=True)
    print(f"  尚未带编号的视频文件夹: {len(untagged)}", flush=True)

    search_cache = load_json(SEARCH_CACHE_PATH)
    leaves = list(tagged)
    unresolved = []
    for u in untagged:
        if resolve_leaf_id(u, search_cache):
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
            f"    ? {u.get('name')[:70]} | {_zh_id_from(u.get('id_from') or 'unresolved')}"
            f" (搜={u.get('search_query')!r} 年={u.get('search_year')})",
            flush=True,
        )

    cache = load_json(CACHE_PATH)
    ids = sorted({L["tmdb"] for L in leaves if L.get("tmdb")})
    need = [i for i in ids if cache_needs_refetch(cache.get(i) or {})]
    print(f"不重复的电影编号 = {len(ids)}，需要联网获取资料 = {len(need)}", flush=True)
    for n, tid in enumerate(need, 1):
        fetch_movie(tid, cache, force=True)
        if n % 25 == 0:
            save_json(CACHE_PATH, cache)
            print(f"  fetched {n}/{len(need)}", flush=True)
    for tid in ids:
        meta = cache.get(tid) or {}
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
                cache[tid] = meta
        if cache_needs_refetch(meta):
            fetch_movie(tid, cache, force=True)
    save_json(CACHE_PATH, cache)

    # Multi-disc sibling folders (D1/D2…) sharing parent+tmdb → one folder
    leaves, multidisc_skips = collapse_multidisc_leaves(leaves, root)
    plan, skip = [], []
    skip.extend(multidisc_skips)
    multidisc_dest_by_primary = {}
    used = {}
    for L in leaves:
        tid = L["tmdb"]
        meta = cache.get(tid) or {}
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
        ) else "no_tmdb_id"
        skip.append({**u, "reason": reason})

    print("-" * 60, flush=True)
    label = "剧集文件夹" if media_is_tv() else "电影文件夹"
    print(f"{label} = {len(leaves)}，待改名 = {len(plan)}，跳过 = {len(skip)}，散落整理 = {len(wrapped)}", flush=True)
    print(f"编号来源统计 = { { _zh_id_from(k): v for k, v in Counter(L.get('id_from') for L in leaves).items() } }", flush=True)
    print(f"跳过原因统计 = { { _zh_reason(k): v for k, v in Counter(s.get('reason') for s in skip).items() } }", flush=True)
    print(f"改名标题语言统计 = {dict(Counter(p.get('lang') for p in plan))}", flush=True)
    for p in plan[:30]:
        print(f"  [{p.get('id_from')}/{p.get('lang')}]: {p['name'][:65]}", flush=True)
        print(f"    -> {p['target'][:80]}", flush=True)
    if len(plan) > 30:
        print(f"  … 另外还有 {len(plan) - 30} 条", flush=True)

    # Stamp media for GUI: classify each TMDB id (cached; title-match on movie/tv id collisions).
    def _quick_stamp(rows):
        out = []
        for x in rows:
            it = dict(x)
            out.append(stamp_item_media(it))
        return out

    try:
        leaves = _quick_stamp(leaves)
    except Exception:
        pass
    try:
        skip = _quick_stamp(skip)
    except Exception:
        pass
    try:
        plan = _quick_stamp(plan)
    except Exception:
        pass
    try:
        unresolved = _quick_stamp(unresolved)
    except Exception:
        pass


    log = {
        "root": str(root),
        "leaf_count": len(leaves),
        "plan_count": len(plan),
        "skip_count": len(skip),
        "unresolved_count": len(unresolved),
        "wrapped_count": len(wrapped),
        "leaves": leaves,
        "unresolved": unresolved,
        "wrapped": wrapped,
        "plan": plan,
        "skip": skip,
        "preview": preview,
        "media": MEDIA_KIND,
    }
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

        def add_target(folder, tid):
            if not folder or not tid:
                return
            key = str(folder).lower()
            if key in seen:
                return
            seen.add(key)
            targets.append((str(folder), str(tid)))

        if preview:
            for rec in plan:
                add_target(rec["path"], rec.get("tmdb"))
            for s in skip:
                if s.get("reason") == "already_ok":
                    add_target(s.get("dest") or s.get("path"), s.get("tmdb"))
        else:
            for rec in ok:
                add_target(rec.get("final") or rec.get("dest"), rec.get("tmdb"))
            for s in skip:
                if s.get("reason") == "already_ok":
                    add_target(s.get("dest") or s.get("path"), s.get("tmdb"))

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
    return 0 if not fail else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", flush=True)
        raise SystemExit(130)
