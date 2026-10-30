"""Exercise real FastAPI routes with in-process T4/official/media fixtures."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

import app as appmod
import config as configmod
import history as histmod
import providers
import store as storemod
import t4_media
import t4_service


API = "http://t4.test/api/demo?token=fixture"


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for module, filename in [(configmod, "config"), (storemod, "titles"),
                                 (histmod, "history")]:
            item = patch.object(module, "_STATE", Path(temp.name) / (filename + ".json"))
            item.start()
            self.addCleanup(item.stop)
        self.config = configmod.Config()
        self.config.save({"sources": [], "removed_ids": [s["id"] for s in providers.DEFAULT_SOURCES]})
        self.store = storemod.Store()
        self.history = histmod.History()
        self.calls = []
        self.protected = False
        self.changed = False
        self.parse = 0
        self.wrapper = "mp4"
        for module, name, value in [
            (appmod, "config", self.config), (appmod, "store", self.store),
            (appmod, "history", self.history), (storemod, "store", self.store),
            (appmod, "t4_service", t4_service.T4Service(self.store, self.config)),
            (appmod, "t4_media", t4_media.T4Media()),
            (appmod, "_schedule_routes_refresh", lambda: None),
        ]:
            item = patch.object(module, name, value)
            item.start()
            self.addCleanup(item.stop)
        appmod._search_cache.clear()
        appmod._search_inflight.clear()
        self.network = httpx.AsyncClient(transport=httpx.MockTransport(self.handle),
                                        follow_redirects=True)
        self.addAsyncCleanup(self.network.aclose)
        appmod.app.state.ext = self.network
        appmod.app.state.up = self.network
        # Do not run lifespan: it starts production workers and config polling.
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app),
                                        base_url="http://fnmedia.test")
        self.addAsyncCleanup(self.client.aclose)
        constructor = httpx.AsyncClient

        def network_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(self.handle)
            return constructor(*args, **kwargs)

        item = patch("providers.httpx.AsyncClient", side_effect=network_client)
        item.start()
        self.addCleanup(item.stop)
        await self.import_source()

    def handle(self, request):
        self.calls.append(request)
        if request.url.host == "localhost":
            return httpx.Response(200, json={"code": 0, "data": []})
        if request.url.host == "media.test":
            if self.protected:
                self.assertEqual(request.headers["Cookie"], "fixture-secret")
            if request.url.path.endswith(".m3u8"):
                return httpx.Response(200, text=(
                    '#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-TARGETDURATION:6\n'
                    '#EXT-X-MAP:URI="clip.mp4",BYTERANGE="16@0"\n'
                    '#EXTINF:6,\n#EXT-X-BYTERANGE:16@16\nclip.mp4\n'
                    '#EXTINF:6,\n#EXT-X-BYTERANGE:16\nclip.mp4\n#EXT-X-ENDLIST\n'),
                    headers={"Content-Type": "application/octet-stream"})
            data = bytes(range(256)) * 4
            start, end = 0, len(data) - 1
            status = 200
            if "range" in request.headers:
                span = request.headers["range"].removeprefix("bytes=").split("-")
                start, end = int(span[0]), int(span[1])
                status = 206
            return httpx.Response(status, stream=BytesStream(
                b"" if request.method == "HEAD" else data[start:end + 1]),
                                  headers={"Content-Type": "video/mp4", "Content-Length": str(end - start + 1),
                                           "Accept-Ranges": "bytes",
                                           "Content-Range": f"bytes {start}-{end}/{len(data)}"})
        self.assertEqual(request.url.host, "t4.test")
        self.assertEqual(request.url.params["token"], "fixture")
        params = request.url.params
        if "wd" in params:
            return httpx.Response(200, json={"list": [
                {"vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧",
                 "vod_pic": "https://img.test/poster.jpg"}]})
        if "ids" in params:
            self.assertEqual(params["ids"], "movie-1")
            return httpx.Response(200, json={"list": [{
                "vod_id": "movie-1", "vod_name": "测试剧", "type_name": "电视剧",
                "vod_play_from": "A$$$B",
                "vod_play_url": ("第01集$a1#第02集$a2$$$第02集$b2#第01集$b1" if not self.changed else
                                 "第02集$a2-new#第01集$a1-new$$$第01集$b1-new#第02集$b2-new")
            }]})
        if "play" in params:
            return httpx.Response(200, json={
                "url": f"https://media.test/{params['play']}.{self.wrapper}",
                "parse": self.parse,
                "header": {"Cookie": "fixture-secret"} if self.protected else {}})
        return httpx.Response(200, json={"class": [], "list": []})

    async def import_source(self):
        response = await self.client.post("/v/api/ext/source/import-subscription",
                                          json={"text": json.dumps({"sites": [
                                              {"type": 4, "key": "demo", "name": "Test T4", "api": API},
                                              {"type": 3, "name": "Push", "api": "csp_Push"}]})})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["t4"]["candidates"], 1)

    async def search(self):
        response = await self.client.get("/v/api/v1/search/list", params={"q": "测试剧"})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]
        self.assertEqual(len(rows), 1)
        return rows[0]["guid"]

    async def load_episode(self):
        guid = await self.search()
        response = await self.client.get(f"/v/api/v1/item/{guid}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["type"], "TV")
        seasons = (await self.client.get(f"/v/api/v1/season/list/{guid}")).json()["data"]
        episodes = (await self.client.get(f"/v/api/v1/episode/list/{seasons[0]['guid']}")).json()["data"]
        return guid, next(e["guid"] for e in episodes if e["title"] == "第02集")

    async def test_search_detail_selection_and_public_direct(self):
        await self.search()
        self.assertFalse(any("ids" in r.url.params or "play" in r.url.params for r in self.calls))
        _, eg = await self.load_episode()
        self.assertEqual(len([r for r in self.calls if "ids" in r.url.params]), 1)
        self.assertFalse(any("play" in r.url.params for r in self.calls))
        info = (await self.client.post("/v/api/v1/play/info", json={"item_guid": eg})).json()["data"]
        stream = (await self.client.get(f"/v/api/v1/stream/list/{eg}")).json()["data"]
        self.assertEqual(len(stream["qualities"]), 2)
        self.assertFalse(any("play" in r.url.params for r in self.calls))
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": info["media_guid"], "bitrate": stream["qualities"][1]["bitrate"]})
        data = response.json()["data"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["play_link"], "https://media.test/b2.mp4")
        self.assertEqual(data["wrapper"], "MP4")
        self.assertFalse(any(r.url.host == "media.test" for r in self.calls))
        local = await self.client.get("/v/api/v1/item/official-guid")
        self.assertEqual(local.json(), {"code": 0, "data": []})

    async def test_app_range_header_and_first_middle_last(self):
        self.protected = True
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        for start, end in [(0, 15), (504, 519), (1008, 1023)]:
            with self.subTest(start=start):
                response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                                  headers={"Range": f"bytes={start}-{end}"},
                                                  follow_redirects=True)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.headers["content-range"], f"bytes {start}-{end}/1024")
                self.assertEqual(len(response.content), 16)
                self.assertNotIn("fixture-secret", str(response.headers))
        response = await self.client.head(f"/v/api/v1/media/range/{mg}", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-length"], "1024")

    async def test_history_restart_reloads_ids_and_does_not_switch_episode(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        await self.client.post("/v/api/v1/play/record", json={
            "item_guid": eg, "media_guid": mg, "ts": 123, "duration": 3600})
        self.history.flush()
        self.changed = True
        restored = storemod.Store()
        history = histmod.History()
        with patch.object(appmod, "store", restored), patch.object(storemod, "store", restored), \
                patch.object(appmod, "history", history), \
                patch.object(appmod, "t4_service", t4_service.T4Service(restored, self.config)):
            response = await self.client.post("/v/api/v1/play/info", json={"item_guid": eg})
            self.assertEqual(response.json()["data"]["ts"], 123)
            response = await self.client.post("/v/api/v1/play/play", json={"media_guid": mg})
        self.assertEqual(response.json()["data"]["play_link"], "https://media.test/a2-new.mp4")

    async def test_explicit_unsupported_parse_and_dash(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        self.parse = 1
        response = await self.client.post("/v/api/v1/play/play", json={"media_guid": mg})
        self.assertEqual(response.status_code, 502)
        self.assertIn("解析", response.json()["msg"])
        self.parse, self.wrapper = 0, "mpd"
        response = await self.client.post("/v/api/v1/play/play", json={"media_guid": mg})
        self.assertEqual(response.status_code, 502)
        self.assertIn("DASH", response.json()["msg"])

    async def test_t4_stream_metadata_uses_native_raw_contract(self):
        _, eg = await self.load_episode()
        menu = (await self.client.get(f"/v/api/v1/stream/list/{eg}")).json()["data"]
        self.assertEqual(menu["video_streams"][0]["wrapper"], "")
        self.assertEqual(menu["video_streams"][0]["codec_name"], "")
        self.assertFalse(any("play" in r.url.params for r in self.calls))
        mg = storemod.ep_media_guid(eg)
        for wrapper in ("mp4", "mkv"):
            self.wrapper = wrapper
            appmod.t4_service._playbacks.clear()
            response = await self.client.post("/v/api/v1/stream", json={"media_guid": mg})
            data = response.json()["data"]
            self.assertEqual(data["video_stream"]["wrapper"], wrapper.upper())
            self.assertEqual(data["video_stream"]["codec_name"], "")
            self.assertEqual(data["cloud_storage_info"]["cloud_storage_type"], 9001)
            self.assertEqual(len(data["direct_link_qualities"]), 2)
            self.assertTrue(all(not q["is_m3u8"] for q in data["direct_link_qualities"]))
            self.assertTrue(all(q["url"].startswith("/v/media/online/")
                                for q in data["direct_link_qualities"]))
            self.assertTrue(data["file_stream"]["file_name"].endswith("." + wrapper))

    async def test_t4_hls_stream_stays_manifest_delivery(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        response = await self.client.post("/v/api/v1/stream", json={
            "media_guid": storemod.ep_media_guid(eg)})
        data = response.json()["data"]
        self.assertEqual(data["video_stream"]["wrapper"], "HLS")
        self.assertIsNone(data["cloud_storage_info"])
        self.assertNotIn("direct_link_qualities", data)

    async def test_import_legacy_entry_and_source_test(self):
        response = await self.client.post("/v/api/ext/source/import-tvbox", json={
            "text": json.dumps({"sites": [{"type": 4, "name": "Other T4",
                                          "api": API + "&site=other"}]})})
        self.assertEqual(response.json()["data"]["t4"]["added"], 1)
        source = next(s for s in self.config.all_sources() if s.get("kind") == "t4")
        response = await self.client.post("/v/api/ext/source/test", json={"id": source["id"]})
        self.assertTrue(response.json()["data"]["ok"])
        self.assertFalse(any("wd" in r.url.params for r in self.calls))

    async def test_status_and_admin_entry(self):
        response = await self.client.get("/v/api/ext/status")
        self.assertEqual(response.status_code, 200)
        response = await self.client.get("/v/online-admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn("TVBox / T4", response.text)

    async def test_cms_search_and_play_regression(self):
        self.config.save({"sources": [{
            "id": "cms", "name": "Fixture CMS", "api": "https://cms.test/provide/vod/at/json",
            "enabled": True}]})
        title = providers._map_vod({
            "vod_id": "1", "vod_name": "测试电影", "type_name": "电影",
            "vod_play_from": "A", "vod_play_url": "正片$https://media.test/movie.mp4"},
            self.config.all_sources()[0])
        with patch.object(providers, "search_all", return_value=[title]):
            response = await self.client.get("/v/api/v1/search/list", params={"q": "测试电影"})
        guid = response.json()["data"][0]["guid"]
        response = await self.client.get(f"/v/api/v1/item/{guid}")
        self.assertEqual(response.json()["data"]["type"], "Movie")
        response = await self.client.post("/v/api/v1/play/info", json={"item_guid": guid})
        info = response.json()["data"]
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": info["media_guid"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["wrapper"], "MP4")
        self.assertIn("/direct?", response.json()["data"]["play_link"])

    async def test_chart_search_does_not_bind_lazy_t4_cards(self):
        with patch.object(providers, "search_all", return_value=[]) as search:
            await appmod._aggregate_briefs("测试剧", include_t4=False)
        self.assertFalse(any(s.get("kind") == "t4" for s in search.call_args.args[1]))

    async def test_all_client_profiles(self):
        for ua in ("trimemedia-web", "1.4.2 (com.trim.tv; Android 35) okHttp/4.9.0",
                   "Dalvik/2.1.0 Android", "1.12.2 (com.trim.media.ios) Flutter/3.11"):
            with self.subTest(ua=ua):
                self.client.headers["User-Agent"] = ua
                _, eg = await self.load_episode()
                response = await self.client.post("/v/api/v1/play/info", json={"item_guid": eg})
                info = response.json()["data"]
                self.assertEqual(response.status_code, 200)
                response = await self.client.post("/v/api/v1/play/play", json={
                    "media_guid": info["media_guid"]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"]["wrapper"], "MP4")

    async def test_native_range_retains_each_official_line_selector(self):
        self.protected = True
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        link = f"/v/media/online/{mg}/t4?line=B"
        for ua in ("com.trim.tv", "Dalvik/2.1.0 Android", "com.trim.media.ios"):
            for query, headers in [
                ({"direct_link_quality_index": "1"}, {}),
                ({"bitrate": "90010001"}, {}),
                ({"playlink": link}, {}),
                ({}, {"Play-Link": link}),
                ({"line": "B"}, {}),
            ]:
                with self.subTest(ua=ua, query=query, headers=headers):
                    self.client.headers["User-Agent"] = ua
                    appmod.t4_service._playbacks.clear()
                    response = await self.client.get(
                        f"/v/api/v1/media/range/{mg}", params=query,
                        headers={"Range": "bytes=32-47", **headers},
                        follow_redirects=True)
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.history, [])
                    self.assertEqual(response.content, (bytes(range(256)) * 4)[32:48])
                    played = [r for r in self.calls if "play" in r.url.params][-1]
                    self.assertEqual(played.url.params["play"], "b2")
                    self.assertEqual(played.url.params["flag"], "B")

    async def test_stream_and_play_use_same_selected_t4_line(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        response = await self.client.post("/v/api/v1/stream", json={
            "media_guid": mg, "bitrate": 90010001})
        self.assertEqual(response.status_code, 200)
        played = [r for r in self.calls if "play" in r.url.params][-1]
        self.assertEqual(played.url.params["play"], "b2")
        self.assertEqual(response.json()["data"]["video_stream"]["resolution_type"], "B")
        self.assertEqual(response.json()["data"]["video_stream"]["bps"], 90010001)
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg, "bitrate": 90010001})
        self.assertEqual(response.json()["data"]["play_link"], "https://media.test/b2.mp4")

    async def test_invalid_t4_line_does_not_silently_play_default(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        for path, query in [
            (f"/v/api/v1/media/range/{mg}", {"direct_link_quality_index": "99"}),
            (f"/v/api/v1/media/range/{mg}", {"direct_link_quality_index": "-1"}),
            (f"/v/api/v1/media/range/{mg}", {"direct_link_quality_index": "bad"}),
            (f"/v/media/online/{mg}/t4", {"line": "missing"}),
        ]:
            with self.subTest(query=query):
                response = await self.client.get(path, params=query, follow_redirects=True)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(any("play" in r.url.params for r in self.calls))

    async def test_native_range_does_not_follow_an_unowned_playlink(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                         params={"playlink": "https://unowned.test/video.mp4"},
                                         follow_redirects=False)
        self.assertNotIn("unowned.test", response.headers.get("location", ""))

    async def test_native_public_playlink_keeps_line_and_control_rpc(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg, "bitrate": 90010001})
        link = response.json()["data"]["play_link"]
        self.assertEqual(link, "https://media.test/b2.mp4")
        response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                         params={"playlink": link})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], link)
        response = await self.client.post("/v/api/v1/media/p", json={
            "playLink": link, "reqid": "fixture", "method": "checkPlayLink"})
        self.assertEqual(response.json()["data"], {"reqid": "fixture", "result": "succ"})
        response = await self.client.post("/v/api/v1/media/p", json={
            "playLink": "https://unowned.test/video.mp4", "reqid": "unknown"})
        self.assertEqual(response.json(), {"code": 0, "data": []})
        self.assertFalse(any(r.url.host == "media.test" for r in self.calls))

    async def test_t4_range_rejects_playlink_for_another_episode(self):
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                         params={"playlink": "/v/media/online/other/t4?line=B"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(any("play" in r.url.params for r in self.calls))

    async def test_hls_quality_rpc_switches_only_its_playback_session(self):
        self.wrapper, self.protected = "m3u8", True
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        first = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg})).json()["data"]["play_link"]
        second = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg})).json()["data"]["play_link"]
        self.assertNotEqual(first, second)
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.resetQuality", "reqid": "switch-b", "playLink": first,
            "quality": {"resolution": "B", "bitrate": 90010001}})
        self.assertEqual(response.json()["data"]["result"], "succ")
        for link, expected in ((first, "b2"), (second, "a2")):
            self.calls.clear()
            response = await self.client.get(link)
            self.assertEqual(response.status_code, 200)
            media = [r for r in self.calls if r.url.host == "media.test"][-1]
            self.assertEqual(media.url.path, f"/{expected}.m3u8")
        self.calls.clear()
        response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                         headers={"Play-Link": first})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r for r in self.calls if r.url.host == "media.test"][-1].url.path,
                         "/b2.m3u8")
        # Switching back uses the same menu codes after selecting B.
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.resetQuality", "playLink": first,
            "quality": {"resolution": "A", "bitrate": 90000000}})
        self.assertEqual(response.json()["data"]["result"], "succ")
        self.calls.clear()
        await self.client.get(first)
        self.assertEqual([r for r in self.calls if r.url.host == "media.test"][-1].url.path,
                         "/a2.m3u8")

    async def test_public_hls_session_redirects_without_proxying_media(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": storemod.ep_media_guid(eg)})
        response = await self.client.get(response.json()["data"]["play_link"])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://media.test/a2.m3u8")
        self.assertFalse(any(r.url.host == "media.test" for r in self.calls))

    async def test_hls_session_format_change_is_not_reported_as_success(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": storemod.ep_media_guid(eg)})
        link = response.json()["data"]["play_link"]
        self.wrapper = "mp4"
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.resetQuality", "playLink": link,
            "quality": {"resolution": "B", "bitrate": 90010001}})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["data"]["result"], "fail")

    async def test_hls_quality_rpc_rejects_unknown_transcoding_quality(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        response = await self.client.post("/v/api/v1/play/play", json={
            "media_guid": storemod.ep_media_guid(eg)})
        link = response.json()["data"]["play_link"]
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.resetQuality", "playLink": link,
            "quality": {"resolution": "720p", "bitrate": 2000000}})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["data"]["result"], "fail")

    async def test_hls_quit_reclaims_session_without_affecting_other_player(self):
        self.wrapper = "m3u8"
        _, eg = await self.load_episode()
        mg = storemod.ep_media_guid(eg)
        first = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg})).json()["data"]["play_link"]
        second = (await self.client.post("/v/api/v1/play/play", json={
            "media_guid": mg})).json()["data"]["play_link"]
        response = await self.client.post("/v/api/v1/media/p", json={
            "req": "media.quit", "playLink": first})
        self.assertEqual(response.json()["data"]["result"], "succ")
        self.assertEqual(len(appmod.t4_service._sessions), 1)
        response = await self.client.get(first)
        self.assertEqual(response.status_code, 410)
        response = await self.client.get(second)
        self.assertEqual(response.status_code, 302)

    async def test_access_log_names_client_without_signed_playlink(self):
        with self.assertLogs(appmod._log, level="INFO") as captured:
            await self.client.get("/v/api/v1/media/range/unknown", params={
                "playlink": "https://media.test/v.mp4?secret=private-signed-value"},
                headers={"User-Agent": "com.trim.media.ios"})
        output = "\n".join(captured.output)
        self.assertIn("client=iOS", output)
        self.assertNotIn("private-signed-value", output)
        self.assertNotIn("media.test", output)
        self.assertNotIn("private-signed-value", json.dumps(appmod.livemod.snapshot()))

    async def test_each_client_hls_byteranges_and_resume_contract(self):
        self.wrapper, self.protected = "m3u8", True
        for ua in ("trimemedia-web", "com.trim.tv", "Dalvik/2.1.0 Android",
                   "com.trim.media.ios"):
            with self.subTest(ua=ua):
                self.client.headers["User-Agent"] = ua
                guid, eg = await self.load_episode()
                mg = storemod.ep_media_guid(eg)
                info = (await self.client.post("/v/api/v1/play/info", json={
                    "item_guid": eg})).json()["data"]
                self.assertEqual(info["media_guid"], mg)
                head = await self.client.head(f"/v/api/v1/media/range/{mg}")
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.headers["content-type"], "application/vnd.apple.mpegurl")
                self.assertNotIn("content-length", head.headers)
                response = await self.client.get(f"/v/api/v1/media/range/{mg}",
                                                 follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                lines = response.text.splitlines()
                uris = [line for line in lines if line and not line.startswith("#")]
                self.assertEqual(uris[0], uris[1])
                for start, end in ((16, 31), (32, 47)):
                    part = await self.client.get(uris[0], headers={
                        "Range": f"bytes={start}-{end}"})
                    self.assertEqual(part.status_code, 206)
                    self.assertEqual(part.content, (bytes(range(256)) * 4)[start:end+1])
                await self.client.post("/v/api/v1/play/record", json={
                    "item_guid": eg, "media_guid": mg, "ts": 123, "duration": 3600})
                history = (await self.client.get("/v/api/v1/play/list")).json()["data"]
                self.assertEqual(history[0]["guid"], eg)
                info = (await self.client.post("/v/api/v1/play/info", json={
                    "item_guid": guid})).json()["data"]
                self.assertEqual(info["guid"], eg)
                self.assertEqual(info["ts"], 123)


if __name__ == "__main__":
    unittest.main()
