#!/usr/bin/env python3
"""TVBox 配置解析：苹果 CMS 与远程 T4，不在本进程运行爬虫。

这三家（及大量换皮 App）共用同一套配置结构：一个 JSON，核心是 sites[]，
每个站点含 key/name/type/api。我们只导入其中能被本插件直接使用的：

- type 1（JSON）且 api 指向苹果CMS provide/vod 的站点；
- type 0（XML）的苹果CMS站点（接口地址在播放链路上可被识别）。
- type 4 的远程 T4 服务（含网盘，依赖由 T4 服务端承担）。

明确不支持、直接跳过：
- type 3 Spider（csp_*，依赖 Android jar/JS 爬虫运行时）；
- 本地网盘运行时、嗅探解析、直播/电台源；
- 加密且无法解出 JSON 的强混淆订阅。

支持的容器形态：单 JSON、base64 包裹、{"sites":[...]}、
{"urls":[{"url":...}]} 多订阅聚合（会递归拉取子订阅，限深度/数量）。
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import urljoin

import httpx

HTTP_TIMEOUT = 12.0
MAX_SUB = 12
MAX_DEPTH = 2

_JSON_HINT = re.compile(r"provide/vod", re.I)
# 苹果CMS XML 接口通常带 ac=list 或 provide/vod 且无 at/json；放宽以路径判定
_CMS_PATH = re.compile(r"/provide/vod", re.I)
_SKIP_NAME = re.compile(r"(直播|电台|广播|iptv|卫视|央视|体育直播)", re.I)
_PAN_HINT = re.compile(r"(quark|uc\.cn|aliyundrive|alipan|115|baidupcs|pan\.baidu|"
                       r"weiyun|xunlei|cloud189|ctfile|c189)", re.I)
_SPIDER_PREFIX = ("csp_", "csp_App", "py_", "java_", "sp_")


def _b64_try(text: str) -> str | None:
    t = text.strip()
    if not t or any(ch in t for ch in "\n{}[]\""):
        return None
    for candidate in (t, t + "=" * (-len(t) % 4)):
        try:
            raw = base64.b64decode(candidate, validate=True)
            s = raw.decode("utf-8", "ignore")
        except (binascii.Error, ValueError):
            continue
        if '"sites"' in s or '"urls"' in s or s.lstrip().startswith("{"):
            return s
    return None


def _strip_jsonc(text: str) -> str:
    """字符串感知地去掉 // 与 /* */ 注释（不破坏 URL 里的 //）。"""
    out: list[str] = []
    i, n = 0, len(text)
    instr = False
    esc = False
    quote = ""
    while i < n:
        c = text[i]
        if instr:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                instr = False
            i += 1
            continue
        if c in ('"', "'"):
            instr = True
            quote = c
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _json_loads_dict(text: str) -> dict | None:
    """json.loads，失败再按 JSONC（去 // 与 /* */ 注释）解析。"""
    if not text or not text.lstrip().startswith(("{", "[")):
        return None
    for candidate in (text, _strip_jsonc(text)):
        try:
            j = json.loads(candidate)
            return j if isinstance(j, dict) else None
        except Exception:
            continue
    return None


# 伪装成图片（.bmp/.jpg）的订阅：图片字节之后追加 base64 配置。
_B64_ALPHA = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_B64_WS = set(b"\r\n\t ")


def _image_embedded_text(data: bytes) -> str | None:
    """从图片伪装订阅中取出真正的配置文本（已 base64 解码，可能仍含注释）。

    常见结构：JPEG(以 FFD9 结束) / PNG(IEND) 之后，先一小段无意义杂凑（常以
    `**` 之类非 base64 字符收尾），随后是 base64(JSON)。
    """
    tails: list[bytes] = []
    for marker in (b"\xff\xd9", b"IEND\xaeB`\x82"):
        k = data.find(marker)
        if k >= 0:
            tails.append(data[k + len(marker):])
    for tail in tails:
        n = len(tail)
        # 找到第一个“非 base64 且非空白”字符（杂凑与正文的分隔符，如 **）。
        j = 0
        while j < n and (tail[j] in _B64_ALPHA or tail[j] in _B64_WS):
            j += 1
        if j < n:
            k = j
            while k < n and tail[k] not in _B64_ALPHA:
                k += 1
            payload = tail[k:]
        else:
            payload = tail
        s = payload.decode("ascii", "ignore").strip()
        if len(s) < 16:
            continue
        for cand in (s, s + "=" * (-len(s) % 4)):
            try:
                dec = base64.b64decode(cand, validate=False).decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                continue
            if '"sites"' in dec or '"urls"' in dec or dec.lstrip().startswith("{"):
                return dec
    return None


def parse_config_text(text: str) -> dict | None:
    """把文本解析成配置 dict；支持裸 JSON、JSONC(//注释) 与 base64 包裹。"""
    text = (text or "").strip()
    if not text:
        return None
    cfg = _json_loads_dict(text)
    if cfg is not None:
        return cfg
    decoded = _b64_try(text)
    if decoded:
        cfg = _json_loads_dict(decoded)
        if cfg is not None:
            return cfg
    return None


def classify_site(site: dict) -> tuple[str, dict | None]:
    """返回 (status, normalized)。status ∈ add_json/add_xml/skip。"""
    if not isinstance(site, dict):
        return "skip", None
    api = str(site.get("api") or "").strip()
    name = str(site.get("name") or site.get("key") or "").strip()
    t = str(site.get("type", 1))
    searchable = site.get("searchable", 1)
    if not api or not api.startswith(("http://", "https://")):
        return "skip", None
    if t == "4":
        import t4
        return "add_t4", {
            "name": name or site.get("key") or "T4",
            "api": api, "kind": "t4", "site_key": str(site.get("key") or ""),
            "ext": site.get("ext"),
            "enabled": t4.switch(site.get("enabled")),
            "searchable": t4.switch(searchable),
            "filterable": t4.switch(site.get("filterable"), False),
            "method": "POST" if str(site.get("method", "")).upper() == "POST" else "GET",
            "raw_type": t,
        }
    low = api.lower()
    if t == "3" or api.startswith(_SPIDER_PREFIX) or "csp_" in low:
        return "skip", None
    if _PAN_HINT.search(api) or _PAN_HINT.search(name):
        return "skip", None
    if _SKIP_NAME.search(name):
        return "skip", None
    if t not in ("0", "1"):
        return "skip", None
    if not _CMS_PATH.search(api):
        # 部分配置 api 只给到域名/采集根，尝试补全 provide/vod
        if re.search(r"/api\.php", low, re.I):
            api = api.split("?")[0].rstrip("/")
        else:
            return "skip", None
    is_json = bool(re.search(r"at/json|/json(\?|$)|format=json", api, re.I))
    if is_json:
        norm_api = api
    else:
        # 统一为苹果CMS JSON 接口：/provide/vod[/] → /provide/vod/at/json，
        # 保留原有 query（如采集站 token）。仅支持 XML 的站点会在探测时被剔除。
        m = re.match(r"^(.*?/provide/vod)/?(\?.*)?$", api, re.I)
        if m:
            norm_api = m.group(1).rstrip("/") + "/at/json" + (m.group(2) or "")
        else:
            norm_api = re.sub(r"/at/xml(\b|/)", "/at/json", api)
    return ("add_json" if is_json else "add_xml"), {
        "name": name or api.split("/")[2],
        "api": norm_api,
        "searchable": searchable,
        "raw_type": t,
    }


def extract_sites(config: dict) -> list[dict]:
    sites = config.get("sites")
    return [s for s in sites if isinstance(s, dict)] if isinstance(sites, list) else []


def sub_urls(config: dict, base: str = "") -> list[str]:
    urls = config.get("urls")
    out: list[str] = []
    if isinstance(urls, list):
        for u in urls:
            if isinstance(u, dict) and isinstance(u.get("url"), str):
                out.append(urljoin(base, u["url"]))
            elif isinstance(u, str):
                out.append(urljoin(base, u))
    return out


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    """拉取订阅正文。

    - 多个 UA 回退：部分源（如饭太硬）只对 App 端 UA(okhttp) 返回真正的伪装
      配置，对浏览器 UA 返回落地网页；
    - 支持图片伪装订阅（.bmp/.jpg，正文是 JPEG 字节后追加的 base64 配置）。
    """
    last_text = ""
    for ua in ("okhttp/3.15", "Mozilla/5.0"):
        try:
            r = await client.get(url, headers={"User-Agent": ua},
                                 timeout=HTTP_TIMEOUT, follow_redirects=True)
            r.raise_for_status()
        except Exception:
            continue
        data = r.content or b""
        # 图片伪装：JPEG/PNG 头
        if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
            embedded = _image_embedded_text(data)
            if embedded and parse_config_text(embedded):
                return embedded
        text = r.text
        if parse_config_text(text):
            return text
        last_text = text
    return last_text


async def load_remote(url: str) -> tuple[list[dict], int]:
    """拉取订阅（含多订阅聚合），返回 (sites, 子订阅数)。"""
    sites, sub, _spider, _base = await load_subscription(url=url)
    return sites, sub


# csp_XxxGuard 加固源：type=3 且 api 以 csp_ 开头、Guard 结尾
_GUARD_API = re.compile(r"^csp_.+Guard$", re.I)
# 风险/重资源源：即便能跑也默认关闭，避免拖慢聚合或在缺登录态时报错
_RISK_NAME = re.compile(
    r"(P2P|磁力|磁鏈|云盘|雲盤|网盘|網盤|夸克|誇克|阿里|alipan|aliyun|115|"
    r"百度|baiduwp|UC网盘|迅雷|thunder|我的|私人|收藏)", re.I)
_PAN_API = re.compile(r"(quark|uc\.cn|aliyun|alipan|aliyundrive|115|baidu|"
                      r"weiyun|xunlei|cloud189|pan\.)", re.I)


def is_guard_site(site: dict) -> bool:
    """type=3 且 api=csp_*Guard（本地 Android 加固运行时可执行）。"""
    if not isinstance(site, dict):
        return False
    api = str(site.get("api") or "").strip()
    return str(site.get("type", 1)) == "3" and bool(_GUARD_API.match(api))


def guard_is_risky(site: dict) -> bool:
    """风险加固源：P2P/磁力/云盘/私人，或需要 dict 型外部配置（云盘登录态等）。"""
    if not isinstance(site, dict):
        return False
    if isinstance(site.get("ext"), dict):
        return True
    name = str(site.get("name") or "")
    api = str(site.get("api") or "")
    return bool(_RISK_NAME.search(name) or _PAN_API.search(api))


def _spider_jar_url(spider: str, base: str = "") -> str:
    """解析 TVBox 全局 spider 声明里的 jar 地址。

    形如 "./jar/top98_1.jar;md5;xxxx" 或纯 URL，取分号前的 URL 并用订阅基址
    补全相对地址。
    """
    s = str(spider or "").strip().split(";", 1)[0].strip()
    if not s:
        return ""
    return urljoin(base, s)


def is_drpy_site(site: dict) -> bool:
    """type=3 且 api 指向远程 drpyS 引擎（http 的 drpy2.min.js）。

    站点规则在 ext（也是 http URL）。区别于本地内置 drpy://。
    """
    if not isinstance(site, dict) or str(site.get("type", 1)) != "3":
        return False
    api = str(site.get("api") or "")
    low = api.lower()
    return api.startswith(("http://", "https://")) and (
        ".js" in low or "dr_py" in low or "drpy" in low)


def classify_subscription(sites: list[dict], spider: str = "",
                          base: str = "") -> dict:
    """把一份订阅的原始 sites 一次分成五类。

    返回：
      cms    : 苹果CMS 归一化结果（name/api/transport...）
      guard  : csp_*Guard 加固源原始 site（交 guard 模块，redroid 跑固定 guard.jar）
      csp    : 普通 csp_ 爬虫（非 Guard，纯 dex），{key,name,cls,ext,jar,...}，
               由安卓运行时按各自 jar 加载
      drpy   : 远程 drpyS（type=3、api 为 http 的 drpy2 js），{name,api,ext}，
               由 Node sidecar 直接跑远程脚本
      skipped: [{name,reason}]
    """
    cms: list[dict] = []
    guard: list[dict] = []
    csp: list[dict] = []
    drpy: list[dict] = []
    t4_sites: list[dict] = []
    seen_t4: set[str] = set()
    skipped: list[dict] = []
    seen_cms: set[str] = set()
    seen_guard: set[str] = set()
    seen_csp: set[str] = set()
    seen_drpy: set[str] = set()
    count: dict[str, int] = {}
    skip_seen: set[str] = set()
    jar_url = _spider_jar_url(spider, base)

    def _skip(name: str, reason: str):
        name = (name or "未命名")[:24]
        tag = name + "|" + reason
        if tag not in skip_seen:
            skip_seen.add(tag)
            skipped.append({"name": name, "reason": reason})

    for s in sites:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or s.get("key") or s.get("api") or "").strip()
        api = str(s.get("api") or "").strip()
        t = str(s.get("type", 1))
        if t == "4":
            _status, normalized = classify_site(s)
            if not normalized:
                _skip(name, "T4 接口地址非 http(s)")
                continue
            import t4
            identity = t4.source_id(normalized["api"], normalized.get("ext"))
            if identity not in seen_t4:
                seen_t4.add(identity)
                t4_sites.append(normalized)
            continue

        # 1) 加固爬虫 csp_*Guard
        if is_guard_site(s):
            gk = api.lower()
            if gk in seen_guard:
                continue
            seen_guard.add(gk)
            guard.append(s)
            continue

        # 2) 远程 drpyS（api 是 http 的 drpy2 js，规则在 ext）
        if is_drpy_site(s):
            dk = (api + "|" + str(s.get("ext") or "")).lower()
            if dk in seen_drpy:
                continue
            seen_drpy.add(dk)
            drpy.append({
                "name": name or api.split("/")[-1] or "drpy",
                "engine": api,
                "ext": str(s.get("ext") or "").strip(),
                "searchable": s.get("searchable", 1),
                "quickSearch": s.get("quickSearch", 0),
            })
            continue

        # 3) type=3 其它爬虫细分
        if t == "3" or api.startswith(_SPIDER_PREFIX) or "csp_" in api.lower():
            low = api.lower()
            if api.startswith("csp_") and not api.endswith("Guard"):
                # 普通纯 dex 爬虫：类名即 csp_Xxx，由订阅全局 spider jar 承载。
                # 同一个 Spider 类可被多个站点以不同 ext 复用（如 csp_XBPQ），
                # 唯一性按 (类, ext) 判定；同类多配置追加序号，等同 guard 规则。
                ext = s.get("ext")
                ext_str = ext if isinstance(ext, str) else (
                    "" if ext is None else json.dumps(ext, ensure_ascii=False))
                sig = (api.lower(), ext_str.strip().lower())
                if sig in seen_csp:
                    continue
                seen_csp.add(sig)
                site_jar = str(s.get("_spider_jar") or "").strip() or jar_url
                base_key = api[4:]                    # 去 csp_ 前缀
                count[base_key] = count.get(base_key, 0) + 1
                uniq = base_key if count[base_key] == 1 else f"{base_key}{count[base_key]}"
                csp.append({
                    "key": uniq,
                    "spider": api,
                    "name": name or api,
                    "cls": "com.github.catvod.spider." + api[4:],
                    "ext": ext_str,
                    "jar": site_jar,
                    "searchable": s.get("searchable", 1),
                    "quickSearch": s.get("quickSearch", 0),
                })
                continue
            if api.startswith(("py_", "java_", "sp_")):
                _skip(name, "Python/Java 内置脚本源（不支持）")
                continue
            _skip(name, "其它爬虫源（无法识别运行时）")
            continue
        if _PAN_HINT.search(api) or _PAN_API.search(api) or _PAN_HINT.search(name):
            _skip(name, "网盘/云盘源")
            continue
        if _SKIP_NAME.search(name):
            _skip(name, "直播/电台源")
            continue
        if not api.startswith(("http://", "https://")):
            _skip(name, "接口地址非 http(s)")
            continue
        if t not in ("0", "1"):
            _skip(name, f"不支持的类型 type={t}")
            continue

        # 4) 苹果 CMS（复用归一化）
        status, norm = classify_site(s)
        if not norm:
            _skip(name or api, "非标准苹果CMS接口")
            continue
        key = norm["api"].rstrip("/").lower()
        if key in seen_cms:
            continue
        seen_cms.add(key)
        norm["transport"] = "json" if status == "add_json" else "xml"
        cms.append(norm)

    return {"cms": cms, "t4": t4_sites, "guard": guard, "csp": csp, "drpy": drpy,
            "skipped": skipped, "jar_url": jar_url}


async def load_subscription_full(url: str | None = None,
                                 text: str | None = None):
    """同 load_subscription，额外返回第 5 项 rules（顶层去广告规则列表）。"""
    seen: set[str] = set()
    all_sites: list[dict] = []
    spider = ""
    root_base = (url or "").strip() if url else ""
    rules: list = []

    async def walk(u: str | None, raw: str | None, depth: int):
        nonlocal spider
        if u is not None:
            if depth > MAX_DEPTH or u in seen or len(seen) >= MAX_SUB:
                return
            seen.add(u)
            try:
                import subfetch
                async with subfetch.async_sub_client() as client:
                    raw = await fetch_text(client, u)
            except Exception:
                return
        cfg = parse_config_text(raw or "")
        if not cfg:
            return
        sub_spider = cfg.get("spider") if isinstance(cfg.get("spider"), str) else ""
        if not spider and (depth == 0) and sub_spider:
            spider = sub_spider
        if depth == 0 and isinstance(cfg.get("rules"), list):
            rules.extend(cfg["rules"])
        sub_base = u or ""
        sub_jar = _spider_jar_url(sub_spider, sub_base)
        for st in extract_sites(cfg):
            if str(st.get("type")) == "4" and sub_base and st.get("api"):
                st["api"] = urljoin(sub_base, str(st.get("api") or ""))
            if sub_jar and isinstance(st, dict) and not st.get("_spider_jar"):
                st["_spider_jar"] = sub_jar
            all_sites.append(st)
        if depth < MAX_DEPTH:
            base = u or ""
            for sub in sub_urls(cfg, base)[:MAX_SUB]:
                await walk(sub, None, depth + 1)

    await walk(root_base or None, text, 0)
    return all_sites, max(0, len(seen) - 1), spider, root_base, rules


async def load_subscription(url: str | None = None,
                            text: str | None = None) -> tuple[list, int, str, str]:
    """拉取/解析订阅，支持裸 JSON、base64、多订阅聚合（向后兼容 4 元组）。"""
    sites, subs, spider, base, _rules = await load_subscription_full(url=url,
                                                                     text=text)
    return sites, subs, spider, base
