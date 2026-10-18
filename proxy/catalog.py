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
        # source_id -> {type_id, type_name, type_pid, cat}
        self._classes: dict[str, list[dict]] = {}
        # cat -> [ {source_id, type_id, type_name, pagecount?} ] 候选（规范源在最前）
        self._routes: dict[str, list[dict]] = {}
        self._updated = 0.0
        self._refreshing = False
        self._page_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._totals: dict[str, int] = {}
        # 会话级去重：资源站分页会让个别 vod 出现在多页，保证每部片全局只出现一次
        self._seen_guids: dict[str, set[str]] = {c: set() for c, _, _ in CATALOG_CATEGORIES}
        # 去重后的顺序条目缓冲与已扫描到的 CMS 页号（顺序浏览时构建）
        self._dense: dict[str, list[dict]] = {c: [] for c, _, _ in CATALOG_CATEGORIES}
        self._scanned_page: dict[str, int] = {c: 0 for c, _, _ in CATALOG_CATEGORIES}
        self._dense_count: dict[str, int] = {c: 0 for c, _, _ in CATALOG_CATEGORIES}

    # -------------------------------------------------- 分类表 / 路由
    async def refresh_routes(self, sources: list[dict]) -> dict[str, Any]:
        if self._refreshing:
            return self.status()
        self._refreshing = True
        try:
            async with httpx.AsyncClient(
                    follow_redirects=True, trust_env=False,
                    verify=False) as client:
                jobs = await asyncio.gather(
                    *[self._load_source_classes(client, s) for s in sources],
                    return_exceptions=True)
            # 为每个大类挑“可深翻”的规范源：探测每个候选子类第一页，记录 pagecount，
            # 取页数最多者作为首选，其余作为回退（无需对每个源都探测所有类，抽样首页即可）。
            routes: dict[str, list[dict]] = {c: [] for c, _, _ in CATALOG_CATEGORIES}
            cand_by_cat: dict[str, list[tuple[str, dict]]] = {
                c: [] for c, _, _ in CATALOG_CATEGORIES}
            for sid, classes in self._classes.items():
                for cl in classes:
                    cat = cl.get("cat")
                    if cat:
                        cand_by_cat[cat].append((sid, cl))
            async with httpx.AsyncClient(
                    follow_redirects=True, trust_env=False,
                    verify=False) as client:
                probe_jobs = []
                seen_leaf: set[tuple[str, str, str]] = set()
                for cat, cands in cand_by_cat.items():
                    # 每个源每类只探测一个代表性子类（选该源该类的第一个叶子），
                    # 避免对全部子类发请求；pagecount 仅用于挑选规范源。
                    per_src: set[str] = set()
                    for sid, cl in cands:
                        if sid in per_src:
                            continue
                        per_src.add(sid)
                        sig = (cat, sid, str(cl["type_id"]))
                        if sig in seen_leaf:
                            continue
                        seen_leaf.add(sig)
                        probe_jobs.append(self._probe_route(client, cat, sid, cl))
                await asyncio.gather(*probe_jobs, return_exceptions=True)
            # 汇总到 routes 并按 pagecount 降序
            for cat in routes:
                seen_src: set[str] = set()
                merged = []
                good = sorted(self._routes.get(cat, []),
                              key=lambda r: -(r.get("pagecount") or 0))
                for r in good:
                    if r["source_id"] in seen_src:
                        continue
                    seen_src.add(r["source_id"])
                    merged.append(r)
                routes[cat] = merged
                self._totals[cat] = self.pagecount(cat) * PAGE_SIZE
            with self._lock:
                self._routes = routes
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

    async def _probe_route(self, client: httpx.AsyncClient, cat: str,
                           source_id: str, cl: dict):
        # 优先用叶子子类（有子类的父类不聚合，跳过含子分类的父类）
        source = self._source_by_id(source_id)
        if not source:
            return
        try:
            _lst, pc = await _fetch_category_page(client, source, cl["type_id"], 1)
        except Exception:
            return
        if not _lst:
            return
        with self._lock:
            self._routes.setdefault(cat, []).append({
                "source_id": source_id, "type_id": cl["type_id"],
                "type_name": cl["type_name"], "pagecount": int(pc or 0)})

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
                        "routes": len(self._routes.get(cat, [])),
                        "source": (self._routes.get(cat) or [{}])[0].get("source_id", ""),
                        "type_name": (self._routes.get(cat) or [{}])[0].get("type_name", ""),
                        "pagecount": min(int((self._routes.get(cat) or [{}])[0]
                                             .get("pagecount") or 0), MAX_PAGES),
                    } for cat, _, _ in CATALOG_CATEGORIES},
            }

    def available(self, cat: str) -> bool:
        with self._lock:
            return bool(self._routes.get(cat))

    def pagecount(self, cat: str) -> int:
        with self._lock:
            routes = self._routes.get(cat) or []
            if not routes:
                return 0
            return min(int(routes[0].get("pagecount") or 0), MAX_PAGES)

    def total(self, cat: str) -> int:
        # 用规范源页数估算 total；最后一页条数未知，前端以空页自然停止。
        return self.pagecount(cat) * PAGE_SIZE

    def routes_stale(self) -> bool:
        return (not self._updated) or (time.time() - self._updated) >= CLASS_REFRESH

    # -------------------------------------------------- 页级抓取
    async def _extend_dense(self, cat: str, need_count: int) -> int:
        """顺序翻页直到去重后的累计条目数达到 need_count（或翻到该源末页）。"""
        pc = self.pagecount(cat)
        seen = self._seen_guids.setdefault(cat, set())
        dense = self._dense.setdefault(cat, [])
        pg = self._scanned_page.get(cat, 0) + 1
        while len(dense) < need_count and pg <= pc:
            rows = await self.get_page(cat, pg)
            added = 0
            for b in rows:
                gid = b.get("guid")
                if gid in seen:
                    continue
                seen.add(gid); dense.append(b); added += 1
            self._scanned_page[cat] = pg
            # 整页没有新增（可能是末页/空页）也要前进，避免死循环
            pg += 1
            if not rows:
                break
        self._dense_count[cat] = len(dense)
        return len(dense)

    async def get_window(self, cat: str, offset: int, limit: int) -> list[dict]:
        """按“去重后的全局偏移”取一窗 brief，自动顺序跨 CMS 页，密集无重复。"""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        dense = self._dense.setdefault(cat, [])
        if len(dense) < offset + limit:
            await self._extend_dense(cat, offset + limit)
        return dense[offset:offset + limit]

    async def get_page(self, cat: str, page: int) -> list[dict]:
        """取某大类第 page 页（1 起）的可播放 brief 列表（已入库）。"""
        page = max(1, int(page))
        if page > MAX_PAGES:
            return []
        key = (cat, page)
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
                    briefs = await self._load_page(cat, page)
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

    async def _load_page(self, cat: str, page: int) -> list[dict]:
        from config import config as _cfg
        from store import store
        with self._lock:
            routes = list(self._routes.get(cat) or [])
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
                    if _cfg.is_junk(t.title, t.remarks):
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
                        "catalog_cat": cat,
                    }
                    briefs.append(brief)
                if briefs:
                    return store.remember_merged(briefs)
        return []


catalog = Catalog()
