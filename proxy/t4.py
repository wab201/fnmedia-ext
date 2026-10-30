"""Remote T4 protocol adapter. Plugins and media decoding stay outside fnmedia."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

TIMEOUT = 30.0


class T4Error(Exception):
    """A diagnostic safe to return without disclosing upstream URLs or tokens."""

    def __init__(self, message: str, *, refresh_detail: bool = False):
        super().__init__(message)
        self.refresh_detail = refresh_detail


def switch(value, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def source_id(api: str, ext=None) -> str:
    identity = json.dumps([api.strip(), ext], sort_keys=True,
                          ensure_ascii=True, separators=(",", ":"))
    return "t4_" + hashlib.sha256(identity.encode()).hexdigest()[:20]


def is_folder(row: dict) -> bool:
    return str(row.get("vod_tag", "")).lower() == "folder" or \
        str(row.get("vod_id", "")).startswith(("action:", "push://"))


async def request(client: httpx.AsyncClient, source: dict,
                  params: dict, timeout: float = TIMEOUT) -> dict:
    query = dict(params)
    if source.get("ext") not in (None, ""):
        ext = source["ext"]
        query["ext"] = ext if isinstance(ext, str) else json.dumps(
            ext, ensure_ascii=False, separators=(",", ":"))
    # httpx.get(params=...) replaces the API query, including its authentication.
    url = httpx.URL(source["api"])
    headers = {"User-Agent": "okhttp/4.12.0"}
    try:
        if source.get("method", "GET").upper() == "POST":
            response = await client.post(url, json=query, headers=headers,
                                         timeout=timeout, follow_redirects=True)
        else:
            response = await client.get(url.copy_merge_params(query), headers=headers,
                                        timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPStatusError as exc:
        raise T4Error(f"T4 服务返回 HTTP {exc.response.status_code}",
                      refresh_detail=exc.response.status_code in (400, 404)) from None
    except (httpx.HTTPError, ValueError):
        raise T4Error("T4 服务连接失败或响应不是 JSON") from None
    if not isinstance(result, dict) or result.get("error") or result.get("detail"):
        raise T4Error("T4 服务返回错误响应")
    if result.get("code") not in (None, 0, "0", 1, "1", 200, "200"):
        raise T4Error("T4 服务报告请求失败")
    return result


def map_title(row: dict, source: dict, *, detail: bool = False,
              locator: dict | None = None):
    import providers

    if not isinstance(row, dict) or is_folder(row):
        return None
    vod_id = str(row.get("vod_id") or "")
    name = str(row.get("vod_name") or "").strip()
    if not vod_id or not name:
        return None
    original = dict(locator or {})
    original.update(kind="t4", source_id=source["id"],
                    source_name=source.get("name", ""),
                    vod_id=original.get("vod_id") or vod_id,
                    content_key=original.get("content_key") or
                    str(row.get("content_key") or vod_id))
    sources = {}
    if detail:
        flags = str(row.get("vod_play_from") or "").split("$$$")
        for i, group in enumerate(str(row.get("vod_play_url") or "").split("$$$")):
            flag = flags[i] if i < len(flags) and flags[i] else f"线路{i + 1}"
            # Display labels must be unique; the actual flag is sent unchanged.
            label = flag if flag not in sources else f"{flag} ({i + 1})"
            episodes = []
            for part in group.split("#"):
                title, sep, play_id = part.partition("$")
                if not sep or not play_id.strip():
                    continue
                title = title.strip() or f"第{len(episodes) + 1}集"
                if play_id.startswith(("action:", "push://")):
                    continue
                episodes.append(providers.Episode(
                    title=title, url=play_id.strip(), direct=False, t4=True,
                    play_from=flag, episode_key=episode_key(title)))
            if episodes:
                sources[label] = episodes
    original["loaded_at"] = time.time() if detail else 0
    return providers.OnlineTitle(
        provider=source["id"], vod_id=original["content_key"], title=name,
        type_name=str(row.get("type_name") or ""),
        year=str(row.get("vod_year") or ""), area=str(row.get("vod_area") or ""),
        updated=str(row.get("vod_time") or ""), remarks=str(row.get("vod_remarks") or ""),
        score=str(row.get("vod_score") or ""), poster=str(row.get("vod_pic") or ""),
        overview=str(row.get("vod_content") or row.get("vod_blurb") or ""),
        actor=str(row.get("vod_actor") or ""), director=str(row.get("vod_director") or ""),
        sources=sources, locator=original)


def episode_key(title: str) -> str:
    # Equal explicit episode labels may share progress; array positions may not.
    normalized = re.sub(r"\s+", "", title).casefold()
    match = re.fullmatch(r"(?:第)?0*(\d+)(?:集|话|期)?", normalized)
    if match:
        return "episode:" + str(int(match[1]))
    match = re.fullmatch(r"s0*(\d+)e0*(\d+)", normalized)
    if match:
        return f"season:{int(match[1])}:episode:{int(match[2])}"
    return "label:" + normalized


async def search(client: httpx.AsyncClient, source: dict, keyword: str):
    if not switch(source.get("searchable")):
        return []
    result = await request(client, source, {"wd": keyword, "quick": "false", "pg": 1})
    rows = result.get("list")
    if not isinstance(rows, list):
        raise T4Error("T4 搜索缺少 list 列表")
    return [title for row in rows if (title := map_title(row, source)) is not None]


async def detail(client: httpx.AsyncClient, source: dict, locator: dict,
                 fallback: dict | None = None):
    vod_id = locator["vod_id"]
    result = await request(client, source, {"ac": "detail", "ids": vod_id})
    rows = result.get("list")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise T4Error("T4 详情未返回唯一影片，请重新搜索")
    row = dict(rows[0])
    for field, brief_key in (("vod_name", "title"), ("vod_pic", "poster"),
                             ("type_name", "type_name"), ("vod_year", "year")):
        if not row.get(field) and fallback:
            row[field] = fallback.get(brief_key, "")
    if is_folder(row):
        raise T4Error("该条目是目录，请从支持目录的入口展开")
    # Sidecar may rewrap the ID. Standard, stable IDs must still match exactly.
    returned_id = str(row.get("vod_id") or "")
    stable_key = str(row.get("content_key") or "")
    if not str(vod_id).startswith("t4_") and returned_id != str(vod_id):
        raise T4Error("T4 详情影片标识不一致")
    if stable_key and stable_key != locator["content_key"]:
        raise T4Error("T4 详情稳定标识不一致")
    title = map_title(row, source, detail=True, locator=locator)
    if not title or not title.sources:
        raise T4Error("T4 详情没有可播放的分集")
    return title


@dataclass
class Playback:
    url: str
    headers: dict
    wrapper: str
    parse: int = 0
    jx: int = 0
    subtitles: list = field(default_factory=list)

    @property
    def needs_proxy(self) -> bool:
        if self.headers:
            return True
        host = urlsplit(self.url).hostname or ""
        if host == "localhost" or host.endswith((".local", ".lan")):
            return True
        try:
            return not ipaddress.ip_address(host).is_global
        except ValueError:
            return False


def playback(result: dict, api: str) -> Playback:
    try:
        parse, jx = int(result.get("parse") or 0), int(result.get("jx") or 0)
    except (ValueError, TypeError):
        raise T4Error("T4 播放解析状态格式错误") from None
    if parse or jx:
        raise T4Error("T4 仍要求客户端嗅探或解析，请在 T4 服务端完成取链")
    url = result.get("url")
    if not isinstance(url, str) or not url.strip():
        raise T4Error("T4 未返回单一媒体地址")
    if url.lstrip().startswith(("<", "#")):
        raise T4Error("T4 返回内联清单，当前适配尚不支持")
    url = urljoin(api, url.strip())
    if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
        raise T4Error("T4 返回的媒体地址不是 HTTP(S)")
    headers = result.get("header") or result.get("headers") or {}
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except ValueError:
            raise T4Error("T4 媒体请求头不是有效 JSON") from None
    if not isinstance(headers, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or
        "\r" in k + v or "\n" in k + v for k, v in headers.items()
    ):
        raise T4Error("T4 媒体请求头格式错误")
    kind = str(result.get("type") or result.get("contentType") or "").lower()
    path = urlsplit(url).path.lower()
    if path.endswith(".m3u8") or "mpegurl" in kind or kind in ("hls", "m3u8"):
        wrapper = "HLS"
    elif path.endswith(".mpd") or "dash" in kind:
        wrapper = "DASH"
    elif path.endswith(".mkv") or "matroska" in kind or kind == "mkv":
        wrapper = "MKV"
    elif path.endswith(".mp4") or kind in ("mp4", "video/mp4"):
        wrapper = "MP4"
    else:
        wrapper = ""
    return Playback(url=url, headers=headers, wrapper=wrapper,
                    subtitles=result.get("subs") or result.get("subtitles") or [])


async def player(client: httpx.AsyncClient, source: dict,
                 play_id: str, flag: str) -> Playback:
    result = await request(client, source, {"play": play_id, "flag": flag})
    return playback(result, source["api"])


async def probe(client: httpx.AsyncClient, source: dict, timeout: float = 8.0):
    start = time.monotonic()
    try:
        result = await request(client, source, {}, timeout=timeout)
        ok = isinstance(result.get("list"), list) or isinstance(result.get("class"), list)
        return {"ok": ok, "status": 200, "elapsed_ms": int(
            (time.monotonic() - start) * 1000), "count": len(result.get("list") or []),
            "sample": "", "error": "" if ok else "T4 首页缺少 class/list"}
    except T4Error as exc:
        return {"ok": False, "status": 0, "elapsed_ms": int(
            (time.monotonic() - start) * 1000), "count": 0, "sample": "", "error": str(exc)}
