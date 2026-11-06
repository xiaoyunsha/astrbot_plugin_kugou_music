import os
import re
import json
import time
import uuid
import base64
import hashlib
import asyncio
import aiohttp
from datetime import datetime

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Image, Plain


class KugouMusic(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession = None

        # Persistent cache - store search results to disk as user requested
        self._cache_dir = os.path.join(
            os.getcwd(), "data", "plugin_data", "astrbot_plugin_kugou_music"
        )
        os.makedirs(self._cache_dir, exist_ok=True)
        self._cache_file = os.path.join(self._cache_dir, "search_cache.json")
        self._search_cache = self._load_cache()
        self._cache_ttl = 300  # 5 minutes
        self._debug = []

    # ── cache helpers ──────────────────────────────────────────

    def _load_cache(self) -> dict:
        try:
            if os.path.exists(self._cache_file):
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load search cache: {e}")
        return {}

    def _save_cache(self):
        try:
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(self._search_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save search cache: {e}")

    def _clean_cache(self):
        now = time.time()
        expired = [
            k for k, v in self._search_cache.items()
            if v.get("expire_at", 0) < now
        ]
        for k in expired:
            del self._search_cache[k]
        if expired:
            self._save_cache()

    # ── cookie persistence (for inline QR login) ─────────────────

    def _cookie_file(self) -> str:
        return os.path.join(self._cache_dir, "cookie.json")

    def _load_saved_cookie(self) -> str:
        """Load cookie that was set via inline QR login, if any."""
        try:
            p = self._cookie_file()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                return d.get("cookie", "") or ""
        except Exception as e:
            logger.warning(f"Failed to load saved cookie: {e}")
        return ""

    def _save_cookie(self, cookie: str, source: str = "qr") -> bool:
        """Persist cookie to disk and update in-memory config."""
        try:
            with open(self._cookie_file(), "w", encoding="utf-8") as f:
                json.dump({
                    "cookie": cookie,
                    "source": source,
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }, f, ensure_ascii=False, indent=2)
            # Update in-memory config so the current session picks it up
            self.config["kugou_cookie"] = cookie
            logger.info(f"[kugou] Cookie saved via {source}")
            return True
        except Exception as e:
            logger.error(f"Failed to save cookie: {e}")
            return False

    def _clear_cookie(self) -> bool:
        try:
            p = self._cookie_file()
            if os.path.exists(p):
                os.remove(p)
        except Exception as e:
            logger.warning(f"Failed to delete cookie file: {e}")
        self.config["kugou_cookie"] = ""
        return True

    # ── config helpers ─────────────────────────────────────────

    def _get_cookie(self) -> str:
        raw = self.config.get("kugou_cookie", "").strip()
        if not raw:
            # Fall back to cookie saved by inline QR login
            raw = self._load_saved_cookie()
        if not raw:
            return ""
        # Auto-detect: if user pasted the full JSON, extract cookie_string
        if raw.startswith("{") and "cookie_string" in raw:
            try:
                data = json.loads(raw)
                raw = data.get("cookie_string", raw)
            except (json.JSONDecodeError, TypeError):
                pass
        return raw

    def _get_ua(self) -> str:
        return self.config.get("kugou_ua", "").strip() or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )

    def _build_headers(self, with_cookie: bool = True) -> dict:
        h = {
            "User-Agent": self._get_ua(),
            "Referer": "https://www.kugou.com/",
            "Accept": "application/json, text/plain, */*",
        }
        if with_cookie:
            cookie = self._get_cookie()
            if cookie:
                # Strip control characters that cause header injection errors
                cookie = re.sub(r'[\x00-\x1f\x7f]', '', cookie)
                h["Cookie"] = cookie
        return h

    # ── debug helpers ──────────────────────────────────────────

    def _add_debug(self, action: str, **kwargs):
        if not self.config.get("debug", False):
            return
        entry = {
            "action": action,
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
        }
        entry.update(kwargs)
        self._debug.append(entry)

    def _flush_debug(self) -> str:
        if not self._debug or not self.config.get("debug", False):
            return ""
        lines = ["\n[_debug]"]
        for d in self._debug:
            lines.append(json.dumps(d, ensure_ascii=False))
        self._debug = []
        return "\n".join(lines)

    # ── session ─────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.get("timeout", 15))
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    # ── Kugou API calls ─────────────────────────────────────────

    async def _search_kugou(self, keyword: str) -> list:
        """Search Kugou music. Returns list of {hash, songname, singername, album_id, duration}."""
        t0 = time.time()

        # Primary: mobile CDN API
        url = "https://mobilecdn.kugou.com/api/v3/search/song"
        params = {
            "format": "json",
            "keyword": keyword,
            "page": 1,
            "pagesize": self.config.get("search_count", 5),
        }
        headers = self._build_headers(with_cookie=False)
        # Ensure JSON is accepted for aiohttp strict content-type check
        headers["Accept"] = "application/json"

        self._add_debug(
            "search_start", url=url, params=params,
            cookie_len=len(self._get_cookie())
        )

        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers) as resp:
                elapsed = (time.time() - t0) * 1000
                content_type = resp.headers.get("Content-Type", "")
                self._add_debug(
                    "search_response", status=resp.status,
                    elapsed_ms=round(elapsed, 1), content_type=content_type[:80]
                )

                if resp.status != 200:
                    logger.error(f"Kugou search HTTP {resp.status}")
                    return []

                # Use resp.text() + json.loads() to bypass strict content-type check
                resp_text = await resp.text()
                data = json.loads(resp_text)
                if data.get("status") != 1:
                    logger.error(f"Kugou search API error: {data.get('error', '')}")
                    return []

                songs = []
                for item in data.get("data", {}).get("info", []):
                    songs.append({
                        "hash": item.get("hash", ""),
                        "songname": item.get("songname", ""),
                        "singername": item.get("singername", ""),
                        "album_id": str(item.get("album_id", "")),
                        "album_audio_id": item.get("album_audio_id", ""),
                        "duration": item.get("duration", 0),
                        "pay_type": item.get("pay_type", 0),
                    })

                self._add_debug("search_result", count=len(songs))
                return songs

        except (json.JSONDecodeError, aiohttp.ClientError) as e:
            # Primary failed, try fallback: song_search_v2
            logger.warning(f"Primary search failed: {e}, trying fallback")
            self._add_debug("search_fallback", reason=str(e)[:80])
            return await self._search_kugou_v2(keyword)

        except Exception as e:
            logger.error(f"Kugou search unexpected error: {e}")
            self._add_debug("search_error", error=str(e)[:120])
            return []

    async def _search_kugou_v2(self, keyword: str) -> list:
        """Fallback: use song_search_v2 endpoint (JSONP)."""
        t0 = time.time()
        url = "https://songsearch.kugou.com/song_search_v2"
        params = {
            "keyword": keyword,
            "page": 1,
            "pagesize": self.config.get("search_count", 5),
            "platform": "WebFilter",
            "filter": "2",
            "iscorrection": "1",
            "privilege_filter": "0",
        }

        self._add_debug("search_v2_start", url=url, params=params)

        try:
            session = await self._get_session()
            headers = self._build_headers(with_cookie=False)
            headers["Accept"] = "*/*"
            async with session.get(url, params=params, headers=headers) as resp:
                elapsed = (time.time() - t0) * 1000
                self._add_debug(
                    "search_v2_response", status=resp.status,
                    elapsed_ms=round(elapsed, 1)
                )

                if resp.status != 200:
                    return []

                resp_text = await resp.text()

                # Strip JSONP callback wrapper: jQuery1910xxx({...})
                json_str = resp_text
                if resp_text.startswith("jQuery"):
                    start = resp_text.index("(") + 1
                    end = resp_text.rindex(")")
                    json_str = resp_text[start:end]

                data = json.loads(json_str)

                if data.get("status") != 1:
                    return []

                songs = []
                for item in data.get("data", {}).get("lists", []):
                    songs.append({
                        "hash": item.get("FileHash", ""),
                        "songname": item.get("SongName", ""),
                        "singername": item.get("SingerName", ""),
                        "album_id": str(item.get("AlbumID", "")),
                        "album_audio_id": item.get("AlbumAudioID", "") or item.get("album_audio_id", ""),
                        "duration": item.get("Duration", 0),
                        "pay_type": item.get("PayType", 0),
                    })

                self._add_debug("search_v2_result", count=len(songs))
                return songs

        except Exception as e:
            logger.error(f"Kugou search_v2 fallback also failed: {e}")
            self._add_debug("search_v2_error", error=str(e)[:120])
            return []

    async def _get_play_info(self, song_hash: str, album_id: str, album_audio_id: str = "") -> dict:
        """Get play URL, cover, and metadata.

        Flow:
        1. Call mobile API for cover + album_audio_id + metadata (free or VIP)
        2. If cookie configured, try v5/url for the actual play_url (VIP support)
        """
        t0 = time.time()
        cookie = self._get_cookie()

        # ── 1. Get cover + metadata + album_audio_id from mobile API ──
        base = await self._get_play_info_mobile(song_hash, cookie, t0)
        if not base:
            return {}

        # Get album_audio_id from mobile API if not provided
        aaid = album_audio_id or base.get("album_audio_id", "")

        # ── 2. If cookie available, try v5/url for VIP play_url ──
        if cookie and aaid:
            v5 = await self._get_play_info_v5(song_hash, album_id, aaid, cookie, t0)
            if v5 and v5.get("play_url"):
                # Merge: use v5 play_url, keep mobile API's cover/metadata
                base["play_url"] = v5["play_url"]
                self._add_debug("playinfo_merged", source="v5_url + mobile_cover")
                return base

        return base

    # ── v5/url endpoint (Android encryption, no external service needed) ──

    # Standard version salts
    ANDROID_SALT = "OIlwieks28dk2k092lksi2UIkp"
    KEY_SALT = "57ae12eb6890223e355ccfcb74edf70d"
    # Concept (Lite) version salts
    ANDROID_SALT_LITE = "LnT6xpN3khm36zse0QzvmgTZ3waWdRSA"
    KEY_SALT_LITE = "185672dd44712f60bb1736df5a377e82"

    def _android_sign(self, params: dict) -> str:
        s = "".join(f"{k}={params[k]}" for k in sorted(params.keys()))
        salt = self.ANDROID_SALT_LITE if self._is_concept() else self.ANDROID_SALT
        return hashlib.md5((salt + s + salt).encode()).hexdigest()

    def _sign_key(self, hash_val: str, mid: str, userid: str, appid: str) -> str:
        salt = self.KEY_SALT_LITE if self._is_concept() else self.KEY_SALT
        return hashlib.md5(
            (hash_val + salt + appid + mid + userid).encode()
        ).hexdigest()

    async def _get_play_info_v5(self, song_hash: str, album_id: str, album_audio_id: str, cookie: str, t0: float) -> dict:
        """trackercdn.kugou.com/v5/url - supports VIP songs with token."""
        token = self._extract_cookie_field(cookie, "token")
        userid = self._extract_cookie_field(cookie, "userid")
        if not token or not userid:
            return {}

        mid = 'eb9cc313237a2f0afd659a6b35966fa5'
        dfid = 'mi6sagitbe1a04a892eadee5fe'
        clienttime = int(time.time())

        # Use concept version params if concept mode is on (required for lite token)
        if self._is_concept():
            appid = "3116"
            page_id = "967177915"
            pid = "411"
        else:
            appid = "1005"
            page_id = "151369488"
            pid = "2"

        key = self._sign_key(song_hash.lower(), mid, userid, appid)

        params = {
            "album_id": album_id,
            "area_code": "1",
            "hash": song_hash.lower(),
            "version": "11430",
            "page_id": page_id,
            "quality": "128",
            "album_audio_id": album_audio_id,
            "behavior": "play",
            "pid": pid,
            "cmd": "26",
            "pidversion": "3001",
            "IsFreePart": "0",
            "cdnBackup": "1",
            "module": "",
            "clientver": "11430",
            "dfid": dfid,
            "mid": mid,
            "uuid": '7aabb67641bfded00c23226cc00f5521',
            "appid": appid,
            "clienttime": str(clienttime),
            "key": key,
            "token": token,
            "userid": userid,
        }
        params["signature"] = self._android_sign(params)

        headers = {
            "User-Agent": "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi",
            "dfid": dfid,
            "clienttime": str(clienttime),
            "mid": mid,
            "kg-rc": "1",
            "kg-thash": "5d816a0",
            "kg-rec": "1",
            "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
        }

        self._add_debug(
            "playinfo_v5_start", hash=song_hash.lower(),
            token_len=len(token), userid=userid[:4] + "***"
        )

        try:
            session = await self._get_session()
            async with session.get(
                "https://trackercdn.kugou.com/v5/url", params=params, headers=headers
            ) as resp:
                elapsed = (time.time() - t0) * 1000
                self._add_debug(
                    "playinfo_v5_response", status=resp.status,
                    elapsed_ms=round(elapsed, 1)
                )

                if resp.status != 200:
                    return {}

                resp_text = await resp.text()
                data = json.loads(resp_text)
                if data.get("status") != 1:
                    self._add_debug("playinfo_v5_fail",
                        error_code=data.get("error_code", ""),
                        error=data.get("error", ""))
                    return {}

                urls = data.get("url", [])
                play_url = urls[0] if urls else ""

                result = {
                    "play_url": play_url,
                    "img": "",
                    "song_name": "",
                    "author_name": "",
                    "timelength": 0,
                    "pay_type": 0,
                    "fail_process": 0,
                }

                self._add_debug("playinfo_v5_result",
                    has_play_url=bool(play_url),
                    play_url_len=len(play_url))
                return result

        except Exception as e:
            self._add_debug("playinfo_v5_error", error=str(e)[:120])
            return {}

    async def _get_play_info_mobile(self, song_hash: str, cookie: str, t0: float) -> dict:
        """Mobile API: m.kugou.com/app/i/getSongInfo.php (free songs only)."""
        url = "https://m.kugou.com/app/i/getSongInfo.php"
        params = {"cmd": "playInfo", "hash": song_hash}
        headers = self._build_headers(with_cookie=bool(cookie))
        headers["Referer"] = "https://m.kugou.com/"

        self._add_debug(
            "playinfo_mobile_start", url=url, hash=song_hash,
            cookie_len=len(cookie)
        )

        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers) as resp:
                elapsed = (time.time() - t0) * 1000
                self._add_debug(
                    "playinfo_mobile_response", status=resp.status,
                    elapsed_ms=round(elapsed, 1)
                )

                if resp.status != 200:
                    return {}

                resp_text = await resp.text()
                data = json.loads(resp_text)

                play_url = data.get("url", "")
                pay_type = data.get("pay_type", 0)
                fail_process = data.get("fail_process", 0)

                album_img = data.get("album_img", "")
                if album_img:
                    album_img = album_img.replace("{size}", "400")

                result = {
                    "play_url": play_url,
                    "img": album_img,
                    "song_name": data.get("songName", ""),
                    "author_name": data.get("singerName", ""),
                    "timelength": data.get("timeLength", 0) * 1000,
                    "pay_type": pay_type,
                    "fail_process": fail_process,
                    "privilege": data.get("privilege", 0),
                    "album_audio_id": str(data.get("album_audio_id", "")),
                }

                self._add_debug(
                    "playinfo_mobile_result",
                    has_play_url=bool(play_url),
                    has_img=bool(album_img),
                    pay_type=pay_type,
                    fail_process=fail_process,
                )
                return result

        except Exception as e:
            logger.error(f"Kugou play info error: {e}")
            self._add_debug("playinfo_mobile_error", error=str(e)[:120])
            return {}

    def _extract_cookie_field(self, cookie: str, field: str) -> str:
        """Extract a specific field from cookie string."""
        if not cookie:
            return ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith(field + "="):
                return part[len(field) + 1:]
        return ""

    # ── music card (built locally, no external API) ─────────────

    CARD_APPID = 205141  # 酷狗音乐 QQ 互联 appid
    CARD_TAG_ICON = "https://open.gtimg.cn/open/app_icon/00/20/51/41/205141_100_m.png"
    # 咸鱼API 签名端点（文档页为 /api/qq-musicArk）
    XIANYU_SIGN_URL = "https://apii.xianyuw.cn/api/v1/qq-musicArk"
    # OIAPI 签名端点 — 支持 kugoulite（酷狗概念版专属）
    OIAPI_SIGN_URL = "https://oiapi.net/api/QQMusicJSONArk"

    async def _sign_card_oiapi(self, play_url: str, song: str, singer: str,
                                cover: str, jump: str) -> dict:
        """Sign a music card via OIAPI (free, no token).  Supports kugoulite
        (酷狗概念版专属 format).  Returns ark dict or {} on failure.
        """
        # kugoulite for concept mode, kugou for standard
        fmt = "kugoulite" if self._is_concept() else "kugou"
        params = {
            "url": play_url,
            "format": fmt,
            "song": song,
            "singer": singer,
            "cover": cover,
            "jump": jump or play_url,
        }
        try:
            session = await self._get_session()
            async with session.get(self.OIAPI_SIGN_URL, params=params) as resp:
                if resp.status != 200:
                    self._add_debug("card_oiapi", http=resp.status)
                    return {}
                data = json.loads(await resp.text())
                # OIAPI 成功返回 code=1（不是 200），负数为错误码
                if data.get("code") == 1 and isinstance(data.get("data"), dict):
                    logger.info(f"[kugou] card signed via OIAPI ({fmt})")
                    self._add_debug("card_oiapi", ok=True, format=fmt,
                                    app=data["data"].get("app"),
                                    tag=data["data"].get("meta", {})
                                             .get("music", {}).get("tag"))
                    return data["data"]
                self._add_debug("card_oiapi", code=data.get("code"),
                                msg=str(data.get("message", ""))[:60])
        except Exception as e:
            logger.warning(f"[kugou] OIAPI card sign failed: {e}")
            self._add_debug("card_oiapi_error", error=str(e)[:80])
        return {}

    async def _sign_card_xianyu(self, play_url: str, song: str, singer: str,
                                cover: str, jump: str) -> dict:
        """Sign a music card via xianyu API (requires token).  Fallback when
        OIAPI is down or for non-concept users.  Returns ark dict or {} on failure.
        """
        key = self.config.get("music_card_key", "").strip()
        if not key:
            return {}

        params = {
            "url": play_url,
            "format": self.config.get("card_format", "kugou"),
            "song": song,
            "singer": singer,
            "cover": cover,
            "jump": jump or play_url,
            "key": key,
        }
        try:
            session = await self._get_session()
            async with session.get(self.XIANYU_SIGN_URL, params=params) as resp:
                if resp.status != 200:
                    self._add_debug("card_xianyu", http=resp.status)
                    return {}
                data = json.loads(await resp.text())
                if data.get("code") == 200 and data.get("data"):
                    logger.info("[kugou] card signed via xianyu API")
                    self._add_debug("card_xianyu", ok=True,
                                    app=data["data"].get("app"))
                    return data["data"]
                self._add_debug("card_xianyu", code=data.get("code"),
                                msg=str(data.get("msg", ""))[:60])
        except Exception as e:
            logger.warning(f"[kugou] xianyu card sign failed: {e}")
            self._add_debug("card_xianyu_error", error=str(e)[:80])
        return {}

    def _build_music_card(self, title: str, artist: str, cover: str,
                          play_url: str, jump_url: str = "", uin: int = 0,
                          song_hash: str = "") -> dict:
        """Build QQ music share card — com.tencent.music.lua format.

        song_hash is carried so _send_card can dispatch NapCat's native
        music.type=kugou segment before falling back to the JSON card.
        """
        ctime = int(time.time())
        token = hashlib.md5(
            f"{title}{artist}{ctime}{play_url}".encode("utf-8")
        ).hexdigest()
        # Use jump_url for musicUrl — NapCat pre-fetches this URL during JSON
        # card sendMsg and times out (1200) on large audio CDN links.  The jump
        # URL (Kugou song detail page) is a lightweight HTML page that never
        # triggers the pre-fetch timeout.
        safe_music_url = jump_url if jump_url else play_url
        return {
            "app": "com.tencent.music.lua",
            "bizsrc": "qqconnect.sdkshare_music",
            "config": {
                "ctime": ctime,
                "forward": 1,
                "token": token,
                "type": "normal",
            },
            "extra": {
                "app_type": 1,
                "appid": self.CARD_APPID,
                "uin": uin,
            },
            "meta": {
                "music": {
                    "app_type": 1,
                    "appid": self.CARD_APPID,
                    "ctime": ctime,
                    "desc": artist,
                    "jumpUrl": jump_url or play_url,
                    "musicUrl": safe_music_url,
                    "preview": cover,
                    "tag": "酷狗音乐",
                    "title": title,
                    "uin": uin,
                    # Custom fields for fallback dispatch
                    "_kg_hash": song_hash,
                    "_audio_url": play_url,
                }
            },
            "prompt": "酷狗音乐",
            "ver": "0.0.0.1",
            "view": "music",
        }

    async def _send_card(self, event: AstrMessageEvent, card_json: dict) -> bool:
        """Send music card.

        Priority:
        0. Signed JSON ark from OIAPI/xianyu — carries a valid token and the
           kugou tag/tagIcon, so QQ renders the true 酷狗音乐 brand.
        1. NapCat native music.type=custom (instant, generic brand).
        2. NapCat native music.type=kugou (true brand only if musicSignUrl is
           configured in NapCat WebUI).
        Unsigned locally built cards skip path 0 (NapCat rejects them with
        retcode=1200) and are identified by the _kg_hash/_audio_url markers.
        """
        bot = getattr(event, "bot", None)
        group_id = event.get_group_id() if hasattr(event, "get_group_id") else None

        meta = card_json.get("meta", {}).get("music", {}) if isinstance(card_json, dict) else {}
        _kg_hash = meta.get("_kg_hash", "")
        # 本地卡用 _audio_url 携带直链；签名卡用标准字段 musicUrl
        _audio_url = meta.get("_audio_url") or meta.get("musicUrl", "")
        title = meta.get("title", "")
        artist = meta.get("desc", "")
        cover = meta.get("preview", "")
        jump_url = meta.get("jumpUrl", "")

        async def _send(message: list):
            if group_id:
                return await bot.api.call_action("send_group_msg", group_id=group_id, message=message)
            else:
                return await bot.api.call_action(
                    "send_private_msg", user_id=event.get_sender_id(), message=message
                )

        # ══ 0. 已签名 JSON ark（OIAPI/咸鱼返回）—— 携带有效 token 与酷狗
        #       tag/tagIcon，QQ 才会渲染为真正的「酷狗音乐」品牌卡片。
        #       本地构造的卡片带 _kg_hash/_audio_url 标记，必须跳过此路径
        #       （未签名 JSON 会被 NapCat 拒绝，retcode=1200）。
        is_local_card = bool(meta.get("_audio_url") or meta.get("_kg_hash"))
        if bot and hasattr(bot, "api") and card_json and not is_local_card:
            try:
                msg = [{
                    "type": "json",
                    "data": {"data": json.dumps(card_json, ensure_ascii=False)},
                }]
                ret = await _send(msg)
                # NapCat 失败时不抛异常而是返回 retcode!=0，需显式判定后降级
                if isinstance(ret, dict) and ret.get("retcode", 0) not in (0, None):
                    raise RuntimeError(f"json send retcode={ret.get('retcode')}")
                logger.info("[kugou] card sent via signed json ark")
                self._add_debug("card_sent", method="signed_json")
                return True
            except Exception as e:
                logger.warning(f"[kugou] signed json failed, fallback to native: {e}")
                self._add_debug("card_fail", method="signed_json", error=str(e)[:80])

        # ══ 1. NapCat 原生 music.type=custom — sends instantly, shows as music card ══
        if bot and hasattr(bot, "api") and title:
            try:
                music_data = {
                    "type": "custom",
                    "url": jump_url,
                    "audio": _audio_url,
                    "title": title,
                }
                if cover:
                    music_data["image"] = cover
                if artist:
                    music_data["singer"] = artist
                msg = [{"type": "music", "data": music_data}]
                await _send(msg)
                logger.info("[kugou] card sent via music.custom")
                self._add_debug("card_sent", method="music_custom")
                return True
            except Exception as e:
                logger.warning(f"[kugou] music_custom failed: {e}")
                self._add_debug("card_fail", method="music_custom", error=str(e)[:80])

        # ══ 2. NapCat 原生 music.type=kugou（配了 musicSignUrl 才显示酷狗品牌） ══
        if bot and hasattr(bot, "api") and _kg_hash:
            try:
                msg = [{"type": "music", "data": {"type": "kugou", "id": _kg_hash}}]
                await _send(msg)
                logger.info("[kugou] card sent via music.kugou")
                self._add_debug("card_sent", method="music_kugou")
                return True
            except Exception as e:
                logger.warning(f"[kugou] music_kugou failed: {e}")
                self._add_debug("card_fail", method="music_kugou", error=str(e)[:80])

        return False

    # ── inline QR login (self-contained, no external service) ───

    # Constants for Kugou web signature (matches KuGouMusicApi util/helper.js)
    _WEB_SIGN_SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
    _QR_APPID = 1001            # for /v2/qrcode appid param (overrides default)
    _APPID = 1005               # default appid (standard)
    _APPID_LITE = 3116          # concept version (lite) appid
    _CLIENTVER = 20489          # standard
    _CLIENTVER_LITE = 11440     # concept version
    _SRCAPPID = 2919
    _KG_UA = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"
    _KG_HDR = {
        "kg-rc": "1",
        "kg-thash": "5d816a0",
        "kg-rec": "1",
        "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
    }

    def _is_concept(self) -> bool:
        """Whether to use Kugou Concept (Lite) platform parameters."""
        return bool(self.config.get("kugou_concept", False))

    def _qr_eff_appid(self) -> int:
        """Effective appid based on concept mode toggle."""
        return self._APPID_LITE if self._is_concept() else self._APPID

    def _qr_eff_clientver(self) -> int:
        return self._CLIENTVER_LITE if self._is_concept() else self._CLIENTVER

    def _web_sign(self, params: dict) -> str:
        """MD5 signature matching KuGouMusicApi's signatureWebParams."""
        sorted_str = "".join(f"{k}={v}" for k, v in sorted(params.items()))
        return hashlib.md5(
            f"{self._WEB_SIGN_SALT}{sorted_str}{self._WEB_SIGN_SALT}".encode("utf-8")
        ).hexdigest()

    def _qr_headers(self, dfid: str, mid: str, clienttime: int) -> dict:
        """Build request headers matching KuGouMusicApi request.js exactly."""
        return {
            "User-Agent": self._KG_UA,
            "dfid": dfid,
            "clienttime": str(clienttime),
            "mid": mid,
            **self._KG_HDR,
        }

    async def _qr_get_key(self) -> dict:
        """Call Kugou /v2/qrcode. Concept mode → plat=4 (Android), standard → plat=1 (PC)."""
        dfid = uuid.uuid4().hex
        mid = uuid.uuid4().hex
        clienttime = int(time.time())
        appid = self._qr_eff_appid()
        clientver = self._qr_eff_clientver()
        concept = self._is_concept()

        # Concept: Android channel (plat=4, appid=3116 in qrcode_txt)
        # Standard: PC channel (plat=1, appid=1005 in qrcode_txt)
        plat = "4" if concept else "1"
        params = {
            "dfid": dfid,
            "mid": mid,
            "uuid": uuid.uuid4().hex,
            "appid": self._QR_APPID,       # module overrides default (1001)
            "clientver": clientver,
            "clienttime": clienttime,
            "type": "1",
            "plat": plat,
            "qrcode_txt": (
                f"https://h5.kugou.com/apps/loginQRCode/html/index.html"
                f"?appid={appid}&"
            ),
            "srcappid": self._SRCAPPID,
        }
        params["signature"] = self._web_sign(params)

        try:
            session = await self._get_session()
            async with session.get(
                "https://login-user.kugou.com/v2/qrcode",
                params=params,
                headers=self._qr_headers(dfid, mid, clienttime),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[kugou] qr/key HTTP {resp.status}")
                    return {}
                data = json.loads(await resp.text())
                inner = data.get("data", data)
                return {
                    "key": inner.get("qrcode", ""),
                    "img_b64": inner.get("qrcode_img", ""),
                }
        except Exception as e:
            logger.error(f"[kugou] qr/key failed: {e}")
            return {}

    async def _qr_check(self, key: str) -> int:
        """Call Kugou /v2/get_userinfo_qrcode. Concept → plat=4, standard → plat=1."""
        dfid = uuid.uuid4().hex
        mid = uuid.uuid4().hex
        clienttime = int(time.time())
        plat = "4" if self._is_concept() else "1"

        params = {
            "dfid": dfid,
            "mid": mid,
            "uuid": uuid.uuid4().hex,
            "appid": self._qr_eff_appid(),
            "clientver": self._qr_eff_clientver(),
            "clienttime": clienttime,
            "plat": plat,
            "srcappid": self._SRCAPPID,
            "qrcode": key,
        }
        params["signature"] = self._web_sign(params)

        try:
            session = await self._get_session()
            async with session.get(
                "https://login-user.kugou.com/v2/get_userinfo_qrcode",
                params=params,
                headers=self._qr_headers(dfid, mid, clienttime),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return 0
                data = json.loads(await resp.text())
                return int(data.get("data", {}).get("status", 0))
        except Exception as e:
            logger.debug(f"[kugou] qr/check error: {e}")
            return -1  # transient

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("酷狗扫码登录")
    async def cmd_qr_login(self, event: AstrMessageEvent):
        """Inline QR login — auto-switches to Android channel when concept mode is on."""
        concept = self._is_concept()
        plat_label = "安卓端" if concept else "PC 端"
        yield event.plain_result(f"⏳ 正在生成二维码（{plat_label}协议，插件内置）...")

        # 1. Get QR key + image directly from Kugou
        qr = await self._qr_get_key()
        if not qr.get("key") or not qr.get("img_b64"):
            yield event.plain_result(
                "❌ 生成二维码失败\n"
                "可能原因：Kugou 接口临时不可用 / 设备指纹被风控。"
            )
            return

        qr_key = qr["key"]

        # 2. Send Kugou's real H5 scan page URL as plain text link
        concept_note = "（概念版，安卓设备渠道）" if concept else ""
        scan_url = f"https://h5.kugou.com/apps/loginQRCode/html/index.html?qrcode={qr_key}"
        yield event.plain_result(
            f"📱 请在手机浏览器打开以下链接，然后扫码登录\n\n"
            f"（登录请求为 {plat_label}协议{concept_note}）\n"
            f"⏱ 二维码有效期 5 分钟\n\n"
            f"👉 {scan_url}\n\n"
            f"打开后页面会显示二维码，用手机酷狗 App 扫一扫即可"
        )

        # 3. Poll for scan confirmation (max 5 min)
        deadline = time.time() + 300
        last_state = 0
        while time.time() < deadline:
            await asyncio.sleep(2)
            status = await self._qr_check(qr_key)
            if status == 1:
                continue
            if status == 2 and last_state != 2:
                yield event.plain_result("✅ 已扫码，请在手机上点「确认登录」")
                last_state = 2
            elif status == 4:
                token, userid = await self._qr_fetch_credentials(qr_key)
                if not token or not userid:
                    yield event.plain_result("⚠️ 扫码成功但未拿到 token/userid")
                    return
                cookie = f"token={token}; userid={userid}; a_id={self._qr_eff_appid()}"
                self._save_cookie(cookie, source="inline_qr")
                yield event.plain_result(
                    f"✅ 登录成功！\n"
                    f"userid: {userid}\n"
                    f"Cookie 已自动保存，立即可用。"
                )
                return
            elif status == 0:
                yield event.plain_result("❌ 二维码已过期，请重试")
                return

        yield event.plain_result("⏱ 二维码超时（5分钟未扫码），请重试。")

    async def _qr_fetch_credentials(self, key: str) -> tuple:
        """Re-poll check endpoint and extract token+userid from the success response."""
        dfid = uuid.uuid4().hex
        mid = uuid.uuid4().hex
        clienttime = int(time.time())
        plat = "4" if self._is_concept() else "1"
        params = {
            "dfid": dfid, "mid": mid, "uuid": uuid.uuid4().hex,
            "appid": self._qr_eff_appid(), "clientver": self._qr_eff_clientver(),
            "clienttime": clienttime, "plat": plat,
            "srcappid": self._SRCAPPID, "qrcode": key,
        }
        params["signature"] = self._web_sign(params)
        try:
            session = await self._get_session()
            async with session.get(
                "https://login-user.kugou.com/v2/get_userinfo_qrcode",
                params=params, headers=self._qr_headers(dfid, mid, clienttime),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return "", ""
                d = json.loads(await resp.text()).get("data", {})
                return d.get("token", ""), d.get("userid", "")
        except Exception:
            return "", ""

    # ── RSA / AES crypto (pure Python, matches KuGouMusicApi crypto.js) ──
    # RSA modulus n (extracted from X.509 SubjectPublicKeyInfo DER)
    _RSA_STANDARD_N = 140445963535792402926033351579977428601305895625550756594559963120307716645785244302059106223535183272284544870503939785450043521994181640022628976140213994438652251761790380365966692759283841664136451950718962987218307250964818338632386480727770923680812615716152721764995019898353671779266879027867752353824
    _RSA_LITE_N     = 137663794008607651653890046938671327369715483147545627213325703442262571612527589810823814641909238236485832314923006785354760218579495585325103045620379147165040411681626483377778892956210750442114872075206236006794712873114289397588119752792503667245748501905527122576158396773297736227160469650546868400160
    _RSA_E          = 65537
    # Lite AES keys (from login_openplat.js)
    _LITE_T2_KEY = "fd14b35e3f81af3817a20ae7adae7020"
    _LITE_T2_IV  = "17a20ae7adae7020"
    _LITE_T1_KEY = "5e4ef500e9597fe004bd09a46d8add98"
    _LITE_T1_IV  = "04bd09a46d8add98"

    def _aes_encrypt(self, data: str, key: str, iv: str) -> str:
        """AES-128-CBC PKCS7 → hex (same as JS cryptoAesEncrypt with fixed key/iv)."""
        cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
        return cipher.encrypt(pad(data.encode(), 16)).hex()

    def _aes_decrypt(self, hex_str: str, key: str, iv: str) -> str:
        cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
        return unpad(cipher.decrypt(bytes.fromhex(hex_str)), 16).decode()

    def _rsa_encrypt(self, data: bytes, n: int, e: int = _RSA_E) -> str:
        """Raw modular exponentiation (same as JS BigInteger.modPow + zero-pad)."""
        klen = (n.bit_length() + 7) // 8
        padded = b"\x00" * (klen - len(data)) + data
        encrypted = pow(int.from_bytes(padded, "big"), e, n)
        return hex(encrypted)[2:].upper().zfill(klen * 2)

    def _rsa_eff_n(self) -> int:
        return self._RSA_LITE_N if self._is_concept() else self._RSA_STANDARD_N

    # ── WeChat login helpers (pure Python, no external service) ──
    # WeChat appid/secret from KuGouMusicApi config.json
    _WX_APPID       = "wx79f2c4418704b4f8"
    _WX_SECRET      = "4efcab88b700769e376e3f6087b8abc9"
    _WX_LITE_APPID  = "wx72b795aca60ad321"
    _WX_LITE_SECRET = "33e486041e5e25729a4e3d2da7502f9a"

    def _wx_eff_appid(self) -> str:
        return self._WX_LITE_APPID if self._is_concept() else self._WX_APPID

    def _wx_eff_secret(self) -> str:
        return self._WX_LITE_SECRET if self._is_concept() else self._WX_SECRET

    async def _wx_get_access_token(self) -> str:
        """Step 1: Get WeChat access_token."""
        appid = self._wx_eff_appid()
        secret = self._wx_eff_secret()
        url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={secret}"
        try:
            session = await self._get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = json.loads(await resp.text())
                token = data.get("access_token", "")
                if not token:
                    logger.warning(f"[kugou] wx access_token failed: {data}")
                return token
        except Exception as e:
            logger.warning(f"[kugou] wx access_token error: {e}")
            return ""

    async def _wx_get_ticket(self, access_token: str) -> str:
        """Step 2: Get WeChat SDK ticket."""
        url = f"https://api.weixin.qq.com/cgi-bin/ticket/getticket?access_token={access_token}&type=2"
        try:
            session = await self._get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = json.loads(await resp.text())
                if data.get("errcode") != 0:
                    logger.warning(f"[kugou] wx ticket failed: {data}")
                    return ""
                return data.get("ticket", "")
        except Exception as e:
            logger.warning(f"[kugou] wx ticket error: {e}")
            return ""

    async def _wx_generate_qr(self) -> dict:
        """Step 3: Generate WeChat QR connect URL. Returns {uuid, qrcodeurl} or {}."""
        access_token = await self._wx_get_access_token()
        if not access_token:
            return {}
        ticket = await self._wx_get_ticket(access_token)
        if not ticket:
            return {}

        appid = self._wx_eff_appid()
        ts = int(time.time() * 1000)
        noncestr = hashlib.md5(uuid.uuid4().hex.encode()).hexdigest()
        sig_str = f"appid={appid}&noncestr={noncestr}&sdk_ticket={ticket}&timestamp={ts}"
        signature = hashlib.sha1(sig_str.encode()).hexdigest()

        params = {"appid": appid, "noncestr": noncestr, "timestamp": ts,
                  "scope": "snsapi_userinfo", "signature": signature}
        try:
            session = await self._get_session()
            async with session.get(
                "https://open.weixin.qq.com/connect/sdk/qrconnect",
                params=params, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = json.loads(await resp.text())
                if data.get("errcode") != 0:
                    logger.warning(f"[kugou] wx qrconnect failed: {data}")
                    return {}
                uuid_val = data.get("uuid", "")
                return {
                    "uuid": uuid_val,
                    "qrcodeurl": f"https://open.weixin.qq.com/connect/confirm?uuid={uuid_val}",
                }
        except Exception as e:
            logger.warning(f"[kugou] wx qrconnect error: {e}")
            return {}

    async def _wx_poll_code(self, uuid_val: str, deadline: float) -> str:
        """Step 4: Poll WeChat QR scan until confirmed or timeout. Returns code or ''."""
        session = await self._get_session()
        url = f"https://long.open.weixin.qq.com/connect/l/qrconnect?f=json&uuid={uuid_val}"
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data = json.loads(await resp.text())
                    # wx_code is the authorization code after user confirms
                    code = data.get("wx_code", "")
                    if code:
                        logger.info(f"[kugou] wx login confirmed, code={code[:10]}...")
                        return code
            except Exception as e:
                logger.debug(f"[kugou] wx poll error: {e}")
                continue
        return ""

    async def _wx_openplat_exchange(self, code: str) -> tuple:
        """Step 5: Exchange WeChat code for Kugou token via Kugou API directly.
        Pure Python — matches login_openplat.js exactly. No external service needed.
        Returns (token, userid) or ("", "").
        """
        concept = self._is_concept()
        appid_wx = self._wx_eff_appid()
        secret = self._wx_eff_secret()
        try:
            session = await self._get_session()
            # 5a. WeChat OAuth: code → access_token + openid
            wx_oauth_url = "https://api.weixin.qq.com/sns/oauth2/access_token"
            async with session.post(
                wx_oauth_url,
                params={"secret": secret, "appid": appid_wx, "code": code,
                        "grant_type": "authorization_code"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                oa_data = json.loads(await resp.text())
            access_token = oa_data.get("access_token", "")
            openid = oa_data.get("openid", "")
            if not access_token or not openid:
                logger.warning(f"[kugou] wx oauth failed: {oa_data}")
                return "", ""

            # 5b. AES encrypt the access_token (same as cryptoAesEncrypt w/o fixed key/iv)
            #     JS: const encrypt = cryptoAesEncrypt({ access_token });
            #     Returns { str: hex, key: random_hex_string }
            #     key = md5(tempKey)[:32], iv = key[-16:]
            temp_key = hashlib.md5(uuid.uuid4().hex.encode()).hexdigest()[:16]
            aes_key = hashlib.md5(temp_key.encode()).hexdigest()[:32]
            aes_iv = aes_key[-16:]
            enc_str = self._aes_encrypt(
                json.dumps({"access_token": access_token}), aes_key, aes_iv
            )

            # 5c. RSA encrypt { clienttime_ms, key } (same as cryptoRSAEncrypt)
            date_now = int(time.time() * 1000)
            pk_str = self._rsa_encrypt(
                json.dumps({"clienttime_ms": date_now, "key": temp_key}).encode(),
                self._rsa_eff_n(),
            )

            # 5d. Lite t1/t2 (only for concept/lite — legacy standard login may be 0)
            guid = "0"
            mac = "0"
            dev = "0"
            t2_plain = f"{guid}|0f607264fc6318a92b9e13c65db7cd3c|{mac}|{dev}|{date_now}"
            t2_enc = self._aes_encrypt(t2_plain, self._LITE_T2_KEY, self._LITE_T2_IV)
            t1_enc = self._aes_encrypt(f"|{date_now}", self._LITE_T1_KEY, self._LITE_T1_IV)

            # 5e. Build data payload for /v6/login_by_openplat
            kg_appid = self._qr_eff_appid()  # 1005 or 3116
            kg_clientver = self._qr_eff_clientver()
            dfid = uuid.uuid4().hex
            mid = uuid.uuid4().hex
            clienttime = int(time.time())

            data_map = {
                "dev": dev,
                "force_login": 1,
                "partnerid": 36,
                "clienttime_ms": date_now,
                "t1": t1_enc if concept else 0,
                "t2": t2_enc if concept else 0,
                "t3": "MCwwLDAsMCwwLDAsMCwwLDA=",
                "openid": openid,
                "params": enc_str,
                "pk": pk_str,
                "dfid": dfid,
                "mid": mid,
                "uuid": uuid.uuid4().hex,
                "appid": kg_appid,
                "clientver": kg_clientver,
                "clienttime": clienttime,
                "srcappid": self._SRCAPPID,
            }

            # 5f. Android signature (same as signatureAndroidParams in helper.js)
            data_map["signature"] = self._android_sign(data_map)

            # 5g. POST to Kugou login endpoint
            headers = {
                "User-Agent": self._KG_UA,
                "dfid": dfid, "mid": mid, "clienttime": str(clienttime),
                "x-router": "login.user.kugou.com",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            async with session.post(
                f"https://gateway.kugou.com/v6/login_by_openplat",
                data=data_map,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = json.loads(await resp.text())

            if body.get("status") != 1:
                logger.warning(f"[kugou] wx openplat Kugou rejected: {body}")
                return "", ""

            # 5h. AES decrypt secu_params to extract token
            secu = body.get("data", {}).get("secu_params", "")
            try:
                decrypted = self._aes_decrypt(secu, aes_key, aes_iv)
                dec_data = json.loads(decrypted) if isinstance(decrypted, str) else decrypted
                if isinstance(dec_data, dict):
                    token = dec_data.get("token", "")
                    userid = str(dec_data.get("userid", "0"))
                else:
                    token = str(dec_data)
                    userid = str(body.get("data", {}).get("userid", "0"))
                if token:
                    logger.info(f"[kugou] wx openplat done: userid={userid} token={token[:8]}...")
                    return token, userid
            except Exception as e:
                # secu decrypt failed; fallback to raw token in body.data
                raw_token = body.get("data", {}).get("token", "")
                raw_userid = str(body.get("data", {}).get("userid", "0"))
                if raw_token:
                    return raw_token, raw_userid
                logger.warning(f"[kugou] wx secu_params decrypt failed: {e} body={body}")
                return "", ""

            logger.warning(f"[kugou] wx openplat no token extracted")
            return "", ""

        except Exception as e:
            logger.warning(f"[kugou] wx openplat error: {e}")
            return "", ""

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("酷狗微信登录")
    async def cmd_wx_login(self, event: AstrMessageEvent):
        """微信授权登录酷狗：生成微信链接 → 复制到微信打开 → 自动换 Cookie。
        仅支持概念版（kugou_concept=true），走安卓设备渠道，授权后在设备列表中显示为安卓手机。
        链接 1 分 50 秒后自动撤回，防止盗用。
        """
        if not self._is_concept():
            yield event.plain_result(
                "❌ 微信登录仅支持酷狗概念版\n\n"
                "请先在 WebUI 插件配置中开启「酷狗概念版」开关（kugou_concept = true），\n"
                "然后重载插件，再发送 /酷狗微信登录\n\n"
                "原因：微信风控升级后，标准版已不支持微信授权登录，\n"
                "只有概念版可通过安卓小程序渠道完成微信授权。"
            )
            return

        yield event.plain_result("⏳ 正在生成微信授权链接（安卓端协议 / 概念版）...")

        # Step 1-3: Generate WeChat QR URL
        qr_data = await self._wx_generate_qr()
        if not qr_data:
            yield event.plain_result(
                "❌ 生成微信授权链接失败，请稍后重试\n"
                "提示：请确认网络正常，且本机可以访问微信 API"
            )
            return

        qrcodeurl = qr_data["qrcodeurl"]
        uuid_val = qr_data["uuid"]
        link_text = (
            f"📱 请在微信中打开以下链接授权登录酷狗概念版\n\n"
            f"👉 {qrcodeurl}\n\n"
            f"⏱ 链接有效期 5 分钟，超时需重新生成\n"
            f"⚠️ 打开链接后在微信中点「确认登录」即可\n"
            f"📱 授权后设备列表显示为安卓手机（概念版安卓渠道）\n"
            f"⏰ 此消息 1 分 50 秒后自动撤回（防止链接被盗用）"
        )

        # Send the link manually via OneBot API to get message_id for recall
        sent_msg_id = None
        bot = getattr(event, "bot", None)
        group_id = event.get_group_id() if hasattr(event, "get_group_id") else None
        if bot and hasattr(bot, "api"):
            try:
                msg = [{"type": "text", "data": {"text": link_text}}]
                if group_id:
                    resp = await bot.api.call_action(
                        "send_group_msg", group_id=group_id, message=msg
                    )
                else:
                    resp = await bot.api.call_action(
                        "send_private_msg",
                        user_id=event.get_sender_id(), message=msg,
                    )
                sent_msg_id = resp.get("data", {}).get("message_id") if isinstance(resp, dict) else None
            except Exception as e:
                logger.warning(f"[kugou] wx send link failed, falling back: {e}")
                yield event.plain_result(link_text)

        # ── Poll + recall — two parallel coroutines ──
        deadline = time.time() + 110

        async def _poll_only():
            return await self._wx_poll_code(uuid_val, deadline)

        async def _recall_after_110s():
            await asyncio.sleep(110)
            if sent_msg_id and bot and hasattr(bot, "api"):
                try:
                    await bot.api.call_action("delete_msg", message_id=sent_msg_id)
                    logger.info("[kugou] wx login link auto-recalled")
                except Exception as e:
                    logger.debug(f"[kugou] wx recall failed: {e}")

        poll_task = asyncio.create_task(_poll_only())
        recall_task = asyncio.create_task(_recall_after_110s())
        code = await poll_task

        if not code:
            yield event.plain_result(
                "⏱ 微信授权超时（1 分 50 秒未确认）\n"
                "请重新发送 /酷狗微信登录"
            )
            return

        # Step 5: Exchange code for Kugou token
        token, userid = await self._wx_openplat_exchange(code)
        if not token:
            yield event.plain_result(
                "⚠️ 微信授权成功，但兑换酷狗 Token 失败\n\n"
                "可能原因：\n"
                "1. 酷狗服务端拒绝此次登录（账号风控/未绑定）\n"
                "2. 此微信号未绑定酷狗账号\n"
                "3. 网络异常或接口请求被拦截\n\n"
                "请排查后重试 /酷狗微信登录\n"
                "提示：可查看 AstrBot 日志中 [kugou] wx openplat 开头的记录"
            )
            return

        # Save cookie
        a_id = self._qr_eff_appid()
        cookie = f"token={token}; userid={userid}; a_id={a_id}"
        self._save_cookie(cookie, source="wx_wechat")
        logger.info(f"[kugou] wx login done: userid={userid}, token={token[:8]}...")

        yield event.plain_result(
            f"✅ 微信授权登录成功！\n"
            f"userid: {userid}\n"
            f"Cookie 已自动保存，立即可用。"
        )

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("酷狗退出登录")
    async def cmd_logout(self, event: AstrMessageEvent):
        """Clear saved cookie."""
        self._clear_cookie()
        yield event.plain_result("✅ 已退出登录，Cookie 已清除。")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("酷狗登录状态")
    async def cmd_login_status(self, event: AstrMessageEvent):
        """Show current login status."""
        cookie = self._get_cookie()
        if not cookie:
            yield event.plain_result(
                "❌ 未登录\n"
                "发送 /酷狗扫码登录 用手机扫码登录\n"
                "发送 /酷狗微信登录 用微信授权登录"
            )
            return

        token = self._extract_cookie_field(cookie, "token")
        userid = self._extract_cookie_field(cookie, "userid")
        token_preview = token[:8] + "..." if token else "无"

        yield event.plain_result(
            f"✅ 已登录\n"
            f"userid: {userid}\n"
            f"token: {token_preview}\n"
            f"来源: {self._load_saved_cookie() and 'inline QR' or 'WebUI 配置'}"
        )

    # ── lifecycle ───────────────────────────────────────────────

    async def initialize(self):
        logger.info("[kugou_music] Plugin initialized")

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("[kugou_music] Plugin terminated")

    # ── commands ────────────────────────────────────────────────

    @filter.command("酷狗搜歌")
    async def cmd_search(self, event: AstrMessageEvent):
        """Search Kugou music: /酷狗搜歌 <keyword>"""
        msg = event.message_str.strip()
        # Strip command prefix (handles /酷狗搜歌, 酷狗搜歌, /酷狗 搜歌 etc.)
        keyword = re.sub(r'^/?\s*酷狗\s*搜歌\s*', '', msg).strip()

        if not keyword:
            yield event.plain_result(
                "用法: /酷狗搜歌 <关键词>\n"
                "例如: /酷狗搜歌 晴天"
            )
            return

        songs = await self._search_kugou(keyword)

        if not songs:
            yield event.plain_result(
                f"[酷狗] 未找到与「{keyword}」相关的歌曲"
                + self._flush_debug()
            )
            return

        # Cache results for this session (persisted to disk)
        session_id = event.unified_msg_origin
        self._search_cache[session_id] = {
            "songs": songs,
            "expire_at": time.time() + self._cache_ttl,
        }
        self._save_cache()
        self._clean_cache()

        # Build result text
        lines = [f"[酷狗搜索] 「{keyword}」共找到 {len(songs)} 首歌:"]
        for i, song in enumerate(songs, 1):
            dur = ""
            if song.get("duration"):
                m, s = divmod(song["duration"], 60)
                dur = f" [{m}:{s:02d}]"
            # Show VIP label from PayType in search results (v2 api)
            pay_type = song.get("pay_type", 0)
            vip_label = " [VIP]" if pay_type == 3 else ""
            lines.append(f"{i}. {song['songname']} - {song['singername']}{dur}{vip_label}")

        lines.append(f"\n发送 /酷狗点歌 序号 进行点歌")

        yield event.plain_result("\n".join(lines) + self._flush_debug())

    @filter.command("酷狗点歌")
    async def cmd_play(self, event: AstrMessageEvent):
        """Play a song: /酷狗点歌 <index>"""
        msg = event.message_str.strip()
        # Strip command prefix
        arg = re.sub(r'^/?\s*酷狗\s*点歌\s*', '', msg).strip()
        session_id = event.unified_msg_origin

        # Check cache
        cached = self._search_cache.get(session_id)
        if not cached or cached.get("expire_at", 0) < time.time():
            yield event.plain_result(
                "[酷狗] 请先使用 /酷狗搜歌 <关键词> 搜索歌曲"
            )
            return

        songs = cached.get("songs", [])

        # Parse index
        try:
            index = int(arg) - 1
            if index < 0 or index >= len(songs):
                yield event.plain_result(
                    f"[酷狗] 序号超出范围，请输入 1-{len(songs)}"
                )
                return
        except ValueError:
            yield event.plain_result(
                "[酷狗] 请输入有效的序号\n"
                f"例如: /酷狗点歌 1  (范围 1-{len(songs)})"
            )
            return

        song = songs[index]

        # Get play info
        play_info = await self._get_play_info(song["hash"], song["album_id"], song.get("album_audio_id", ""))

        if not play_info:
            yield event.plain_result(
                f"[酷狗] 获取「{song['songname']} - {song['singername']}」播放信息失败，请稍后重试"
                + self._flush_debug()
            )
            return

        # Extract fields
        play_url = play_info.get("play_url") or play_info.get("url", "")
        cover_url = play_info.get("img", "")
        song_name = play_info.get("song_name") or song["songname"]
        artist = play_info.get("author_name") or song["singername"]
        timelength = play_info.get("timelength", 0)

        if timelength:
            m, s = divmod(int(timelength) // 1000, 60)
            duration_str = f"{m}:{s:02d}"
        else:
            duration_str = "未知"

        # Build text
        has_cookie = bool(self._get_cookie())
        pay_type = play_info.get("pay_type", 0)
        if not play_url:
            if pay_type == 3:
                if not has_cookie:
                    text = (
                        f"[酷狗] {song_name} - {artist}\n"
                        f"时长: {duration_str}\n"
                        f"⚠️ VIP歌曲，请在WebUI配置酷狗会员Cookie后重试"
                    )
                else:
                    text = (
                        f"[酷狗] {song_name} - {artist}\n"
                        f"时长: {duration_str}\n"
                        f"⚠️ VIP歌曲，Cookie可能已失效，请重新获取"
                    )
            else:
                text = (
                    f"[酷狗] {song_name} - {artist}\n"
                    f"时长: {duration_str}\n"
                    f"⚠️ 获取播放链接失败，请稍后重试"
                )
        else:
            text = (
                f"[酷狗] {song_name} - {artist}\n"
                f"时长: {duration_str}\n"
                f"播放: {play_url}"
            )

        # Build jump URL based on config
        card_jump = self.config.get("card_jump", "detail")
        if card_jump == "audio":
            jump_url = play_url
        else:
            jump_url = f"https://www.kugou.com/song/#hash={song['hash']}&album_id={song['album_id']}"

        # Build message chain: try music card first, fallback to cover image + text
        chain = []
        card_sent = False
        if play_url and cover_url:
            # 1. OIAPI (free, no token, supports kugoulite for concept mode)
            card_json = await self._sign_card_oiapi(
                play_url, song_name, artist, cover_url, jump_url
            )
            # 2. xianyu API (needs music_card_key, fallback when OIAPI is down)
            if not card_json:
                card_json = await self._sign_card_xianyu(
                    play_url, song_name, artist, cover_url, jump_url
                )
            # 3. Fallback: locally built card (no external dependency)
            if not card_json:
                try:
                    uin = int(event.get_self_id() or 0)
                except (TypeError, ValueError, AttributeError):
                    uin = 0
                card_json = self._build_music_card(
                    song_name, artist, cover_url, play_url, jump_url, uin,
                    song_hash=song.get("hash", "")
                )
            if card_json:
                card_sent = await self._send_card(event, card_json)

        if card_sent:
            # 卡片已通过 OneBot API 直接发出；必须停止事件传播，否则 AstrBot
            # 会把 "/酷狗点歌 N" 继续交给 LLM，AI 会回复「未存在的指令」。
            self._flush_debug()
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        if cover_url:
            chain.append(Image.fromURL(cover_url))
        chain.append(Plain(text + self._flush_debug()))
        yield event.chain_result(chain)