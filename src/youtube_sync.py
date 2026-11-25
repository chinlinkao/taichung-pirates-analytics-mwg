"""YouTube 兩頻道「海盜」比賽影片同步與本地快取模組 (src/youtube_sync.py)。

功能職責：
- 使用 yt-dlp 列舉指定兩個 YouTube 頻道的 /streams 與 /videos 頁籤
- 篩選標題含「海盜」之影片並排除 MVP / SVP 賽後訪問短片
- 僅針對尚未快取開播時間之新影片，以 >= 1.0 秒間隔抓取 watch page
  取得精確開播時間 (liveBroadcastDetails.startTimestamp / publishDate)，
  並統一轉換為台灣時間 (UTC+8) 之 YYYY-MM-DD 與 HH:MM
- 將中繼資料快取至 data/youtube_videos.json，支援離線冪等重跑
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

# 支援直接執行腳本時能匯入根目錄模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_YT_CACHE_PATH = Path("data/youtube_videos.json")
TAIPEI_TZ = timezone(timedelta(hours=8))

YOUTUBE_CHANNELS: list[tuple[str, str, str]] = [
    (
        "UCPV6fPtF1DjGid4uxduEgYA",
        "@justbestgames6021",
        "https://www.youtube.com/@justbestgames6021",
    ),
    (
        "UCZBmgBnKf1GOFEGq1tMdJcg",
        "@就是棒-臺中桃園高雄",
        "https://www.youtube.com/@%E5%B0%B1%E6%98%AF%E6%A3%92-%E8%87%BA%E4%B8%AD%E6%A1%83%E5%9C%92%E9%AB%98%E9%9B%84",
    ),
]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

MIN_MATCH_DURATION_SEC = 180
DEFAULT_INCREMENTAL_LIMIT = 80
REQUEST_INTERVAL_SEC = 1.0


def normalize_text(text: str) -> str:
    """正規化全形字元與臺/台異體字。"""
    return unicodedata.normalize("NFKC", text).replace("臺", "台")


def contains_pirates(title: str) -> bool:
    """判斷影片標題是否包含海盜關鍵字。"""
    return "海盜" in normalize_text(title)


def extract_part_label(title: str) -> Optional[str]:
    """從影片標題抽取分段標記（如 Part 1 / Part 2）。"""
    norm = normalize_text(title)
    m = re.search(r"(?<![A-Za-z])(?:part|pt\.?)\s*(\d+)(?![A-Za-z0-9])", norm, re.IGNORECASE)
    if m:
        return f"Part {int(m.group(1))}"
    m_p = re.search(r"(?:^|[\s\-_(\[])p(\d+)(?:$|[\s\-_)\]])", norm, re.IGNORECASE)
    if m_p:
        return f"Part {int(m_p.group(1))}"
    if re.search(r"(?:上集|上半場|\(上\))", norm):
        return "Part 1"
    if re.search(r"(?:下集|下半場|\(下\))", norm):
        return "Part 2"
    return None


def is_obvious_non_match_title(title: str) -> bool:
    """檢查標題是否明確屬於 MVP / SVP 訪問、頒獎或花絮短片。"""
    norm = normalize_text(title)
    lower = norm.lower()
    if re.search(r"(?<![a-z])(mvp|svp)\s*[-–:：#]", lower):
        return True
    has_vs = bool(re.search(r"(?:(?<![a-z])vs\.?(?![a-z])|(?<![a-z])v\.s\.?(?![a-z])|對決)", lower))
    if not has_vs and any(
        kw in lower for kw in ("mvp", "svp", "頒獎", "訪問", "花絮", "精華", "採訪")
    ):
        return True
    return False


def classify_match_video(
    title: str,
    duration_sec: Optional[int],
    is_live_content: Optional[bool],
) -> bool:
    """判定是否為正式比賽影片（排除 MVP/SVP 賽後訪問與 <3 分鐘短片）。"""
    if not contains_pirates(title):
        return False
    if is_obvious_non_match_title(title):
        return False
    if duration_sec is not None and duration_sec < MIN_MATCH_DURATION_SEC:
        return False

    lower = normalize_text(title).lower()
    has_vs = bool(re.search(r"(?:(?<![a-z])vs\.?(?![a-z])|(?<![a-z])v\.s\.?(?![a-z])|對決)", lower))
    if has_vs:
        return True
    if is_live_content is True and (duration_sec is None or duration_sec >= MIN_MATCH_DURATION_SEC):
        return True
    return False


def _parse_iso_to_taipei(raw_ts: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """將 ISO8601 時間字串轉為 (broadcast_start_utc, taipei_date_iso, taipei_time)。"""
    if not raw_ts or not isinstance(raw_ts, str):
        return (None, None, None)

    text = raw_ts.strip()
    # 純日期格式 YYYY-MM-DD（無法推算開播時刻）
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return (None, text, None)

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return (None, None, None)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    dt_utc = dt.astimezone(timezone.utc)
    dt_tw = dt.astimezone(TAIPEI_TZ)
    return (
        dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        dt_tw.strftime("%Y-%m-%d"),
        dt_tw.strftime("%H:%M"),
    )


def _extract_json_after_marker(html: str, marker_pattern: str) -> Optional[dict[str, Any]]:
    """在 HTML 中以 JSONDecoder.raw_decode 安全擷取指定標記後的 JSON 物件。"""
    m = re.search(marker_pattern, html)
    if not m:
        return None
    start = m.end()
    while start < len(html) and html[start] in " \t\r\n=":
        start += 1
    if start >= len(html) or html[start] != "{":
        return None
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(html, start)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


def fetch_watch_metadata(
    video_id: str,
    client: httpx.Client,
) -> dict[str, Any]:
    """抓取單支 YouTube watch page 以取得精確開播時間、片長與是否為直播。"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    resp = client.get(url, headers=REQUEST_HEADERS, timeout=20.0)
    resp.raise_for_status()

    pr = _extract_json_after_marker(resp.text, r"var\s+ytInitialPlayerResponse\s*=")
    if pr is None:
        pr = _extract_json_after_marker(resp.text, r"ytInitialPlayerResponse\s*=")

    result: dict[str, Any] = {
        "title": None,
        "duration_sec": None,
        "is_live_content": None,
        "broadcast_start_utc": None,
        "broadcast_date_iso": None,
        "broadcast_time": None,
    }
    if not pr:
        return result

    vd = pr.get("videoDetails") or {}
    mf = (pr.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    live_details = mf.get("liveBroadcastDetails") or {}

    if vd.get("title"):
        result["title"] = str(vd["title"]).strip()

    raw_len = vd.get("lengthSeconds") or mf.get("lengthSeconds")
    if raw_len is not None:
        try:
            result["duration_sec"] = int(raw_len)
        except (TypeError, ValueError):
            pass

    if "isLiveContent" in vd:
        result["is_live_content"] = bool(vd["isLiveContent"])

    # 優先使用實際直播開播時間，再退回 publishDate / uploadDate
    for ts_candidate in (
        live_details.get("startTimestamp"),
        mf.get("publishDate"),
        mf.get("uploadDate"),
    ):
        utc_str, date_iso, time_str = _parse_iso_to_taipei(ts_candidate)
        if date_iso:
            result["broadcast_start_utc"] = utc_str
            result["broadcast_date_iso"] = date_iso
            result["broadcast_time"] = time_str
            # 若僅有 date_iso 而尚無時間，仍試著往後找有時間者覆蓋時間
            if time_str is not None:
                break

    return result


def fetch_video_metadata_ytdlp(video_id: str) -> dict[str, Any]:
    """當 watch page HTML 解析不到中繼資料時，以 yt-dlp 單支抓取作備援。"""
    import yt_dlp

    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    ts = info.get("release_timestamp") or info.get("timestamp")
    utc_str: Optional[str] = None
    date_iso: Optional[str] = None
    time_str: Optional[str] = None
    if isinstance(ts, (int, float)) and ts > 0:
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        dt_tw = dt_utc.astimezone(TAIPEI_TZ)
        utc_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        date_iso = dt_tw.strftime("%Y-%m-%d")
        time_str = dt_tw.strftime("%H:%M")
    elif info.get("upload_date"):
        ud = str(info["upload_date"])
        if re.fullmatch(r"\d{8}", ud):
            date_iso = f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}"

    dur = info.get("duration")
    duration_sec = int(dur) if isinstance(dur, (int, float)) else None
    is_live = info.get("was_live")
    if is_live is None and info.get("live_status"):
        is_live = info.get("live_status") in ("was_live", "is_live", "post_live")

    return {
        "title": info.get("title"),
        "duration_sec": duration_sec,
        "is_live_content": bool(is_live) if is_live is not None else None,
        "broadcast_start_utc": utc_str,
        "broadcast_date_iso": date_iso,
        "broadcast_time": time_str,
    }


def list_channel_pirate_entries(
    channel_id: str,
    channel_handle: str,
    base_url: str,
    full: bool = False,
) -> list[dict[str, Any]]:
    """使用 yt-dlp 列舉指定頻道 /streams 與 /videos 中標題含「海盜」的影片。"""
    import yt_dlp

    discovered: dict[str, dict[str, Any]] = {}
    for tab in ("streams", "videos"):
        tab_url = f"{base_url}/{tab}"
        ydl_opts: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "skip_download": True,
        }
        if not full:
            ydl_opts["playlistend"] = DEFAULT_INCREMENTAL_LIMIT

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(tab_url, download=False)
        except Exception as exc:
            print(f"  [WARN] 列舉 {channel_handle}/{tab} 失敗: {exc}")
            continue

        if not info or "entries" not in info:
            continue

        entries = list(info.get("entries") or [])
        pirate_hits = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            vid = entry.get("id")
            title = str(entry.get("title") or "").strip()
            if not vid or not title or not contains_pirates(title):
                continue

            pirate_hits += 1
            raw_dur = entry.get("duration")
            duration_sec = int(raw_dur) if isinstance(raw_dur, (int, float)) else None
            live_status = entry.get("live_status")
            is_live_hint = (
                True
                if (tab == "streams" or live_status in ("was_live", "is_live", "post_live"))
                else None
            )

            existing = discovered.get(vid)
            if existing is None:
                discovered[vid] = {
                    "video_id": vid,
                    "channel_id": channel_id,
                    "channel_handle": channel_handle,
                    "source_tab": tab,
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "duration_sec": duration_sec,
                    "is_live_content": is_live_hint,
                }
            else:
                if existing["duration_sec"] is None and duration_sec is not None:
                    existing["duration_sec"] = duration_sec
                if existing["is_live_content"] is None and is_live_hint is not None:
                    existing["is_live_content"] = is_live_hint

        print(
            f"  - {channel_handle}/{tab}: 掃描 {len(entries)} 筆，含「海盜」 {pirate_hits} 筆"
        )

    return list(discovered.values())


def _extract_search_videos_and_cont(
    obj: Any,
    vids: list[dict[str, Any]],
    conts: list[str],
) -> None:
    """遞迴萃取 YouTube 頻道搜尋回應中的 videoRenderer 與下一頁 token。"""
    if isinstance(obj, dict):
        if "videoRenderer" in obj:
            vr = obj["videoRenderer"]
            vid = vr.get("videoId")
            title = "".join(r.get("text", "") for r in (vr.get("title") or {}).get("runs", []))
            if not title:
                title = (vr.get("title") or {}).get("simpleText", "")
            length_text = (vr.get("lengthText") or {}).get("simpleText", "")
            dur_sec: Optional[int] = None
            if length_text and re.fullmatch(r"\d+(?::\d{2}){1,2}", length_text.strip()):
                parts = [int(p) for p in length_text.strip().split(":")]
                if len(parts) == 2:
                    dur_sec = parts[0] * 60 + parts[1]
                elif len(parts) == 3:
                    dur_sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
            pub_text = (vr.get("publishedTimeText") or {}).get("simpleText", "")
            is_live_hint = True if "直播" in pub_text else None
            if vid and title:
                vids.append(
                    {
                        "video_id": vid,
                        "title": title.strip(),
                        "duration_sec": dur_sec,
                        "is_live_content": is_live_hint,
                    }
                )
        if "continuationItemRenderer" in obj:
            tok = (
                ((obj["continuationItemRenderer"].get("continuationEndpoint") or {}).get("continuationCommand"))
                or {}
            ).get("token")
            if tok:
                conts.append(tok)
        for v in obj.values():
            _extract_search_videos_and_cont(v, vids, conts)
    elif isinstance(obj, list):
        for item in obj:
            _extract_search_videos_and_cont(item, vids, conts)


def list_channel_search_pirate_entries(
    channel_id: str,
    channel_handle: str,
    base_url: str,
    full: bool = False,
) -> list[dict[str, Any]]:
    """透過頻道 /search?query=海盜 補齊 /streams 偶發遺漏的近期或特定狀態直播。"""
    search_url = f"{base_url}/search?query=%E6%B5%B7%E7%9B%9C"
    discovered: dict[str, dict[str, Any]] = {}
    max_pages = 30 if full else 5

    try:
        with httpx.Client(follow_redirects=True, timeout=20.0) as client:
            resp = client.get(search_url, headers=REQUEST_HEADERS)
            resp.raise_for_status()
            html = resp.text

            data = _extract_json_after_marker(html, r"var\s+ytInitialData\s*=")
            if data is None:
                data = _extract_json_after_marker(html, r"ytInitialData\s*=")
            if not data:
                return []

            m_key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
            m_ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)

            vids, conts = [], []
            _extract_search_videos_and_cont(data, vids, conts)
            for item in vids:
                if contains_pirates(item["title"]):
                    vid = item["video_id"]
                    discovered[vid] = {
                        "video_id": vid,
                        "channel_id": channel_id,
                        "channel_handle": channel_handle,
                        "source_tab": "search",
                        "title": item["title"],
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "duration_sec": item["duration_sec"],
                        "is_live_content": item["is_live_content"],
                    }

            page = 1
            seen_tokens: set[str] = set()
            while conts and page < max_pages and m_key and m_ver:
                tok = conts[0]
                if tok in seen_tokens:
                    break
                seen_tokens.add(tok)
                page += 1
                time.sleep(0.5)
                cont_resp = client.post(
                    f"https://www.youtube.com/youtubei/v1/browse?key={m_key.group(1)}",
                    headers={"Content-Type": "application/json", **REQUEST_HEADERS},
                    json={
                        "context": {
                            "client": {
                                "clientName": "WEB",
                                "clientVersion": m_ver.group(1),
                                "hl": "zh-TW",
                                "gl": "TW",
                            }
                        },
                        "continuation": tok,
                    },
                )
                if cont_resp.status_code != 200:
                    break
                j = cont_resp.json()
                vids, conts = [], []
                _extract_search_videos_and_cont(j, vids, conts)
                if not vids:
                    break
                for item in vids:
                    if contains_pirates(item["title"]):
                        vid = item["video_id"]
                        discovered.setdefault(
                            vid,
                            {
                                "video_id": vid,
                                "channel_id": channel_id,
                                "channel_handle": channel_handle,
                                "source_tab": "search",
                                "title": item["title"],
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "duration_sec": item["duration_sec"],
                                "is_live_content": item["is_live_content"],
                            },
                        )

        print(f"  - {channel_handle}/search: 補充掃描含「海盜」 {len(discovered)} 筆")
    except Exception as exc:
        print(f"  [WARN] 掃描 {channel_handle}/search 失敗: {exc}")

    return list(discovered.values())


def load_youtube_cache(cache_path: Path | str = DEFAULT_YT_CACHE_PATH) -> dict[str, Any]:
    """讀取本地 YouTube 影片快取 JSON。"""
    path = Path(cache_path)
    if not path.exists():
        return {"updated_at": None, "videos": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("videos"), dict):
            return data
    except Exception as exc:
        print(f"  [WARN] 讀取快取 {path} 失敗，將以空快取重建: {exc}")
    return {"updated_at": None, "videos": {}}


def save_youtube_cache(
    cache_data: dict[str, Any],
    cache_path: Path | str = DEFAULT_YT_CACHE_PATH,
) -> Path:
    """儲存本地 YouTube 影片快取 JSON（依 video_id 穩定排序）。"""
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_videos = {
        vid: cache_data["videos"][vid]
        for vid in sorted(cache_data["videos"].keys())
    }
    payload = {
        "updated_at": cache_data.get("updated_at"),
        "videos": sorted_videos,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def sync_youtube_videos(
    cache_path: Path | str = DEFAULT_YT_CACHE_PATH,
    full: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """同步兩個 YouTube 頻道含「海盜」之影片至本地快取，具斷網容錯保護。"""
    path = Path(cache_path)
    cache_data = load_youtube_cache(path)
    videos: dict[str, dict[str, Any]] = dict(cache_data.get("videos", {}))

    mode_label = "全量掃描" if full else f"增量掃描 (各頁籤前 {DEFAULT_INCREMENTAL_LIMIT} 筆)"
    print(f"[*] 開始同步 YouTube 頻道海盜影片 ({mode_label})...")

    all_entries_by_vid: dict[str, dict[str, Any]] = {}
    try:
        for channel_id, handle, base_url in YOUTUBE_CHANNELS:
            tab_entries = list_channel_pirate_entries(
                channel_id=channel_id,
                channel_handle=handle,
                base_url=base_url,
                full=full,
            )
            search_entries = list_channel_search_pirate_entries(
                channel_id=channel_id,
                channel_handle=handle,
                base_url=base_url,
                full=full,
            )
            for item in tab_entries + search_entries:
                vid = item["video_id"]
                existing = all_entries_by_vid.get(vid)
                if existing is None:
                    all_entries_by_vid[vid] = item
                else:
                    if existing.get("duration_sec") is None and item.get("duration_sec") is not None:
                        existing["duration_sec"] = item["duration_sec"]
                    if existing.get("is_live_content") is None and item.get("is_live_content") is not None:
                        existing["is_live_content"] = item["is_live_content"]
        all_entries = list(all_entries_by_vid.values())
    except Exception as exc:
        if strict:
            raise
        print(f"  [WARN] YouTube 頻道掃描發生錯誤，沿用本地快取 ({len(videos)} 筆): {exc}")
        return cache_data

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    need_detail: list[dict[str, Any]] = []

    for item in all_entries:
        vid = item["video_id"]
        cached = videos.get(vid)

        if cached is None:
            # 明確短片且已知短於 3 分鐘者，不浪費額外請求抓開播時刻
            if is_obvious_non_match_title(item["title"]) and (
                item["duration_sec"] is not None and item["duration_sec"] < MIN_MATCH_DURATION_SEC
            ):
                videos[vid] = {
                    **item,
                    "broadcast_start_utc": None,
                    "broadcast_date_iso": None,
                    "broadcast_time": None,
                    "part_label": extract_part_label(item["title"]),
                    "is_match_video": 0,
                    "fetched_at": now_utc,
                }
            else:
                need_detail.append(item)
        else:
            # 若已有快取但標題更新或尚缺時間資訊（且非已判定短片），補齊資訊
            cached["title"] = item["title"]
            cached["channel_handle"] = item["channel_handle"]
            cached["part_label"] = extract_part_label(item["title"])
            if cached.get("duration_sec") is None and item.get("duration_sec") is not None:
                cached["duration_sec"] = item["duration_sec"]
            if cached.get("is_match_video") == 1 and not cached.get("broadcast_date_iso"):
                need_detail.append(item)
            elif (
                cached.get("broadcast_date_iso") is None
                and not is_obvious_non_match_title(item["title"])
                and (cached.get("duration_sec") is None or cached["duration_sec"] >= MIN_MATCH_DURATION_SEC)
            ):
                need_detail.append(item)

    if need_detail:
        print(f"[*] 補全 {len(need_detail)} 筆新增／缺漏開播時間之影片中繼資料（間隔 >= {REQUEST_INTERVAL_SEC:.1f}s）...")

    last_req_ts = 0.0
    failed_count = 0
    with httpx.Client(follow_redirects=True) as client:
        for idx, item in enumerate(need_detail, start=1):
            vid = item["video_id"]
            # 若先判定明確屬 MVP/SVP 標題（即使 flat 無片長），直接標記排除，節省大量請求
            if is_obvious_non_match_title(item["title"]):
                videos[vid] = {
                    **item,
                    "broadcast_start_utc": None,
                    "broadcast_date_iso": None,
                    "broadcast_time": None,
                    "part_label": extract_part_label(item["title"]),
                    "is_match_video": 0,
                    "fetched_at": now_utc,
                }
                continue

            elapsed = time.monotonic() - last_req_ts
            if last_req_ts > 0 and elapsed < REQUEST_INTERVAL_SEC:
                time.sleep(REQUEST_INTERVAL_SEC - elapsed)

            meta: dict[str, Any] = {}
            try:
                last_req_ts = time.monotonic()
                meta = fetch_watch_metadata(vid, client)
                if not meta.get("broadcast_date_iso") or meta.get("duration_sec") is None:
                    fallback = fetch_video_metadata_ytdlp(vid)
                    for k, v in fallback.items():
                        if meta.get(k) is None and v is not None:
                            meta[k] = v
            except Exception as exc:
                failed_count += 1
                if strict:
                    raise
                print(f"  [WARN] 取得影片 {vid} 詳細資訊失敗 ({exc})")

            title = meta.get("title") or item["title"]
            duration_sec = (
                meta.get("duration_sec")
                if meta.get("duration_sec") is not None
                else item.get("duration_sec")
            )
            is_live_content = (
                meta.get("is_live_content")
                if meta.get("is_live_content") is not None
                else item.get("is_live_content")
            )
            is_match = 1 if classify_match_video(title, duration_sec, is_live_content) else 0

            videos[vid] = {
                "video_id": vid,
                "channel_id": item["channel_id"],
                "channel_handle": item["channel_handle"],
                "source_tab": item["source_tab"],
                "title": title,
                "url": item["url"],
                "duration_sec": duration_sec,
                "is_live_content": (
                    1 if is_live_content is True else (0 if is_live_content is False else None)
                ),
                "broadcast_start_utc": meta.get("broadcast_start_utc"),
                "broadcast_date_iso": meta.get("broadcast_date_iso"),
                "broadcast_time": meta.get("broadcast_time"),
                "part_label": extract_part_label(title),
                "is_match_video": is_match,
                "fetched_at": now_utc,
            }

            if idx % 20 == 0 or idx == len(need_detail):
                print(f"  - 中繼資料進度: [{idx}/{len(need_detail)}]")

    # 對既有快取全部重新套用最新分類規則（確保規則調整後離線也一致）
    for vid, v in videos.items():
        live_flag = (
            True if v.get("is_live_content") == 1 else (False if v.get("is_live_content") == 0 else None)
        )
        v["part_label"] = extract_part_label(v["title"])
        v["is_match_video"] = 1 if classify_match_video(v["title"], v.get("duration_sec"), live_flag) else 0

    result_cache = {"updated_at": now_utc, "videos": videos}
    save_youtube_cache(result_cache, path)

    match_cnt = sum(1 for v in videos.values() if v.get("is_match_video") == 1)
    excluded_cnt = len(videos) - match_cnt
    print(
        f"[+] YouTube 快取更新完成: {path} "
        f"(總海盜影片 {len(videos)} 筆：比賽影片 {match_cnt} 筆、排除短片 {excluded_cnt} 筆"
        + (f"、失敗 {failed_count} 筆" if failed_count else "")
        + ")"
    )
    return result_cache


def main() -> None:
    parser = argparse.ArgumentParser(description="同步兩個就是棒 YouTube 頻道的台中海盜比賽影片")
    parser.add_argument(
        "--full",
        action="store_true",
        help="完整掃描兩頻道歷史所有直播與影片（預設為增量掃描最新 80 筆）",
    )
    parser.add_argument(
        "--cache",
        type=str,
        default=str(DEFAULT_YT_CACHE_PATH),
        help="指定 YouTube 影片快取 JSON 路徑 (預設: data/youtube_videos.json)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="遇到網路或解析錯誤時立即中止並回傳失敗",
    )
    args = parser.parse_args()
    sync_youtube_videos(cache_path=args.cache, full=args.full, strict=args.strict)


if __name__ == "__main__":
    main()
