#!/usr/bin/env python3
"""TVBox / 影视仓 / OK影视 配置解析（只取标准苹果CMS 源）。

这三家（及大量换皮 App）共用同一套配置结构：一个 JSON，核心是 sites[]，
每个站点含 key/name/type/api。我们只导入其中能被本插件直接使用的：

- type 1（JSON）且 api 指向苹果CMS provide/vod 的站点；
- type 0（XML）的苹果CMS站点（接口地址在播放链路上可被识别）。

明确不支持、直接跳过：
- type 3 Spider（csp_*，依赖 Android jar/JS 爬虫运行时）；
- 网盘（夸克/阿里/115/百度/UC 等）、嗅探解析、直播/电台源；
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


def parse_config_text(text: str) -> dict | None:
    """把文本解析成配置 dict；支持裸 JSON 与 base64 包裹。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        j = json.loads(text)
        return j if isinstance(j, dict) else None
    except Exception:
        pass
    decoded = _b64_try(text)
    if decoded:
        try:
            j = json.loads(decoded)
            return j if isinstance(j, dict) else None
        except Exception:
            return None
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
    r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"},
                         timeout=HTTP_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.text


async def load_remote(url: str) -> tuple[list[dict], int]:
    """拉取订阅（含多订阅聚合），返回 (sites, 子订阅数)。"""
    seen: set[str] = set()
    all_sites: list[dict] = []

    async def walk(u: str, depth: int):
        if depth > MAX_DEPTH or u in seen or len(seen) >= MAX_SUB:
            return
        seen.add(u)
        try:
            async with httpx.AsyncClient(trust_env=False, verify=False,
                                         follow_redirects=True,
                                         timeout=HTTP_TIMEOUT) as client:
                text = await fetch_text(client, u)
        except Exception:
            return
        cfg = parse_config_text(text)
        if not cfg:
            return
        all_sites.extend(extract_sites(cfg))
        if depth < MAX_DEPTH:
            for sub in sub_urls(cfg, u)[:MAX_SUB]:
                await walk(sub, depth + 1)

    await walk(url, 0)
    return all_sites, max(0, len(seen) - 1)
