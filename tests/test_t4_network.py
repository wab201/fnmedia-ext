"""Real loopback HTTP verification with synthetic media, not a NAS acceptance."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import t4
from t4_media import T4Media


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_t4_and_header_media_over_real_tcp(self):
        calls = []
        media = bytes(range(256)) * 4096

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                calls.append((parsed.path, query, self.headers.get("Range")))
                if parsed.path == "/api":
                    if "play" in query:
                        payload = {"url": f"http://127.0.0.1:{self.server.server_port}/video",
                                   "type": "video/mp4", "header": {"Referer": "fixture"}}
                    elif "ids" in query:
                        payload = {"list": [{"vod_id": "1", "vod_name": "Fixture",
                                             "vod_play_from": "line",
                                             "vod_play_url": "正片$opaque"}]}
                    else:
                        payload = {"list": [{"vod_id": "1", "vod_name": "Fixture"}]}
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.headers.get("Referer") != "fixture":
                    self.send_error(403)
                    return
                span = self.headers.get("Range", "").removeprefix("bytes=").split("-")
                start, end = int(span[0]), int(span[1])
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(media)}")
                self.end_headers()
                self.wfile.write(media[start:end + 1])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        source = {"id": "loopback", "api": f"http://127.0.0.1:{server.server_port}/api",
                  "kind": "t4", "enabled": True}
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                titles = await t4.search(client, source, "Fixture")
                title = await t4.detail(client, source, titles[0].locator)
                episode = title.sources["line"][0]
                result = await t4.player(client, source, episode.url, episode.play_from)
                proxy = T4Media()
                for start, end in [(0, 15), (500000, 500015), (len(media) - 16, len(media) - 1)]:
                    response = await proxy.deliver(client, result.url, result,
                                                  SimpleNamespace(method="GET", headers={
                                                      "range": f"bytes={start}-{end}"}))
                    data = b"".join([chunk async for chunk in response.body_iterator])
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(data, media[start:end + 1])
                    self.assertEqual(response.headers["content-range"],
                                     f"bytes {start}-{end}/{len(media)}")
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(len(calls), 6)


if __name__ == "__main__":
    unittest.main()
