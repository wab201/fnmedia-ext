"""应用自更新：后台检查 GitHub Release、下载 fpk、调用 fnOS appcenter-cli 升级。

设计要点（安全/可逆优先）：
- 只读 GitHub 官方 API（releases/latest 与 releases/tags），不接受第三方地址；
- 资产仅在本仓库自己的 Release 附件中按文件名规则挑选（轻量版/全功能版）；
- 下载后强制 sha256 校验（Release 必须带同名 .sha256 附件），不符则拒绝安装；
- 安装走系统原生 `appcenter-cli install-fpk`（fnOS 应用中心正规升级通道，
  自动执行包内 upgrade_init/upgrade_callback：停服务→备份 state→落盘→恢复→重启）；
- 升级动作是“下载新版本并交给系统安装覆盖”，并非先卸载（避免清空 state）。
- 全程只在管理页显式点击“立即更新”时触发；后台只做只读检查与提示，绝不自动安装。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

try:
    import features
except Exception:  # pragma: no cover
    features = None

REPO_SLUG = "yujian0815/fnmedia-ext"
API_BASE = f"https://api.github.com/repos/{REPO_SLUG}/releases"
# 检查结果缓存时长（秒）：避免频繁打 GitHub，也让管理页打开即时。
CHECK_TTL = 30 * 60
# 后台启动后首次检查延迟 + 周期间隔。
BOOT_DELAY = 20
PERIOD = 6 * 3600
# 下载/安装总时长上限。
DOWNLOAD_TIMEOUT = 180
INSTALL_TIMEOUT = 600

_LOCK = threading.Lock()
_STATE: dict = {
    "last_check": 0.0,
    "last_error": "",
    "info": None,          # 最近一次 release 信息
    "status": "idle",      # idle | checking | downloading | installing | done | error
    "progress": "",        # 人类可读的当前步骤
    "installed_path": "",  # 已落盘待/正在安装的 fpk
    "updated_version": "",
    "finished_at": 0.0,
}

_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = Path("/tmp/fnmedia-update/state.json")


def _persist() -> None:
    """把进度落盘：安装会重启服务，重启后管理页仍能读到“已完成/失败”。"""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        snap = {k: _STATE.get(k) for k in (
            "last_check", "last_error", "info", "status", "progress",
            "installed_path", "updated_version", "finished_at")}
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception:  # noqa: BLE001
        pass


def _restore() -> None:
    try:
        if _STATE_FILE.exists():
            snap = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            # 重启后若停在“下载/安装中”，说明安装已接管，归一成完成态
            if snap.get("status") in ("downloading", "installing"):
                snap["status"] = "done"
                snap["progress"] = "安装完成，服务已重启"
                snap["finished_at"] = time.time()
            _STATE.update(snap)
    except Exception:  # noqa: BLE001
        pass


_restore()



# --------------------------------------------------------------- 版本

def current_version() -> str:
    """读取打包/仓库根目录 VERSION；读不到返回 '0.0.0'。"""
    for cand in (_ROOT / "VERSION", Path(__file__).resolve().parent / "VERSION"):
        try:
            v = cand.read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            continue
    return "0.0.0"


def _vtuple(v: str) -> tuple:
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in (nums + [0, 0, 0])[:3])


def is_newer(latest: str, current: str | None = None) -> bool:
    return _vtuple(latest) > _vtuple(current or current_version())


def edition_full() -> bool:
    """当前运行副本是否为全功能版（决定默认推荐下载哪个 fpk）。"""
    try:
        return bool(getattr(features, "DDUO", False) or getattr(features, "GUARD", False))
    except Exception:
        return False


# --------------------------------------------------------------- GitHub

def _pick_asset(assets: list[dict], full: bool) -> dict | None:
    """在 Release 附件里挑目标安装包。

    全功能版：fnmedia-ext-<ver>-full.fpk
    轻量版：fnmedia-ext-<ver>.fpk（排除 -full / -lite / -public），
            找不到则退回 -lite.fpk。
    """
    lite_plain = lite_alt = full_pkg = None
    for a in assets:
        name = a.get("name") or ""
        if not name.endswith(".fpk"):
            continue
        if re.search(r"-full\.fpk$", name):
            full_pkg = a
        elif re.search(r"-(lite|public)\.fpk$", name):
            lite_alt = a
        elif re.match(r"^fnmedia-ext-.+\.fpk$", name):
            lite_plain = a
    if full:
        return full_pkg
    return lite_plain or lite_alt


def _fetch_release_sync() -> dict | None:
    """拉取 latest Release（同步，固定在工作线程执行，绝不阻塞事件循环）。"""
    with httpx.Client(timeout=15, follow_redirects=True, trust_env=False,
                      headers={"Accept": "application/vnd.github+json",
                               "User-Agent": "fnmedia-ext-updater"}) as client:
        r = client.get(f"{API_BASE}/latest")
        if r.status_code != 200:
            raise RuntimeError(f"GitHub 返回 {r.status_code}")
        return r.json()


def _normalize(release: dict, full: bool) -> dict:
    tag = (release.get("tag_name") or "").lstrip("v")
    assets = release.get("assets") or []
    pkg = _pick_asset(assets, full)
    sum_url = ""
    if pkg:
        want = pkg.get("name") + ".sha256"
        for a in assets:
            if a.get("name") == want:
                sum_url = a.get("browser_download_url") or ""
                break
    return {
        "version": tag,
        "name": release.get("name") or tag,
        "notes": (release.get("body") or "").strip(),
        "published_at": release.get("published_at") or "",
        "html_url": release.get("html_url") or "",
        "edition": "full" if full else "lite",
        "asset": (pkg.get("browser_download_url") if pkg else ""),
        "asset_name": (pkg.get("name") if pkg else ""),
        "asset_size": (pkg.get("size") if pkg else 0),
        "sum_url": sum_url,
        "has_package": bool(pkg),
    }


def check_sync(force: bool = False) -> dict:
    """检查更新（同步、带缓存）。仅在工作线程执行，避免网络阻塞事件循环。"""
    now = time.time()
    with _LOCK:
        if (not force and _STATE["info"]
                and now - _STATE["last_check"] < CHECK_TTL):
            return _snapshot()
        if _STATE["status"] in ("checking", "downloading", "installing"):
            return _snapshot()
        _STATE["status"] = "checking"
        _STATE["progress"] = "正在检查 GitHub 更新…"

    full = edition_full()
    try:
        release = _fetch_release_sync()
        if not release or release.get("draft"):
            raise RuntimeError("未获取到有效 Release")
        info = _normalize(release, full)
        cur = current_version()
        info["current_version"] = cur
        info["update_available"] = is_newer(info["version"], cur)
        with _LOCK:
            _STATE.update(info=info, last_check=now, last_error="",
                          status="idle", progress="")
        _persist()
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _STATE.update(last_check=now, last_error=str(exc),
                          status="error", progress="检查更新失败")
        _persist()
    return _view()


async def check(force: bool = False) -> dict:
    """异步入口：把同步网络检查丢到工作线程，绝不占用事件循环。"""
    return await asyncio.to_thread(check_sync, force)


def _snapshot() -> dict:
    """组装状态视图（不加锁；调用方需自行确保不在写竞争中或已持锁）。"""
    info = dict(_STATE["info"]) if _STATE["info"] else None
    return {
        "code": 0,
        "current_version": current_version(),
        "edition": "full" if edition_full() else "lite",
        "status": _STATE["status"],
        "progress": _STATE["progress"],
        "last_check": _STATE["last_check"],
        "last_error": _STATE["last_error"],
        "update": info,
        "updated_version": _STATE["updated_version"],
        "finished_at": _STATE["finished_at"],
    }


def _view() -> dict:
    with _LOCK:
        return _snapshot()


def status() -> dict:
    return _view()


# --------------------------------------------------------------- 下载 / 安装

def _appcenter_bin() -> str | None:
    for name in ("appcenter-cli", "/usr/local/bin/appcenter-cli"):
        p = shutil.which(name) if "/" not in name else (name if os.path.exists(name) else None)
        if p:
            return p
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def start_update() -> dict:
    """触发一次“下载校验 → 系统安装覆盖”的升级（仅管理页显式调用）。"""
    with _LOCK:
        if _STATE["status"] in ("checking", "downloading", "installing"):
            return {"code": 409, "msg": "已有更新任务在进行中", "data": _snapshot()}
        info = dict(_STATE["info"]) if _STATE["info"] else None
        if not info or not info.get("update_available"):
            return {"code": 400, "msg": "当前没有可安装的更新", "data": _snapshot()}
        if not info.get("asset"):
            return {"code": 400, "msg": "该版本未提供对应安装包", "data": _snapshot()}
        binp = _appcenter_bin()
        if not binp:
            return {"code": 500, "msg": "未找到 fnOS appcenter-cli，无法自动安装",
                    "data": _snapshot()}
        _STATE["status"] = "downloading"
        _STATE["progress"] = "正在下载安装包…"
    _persist()

    asyncio.get_event_loop().run_in_executor(
        None, lambda: _blocking_install(info, binp))
    return {"code": 0, "msg": "已开始下载并安装更新", "data": _view()}


def _blocking_install(info: dict, binp: str) -> None:
    tmp_dir = Path("/tmp/fnmedia-update")
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        pkg_name = info["asset_name"] or "fnmedia-ext.fpk"
        pkg_path = tmp_dir / pkg_name

        with _LOCK:
            _STATE["status"] = "downloading"
            _STATE["progress"] = "正在下载安装包…"
        _persist()

        with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True,
                          trust_env=False,
                          headers={"User-Agent": "fnmedia-ext-updater"}) as cli:
            with cli.stream("GET", info["asset"]) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"下载失败 HTTP {resp.status_code}")
                total = int(resp.headers.get("content-length") or info.get("asset_size") or 0)
                got = 0
                with pkg_path.open("wb") as f:
                    for chunk in resp.iter_bytes(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            pct = got * 100 // total
                            with _LOCK:
                                _STATE["progress"] = f"下载中 {pct}%"

        # sha256 强制校验：Release 必须带 .sha256 附件。
        if info.get("sum_url"):
            with httpx.Client(timeout=30, follow_redirects=True,
                              trust_env=False,
                              headers={"User-Agent": "fnmedia-ext-updater"}) as cli:
                sr = cli.get(info["sum_url"])
                sr.raise_for_status()
            expect = (sr.text or "").strip().split()[0].lower()
            actual = _sha256(pkg_path)
            if not expect or not re.fullmatch(r"[0-9a-f]{64}", expect):
                raise RuntimeError("校验文件格式异常，已中止")
            if expect != actual:
                raise RuntimeError("sha256 校验不一致，已中止安装")
        else:
            raise RuntimeError("Release 缺少 sha256 校验附件，已中止")

        with _LOCK:
            _STATE["status"] = "installing"
            _STATE["progress"] = "正在通过应用中心安装（服务将短暂重启）…"
            _STATE["installed_path"] = str(pkg_path)
        _persist()

        # 免责声明同意：升级等价于再次确认安装条款（用户在管理页已主动点更新）。
        env_file = tmp_dir / "fpk-env"
        env_file.write_text("wizard_agree_terms=agree\n", encoding="utf-8")

        # appcenter-cli install-fpk <file> -e <env>
        # 安装器会执行包内 upgrade_init/upgrade_callback：停服务→备份 state→
        # 落盘新代码→恢复 state→重启服务。当前进程随后会被系统停止，属预期。
        proc = subprocess.run(
            [binp, "install-fpk", str(pkg_path), "-e", str(env_file)],
            capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
        tail = (proc.stdout or "")[-400:] + (proc.stderr or "")[-400:]
        if proc.returncode != 0:
            raise RuntimeError(f"应用中心安装失败(rc={proc.returncode})：{tail.strip()}")

        with _LOCK:
            _STATE.update(status="done",
                          progress="安装完成，服务正在重启…",
                          updated_version=info["version"],
                          finished_at=time.time())
        _persist()
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _STATE.update(status="error",
                          progress=f"更新失败：{exc}",
                          last_error=str(exc))
        _persist()


# --------------------------------------------------------------- 后台周期检查

async def periodic_loop(stop_event: asyncio.Event) -> None:
    """启动后延迟首检，之后每 PERIOD 秒只读检查一次（绝不自动安装）。"""
    try:
        await asyncio.wait_for(
            asyncio.shield(stop_event.wait()), timeout=BOOT_DELAY)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(check_sync, False)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()), timeout=PERIOD)
        except asyncio.TimeoutError:
            continue
