#!/usr/bin/env python3
"""fnmedia-ext 主代理（FastAPI）。

链路：nginx(/v) -> 本代理(UDS) -> trim-media 官方后端(upstream UDS)
策略：
  - 搜索 GET /v/api/v1/search/list   透传本地结果 + 追加在线资源
  - 详情 GET /v/api/v1/item/<guid>   在线 guid 返回伪造详情
  - 播放 POST /play/info、GET /stream/list、POST /stream、POST /play/play
                                     在线 guid/media_guid 返回伪造链路
  - 流   GET /v/media/online/<mg>/*  HLS 代理（重写 m3u8 分片/密钥）
  - 海报 GET /v/online/poster        外站海报同源代理
  - 其余全部原样透传官方后端
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import (JSONResponse, RedirectResponse,
                               StreamingResponse)

import providers
import inject as inj
import config as cfgmod
from config import config
import hot as hotmod
from hot import hot_cache
import history as histmod
from history import history
from store import (store, make_guid, media_guid, video_guid, is_online_guid,
                   season_guid, episode_guid, ep_media_guid, ep_video_guid)

UPSTREAM_SOCK = os.environ.get("FNMEDIA_UPSTREAM_SOCK",
                               "/var/run/trim_media_upstream.sock")
ONLINE_PREFIX = "/v/media/online"
_search_cache: dict[str, tuple[float, list]] = {}
_search_inflight: dict[str, asyncio.Future] = {}
CACHE_TTL = 120
# 在线热门按内容拆成 4 个虚拟媒体库。guid 由固定串 md5 派生，保持稳定。
HOT_CATEGORIES = [
    ("movie",   "🌐在线电影", "Movie"),
    ("tv",      "🌐在线剧集", "TV"),
    ("anime",   "🌐在线动漫", "Mix"),
    ("variety", "🌐在线综艺", "Mix"),
]
HOT_LIB_GUIDS: dict[str, str] = {
    "movie":   "f4f27c311388d90f658670995a7fddb8",
    "tv":      "2a6937a71b652ea061a68456a72e69a8",
    "anime":   "5036a366ecfc047b4732b6bc2a39c3d8",
    "variety": "61d5d026ae7390a9f333b5d3a43ef5ff",
}
HOT_GUID_CAT = {g: c for c, g in HOT_LIB_GUIDS.items()}
# 兼容历史单库 guid：旧链接/旧缓存仍可打开，映射到剧集库。
HOT_LEGACY_GUID = "a110c1a0b1a04000900010000000f001"
HOT_DEFAULT_TITLE = "🌐在线热门"


def hot_cat_title(cat: str) -> str:
    for c, title, _ in HOT_CATEGORIES:
        if c == cat:
            return title
    return HOT_DEFAULT_TITLE


def hot_cat_category(cat: str) -> str:
    for c, _, mdbcat in HOT_CATEGORIES:
        if c == cat:
            return mdbcat
    return "Mix"


def is_hot_guid(guid: str) -> bool:
    return guid in HOT_GUID_CAT or guid == HOT_LEGACY_GUID


def b64e(u: str) -> str:
    return base64.urlsafe_b64encode(u.encode()).decode().rstrip("=")


def b64d(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.up = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=UPSTREAM_SOCK),
        timeout=httpx.Timeout(120), trust_env=False)
    app.state.ext = httpx.AsyncClient(
        timeout=httpx.Timeout(30), trust_env=False, follow_redirects=True)
    app.state.stop_hot = asyncio.Event()
    app.state.hot_task = asyncio.create_task(_hot_worker(app))
    try:
        yield
    finally:
        app.state.stop_hot.set()
        app.state.hot_task.cancel()
        try:
            await app.state.hot_task
        except (asyncio.CancelledError, Exception):
            pass
        await app.state.up.aclose()
        await app.state.ext.aclose()


async def _hot_worker(app: FastAPI):
    """热门库后台刷新：启动先读旧缓存，稍等启动流程，再按需刷新；之后按配置轮询。"""
    await asyncio.sleep(5)
    while not app.state.stop_hot.is_set():
        cfg = config.get()
        interval = max(1800, int(cfg.get("hot_refresh_interval", 6 * 3600)))
        status = hot_cache.status()
        stale_for = interval
        if status.get("updated_at"):
            stale_for = max(0, int(time.time() - status["updated_at"]))
        if cfg.get("hot_library_enabled", True) and (
                not status.get("updated_at") or stale_for >= interval):
            try:
                await asyncio.wait_for(hot_cache.refresh(store), timeout=90)
            except asyncio.TimeoutError:
                _log.warning("hot refresh timed out; keeping previous cache")
            except Exception as exc:
                _log.warning("hot refresh failed, keeping previous cache: %s", exc)
        try:
            await asyncio.wait_for(app.state.stop_hot.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass


app = FastAPI(lifespan=lifespan)

import logging
_log = logging.getLogger("fnmedia")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


@app.middleware("http")
async def _access(request: Request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if "/api/" in p or p.startswith(ONLINE_PREFIX) or p.startswith("/v/media"):
        _log.info("%s %s -> %s", request.method,
                  (p + ("?" + request.url.query if request.url.query else ""))[:120],
                  resp.status_code)
    return resp


def up_client(req: Request) -> httpx.AsyncClient:
    return req.app.state.up


def ext_client(req: Request) -> httpx.AsyncClient:
    return req.app.state.ext


async def forward_upstream(request: Request) -> Response:
    """原样透传官方后端（走 unix socket）。"""
    client = up_client(request)
    url = "http://localhost" + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("connection", "content-length", "host")}
    headers["Host"] = "localhost"
    try:
        r = await client.request(request.method, url, headers=headers, content=body)
    except Exception as exc:
        return JSONResponse({"code": -1, "msg": f"upstream {type(exc).__name__}", "data": None},
                            status_code=502)
    resp_headers = {k: v for k, v in r.headers.items()
                    if k.lower() not in ("content-length", "content-encoding",
                                         "transfer-encoding", "connection")}
    return Response(content=r.content, status_code=r.status_code,
                    headers=resp_headers,
                    media_type=r.headers.get("content-type"))


# ------------------------------------------------------------- 在线搜索

async def online_search(keyword: str, client: httpx.AsyncClient) -> list:
    now = time.time()
    if keyword in _search_cache and now - _search_cache[keyword][0] < CACHE_TTL:
        return _search_cache[keyword][1]
    fut = _search_inflight.get(keyword)
    if fut is None:
        fut = asyncio.get_event_loop().create_future()
        _search_inflight[keyword] = fut

        async def work():
            try:
                sources = config.enabled_sources()
                titles = await providers.search_all(keyword, sources)
                maxn = int(config.get().get("max_per_keyword", 12))
                src_name = {s["id"]: s.get("name", s["id"])
                            for s in config.all_sources()}
                # 跨源同名合并：同 规范化片名+年份 聚成一个条目，每个来源各成一条
                # 播放线路（供播放器「切换线路」）；过滤解说/预告/无直链。
                groups: dict[tuple, dict] = {}
                for t in titles:
                    if config.is_junk(t.title, t.remarks):
                        continue
                    playable = [e for line in t.sources.values() for e in line
                                if e.direct or e.resolve]
                    if not playable:
                        continue
                    key = (_norm_title(t.title), t.year)
                    g = groups.get(key)
                    if g is None:
                        g = {"rep": t, "members": [], "lines": {}}
                        groups[key] = g
                    g["members"].append(t)
                    for lname, eps in t.sources.items():
                        play = [e for e in eps if e.direct or e.resolve]
                        if not play:
                            continue
                        lkey = f"{t.provider}::{lname}"
                        # 同 key 保留直链更多（更优）者；否则集数更多者
                        cur = g["lines"].get(lkey)
                        nd_new = sum(1 for e in play if e.direct)
                        nd_cur = sum(1 for e in (cur or []) if e.get("direct"))
                        if cur is None or nd_new > nd_cur or \
                                (nd_new == nd_cur and len(play) > len(cur)):
                            g["lines"][lkey] = [e.__dict__ for e in play]
                # 排序：按可播直链总数（跨线路）降序
                ordered = sorted(groups.values(),
                                 key=lambda g: sum(len(v) for v in g["lines"].values()),
                                 reverse=True)[:maxn]
                briefs = []
                for g in ordered:
                    rep = g["rep"]
                    brief = inj.brief_of(rep)
                    brief["lines"] = g["lines"]
                    briefs.append(brief)
                briefs = store.remember_merged(briefs)
                items = [inj.search_item(b) for b in briefs]
                _search_cache[keyword] = (time.time(), items)
                fut.set_result(items)
            except Exception as exc:
                fut.set_result([])
            finally:
                _search_inflight.pop(keyword, None)

        asyncio.create_task(work())
    return await fut


def _norm_title(t: str) -> str:
    """跨源同名归并的片名规范化：去空白/标点/常见后缀，便于把不同站的同一部片聚到一起。"""
    t = (t or "").strip().lower()
    # 去掉空格与常见分隔标点
    t = re.sub(r"[\s·・:：,，.。!！?？'’\"\-—_()（）\[\]【】]+", "", t)
    # 去掉年份尾缀（年份单独作为合并键）
    t = re.sub(r"(19|20)\d{2}$", "", t)
    return t


_CJK_RE = re.compile(r"[一-鿿]")
# 拼音输入碎片：含音节分隔符 ' 或 ’（gan、gang't、gang'tie'xia）
_PINYIN_SEP_RE = re.compile(r"['’]")


def _should_online(q: str) -> bool:
    """只对"整词"触发在线搜索：
       - 含中文（输入法上屏后）→ 触发
       - 拼音碎片（含 ' 分隔符，或纯小写短音节 ≤6 字符）→ 不触发
       - 英文整词：含空格的多词，或长度 ≥7 的单词（Inception）→ 触发
    """
    if _CJK_RE.search(q):
        return True
    if _PINYIN_SEP_RE.search(q):
        return False
    if " " in q.strip() and len(q.strip()) >= 4:
        return True
    # 纯单词：拼音音节最长 6（zhuang），用 7 作为英文词门槛
    return len(q) >= 7


# ------------------------------------------------------------- 在线热门媒体库

def _hot_card(brief: dict, cat: str) -> dict:
    row = inj.search_item(brief)
    # 媒体库内不再重复显示【在线】前缀，让海报墙更接近原生媒体库体验
    title = (brief.get("title") or "").strip()
    remarks = (brief.get("remarks") or "").strip()
    row["title"] = title + (f" ·{remarks}" if remarks else "")
    row["ancestor_guid"] = HOT_LIB_GUIDS[cat]
    row["ancestor_name"] = hot_cat_title(cat)
    row["ancestor_category"] = hot_cat_category(cat)
    row["video_guid"] = video_guid(brief["guid"])
    return row


def _hot_cat_items(cat: str) -> list[dict]:
    return [b for b in hot_cache.items() if inj.hot_category(b) == cat]


def _hot_rows_all(body: dict, cat: str) -> list[dict]:
    items = _hot_cat_items(cat)
    # parent_guid 可能用于目录/分组；热门库只有一层，非根 parent 直接为空。
    parent = (body.get("parent_guid") or "").strip()
    if parent:
        return []
    rows = [_hot_card(b, cat) for b in items]
    tags = body.get("tags")
    if isinstance(tags, dict):
        wanted = set(tags.get("type") or [])
        media_wanted = wanted & {"Movie", "TV", "Movie,TV"}
        if "Movie,TV" in media_wanted:
            media_wanted.discard("Movie,TV")
            media_wanted.update({"Movie", "TV"})
        if media_wanted:
            rows = [r for r in rows if r.get("type") in media_wanted]
        watched = str(tags.get("watched") or "")
        if watched in {"0", "1"}:
            want_watched = watched == "1"
            filtered = []
            for r in rows:
                rec = history.get(r["guid"])
                is_watched = bool(rec and rec.get("duration") and
                                  int(rec.get("ts") or 0) >= int(rec.get("duration") or 0) * 0.95)
                if is_watched == want_watched:
                    filtered.append(r)
            rows = filtered
    elif isinstance(tags, list):
        wanted = set(tags) & {"Movie", "TV"}
        if wanted:
            rows = [r for r in rows if r.get("type") in wanted]

    if int(body.get("is_favorite") or 0):
        # 热门虚拟库暂不单独维护收藏；避免错误地展示全部内容。
        return []
    kw = str(body.get("keyword") or body.get("q") or body.get("title") or "").strip().lower()
    if kw:
        rows = [r for r in rows if kw in (r.get("title") or "").lower()]

    col = body.get("sort_column", "create_time")
    order = str(body.get("sort_type", "DESC")).upper()
    reverse = order.lower() != "asc"

    def rank(r: dict):
        g = r["guid"]
        b = hot_cache.get_guid(g) or {}
        h = b.get("hot") or {}
        return (h.get("rank") if reverse else -int(h.get("rank") or 0))

    if col == "sort_title":
        rows.sort(key=lambda r: (r.get("title") or ""), reverse=reverse)
    elif col in ("release_date", "air_date", "year"):
        rows.sort(key=lambda r: (r.get("release_date") or ""), reverse=reverse)
    elif col in ("vote_average", "score"):
        def score(r):
            try:
                return float(r.get("vote_average") or 0)
            except ValueError:
                return 0
        rows.sort(key=lambda r: (score(r), rank(r)), reverse=True)
    # create_time / 默认：保持 collect_hot 已计算好的热门顺序，不在这里二次排序。
    return rows


def _page_args(body: dict, total: int) -> tuple[int, int, int]:
    page_size = max(1, min(int(body.get("page_size") or 22), 100))
    page = int(body.get("page") or body.get("p") or body.get("page_index")
               or body.get("pn") or 1)
    page = max(1, page)
    offset = int(body.get("offset") if body.get("offset") is not None else
                 (page - 1) * page_size)
    offset = max(0, min(offset, total))
    return page_size, offset, min(offset + page_size, total)


async def _json_from_upstream(upstream: Response) -> dict | None:
    try:
        data = json.loads(upstream.body)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


@app.get("/v/api/v1/mediadb/list")
async def mediadb_list(request: Request):
    upstream = await forward_upstream(request)
    if not config.get().get("hot_library_enabled", True):
        return upstream
    payload = await _json_from_upstream(upstream)
    if payload is None:
        return upstream
    dbs = payload.get("data")
    hot_guids = set(HOT_LIB_GUIDS.values()) | {HOT_LEGACY_GUID}
    if not isinstance(dbs, list) or any(d.get("guid") in hot_guids for d in dbs):
        return upstream
    try:
        # 每个分类注入一个虚拟媒体库，按电影/剧集/动漫/综艺排序置于最前。
        cat_posters: dict[str, list[str]] = {}
        for cat, _t, _c in HOT_CATEGORIES:
            ps = []
            for b in _hot_cat_items(cat):
                p = inj._poster(b.get("poster") or "")
                if p:
                    ps.append(p)
                if len(ps) >= 8:
                    break
            cat_posters[cat] = ps
        inserts = []
        for cat, _t, _c in HOT_CATEGORIES:
            ps = cat_posters[cat]
            inserts.append({
                "guid": HOT_LIB_GUIDS[cat],
                "title": hot_cat_title(cat),
                "poster": ps[0] if ps else "",
                "posters": ps,
                "category": hot_cat_category(cat),
                "view_type": 0,
                "poster_type": 0,
                "refresh_disabled": True,
            })
        dbs[0:0] = inserts
        return JSONResponse(payload)
    except Exception as exc:
        _log.warning("inject hot mediadb failed: %s", exc)
        return upstream


@app.get("/v/api/v1/mediadb/sum")
async def mediadb_sum(request: Request):
    upstream = await forward_upstream(request)
    if not config.get().get("hot_library_enabled", True):
        return upstream
    payload = await _json_from_upstream(upstream)
    if not payload or not isinstance(payload.get("data"), dict):
        return upstream
    total_added = 0
    for cat, _t, _c in HOT_CATEGORIES:
        n = len(_hot_cat_items(cat))
        if n:
            payload["data"][HOT_LIB_GUIDS[cat]] = n
            total_added += n
    if total_added:
        payload["data"]["total"] = int(payload["data"].get("total", 0)) + total_added
    return JSONResponse(payload)


@app.post("/v/api/v1/item/list")
async def item_list(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        body = {}
    aguid = body.get("ancestor_guid")
    cat = HOT_GUID_CAT.get(aguid)
    # 兼容历史单库 guid：作为剧集库打开
    if aguid == HOT_LEGACY_GUID:
        cat = "tv"
    if cat is None:
        return await forward_upstream(request)
    mdb_name = hot_cat_title(cat)
    mdb_cat = hot_cat_category(cat)
    if not config.get().get("hot_library_enabled", True):
        return JSONResponse({"code": 0, "msg": "", "data": {
            "mdb_name": mdb_name, "mdb_category": mdb_cat, "top_dir": "",
            "dir": "", "total": 0, "list": [], "jump_list": None,
        }})
    # 即使缓存还在第一次刷新，也立即返回（不实时串联各资源站拖慢 UI）
    all_rows = _hot_rows_all(body, cat)
    page_size, start, end = _page_args(body, len(all_rows))
    return JSONResponse({"code": 0, "msg": "", "data": {
        "mdb_name": mdb_name,
        "mdb_category": mdb_cat,
        "top_dir": "",
        "dir": "",
        "total": len(all_rows),
        "list": all_rows[start:end],
        "jump_list": None,
    }})


@app.get("/v/api/v1/search/list")
async def search_list(request: Request):
    local = await forward_upstream(request)
    q = request.query_params.get("q", "").strip()
    try:
        import json
        data = json.loads(local.body)
    except Exception:
        return local
    if not isinstance(data, dict):
        return local
    items = data.get("data")
    if not isinstance(items, list):
        items = []
    online = []
    if q and _should_online(q):
        try:
            online = await asyncio.wait_for(online_search(q, ext_client(request)),
                                            timeout=6.0)
        except asyncio.TimeoutError:
            online = []
        except Exception:
            online = []
    # 在线片库更齐全，默认把在线结果排到前面；可在管理页关闭
    if config.get().get("online_first", True):
        data["data"] = online + items
    else:
        data["data"] = items + online
    return JSONResponse(data, status_code=200,
                        headers={"Content-Type": "application/json; charset=utf-8"})


# ------------------------------------------------------------- 详情 / 播放

@app.get("/v/api/v1/item/{guid}")
async def item_detail(guid: str, request: Request):
    # 在线热门虚拟媒体库根节点（进入媒体库时前端会请求 item/<库guid>）。
    # 必须在透传前拦截，否则官方后端返回 -6，页面提示「此媒体库不存在」。
    if is_hot_guid(guid):
        cat = HOT_GUID_CAT.get(guid) or ("tv" if guid == HOT_LEGACY_GUID else "movie")
        n = len(_hot_cat_items(cat))
        data = inj._base_item(
            guid=guid, type="MediaDB", title=hot_cat_title(cat),
            can_play=1, number_of_episodes=n, local_number_of_episodes=n,
            ancestor_guid="", ancestor_name="", ancestor_category="",
            poster="", logic_type=0)
        data["play_item_guid"] = ""
        data["play_error"] = ""
        return JSONResponse({"code": 0, "msg": "", "data": data})
    # 伪造的 Season / Episode 节点
    if store.get_episode(guid):
        ep = store.get_episode(guid)
        data = inj._base_item(
            guid=guid, type="Episode", title=ep["ep_title"],
            parent_guid=ep["season_guid"], parent_title="第 1 季",
            tv_title=ep["title"], season_number=1, episode_number=ep["index"] + 1,
            number_of_seasons=1, can_play=1,
            media_guid=ep_media_guid(guid), video_guid=ep_video_guid(guid))
        return JSONResponse({"code": 0, "msg": "", "data": data})
    from store import season_guid as _sg
    # 反查 season guid
    tv = None
    for g in store._titles:
        if _sg(g) == guid:
            tv = g
            break
    if tv:
        brief = store.get_title_by_guid(tv)
        data = inj._base_item(
            guid=guid, type="Season", title="第 1 季", parent_guid=tv,
            parent_title=brief["title"], tv_title=brief["title"],
            season_number=1, number_of_seasons=1,
            number_of_episodes=store.episode_count(tv), can_play=None,
            poster=inj._poster(brief.get("poster")))
        return JSONResponse({"code": 0, "msg": "", "data": data})
    brief = store.get_title_by_guid(guid)
    if not brief:
        return await forward_upstream(request)
    return JSONResponse({"code": 0, "msg": "", "data": inj.detail_item(brief)})


@app.get("/v/api/v1/season/list/{guid}")
async def season_list_route(guid: str, request: Request):
    brief = store.get_title_by_guid(guid)
    if not brief or not inj.is_tv(brief):
        return await forward_upstream(request)
    return JSONResponse({"code": 0, "msg": "", "data": inj.season_list(brief)})


@app.get("/v/api/v1/episode/list/{guid}")
async def episode_list_route(guid: str, request: Request):
    # guid 可能是伪造的 season guid，也可能是 tv guid
    tv = None
    from store import season_guid as _sg
    for g in store._titles:
        if _sg(g) == guid:
            tv = g
            break
    if not tv:
        brief = store.get_title_by_guid(guid)
        if brief and inj.is_tv(brief):
            tv = guid
    if not tv:
        return await forward_upstream(request)
    return JSONResponse({"code": 0, "msg": "",
                         "data": inj.episode_list(store.get_title_by_guid(tv))})


@app.get("/v/api/v1/stream/list/{guid}")
async def stream_list(guid: str, request: Request):
    ep = store.get_episode(guid)
    target = None
    if ep:
        sl = inj.episode_stream_list(ep)
        target = store.get_episode_target(guid)
    else:
        brief = store.get_title_by_guid(guid)
        if not brief:
            return await forward_upstream(request)
        sl = inj.stream_list(brief)
        if inj.is_tv(brief):
            # 标题播放键实际播放智能续播集，线路菜单也以该集为准
            egs = store.tv_episode_guids(guid)
            if egs:
                target = store.get_episode_target(egs[0])
        else:
            target = store.resolve_media_guid(store.media_guid(guid))
    quals = _qualities(target) if target else []
    if quals:
        sl["video_streams"][0]["resolution_type"] = quals[0]["resolution"]
        sl["video_streams"][0]["bps"] = quals[0]["bitrate"]
        sl["qualities"] = quals
        sl["cloud_storage_info"] = None
    return JSONResponse({"code": 0, "msg": "", "data": sl})


@app.post("/v/api/v1/play/info")
async def play_info(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    guid = body.get("item_guid", "")
    ep = store.get_episode(guid)
    rec = history.get(guid)
    if ep:
        info = inj.episode_play_info(ep)
        if rec:
            info["ts"] = rec.get("ts", 0)
        return JSONResponse({"code": 0, "msg": "", "data": info})
    brief = store.get_title_by_guid(guid)
    if not brief:
        return await forward_upstream(request)
    # 多集（TV）：标题播放键 → 智能续播（没看过=第1集；看过未看完=该集+进度；
    # 已看完=下一集）。返回该集的 play info，后续 stream/play 都按集走。
    if inj.is_tv(brief):
        ep_guids = store.tv_episode_guids(guid)
        if ep_guids:
            target_eg, resume_ts = history.resume_episode(ep_guids)
            if target_eg:
                ep = store.get_episode(target_eg)
                if ep:
                    info = inj.episode_play_info(ep)
                    info["ts"] = resume_ts
                    return JSONResponse({"code": 0, "msg": "", "data": info})
    info = inj.play_info(brief)
    if rec:
        info["ts"] = rec.get("ts", 0)  # 续播位置（秒），播放器据此恢复
    return JSONResponse({"code": 0, "msg": "", "data": info})


def _line_choices(target: dict) -> list[dict]:
    """该播放目标可切换的线路：按资源站去重（同一站直链优先于 share 页），
    当前播放线路排第一。"""
    raw = store.target_lines(target)
    cur = target.get("line")
    # 当前线路置顶，其余直链在前、可解析页在后
    raw.sort(key=lambda t: (t["line"] != cur, not t["m3u8"]
                            and not re.search(r"\.mp4(\?|$)", t["url"], re.I),
                            t.get("resolve", False)))
    out, seen_provider, seen_name = [], set(), set()
    for t in raw:
        provider = t["line"].split("::", 1)[0]
        name = _PROVIDER_CN.get(provider, provider)
        if provider in seen_provider:
            continue
        # 极端情况下不同 provider 映射到同名，加序号区分
        if name in seen_name:
            k = 2
            while f"{name}{k}" in seen_name:
                k += 1
            name = f"{name}{k}"
        seen_provider.add(provider)
        seen_name.add(name)
        out.append({**t, "label": name})
    # 保持当前线路第一
    out.sort(key=lambda t: t["line"] != cur)
    return out


def _line_bitrate(line: str, lines: list[str]) -> int:
    # 用 9000 万段的稳定整数编码线路，避免与真实码率（通常<3000万）冲突
    try:
        idx = lines.index(line)
    except ValueError:
        idx = 0
    return 90_000_000 + idx * 10001


def _line_of_bitrate(br, lines: list[str]) -> str | None:
    try:
        b = int(br or 0)
    except (TypeError, ValueError):
        return None
    if 90_000_000 <= b < 91_000_000:
        idx = (b - 90_000_000) // 10001
        if 0 <= idx < len(lines):
            return lines[idx]
    return None


def _qualities(target: dict) -> list[dict]:
    """把每条播放线路表达成播放器「清晰度」菜单里的一个可选项。"""
    lines = _line_choices(target)
    if len(lines) <= 1:
        return []
    out = []
    for i, t in enumerate(lines):
        out.append({
            "resolution": t.get("label") or _friendly_line(t["line"]),
            "bitrate": 90_000_000 + i * 10001,
            "progressive": True,
            "is_m3u8": False,  # 置 False 才不会被菜单项置灰
        })
    return out


@app.post("/v/api/v1/stream")
async def stream_post(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    mg = body.get("media_guid", "")
    target = store.resolve_media_guid(mg)
    if not target:
        return await forward_upstream(request)
    if target.get("kind") == "episode":
        sl = inj.episode_stream_list(target)
    else:
        brief = store.get_title_by_guid(target["online_guid"])
        sl = inj.stream_list(brief)
    f = sl["files"][0]
    v = sl["video_streams"][0]
    quals = _qualities(target)
    if quals:
        # 默认选中第一条线路，让播放器初始分辨率/码率与之一致
        v["resolution_type"] = quals[0]["resolution"]
        v["bps"] = quals[0]["bitrate"]
    return JSONResponse({"code": 0, "msg": "", "data": {
        "file_stream": f, "video_stream": v,
        "audio_streams": [], "subtitle_streams": [],
        "qualities": quals, "cloud_storage_info": None}})


@app.post("/v/api/v1/play/play")
async def play_play(request: Request):
    import json
    body = json.loads(await request.body() or b"{}")
    mg = body.get("media_guid", "")
    base_target = store.resolve_media_guid(mg)
    if not base_target:
        return await forward_upstream(request)
    # 播放器「清晰度/线路」菜单切换时会带我们编码的 bitrate，据此选线路
    choices = _line_choices(base_target)
    chosen_line = None
    if len(choices) > 1:
        chosen_line = _line_of_bitrate(body.get("bitrate"), [t["line"] for t in choices])
    if chosen_line and chosen_line != base_target.get("line"):
        if base_target.get("kind") == "episode" and base_target.get("ep_guid"):
            target = store.get_episode_target(base_target["ep_guid"], chosen_line) or base_target
        else:
            target = store.resolve_episode(
                base_target["online_guid"], chosen_line,
                int(base_target.get("index", 0) or 0))
            if target:
                target = dict(target, online_guid=base_target["online_guid"],
                              ep_guid="", kind="title")
            else:
                target = base_target
    else:
        target = base_target
    await _resolve_if_needed(target, request)
    # mg 已与"标题第一集"或"具体某集"绑定；按源类型返回 HLS 或直链代理
    src_url = target.get("url", "")
    if re.search(r"\.m3u8(\?|$)", src_url, re.I):
        q = urllib.parse.urlencode({"line": target["line"], "index": target["index"]})
        play_link = f"{ONLINE_PREFIX}/{mg}/preset.m3u8?{q}"
        wrapper = "HLS"
    else:
        q = urllib.parse.urlencode({"line": target["line"], "index": target["index"]})
        play_link = f"{ONLINE_PREFIX}/{mg}/direct?{q}"
        wrapper = "MP4"
    return JSONResponse({"code": 0, "msg": "", "data": {
        "audio_guid": "", "audio_index": -1, "hls_time": 6,
        "is_subtitle_external": 0, "media_guid": mg,
        "play_link": play_link, "subtitle_guid": "", "subtitle_index": -1,
        "subtitle_link": "", "video_guid": body.get("video_guid", vg_for(mg)),
        "video_index": 0, "wrapper": wrapper,
        "non_fatal_errno": None, "video_encoder": "",
        "not_supported_resolution": "", "supported_highest_resolution": ""}})


@app.get(f"{ONLINE_PREFIX}/{{mg}}/direct")
async def direct_proxy(mg: str, request: Request):
    """mp4 等非 HLS 直链代理：同源、带 Range 支持（播放器 seek）。"""
    target = store.resolve_media_guid(mg)
    if not target:
        return Response("not found", status_code=404)
    await _resolve_if_needed(target, request)
    src_url = target["url"]
    if re.search(r"\.m3u8(\?|$)", src_url, re.I):
        # 源是 HLS 却走到直链：重定向到 preset
        return RedirectResponse(f"{ONLINE_PREFIX}/{mg}/preset.m3u8", status_code=302)
    fwd = dict(target.get("headers") or {})
    if request.headers.get("range"):
        fwd["Range"] = request.headers["range"]
    try:
        req = ext_client(request).build_request("GET", src_url, headers=fwd)
        r = await ext_client(request).send(req, stream=True)
    except Exception as exc:
        return Response(f"direct fail {type(exc).__name__}", status_code=502)
    drop = {"content-length", "content-encoding", "transfer-encoding",
            "connection", "content-security-policy"}
    h = {k: v for k, v in r.headers.items() if k.lower() not in drop}
    h["Access-Control-Allow-Origin"] = "*"

    async def pump():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()

    return StreamingResponse(pump(), status_code=r.status_code, headers=h,
                             media_type=r.headers.get("content-type", "video/mp4"))


def vg_for(mg: str) -> str:
    t = store.resolve_media_guid(mg)
    if not t:
        return ""
    if t.get("kind") == "episode":
        return ep_video_guid(t["ep_guid"])
    return video_guid(t["online_guid"])


@app.post("/v/api/v1/person/list/{guid}")
async def person_list(guid: str, request: Request):
    if not store.get_title_by_guid(guid) and not store.get_episode(guid):
        return await forward_upstream(request)
    return JSONResponse({"code": 0, "msg": "", "data": {"total": 0, "list": []}})


@app.post("/v/api/v1/play/record")
async def play_record(request: Request):
    import json
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    ig = body.get("item_guid", "")
    if store.get_title_by_guid(ig) or store.get_episode(ig):
        # 记录在线播放进度到本地（继续观看），不上报官方库
        history.record(body)
        return JSONResponse({"code": 0, "msg": "", "data": True})
    return await forward_upstream(request)


@app.delete("/v/api/v1/play/record")
async def play_record_delete(request: Request):
    import json
    ids = []
    try:
        body = await request.body()
        if body:
            j = json.loads(body)
            if isinstance(j, dict):
                g = j.get("item_guid") or j.get("guid")
                ids = g if isinstance(g, list) else ([g] if g else [])
            elif isinstance(j, list):
                ids = [x.get("item_guid") if isinstance(x, dict) else x for x in j]
    except Exception:
        ids = []
    ids = [g for g in ids if g and (store.get_title_by_guid(g) or store.get_episode(g))]
    if ids:
        history.delete(ids)
        return JSONResponse({"code": 0, "msg": "", "data": True})
    return await forward_upstream(request)


@app.get("/v/api/v1/play/list")
async def play_list(request: Request):
    upstream = await forward_upstream(request)
    try:
        import json
        data = json.loads(upstream.body)
    except Exception:
        return upstream
    local_items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(local_items, list):
        local_items = []

    online_items = []
    for rec in history.all():
        ig = rec.get("item_guid", "")
        ep = store.get_episode(ig)
        brief = store.get_title_by_guid(ig)
        if ep:
            brief = store.get_title_by_guid(ep["online_guid"])
        if not brief:
            continue  # 缓存已淘汰的标题不再展示
        online_items.append(inj.history_item(brief, rec, ep))

    # 在线历史按最近播放排序，置顶；按 guid 与官方去重
    online_items.sort(key=lambda x: x.get("_updated_at", 0), reverse=True)
    seen = {x.get("guid") for x in local_items}
    merged = online_items + [x for x in local_items if x.get("guid") not in
                             {o["guid"] for o in online_items}]
    # 剥掉私有字段
    for o in online_items:
        o.pop("_online", None)
        o.pop("_updated_at", None)
        o.pop("_year", None)
    return JSONResponse({"code": 0, "msg": "", "data": merged},
                        headers={"Content-Type": "application/json; charset=utf-8"})


# media/p 的 RPC（quit/checkPlayLink 等）对在线 playLink 直接回 succ
@app.post("/v/api/v1/media/p")
async def media_p(request: Request):
    import json
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    link = body.get("playLink", "")
    if isinstance(link, str) and link.startswith(ONLINE_PREFIX):
        # 在线播放的清晰度/线路切换（resetQuality）等无官方媒体任务，直接成功
        return JSONResponse({"code": 0, "msg": "",
                             "data": {"reqid": body.get("reqid", ""), "result": "succ"}})
    return await forward_upstream(request)


# ------------------------------------------------------------- HLS 代理

def _pick_episode(mg: str, request: Request) -> dict | None:
    # 优先：mg 已绑定具体某集（TV 选集）或标题第一集
    target = store.resolve_media_guid(mg)
    if target:
        # query 可显式覆盖集序/线路（master 变体、STRM/调试用）
        if request.query_params.get("index") is not None or request.query_params.get("line"):
            try:
                idx = int(request.query_params.get("index", target.get("index", 0)))
            except ValueError:
                idx = target.get("index", 0)
            r = store.resolve_episode(
                target["online_guid"], request.query_params.get("line"), idx)
            if r:
                return dict(r, online_guid=target["online_guid"],
                            ep_guid=target.get("ep_guid", ""), kind=target.get("kind"))
        return target
    # 兼容：未在索引中的旧 mg，退回标题解析
    og = store.online_guid_by_media(mg)
    if not og:
        return None
    try:
        index = int(request.query_params.get("index", "0"))
    except ValueError:
        index = 0
    return store.resolve_episode(og, request.query_params.get("line"), index)


def _seg_url(mg: str, absolute: str, qs: str = "") -> str:
    suffix = f"&{qs}" if qs else ""
    return f"{ONLINE_PREFIX}/{mg}/seg?u={b64e(absolute)}{suffix}"


async def _resolve_if_needed(picked: dict, request: Request) -> dict:
    """播放页(/share/<id>)在播放时解析出真实媒体地址（原地更新 picked）。"""
    if picked and picked.get("resolve"):
        real = await providers.resolve_share(
            ext_client(request), picked["url"],
            (picked.get("headers") or {}).get("Referer", ""))
        if real:
            picked["url"] = real
            picked["resolve"] = False
    return picked


@app.get(f"{ONLINE_PREFIX}/{{mg}}/preset.m3u8")
async def hls_preset(mg: str, request: Request):
    picked = _pick_episode(mg, request)
    if not picked:
        return Response("not found", status_code=404)
    await _resolve_if_needed(picked, request)
    # play/play 选定线路后会带 line/index；此时只输出该线路，避免与清晰度菜单重复
    explicit_line = request.query_params.get("line")
    if explicit_line:
        targets = [{"line": picked["line"], "url": picked["url"],
                    "m3u8": bool(re.search(r"\.m3u8(\?|$)", picked["url"], re.I)),
                    "resolve": False}]
    else:
        targets = store.target_lines(picked)
    # 枚举该集/正片在各播放线路下的地址；可解析页先解析
    hls_targets = []
    for t in targets:
        if t.get("resolve"):
            real = await providers.resolve_share(
                ext_client(request), t["url"],
                (t.get("headers") or {}).get("Referer", ""))
            if real:
                t["url"] = real
                t["m3u8"] = bool(re.search(r"\.m3u8(\?|$)", real, re.I))
        if t["m3u8"]:
            hls_targets.append(t)
    if not hls_targets:
        return Response("no hls line", status_code=404)

    idx = int(picked.get("index", 0) or 0)

    def variant_uri(t: dict) -> str:
        q = {"index": idx, "line": t["line"]}
        # 透传可能存在的额外选集参数
        return "main.m3u8?" + urllib.parse.urlencode(q)

    if len(hls_targets) == 1:
        # 单线路：保持原有清晰度写法
        master = ("#EXTM3U\n#EXT-X-VERSION:3\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080\n"
                  f"{variant_uri(hls_targets[0])}\n")
    else:
        # 多线路：无 RESOLUTION 的变体，飞牛 HLS 播放器呈现为可切换「线路」
        out = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for i, t in enumerate(hls_targets):
            name = _friendly_line(t["line"])
            # NAME 供支持的播放器显示线路名；BANDWIDTH 差异化避免被合并
            out.append(f'#EXT-X-STREAM-INF:BANDWIDTH={1000000 + i * 100000},'
                       f'PROGRAM-ID=1,NAME="{name}"')
            out.append(variant_uri(t))
        master = "\n".join(out) + "\n"
    return Response(master, media_type="application/vnd.apple.mpegurl")


_PROVIDER_CN = {
    "baofeng": "暴风", "ffzy": "非凡", "lzi": "量子", "wujin": "无尽",
    "youku": "优酷", "guangsu": "光速", "ruyi": "如意", "jisu": "极速",
    "xinlang": "新狼", "dytt": "电影天堂", "s360": "360",
}
_GROUP_CN = {"m3u8": "主线路", "yun": "云播", "ff": "非凡", "uk": "优酷",
             "gs": "光速", "bf": "暴风", "lz": "量子"}


def _friendly_line(key: str) -> str:
    """把 'provider::组名' 转成好认的线路名，如 baofeng::bfzym3u8 -> 暴风。"""
    provider, _, group = key.partition("::")
    pcn = _PROVIDER_CN.get(provider, provider)
    if not group:
        return pcn
    if group in _GROUP_CN:
        return f"{pcn}·{_GROUP_CN[group]}"
    stem = group
    for suf in ("zym3u8", "m3u8", "yun"):
        if stem.endswith(suf):
            stem = stem[:-len(suf)]
            break
    stem = stem.upper() if stem.isascii() and stem else ""
    return f"{pcn}·{stem}" if stem else pcn


def _is_media_playlist(text: str) -> bool:
    return "#EXTINF" in text or "#EXT-X-TARGETDURATION" in text


def _first_variant(text: str, base: str) -> str | None:
    """从 master 播放列表里取第一个变体（子 m3u8）的绝对地址。"""
    uri = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        uri = s
        break
    if not uri:
        return None
    return urllib.parse.urljoin(base, uri)


@app.get(f"{ONLINE_PREFIX}/{{mg}}/main.m3u8")
async def hls_main(mg: str, request: Request):
    picked = _pick_episode(mg, request)
    if not picked:
        return Response("not found", status_code=404)
    await _resolve_if_needed(picked, request)
    src_url, headers = picked["url"], picked["headers"]
    # 跟随嵌套 master（如 index.m3u8 -> 3000k/hls/mixed.m3u8），最多 4 层
    cur, text = src_url, None
    for _ in range(4):
        try:
            r = await ext_client(request).get(cur, headers=headers)
            if r.status_code != 200:
                return Response(f"upstream {r.status_code}", status_code=502)
            text = r.text
        except Exception as exc:
            return Response(f"fetch fail {type(exc).__name__}", status_code=502)
        if _is_media_playlist(text):
            break
        nxt = _first_variant(text, cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    base_url = cur  # 分片相对地址以最终媒体列表为基准

    sel_qs = ""
    if picked.get("line"):
        sel_qs = f"line={urllib.parse.quote(picked['line'])}&index={picked['index']}"
    out_lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-KEY") or line.startswith("#EXT-X-MAP"):
            def repl(m):
                uri = m.group(1)
                absu = urllib.parse.urljoin(base_url, uri)
                return f'URI="{_seg_url(mg, absu, sel_qs)}"'
            raw = re.sub(r'URI="([^"]+)"', repl, raw)
            out_lines.append(raw)
        elif line.startswith("#") or not line:
            out_lines.append(raw)
        else:
            out_lines.append(_seg_url(mg, urllib.parse.urljoin(base_url, line), sel_qs))
    body = "\n".join(out_lines) + "\n"
    return Response(body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-cache",
                             "Access-Control-Allow-Origin": "*"})


@app.get(f"{ONLINE_PREFIX}/{{mg}}/seg")
async def hls_seg(mg: str, request: Request):
    u = request.query_params.get("u", "")
    try:
        src_url = b64d(u)
    except Exception:
        return Response("bad u", status_code=400)
    picked = _pick_episode(mg, request)
    headers = picked["headers"] if picked else {}
    # 透传 Range（播放器 seek）
    fwd = dict(headers)
    if "range" in request.headers:
        fwd["Range"] = request.headers["range"]
    try:
        r = await ext_client(request).get(src_url, headers=fwd)
    except Exception as exc:
        return Response(f"seg fail {type(exc).__name__}", status_code=502)
    drop = ("content-length", "content-encoding", "transfer-encoding",
            "connection", "content-security-policy")
    h = {k: v for k, v in r.headers.items() if k.lower() not in drop}
    h["Access-Control-Allow-Origin"] = "*"
    return Response(content=r.content, status_code=r.status_code, headers=h)


# ------------------------------------------------------------- 海报代理

def _decode_tok(tok: str) -> str:
    tok = tok.split(".", 1)[0]
    return b64d(tok)


def _poster_fallback() -> Response:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#1f6feb"/><stop offset="1" stop-color="#7c3aed"/>
  </linearGradient></defs>
  <rect width="300" height="450" fill="url(#g)"/>
  <circle cx="150" cy="180" r="58" fill="rgba(255,255,255,.18)"/>
  <polygon points="132,145 132,215 192,180" fill="#fff" opacity=".92"/>
  <text x="150" y="285" text-anchor="middle" fill="#fff" font-family="sans-serif"
    font-size="26" font-weight="700">在线影视</text>
  <text x="150" y="322" text-anchor="middle" fill="rgba(255,255,255,.72)"
    font-family="sans-serif" font-size="15">fnmedia-ext</text>
</svg>"""
    return Response(svg, media_type="image/svg+xml; charset=utf-8",
                    headers={"Cache-Control": "max-age=3600"})


async def _serve_online_poster(tok_path: str, request: Request) -> Response:
    # 兼容 /online/<token>.jpg 与官方图片前缀 /v/api/v1/sys/img/online/<token>.jpg
    tok_path = tok_path.lstrip("/")
    marker = "online/"
    if marker in tok_path:
        tok_path = tok_path.split(marker, 1)[1]
    try:
        u = _decode_tok(tok_path)
    except Exception:
        return _poster_fallback()
    if not u.startswith("http"):
        return _poster_fallback()
    try:
        r = await ext_client(request).get(u, headers={"User-Agent": "Mozilla/5.0"})
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status_code != 200 or not r.content or not ctype.startswith("image/"):
            return _poster_fallback()
    except Exception:
        return _poster_fallback()
    return Response(content=r.content, status_code=200,
                    media_type=ctype or "image/jpeg",
                    headers={"Cache-Control": "max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


@app.get("/online/{tok_path:path}")
async def online_poster_short(tok_path: str, request: Request):
    return await _serve_online_poster(tok_path, request)


@app.get("/v/api/v1/sys/img/online/{tok_path:path}")
async def online_poster(tok_path: str, request: Request):
    return await _serve_online_poster(tok_path, request)


@app.get("/_ext/healthz")
async def healthz():
    return {"ok": True, "service": "fnmedia-ext", "upstream": _up_status()}


def _up_status() -> str:
    return "unknown"


# ------------------------------------------------------------- 管理 API

@app.get("/v/api/ext/status")
async def ext_status(request: Request):
    return {"ok": True,
            "config": {k: v for k, v in config.get().items() if k != "admin_token"},
            "cached_titles": len(store._titles),
            "hot": hot_cache.status()}


@app.post("/v/api/ext/hot/refresh")
async def ext_hot_refresh(request: Request):
    data = await hot_cache.refresh(store, force=True)
    return {"code": 0, "msg": "", "data": hot_cache.status()}


@app.get("/v/api/ext/online")
async def ext_online(q: str = "", limit: int = 200):
    items = []
    for g, b in store._titles.items():
        items.append({
            "guid": g, "title": b.get("title"), "year": b.get("year"),
            "area": b.get("area"), "remarks": b.get("remarks"),
            "score": b.get("score"), "provider": b.get("online_id", "").split(":")[0],
            "episodes": sum(len(e) for e in b.get("lines", {}).values()),
            "poster": b.get("poster"),
        })
    items.sort(key=lambda x: x.get("year") or "", reverse=True)
    if q:
        items = [x for x in items if q.lower() in (x["title"] or "").lower()]
    return {"code": 0, "data": items[:limit]}


@app.post("/v/api/ext/config")
async def ext_set_config(request: Request):
    body = await request.json()
    allowed = {
        "max_per_keyword", "filter_junk", "junk_keywords", "online_first",
        "hot_library_enabled", "hot_library_title", "hot_limit",
        "hot_per_source", "hot_pages_per_source", "hot_refresh_interval",
    }
    patch = {k: v for k, v in body.items() if k in allowed}
    if "hot_library_title" in patch:
        patch["hot_library_title"] = str(patch["hot_library_title"]).strip()[:20] or HOT_DEFAULT_TITLE
    if "hot_limit" in patch:
        patch["hot_limit"] = max(20, min(120, int(patch["hot_limit"])))
    if "hot_per_source" in patch:
        patch["hot_per_source"] = max(10, min(40, int(patch["hot_per_source"])))
    if "hot_pages_per_source" in patch:
        patch["hot_pages_per_source"] = max(1, min(3, int(patch["hot_pages_per_source"])))
    if "hot_refresh_interval" in patch:
        patch["hot_refresh_interval"] = max(3 * 3600, min(24 * 3600,
                                                          int(patch["hot_refresh_interval"])))
    if "sources" in body and isinstance(body["sources"], list):
        cur = {s["id"]: s for s in config.all_sources()}
        for ch in body["sources"]:
            sid = ch.get("id")
            if sid in cur and isinstance(ch.get("enabled"), bool):
                cur[sid]["enabled"] = ch["enabled"]
        patch["sources"] = list(cur.values())
    config.save(patch)
    _search_cache.clear()
    return {"code": 0, "msg": "saved"}


@app.post("/v/api/ext/junk-keywords")
async def ext_junk_keywords(request: Request):
    body = await request.json()
    if body.get("reset"):
        kws = config.reset_junk_keywords()
    else:
        kws = config.set_junk_keywords(body.get("keywords", []))
    _search_cache.clear()
    return {"code": 0, "msg": "saved", "data": {"junk_keywords": kws}}


@app.post("/v/api/ext/source/add")
async def ext_source_add(request: Request):
    body = await request.json()
    ok, info = config.add_source(body.get("name", ""), body.get("api", ""),
                                 bool(body.get("enabled", True)))
    if not ok:
        return JSONResponse({"code": 400, "msg": info}, status_code=400)
    _search_cache.clear()
    return {"code": 0, "msg": "added", "data": {"id": info}}


@app.post("/v/api/ext/source/delete")
async def ext_source_delete(request: Request):
    body = await request.json()
    ok, info = config.remove_source(body.get("id", ""))
    if not ok:
        return JSONResponse({"code": 404, "msg": info}, status_code=404)
    _search_cache.clear()
    return {"code": 0, "msg": "deleted"}


@app.post("/v/api/ext/source/restore")
async def ext_source_restore(request: Request):
    config.restore_builtin()
    _search_cache.clear()
    return {"code": 0, "msg": "restored"}


@app.post("/v/api/ext/source/test")
async def ext_source_test(request: Request):
    body = await request.json()
    api = (body.get("api") or "").strip()
    sid = body.get("id")
    if not api and sid:
        src = next((s for s in config.all_sources() if s["id"] == sid), None)
        if not src:
            return JSONResponse({"code": 404, "msg": "源不存在"}, status_code=404)
        api = src["api"]
    keyword = (body.get("keyword") or "庆余年").strip()
    result = await providers.probe_source(api, keyword=keyword)
    return {"code": 0, "msg": "", "data": result}


@app.post("/v/api/ext/source/test-all")
async def ext_source_test_all(request: Request):
    """并发探测全部源，返回 [{id,name,...result}]。"""
    async def one(s):
        r = await providers.probe_source(s["api"])
        r.update(id=s["id"], name=s["name"], enabled=bool(s.get("enabled")),
                 builtin=bool(s.get("builtin")))
        return r
    results = await asyncio.gather(*[one(s) for s in config.all_sources()],
                                   return_exceptions=True)
    out = [r for r in results if isinstance(r, dict)]
    return {"code": 0, "msg": "", "data": out}


@app.get("/v/online-admin")
async def admin_page():
    from admin_ui import ADMIN_HTML
    return Response(ADMIN_HTML, media_type="text/html; charset=utf-8")


# ------------------------------------------------------------- 兜底透传

@app.api_route("/{full_path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    return await forward_upstream(request)
