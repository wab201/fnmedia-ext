#!/usr/bin/env python3
"""海量分类片库（方案 C）：直接按资源站苹果 CMS 分类深翻。

与豆瓣“热榜”（charts.py，精选 ~160 部、点击才搜源）不同，这里面向“无限浏览”：
- 启动/定时拉取各资源站的分类表 ac=list，把所有子分类按名称归并为
  movie / tv / anime / variety 四个标准大类；
- 每个大类选一个“规范源”（可深翻、页数多、稳定），浏览时按页直连
  ac=detail&t=<子类id>&pg=N，每页 20 条且自带播放地址，点开即可播，无需再搜索；
- 页级短 TTL 内存缓存 + 单 flight；规范源某页失败自动回退到备选源；
- 条目 guid 绑定 (源, vod_id)，brief 入库后复用既有详情/选集/播放/续播链。
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx

import providers
from store import make_guid

PAGE_SIZE = 20
# 每个大类最多可浏览的页数（页数×20=条数）。250 页 ≈ 5000 部。
MAX_PAGES = 250
CLASS_REFRESH = 6 * 3600
PAGE_TTL = 10 * 60
HTTP_TIMEOUT = 12.0

# 大类 -> 展示名 / MediaDB category
CATALOG_CATEGORIES = [
    ("movie", "🎞海量电影", "Movie"),
    ("tv", "📺海量剧集", "TV"),
    ("anime", "🌸海量动漫", "Mix"),
    ("variety", "🎤海量综艺", "Mix"),
]

# 跨源聚合的“海量四库”使用的保留作用域 id；单源独立库作用域为其 source id。
AGG_SCOPE = "__agg__"
CATS = [c for c, _, _ in CATALOG_CATEGORIES]

# 名称关键字（匹配 type_name 子串）。顺序：动漫/综艺优先于“电影/剧”判定。
_ANIME_KW = ("动漫", "动画", "番剧", "卡通")
_VARIETY_KW = ("综艺", "真人秀", "脱口秀", "娱乐")
# 电影：电影/影片/各类型“XX片”且非剧非动漫；剧：电视/连续剧/美剧/国产剧…
_MOVIE_KW = ("电影", "影片", "纪录片", "纪录")
_TV_KW = ("电视", "连续剧", "剧集", "美剧", "英剧", "韩剧", "日剧", "泰剧",
          "国产剧", "港台剧", "海外剧", "番剧")
# 明确排除的脏类（成人/资讯/解说/预告/体育/短剧等）
_EXCLUDE_KW = ("伦理", "理论", "福利", "写真", "情色", "午夜", "解说", "预告",
               "花絮", "资讯", "新闻", "体育", "赛事", "短剧", "漫剧", "AI",
               "演员", "音乐", "MV", "萝莉")


def catalog_title(cat: str) -> str:
    for c, title, _ in CATALOG_CATEGORIES:
        if c == cat:
            return title
    return "🌐海量片库"


def catalog_mdb(cat: str) -> str:
    for c, _, mdb in CATALOG_CATEGORIES:
        if c == cat:
            return mdb
    return "Mix"


def classify(name: str) -> str | None:
    n = (name or "").strip()
    if not n or any(k in n for k in _EXCLUDE_KW):
        return None
    if any(k in n for k in _ANIME_KW):
        return "anime"
    if any(k in n for k in _VARIETY_KW):
        return "variety"
    if any(k in n for k in _TV_KW):
        return "tv"
    if any(k in n for k in _MOVIE_KW):
        return "movie"
    # “动作片/喜剧片/剧情片/科幻片…”这类以“片”结尾的归入电影
    if n.endswith("片") and len(n) <= 4:
        return "movie"
    return None


async def _fetch_class_list(client: httpx.AsyncClient, source: dict) -> list[dict]:
    base = source["api"].split("/at/json")[0].split("?")[0]
    r = await client.get(base, params={"ac": "list"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    cls = j.get("class") if isinstance(j, dict) else None
    return cls if isinstance(cls, list) else []


async def _fetch_category_page(client: httpx.AsyncClient, source: dict,
                               type_id, page: int) -> tuple[list, int]:
    """返回 (本页 vod 列表, 该分类总页数)。"""
    base = source["api"].split("/at/json")[0].split("?")[0]
    # 注意：分类深翻不能带 h 参数（部分源加 h 后返回空/pagecount=0）。
    r = await client.get(base, params={"ac": "detail", "t": type_id, "pg": page},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or j.get("code") not in (1, "1"):
        return [], 0
    return (j.get("list") or []), int(j.get("pagecount") or 0)


class Catalog:
    def __init__(self):
        self._lock = threading.RLock()
        # source_id -> [ {type_id, type_name, type_pid, cat} ]
        self._classes: dict[str, list[dict]] = {}
        # "scope:cat" -> [ {source_id, type_id, type_name, pagecount?} ]（首选在最前）
        # scope 为 AGG_SCOPE（跨源聚合）或某个 source_id（单源独立媒体库）。
        self._routes: dict[str, list[dict]] = {}
        self._updated = 0.0
        self._refreshing = False
        # key = (scope, cat, page)
        self._page_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        # "scope:cat" -> 去重缓冲（资源站分页会重复个别 vod）
        self._seen_guids: dict[str, set[str]] = {}
        self._dense: dict[str, list[dict]] = {}
        self._scanned_page: dict[str, int] = {}
        self._dense_count: dict[str, int] = {}

    @staticmethod
    def _scope_key(scope: str, cat: str) -> str:
        return f"{scope}:{cat}"

    def standalone_scopes(self) -> list[str]:
        """当前需要独立成库的源 id（来源配置中的 standalone 标记）。"""
        try:
            from config import config
            return [s["id"] for s in config.all_sources() if s.get("standalone")]
        except Exception:
            return []

    # -------------------------------------------------- 分类表 / 路由
    async def refresh_routes(self, sources: list[dict]) -> dict[str, Any]:
        if self._refreshing:
            return self.status()
        self._refreshing = True
        try:
            async with httpx.AsyncClient(
                    follow_redirects=True, trust_env=False,
                    verify=False) as client:
                await asyncio.gather(
                    *[self._load_source_classes(client, s) for s in sources],
                    return_exceptions=True)

            # 每个源每类只探测一个代表性叶子子类（第一页），用 pagecount 判断
            # 是否可深翻并挑选首选；分类表本身（全部叶子）已在 _classes 中。
            cand: list[tuple[str, str, dict]] = []  # (cat, sid, cl)
            for sid, classes in self._classes.items():
                per_cat: dict[str, dict] = {}
                for cl in classes:
                    cat = cl.get("cat")
                    if cat and cat not in per_cat:
                        per_cat[cat] = cl
                for cat, cl in per_cat.items():
                    cand.append((cat, sid, cl))
            probed: dict[tuple[str, str], dict] = {}

            async with httpx.AsyncClient(
                    follow_redirects=True, trust_env=False,
                    verify=False) as client:
                async def probe(cat, sid, cl):
                    source = self._source_by_id(sid)
                    if not source:
                        return
                    try:
                        _lst, pc = await _fetch_category_page(
                            client, source, cl["type_id"], 1)
                    except Exception:
                        return
                    if not _lst:
                        return
                    probed[(cat, sid)] = {
                        "source_id": sid, "type_id": cl["type_id"],
                        "type_name": cl["type_name"],
                        "pagecount": int(pc or 0)}
                await asyncio.gather(*[probe(c, s, cl) for c, s, cl in cand],
                                     return_exceptions=True)

            def routes_for_scope(scope: str) -> dict[str, list[dict]]:
                out: dict[str, list[dict]] = {}
                for cat in CATS:
                    if scope == AGG_SCOPE:
                        rs = [probed[(cat, sid)] for (c2, sid) in probed if c2 == cat]
                        rs.sort(key=lambda r: -(r.get("pagecount") or 0))
                        # 聚合：首选最深的源，并附带其余可深翻源做回退
                        out[cat] = rs
                    else:
                        hit = probed.get((cat, scope))
                        out[cat] = [hit] if hit else []
                return out

            new_routes: dict[str, list[dict]] = {}
            agg = routes_for_scope(AGG_SCOPE)
            for cat, rs in agg.items():
                new_routes[self._scope_key(AGG_SCOPE, cat)] = rs
            for sid in self.standalone_scopes():
                for cat, rs in routes_for_scope(sid).items():
                    if rs:
                        new_routes[self._scope_key(sid, cat)] = rs
            with self._lock:
                # 清理已不再需要的作用域缓冲
                valid_keys = set(new_routes.keys())
                self._routes = new_routes
                for d in (self._seen_guids, self._dense, self._scanned_page):
                    for k in list(d.keys()):
                        if k not in valid_keys:
                            d.pop(k, None)
                self._updated = time.time()
        finally:
            self._refreshing = False
        return self.status()

    async def _load_source_classes(self, client: httpx.AsyncClient, source: dict):
        try:
            cls = await _fetch_class_list(client, source)
        except Exception:
            cls = []
        # 只取叶子子类：苹果CMS 的父类（电影片/连续剧…）不聚合子类、只有零星条目，
        # 真正可深翻的是动作片/国产剧这类子分类。
        parent_ids = {str(x.get("type_id")) for x in cls}
        pids_with_child = {str(x.get("type_pid") or "0") for x in cls
                           if str(x.get("type_pid") or "0") not in ("", "0")}
        norm = []
        for x in cls:
            try:
                tid = x.get("type_id")
                pid = str(x.get("type_pid") or "0")
                name = str(x.get("type_name") or "")
                cat = classify(name)
                if tid is None or cat is None:
                    continue
                is_leaf = pid != "0" or str(tid) not in pids_with_child
                if not is_leaf:
                    continue
                norm.append({"type_id": tid, "type_name": name,
                             "type_pid": pid, "cat": cat})
            except Exception:
                continue
        with self._lock:
            self._classes[source["id"]] = norm

    def _source_by_id(self, sid: str) -> dict | None:
        try:
            from config import config
            return next((s for s in config.all_sources() if s["id"] == sid), None)
        except Exception:
            return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "updated_at": int(self._updated),
                "categories": {
                    cat: {
                        "routes": len(self._routes.get(
                            self._scope_key(AGG_SCOPE, cat), [])),
                        "source": (self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0].get("source_id", ""),
                        "type_name": (self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0].get("type_name", ""),
                        "pagecount": min(int((self._routes.get(
                            self._scope_key(AGG_SCOPE, cat)) or [{}])[0]
                            .get("pagecount") or 0), MAX_PAGES),
                    } for cat, _, _ in CATALOG_CATEGORIES},
            }

    def available(self, cat: str, scope: str = AGG_SCOPE) -> bool:
        with self._lock:
            return bool(self._routes.get(self._scope_key(scope, cat)))

    def pagecount(self, cat: str, scope: str = AGG_SCOPE) -> int:
        with self._lock:
            routes = self._routes.get(self._scope_key(scope, cat)) or []
            if not routes:
                return 0
            return min(int(routes[0].get("pagecount") or 0), MAX_PAGES)

    def total(self, cat: str, scope: str = AGG_SCOPE) -> int:
        # 用规范源页数估算 total；最后一页条数未知，前端以空页自然停止。
        return self.pagecount(cat, scope) * PAGE_SIZE

    def routes_stale(self) -> bool:
        return (not self._updated) or (time.time() - self._updated) >= CLASS_REFRESH

    # -------------------------------------------------- 页级抓取
    async def _extend_dense(self, scope: str, cat: str, need_count: int) -> int:
        """顺序翻页直到去重后的累计条目数达到 need_count（或翻到该源末页）。"""
        sk = self._scope_key(scope, cat)
        pc = self.pagecount(cat, scope)
        seen = self._seen_guids.setdefault(sk, set())
        dense = self._dense.setdefault(sk, [])
        pg = self._scanned_page.get(sk, 0) + 1
        while len(dense) < need_count and pg <= pc:
            rows = await self.get_page(cat, pg, scope)
            for b in rows:
                gid = b.get("guid")
                if gid in seen:
                    continue
                seen.add(gid); dense.append(b)
            self._scanned_page[sk] = pg
            # 整页没有新增（可能是末页/空页）也要前进，避免死循环
            pg += 1
            if not rows:
                break
        self._dense_count[sk] = len(dense)
        return len(dense)

    async def get_window(self, cat: str, offset: int, limit: int,
                         scope: str = AGG_SCOPE) -> list[dict]:
        """按“去重后的全局偏移”取一窗 brief，自动顺序跨 CMS 页，密集无重复。"""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        sk = self._scope_key(scope, cat)
        dense = self._dense.setdefault(sk, [])
        if len(dense) < offset + limit:
            await self._extend_dense(scope, cat, offset + limit)
        return dense[offset:offset + limit]

    async def get_page(self, cat: str, page: int, scope: str = AGG_SCOPE) -> list[dict]:
        """取某作用域某大类第 page 页（1 起）的可播放 brief 列表（已入库）。"""
        page = max(1, int(page))
        if page > MAX_PAGES:
            return []
        key = (scope, cat, page)
        with self._lock:
            hit = self._page_cache.get(key)
            if hit and time.time() - hit[0] < PAGE_TTL:
                return hit[1]
            fut = self._inflight.get(key)
        if fut is None:
            fut = asyncio.get_event_loop().create_future()
            with self._lock:
                self._inflight[key] = fut

            async def work():
                try:
                    briefs = await self._load_page(scope, cat, page)
                    with self._lock:
                        self._page_cache[key] = (time.time(), briefs)
                    fut.set_result(briefs)
                except Exception:
                    fut.set_result([])
                finally:
                    with self._lock:
                        self._inflight.pop(key, None)

            asyncio.create_task(work())
        return await fut

    async def _load_page(self, scope: str, cat: str, page: int) -> list[dict]:
        from config import config as _cfg
        from store import store
        with self._lock:
            routes = list(self._routes.get(self._scope_key(scope, cat)) or [])
        last_err = None
        async with httpx.AsyncClient(follow_redirects=True, trust_env=False,
                                     verify=False) as client:
            for route in routes[:4]:  # 最多回退 4 个源
                source = self._source_by_id(route["source_id"])
                if not source:
                    continue
                try:
                    vods, pc = await _fetch_category_page(
                        client, source, route["type_id"], page)
                except Exception as exc:
                    last_err = exc
                    continue
                if not vods:
                    # 超出该源页数
                    if pc and page > min(int(pc), MAX_PAGES):
                        continue
                    last_err = RuntimeError("empty page")
                    continue
                briefs = []
                for v in vods:
                    try:
                        t = providers._map_vod(v, source)
                    except Exception:
                        continue
                    if not t or not t.title:
                        continue
                    playable = [e for line in t.sources.values() for e in line
                                if e.direct or e.resolve]
                    if not playable:
                        continue
                    # 私有源放宽：单源作用域且该源开启 loose_filter 时，
                    # 跳过标题/备注噪音过滤；分类层面的红线仍在（classify 已剔除）。
                    loose = (scope != AGG_SCOPE
                             and bool(source.get("loose_filter")))
                    if not loose and _cfg.is_junk(t.title, t.remarks):
                        continue
                    brief = {
                        "guid": make_guid(t),
                        "online_id": t.online_id, "title": t.title,
                        "type_name": t.type_name, "year": t.year,
                        "poster": t.poster, "overview": t.overview,
                        "remarks": t.remarks, "score": t.score,
                        "actor": t.actor, "director": t.director, "area": t.area,
                        "lines": {name: [e.__dict__ for e in eps]
                                  for name, eps in t.sources.items()},
                        "catalog_cat": cat, "catalog_scope": scope,
                        "catalog_source": route["source_id"],
                    }
                    briefs.append(brief)
                if briefs:
                    return store.remember_merged(briefs)
        return []

    # --------------------------------------- 单源独立库：跨四类交错窗口
    async def standalone_window(self, scope: str, offset: int,
                                limit: int) -> tuple[list[dict], int]:
        """单源独立库：把该源的电影/剧/动漫/综艺按轮次交错成一条“最近更新”流。

        返回 (brief 列表, 估算 total)。四类各取 offset..offset+limit 的窗，
        再按排名轮次合并去重，最后按全局 offset 切片。
        """
        cats = [c for c in CATS if self.available(c, scope)]
        if not cats:
            return [], 0
        end = offset + limit
        results = await asyncio.gather(
            *[self.get_window(c, 0, end, scope) for c in cats],
            return_exceptions=True)
        per: list[list[dict]] = []
        total = 0
        for r in results:
            if isinstance(r, list):
                per.append(r)
        for c in cats:
            total += self.total(c, scope)
        merged: list[dict] = []
        seen: set[str] = set()
        maxlen = max((len(x) for x in per), default=0)
        # 轮次交错：第 i 轮依次取每类第 i 条，近似源站“最近更新”混合排序
        for i in range(maxlen):
            for arr in per:
                if i < len(arr):
                    b = arr[i]
                    g = b.get("guid")
                    if g and g not in seen:
                        seen.add(g)
                        merged.append(b)
        return merged[offset:offset + limit], total

    def standalone_available(self, scope: str) -> bool:
        return any(self.available(c, scope) for c in CATS)


catalog = Catalog()
