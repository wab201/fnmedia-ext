"""Bounded header-aware network delivery; no transcoding or video decoding."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections import OrderedDict
from urllib.parse import urljoin

import httpx
from fastapi.responses import RedirectResponse, Response, StreamingResponse

import t4

PREFIX = "/v/media/t4-resource"
MAX_MANIFEST = 2 * 1024 * 1024


class T4Media:
    def __init__(self):
        self._resources = OrderedDict()
        self._resource_key = secrets.token_bytes(32)

    def resource(self, url: str, playback: t4.Playback, *, manifest=False) -> str:
        # HLS implicit byte ranges require repeated URIs to stay identical.
        identity = json.dumps([url, sorted(playback.headers.items()),
                               playback.wrapper, manifest], separators=(",", ":"))
        token = hashlib.blake2s(identity.encode(), key=self._resource_key,
                               digest_size=18).hexdigest()
        now = time.monotonic()
        self._resources.pop(token, None)
        while self._resources and (len(self._resources) >= 4096 or
                                  next(iter(self._resources.values()))[0] <= now):
            self._resources.popitem(last=False)
        self._resources[token] = now + 6 * 3600, url, playback, manifest
        return f"{PREFIX}/{token}"

    async def resource_response(self, client, token: str, request):
        hit = self._resources.get(token)
        if not hit or hit[0] <= time.monotonic():
            self._resources.pop(token, None)
            return Response("T4 播放会话已过期，请重新播放", status_code=410)
        _, url, playback, manifest = hit
        return await self.deliver(client, url, playback, request, manifest=manifest)

    async def identify(self, client, playback: t4.Playback) -> t4.Playback:
        if playback.wrapper:
            return playback
        try:
            response = await client.head(playback.url, headers=playback.headers,
                                         timeout=10, follow_redirects=True)
            response.raise_for_status()
            detected = t4.playback({"url": str(response.url),
                                    "type": response.headers.get("content-type", ""),
                                    "header": playback.headers}, playback.url)
        except httpx.HTTPError:
            raise t4.T4Error("无法识别 T4 媒体格式，服务端需返回 type") from None
        if not detected.wrapper:
            raise t4.T4Error("T4 媒体格式未知，不会将其伪装成 HLS")
        playback.wrapper = detected.wrapper
        return playback

    async def deliver(self, client, url: str, playback: t4.Playback, request,
                      *, manifest: bool | None = None):
        if not playback.needs_proxy:
            return RedirectResponse(url, status_code=302,
                                    headers={"Cache-Control": "no-store"})
        headers = httpx.Headers(playback.headers)
        for name in ("range", "if-range"):
            if name in request.headers:
                headers[name] = request.headers[name]
        upstream = None
        try:
            req = client.build_request(request.method, url, headers=headers,
                                       timeout=httpx.Timeout(60, connect=10))
            upstream = await client.send(req, stream=True, follow_redirects=True)
            if upstream.status_code >= 400:
                status = upstream.status_code
                extra = {"Content-Range": upstream.headers["content-range"]} \
                    if status == 416 and "content-range" in upstream.headers else {}
                await upstream.aclose()
                return Response(f"T4 媒体返回 HTTP {status}", status_code=status, headers=extra)
            ctype = upstream.headers.get("content-type", "").lower()
            is_manifest = (playback.wrapper == "HLS" if manifest is None else manifest) or \
                "mpegurl" in ctype or \
                upstream.url.path.lower().endswith(".m3u8")
            if is_manifest and request.method != "HEAD":
                data = bytearray()
                async for chunk in upstream.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_MANIFEST:
                        raise t4.T4Error("T4 HLS 清单超过大小限制")
                base = str(upstream.url)
                text = data.decode("utf-8-sig")
                if not text.lstrip().startswith("#EXTM3U"):
                    raise t4.T4Error("T4 返回的不是 HLS 清单")
                lines = []
                master = any(line.strip().startswith("#EXT-X-STREAM-INF:")
                             for line in text.splitlines())
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        line = self.resource(urljoin(base, stripped), playback,
                                             manifest=master)
                    elif 'URI="' in line:
                        playlist = stripped.startswith((
                            "#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:",
                            "#EXT-X-IMAGE-STREAM-INF:", "#EXT-X-RENDITION-REPORT:"))
                        line = re.sub(r'URI="([^"]+)"', lambda match:
                                      'URI="' + self.resource(
                                          urljoin(base, match[1]), playback,
                                          manifest=playlist) + '"', line)
                    lines.append(line)
                await upstream.aclose()
                return Response("\n".join(lines) + "\n",
                                media_type="application/vnd.apple.mpegurl",
                                headers={"Cache-Control": "no-store"})
            forwarded = {k: v for k, v in upstream.headers.items() if k.lower() in (
                "content-type", "content-length", "content-range", "accept-ranges",
                "content-encoding", "etag", "last-modified")}
            forwarded["Cache-Control"] = "no-store"
            if request.method == "HEAD":
                await upstream.aclose()
                if is_manifest:
                    response = Response(status_code=upstream.status_code,
                                        media_type="application/vnd.apple.mpegurl",
                                        headers={"Cache-Control": "no-store"})
                    # GET rewrites the manifest, so the origin length is not ours.
                    if "content-length" in response.headers:
                        del response.headers["content-length"]
                    return response
                return Response(status_code=upstream.status_code, headers=forwarded)
        except (httpx.HTTPError, UnicodeError, t4.T4Error):
            if upstream is not None:
                await upstream.aclose()
            return Response("T4 媒体网络请求失败", status_code=502)
        except BaseException:
            if upstream is not None:
                await upstream.aclose()
            raise

        async def pump():
            try:
                async for chunk in upstream.aiter_raw(chunk_size=256 * 1024):
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(pump(), status_code=upstream.status_code,
                                 headers=forwarded)
