"""YouTube 比賽影片與賽程智慧配對及報表匯出模組 (src/youtube_match.py)。

功能職責：
- 純離線讀取本地快取 data/youtube_videos.json 與 SQLite data/pirates.db
- 依據開播日期 (±1 天)、層級 (U8/U10/U12/U15)、組別／賽制、雙方隊名正規化
  與開賽時間進行多維度加權配對（以 boxscore_url 為真實場次單位，海盜內戰同時綁定雙視角）
- 支援 data/youtube_manual_overrides.csv 人工修正（bind / unbind / ignore）
- 將影片與配對結果寫入 youtube_videos / match_videos 資料表
- 匯出全部「海盜」比賽影片對照總表 data/youtube_match_mapping_report.csv (utf-8-sig)
"""

import argparse
import csv
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

# 支援直接執行腳本時能匯入根目錄模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import DEFAULT_DB_PATH, get_db_connection, init_db
from src.youtube_sync import DEFAULT_YT_CACHE_PATH, load_youtube_cache

DEFAULT_OVERRIDES_PATH = Path("data/youtube_manual_overrides.csv")
DEFAULT_REPORT_PATH = Path("data/youtube_match_mapping_report.csv")

OVERRIDES_HEADERS = ["action", "video_id", "boxscore_url", "label", "note"]

# 隊名正規化等價別名映射（皆先經 normalize_team_name 正規化後比對）
TEAM_ALIAS_GROUPS: list[tuple[str, ...]] = [
    ("台中小海盜a", "台中小海盜白a"),
    ("台中小海盜b", "台中小海盜白b"),
    ("台中雷爵", "北勢雷爵"),
    ("台中戰狼", "戰狼"),
    ("台中道奇", "台中小道奇"),
    ("健行小牛u12大", "健行小牛"),
    ("台中向上u10", "台中向上"),
    ("台中向上u13", "台中向上"),
    ("台中向上a", "台中向上"),
]

ALIAS_LOOKUP: dict[str, set[str]] = {}
for group in TEAM_ALIAS_GROUPS:
    group_set = set(group)
    for item in group:
        ALIAS_LOOKUP[item] = group_set


@dataclass
class MatchEvent:
    """代表一場真實比賽（海盜內戰的 2 筆 match_id 會共用同一 MatchEvent）。"""

    event_key: str
    boxscore_url: Optional[str]
    league_id: int
    league_title: str
    year: int
    season: str
    tournament_type: str
    age_group: str
    division: str
    match_date_iso: Optional[str]
    match_time: Optional[str]
    away_team: str
    home_team: str
    away_score: Optional[int]
    home_score: Optional[int]
    is_civil_war: bool
    match_rows: list[dict[str, Any]] = field(default_factory=list)


def _norm_basic(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("臺", "台").strip()


def normalize_team_name(name: str) -> str:
    """將隊名統一轉換為可跨格式比對的正規化字串。"""
    s = _norm_basic(name).lower()
    s = re.sub(r"[\s()（）\-_・·]+", "", s)
    return s


def strip_team_suffix(norm_name: str) -> str:
    """移除常見分隊尾綴 (如 a/b/j/m/major/minor/紅/藍/灰/黃) 以比對基底校隊名。"""
    s = re.sub(r"(?:major|minor|u\d+[大中小]?|[abjm紅藍灰黃白黑])$", "", norm_name)
    return s or norm_name


def extract_pirate_color(norm_name: str) -> Optional[str]:
    """抽取海盜隊之白/黑顏色標記。"""
    if "海盜" not in norm_name:
        return None
    if "白" in norm_name:
        return "白"
    if "黑" in norm_name:
        return "黑"
    return None


def extract_pirate_subtag(norm_name: str) -> Optional[str]:
    """抽取海盜隊之 A/B 組別標記。"""
    if "海盜" not in norm_name:
        return None
    if norm_name.endswith("a"):
        return "a"
    if norm_name.endswith("b"):
        return "b"
    return None


def parse_video_title(title: str) -> dict[str, Any]:
    """從 YouTube 標題解析層級、組別、賽事型態與對戰雙隊。"""
    norm = _norm_basic(title)

    # 抽取所有【...】或 [...] 標籤內容
    bracket_chunks = re.findall(r"[【\[]([^】\]]+)[】\]]", norm)
    bracket_text = " ".join(bracket_chunks)
    full_upper = norm.upper()

    age_group: Optional[str] = None
    m_age = re.search(r"(?<![A-Z])U(8|10|12|15)(?![A-Z0-9])", full_upper)
    if m_age:
        age_group = f"U{m_age.group(1)}"

    division: Optional[str] = None
    if "大聯盟" in norm:
        division = "大聯盟"
    elif "小聯盟" in norm:
        division = "小聯盟"

    tournament_type: Optional[str] = None
    if "台灣大賽" in norm or "臺灣大賽" in title:
        tournament_type = "台灣大賽"
    elif any(k in norm for k in ("季後賽", "冠軍戰", "季軍戰", "四強", "八強", "決賽", "準決賽")):
        tournament_type = "季後賽"

    # 清除括號賽事標頭，準備解析對戰雙隊
    body = re.sub(r"[【\[][^】\]]+[】\]]", " ", norm)
    # 清除常見賽事階段與場次編號前綴（如 四強賽 G7、冠軍戰G8、循環賽A組 G2、G25）
    body = re.sub(
        r"(?:四強賽|八強賽|冠軍戰|季軍戰|準決賽|決賽|循環賽[A-Za-z0-9]*組?|[A-Za-z]組)",
        " ",
        body,
    )
    body = re.sub(r"(?<![A-Za-z])G\d+(?![A-Za-z0-9])", " ", body, flags=re.IGNORECASE)
    # 清除尾綴 part 1 / p1 / 上下集 / 直播 / 重播 等字樣
    body = re.sub(
        r"(?<![A-Za-z])(?:part|pt\.?)\s*\d+.*$",
        "",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(r"(?:上集|下集|上半場|下半場|\(上\)|\(下\)|重播|精華).*$", "", body)

    parts = re.split(r"\s*(?:(?<![A-Za-z])v\.?s\.?(?![A-Za-z])|對決)\s*", body, maxsplit=1, flags=re.IGNORECASE)
    team_a: Optional[str] = None
    team_b: Optional[str] = None
    if len(parts) == 2:
        team_a = parts[0].strip(" -–—:：|,，。")
        team_b = parts[1].strip(" -–—:：|,，。")
        # 移除隊名後半部可能夾帶的場地／日期間雜訊
        team_b = re.split(r"\s+(?:\d{4}[/-]\d{1,2}[/-]\d{1,2}|#\S+)", team_b)[0].strip()

    return {
        "bracket_text": bracket_text,
        "age_group": age_group,
        "division": division,
        "tournament_type": tournament_type,
        "team_a": team_a or None,
        "team_b": team_b or None,
    }


def _parse_hhmm_minutes(time_str: Optional[str]) -> Optional[int]:
    if not time_str:
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", time_str)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _teams_match_score(vid_team: str, db_team: str) -> tuple[int, bool]:
    """比對單一對手隊名相似度，回傳 (分數 0..30, 是否判定相容)。"""
    nv = normalize_team_name(vid_team)
    nd = normalize_team_name(db_team)
    if not nv or not nd:
        return (0, False)
    if nv == nd:
        return (30, True)

    # 別名群組精準匹配
    if nd in ALIAS_LOOKUP.get(nv, set()):
        return (26, True)

    # 包含關係（例如 熊快樂 vs 熊快樂社區、仁美 vs 台中仁美）
    if len(nv) >= 2 and len(nd) >= 2 and (nv in nd or nd in nv):
        return (24, True)

    # 去除分隊尾綴後比對（如 台中仁美J vs 台中仁美）
    sv = strip_team_suffix(nv)
    sd = strip_team_suffix(nd)
    if sv == sd or sd in ALIAS_LOOKUP.get(sv, set()):
        return (21, True)

    ratio = SequenceMatcher(None, nv, nd).ratio()
    if ratio >= 0.72:
        return (round(20 * ratio), True)

    return (0, False)


def _pirate_side_score(vid_pirate: str, db_pirate: str) -> tuple[int, bool]:
    """比對海盜隊自身名稱與顏色/分組，回傳 (分數 0..15, 是否相容)。"""
    nv = normalize_team_name(vid_pirate)
    nd = normalize_team_name(db_pirate)
    if nv == nd or nd in ALIAS_LOOKUP.get(nv, set()):
        return (15, True)

    cv = extract_pirate_color(nv)
    cd = extract_pirate_color(nd)
    if cv and cd and cv != cd:
        return (0, False)

    sv = extract_pirate_subtag(nv)
    sd = extract_pirate_subtag(nd)
    if sv and sd and sv != sd:
        return (0, False)

    # Major / 一軍與黑隊分屬不同編制，不可混配
    if ("major" in nv or nv.endswith("a")) and cd == "黑":
        return (0, False)
    if ("major" in nd or nd.endswith("a")) and cv == "黑":
        return (0, False)

    # 同屬小海盜 vs U15海盜（不可混配不同年齡層）
    if ("小海盜" in nv and "小海盜" not in nd) or ("小海盜" not in nv and "小海盜" in nd):
        if cv != cd or cv is None:
            return (6, True)

    return (10, True)


def score_video_against_event(
    video: dict[str, Any],
    parsed: dict[str, Any],
    event: MatchEvent,
) -> Optional[tuple[float, str]]:
    """計算單支影片對單場比賽事件的配對分數；不相容則回傳 None。"""
    v_date_str = video.get("broadcast_date_iso")
    m_date_str = event.match_date_iso
    if not v_date_str or not m_date_str:
        return None

    try:
        date_diff = (date.fromisoformat(v_date_str) - date.fromisoformat(m_date_str)).days
    except ValueError:
        return None

    # 嚴格限制於開播日期與賽程日期 ±1 天內
    if abs(date_diff) > 1:
        return None

    # 層級硬性防呆：若影片明確寫 U10 而賽事為 U12，直接排除
    v_age = parsed.get("age_group")
    if v_age and event.age_group and v_age != event.age_group:
        return None

    # 解析影片中的對戰雙隊
    team_a = parsed.get("team_a")
    team_b = parsed.get("team_b")
    if not team_a or not team_b:
        return None

    a_has_p = "海盜" in _norm_basic(team_a)
    b_has_p = "海盜" in _norm_basic(team_b)
    if not a_has_p and not b_has_p:
        return None

    vid_is_civil = a_has_p and b_has_p
    if vid_is_civil != event.is_civil_war:
        return None

    score = 0.0
    reasons: list[str] = []

    # 1. 日期分數（同日 +40，隔日 +20）
    if date_diff == 0:
        score += 40
        reasons.append("同日")
    else:
        score += 20
        reasons.append(f"日差{date_diff:+d}")

    # 2. 層級分數（+15）
    if v_age and v_age == event.age_group:
        score += 15
        reasons.append(v_age)

    # 3. 賽制與大小聯盟（最高 +15）
    v_tourney = parsed.get("tournament_type")
    if v_tourney == "台灣大賽":
        if event.tournament_type == "台灣大賽":
            score += 10
            reasons.append("台灣大賽")
        else:
            score -= 15
    elif v_tourney == "季後賽":
        if event.tournament_type in ("季後賽", "台灣大賽"):
            score += 5
            reasons.append("季後賽")

    v_div = parsed.get("division")
    if v_div and event.division in ("大聯盟", "小聯盟"):
        if v_div == event.division:
            score += 5
            reasons.append(v_div)
        else:
            score -= 15

    # 4. 雙方隊名匹配（最高 +45）
    if vid_is_civil:
        # 海盜內戰：檢查是否同順序，再+40~45
        p1, ok1 = _pirate_side_score(team_a, event.away_team)
        p2, ok2 = _pirate_side_score(team_b, event.home_team)
        p1_rev, ok1_rev = _pirate_side_score(team_a, event.home_team)
        p2_rev, ok2_rev = _pirate_side_score(team_b, event.away_team)
        if ok1 and ok2:
            score += 45
            reasons.append("內戰雙隊吻合")
        elif ok1_rev and ok2_rev:
            score += 40
            reasons.append("內戰雙隊吻合(主客互調)")
        else:
            return None
    else:
        # 一般對戰：區分海盜方與對手方
        vid_pirate = team_a if a_has_p else team_b
        vid_opp = team_b if a_has_p else team_a

        event_away_has_p = "海盜" in _norm_basic(event.away_team)
        if event_away_has_p:
            db_pirate, db_opp = event.away_team, event.home_team
            same_order = a_has_p
        else:
            db_pirate, db_opp = event.home_team, event.away_team
            same_order = b_has_p

        opp_pts, opp_ok = _teams_match_score(vid_opp, db_opp)
        if not opp_ok:
            return None
        # 若為隔日補上傳，要求對手名稱必須高度一致 (>=21)
        if abs(date_diff) == 1 and opp_pts < 21:
            return None

        pirate_pts, pirate_ok = _pirate_side_score(vid_pirate, db_pirate)
        if not pirate_ok:
            return None

        order_bonus = 5 if same_order else 0
        score += opp_pts + pirate_pts + order_bonus
        reasons.append(f"對手:{opp_pts}+海盜:{pirate_pts}")

    # 5. 開賽時間接近度（最高 +12）
    v_min = _parse_hhmm_minutes(video.get("broadcast_time"))
    m_min = _parse_hhmm_minutes(event.match_time)
    if v_min is not None and m_min is not None:
        diff_min = abs(v_min - m_min)
        if diff_min <= 45:
            score += 12
            reasons.append(f"時間差{diff_min}m")
        elif diff_min <= 105:
            score += 5
            reasons.append(f"時間差{diff_min}m")
        elif diff_min > 180:
            score -= 5

    # 保留未截斷原始總分供候選比較，避免高分皆被壓縮在 100.0 而無法消歧義
    return (round(max(0.0, score), 1), " / ".join(reasons))


def load_match_events(conn: sqlite3.Connection) -> list[MatchEvent]:
    """從 SQLite 讀取所有賽程並依真實比賽事件分組（合併海盜內戰雙視角）。"""
    rows = conn.execute(
        """
        SELECT m.id, m.team_id, m.league_id, m.match_date, m.match_date_iso, m.match_time,
               m.away_team, m.home_team, m.away_score, m.home_score,
               m.venue, m.boxscore_url,
               l.league_title, l.year, l.season, l.tournament_type, l.age_group, l.division
        FROM matches m
        JOIN leagues l ON m.league_id = l.league_id
        ORDER BY m.id ASC;
        """
    ).fetchall()

    events_by_key: dict[str, MatchEvent] = {}
    for row in rows:
        d = dict(row)
        box_url = (d.get("boxscore_url") or "").strip() or None
        if box_url:
            event_key = box_url
        else:
            event_key = (
                f"{d['league_id']}|{d['match_date_iso'] or ''}|{d['match_time'] or ''}"
                f"|{d['away_team']}|{d['home_team']}"
            )

        ev = events_by_key.get(event_key)
        if ev is None:
            away_has_p = "海盜" in _norm_basic(d["away_team"])
            home_has_p = "海盜" in _norm_basic(d["home_team"])
            ev = MatchEvent(
                event_key=event_key,
                boxscore_url=box_url,
                league_id=d["league_id"],
                league_title=d["league_title"],
                year=d["year"],
                season=d["season"],
                tournament_type=d["tournament_type"],
                age_group=d["age_group"],
                division=d["division"],
                match_date_iso=d["match_date_iso"],
                match_time=d["match_time"],
                away_team=d["away_team"],
                home_team=d["home_team"],
                away_score=d["away_score"],
                home_score=d["home_score"],
                is_civil_war=(away_has_p and home_has_p),
            )
            events_by_key[event_key] = ev
        ev.match_rows.append(d)

    return list(events_by_key.values())


def ensure_overrides_template(overrides_path: Path) -> None:
    """若人工修正覆寫檔不存在，建立含 UTF-8 BOM 表頭的空白範本。"""
    if overrides_path.exists():
        return
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    with open(overrides_path, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(OVERRIDES_HEADERS)


def load_manual_overrides(overrides_path: Path) -> list[dict[str, str]]:
    """讀取人工修正覆寫 CSV (utf-8-sig)。"""
    ensure_overrides_template(overrides_path)
    entries: list[dict[str, str]] = []
    with open(overrides_path, mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            action = (row.get("action") or "").strip().lower()
            video_id = (row.get("video_id") or "").strip()
            if not action or not video_id or action.startswith("#"):
                continue
            entries.append(
                {
                    "action": action,
                    "video_id": video_id,
                    "boxscore_url": (row.get("boxscore_url") or "").strip(),
                    "label": (row.get("label") or "").strip(),
                    "note": (row.get("note") or "").strip(),
                }
            )
    return entries


def _format_duration(sec: Optional[int]) -> str:
    if sec is None:
        return ""
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _part_sort_num(part_label: Optional[str]) -> int:
    if not part_label:
        return 99
    m = re.search(r"(\d+)", part_label)
    return int(m.group(1)) if m else 99


def match_and_store_videos(
    db_path: Path | str = DEFAULT_DB_PATH,
    cache_path: Path | str = DEFAULT_YT_CACHE_PATH,
    overrides_path: Path | str = DEFAULT_OVERRIDES_PATH,
    report_path: Path | str = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    """執行離線影片配對、寫入 SQLite 並匯出對照報表 CSV。"""
    cache_file = Path(cache_path)
    if not cache_file.exists():
        print(f"  [WARN] 找不到 YouTube 快取檔 {cache_file}，跳過影片配對。")
        return {"total_videos": 0, "matched_videos": 0, "matched_matches": 0}

    init_db(db_path)
    cache_data = load_youtube_cache(cache_file)
    videos_map: dict[str, dict[str, Any]] = cache_data.get("videos", {})
    overrides = load_manual_overrides(Path(overrides_path))

    ignore_vids = {o["video_id"] for o in overrides if o["action"] == "ignore"}
    unbind_pairs = {
        (o["video_id"], o["boxscore_url"])
        for o in overrides
        if o["action"] == "unbind"
    }
    bind_overrides = [o for o in overrides if o["action"] == "bind" and o["boxscore_url"]]

    conn = get_db_connection(db_path)
    try:
        events = load_match_events(conn)
        events_by_key = {ev.event_key: ev for ev in events}

        # 先將所有快取中的海盜影片寫入 youtube_videos 表
        with conn:
            for vid, v in videos_map.items():
                conn.execute(
                    """
                    INSERT INTO youtube_videos (
                        video_id, channel_id, channel_handle, source_tab, title, url,
                        duration_sec, is_live_content, broadcast_start_utc,
                        broadcast_date_iso, broadcast_time, part_label, is_match_video, fetched_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(video_id) DO UPDATE SET
                        channel_id=excluded.channel_id,
                        channel_handle=excluded.channel_handle,
                        source_tab=excluded.source_tab,
                        title=excluded.title,
                        url=excluded.url,
                        duration_sec=excluded.duration_sec,
                        is_live_content=excluded.is_live_content,
                        broadcast_start_utc=excluded.broadcast_start_utc,
                        broadcast_date_iso=excluded.broadcast_date_iso,
                        broadcast_time=excluded.broadcast_time,
                        part_label=excluded.part_label,
                        is_match_video=excluded.is_match_video,
                        fetched_at=excluded.fetched_at;
                    """,
                    (
                        vid,
                        v["channel_id"],
                        v["channel_handle"],
                        v.get("source_tab"),
                        v["title"],
                        v["url"],
                        v.get("duration_sec"),
                        v.get("is_live_content"),
                        v.get("broadcast_start_utc"),
                        v.get("broadcast_date_iso"),
                        v.get("broadcast_time"),
                        v.get("part_label"),
                        int(v.get("is_match_video", 1)),
                        v.get("fetched_at"),
                    ),
                )

            # 配對所有正式比賽影片 (is_match_video == 1)
            match_videos_list = [
                v for v in videos_map.values() if int(v.get("is_match_video", 1)) == 1
            ]
            # 依日期時間新到舊排序以輸出整齊報表
            match_videos_list.sort(
                key=lambda x: (
                    x.get("broadcast_date_iso") or "",
                    x.get("broadcast_time") or "",
                    x["video_id"],
                ),
                reverse=True,
            )

            # event_key -> list of binding dicts
            event_bindings: dict[str, list[dict[str, Any]]] = {}
            report_rows: list[dict[str, Any]] = []

            for v in match_videos_list:
                vid = v["video_id"]
                if vid in ignore_vids:
                    report_rows.append(
                        {
                            "status": "ignored",
                            "confidence": "",
                            "video": v,
                            "event": None,
                            "note": "人工設定 ignore",
                        }
                    )
                    continue

                parsed = parse_video_title(v["title"])
                scored: list[tuple[float, MatchEvent, str]] = []
                for ev in events:
                    res = score_video_against_event(v, parsed, ev)
                    if res is not None:
                        scored.append((res[0], ev, res[1]))

                scored.sort(key=lambda item: item[0], reverse=True)

                chosen_ev: Optional[MatchEvent] = None
                chosen_conf: Optional[float] = None
                status = "unmatched"
                note = ""

                if scored:
                    top_score, top_ev, top_reason = scored[0]
                    second_score = scored[1][0] if len(scored) > 1 else -1.0
                    if top_score >= 75.0 and (len(scored) == 1 or (top_score - second_score) >= 8.0):
                        if (vid, top_ev.event_key) in unbind_pairs or (vid, "") in unbind_pairs:
                            status = "unbound"
                            note = "人工設定 unbind"
                        else:
                            status = "matched"
                            chosen_ev = top_ev
                            chosen_conf = min(100.0, top_score)
                            note = top_reason
                    elif top_score >= 55.0:
                        status = "ambiguous"
                        chosen_ev = top_ev
                        chosen_conf = min(100.0, top_score)
                        cand_desc = f"次高 {min(100.0, second_score)}" if len(scored) > 1 else ""
                        note = f"需人工確認 ({top_reason}{'；' + cand_desc if cand_desc else ''})"
                    else:
                        note = f"最高分不足 ({top_score}: {top_reason})"
                else:
                    if not parsed.get("team_a") or not parsed.get("team_b"):
                        note = "標題無法解析出 vs 對戰雙隊"
                    elif not v.get("broadcast_date_iso"):
                        note = "缺少開播日期"
                    else:
                        note = "資料庫無 ±1 天同層級符合對手之賽程（可能為未納入CSV聯賽或友誼賽）"

                if status == "matched" and chosen_ev is not None:
                    event_bindings.setdefault(chosen_ev.event_key, []).append(
                        {
                            "video": v,
                            "confidence": chosen_conf,
                            "source": "auto",
                            "custom_label": None,
                        }
                    )

                report_rows.append(
                    {
                        "status": status,
                        "confidence": chosen_conf if chosen_conf is not None else "",
                        "video": v,
                        "event": chosen_ev,
                        "note": note,
                    }
                )

            # 套用人工強制綁定 (bind)
            manual_bound_count = 0
            for ov in bind_overrides:
                vid = ov["video_id"]
                box_url = ov["boxscore_url"]
                if vid not in videos_map:
                    print(f"  [WARN] 人工覆寫 bind 找不到 video_id: {vid}")
                    continue
                ev = events_by_key.get(box_url)
                if ev is None:
                    print(f"  [WARN] 人工覆寫 bind 找不到賽程 boxscore_url: {box_url}")
                    continue
                existing_list = event_bindings.setdefault(ev.event_key, [])
                existing_list = [b for b in existing_list if b["video"]["video_id"] != vid]
                existing_list.append(
                    {
                        "video": videos_map[vid],
                        "confidence": 100.0,
                        "source": "manual",
                        "custom_label": ov["label"] or None,
                    }
                )
                event_bindings[ev.event_key] = existing_list
                manual_bound_count += 1
                for rr in report_rows:
                    if rr["video"]["video_id"] == vid:
                        rr["status"] = "matched_manual"
                        rr["confidence"] = 100.0
                        rr["event"] = ev
                        rr["note"] = ov["note"] or "人工手動綁定"

            # 重建 match_videos 關聯表（確保冪等且乾淨）
            conn.execute("DELETE FROM match_videos;")

            total_links = 0
            matched_match_ids: set[int] = set()
            matched_video_ids: set[str] = set()

            for ev_key, bindings in event_bindings.items():
                ev = events_by_key[ev_key]
                # 排序同一場的多支影片：Part 序號 -> 開播時間 -> video_id
                bindings.sort(
                    key=lambda b: (
                        _part_sort_num(b["video"].get("part_label")),
                        b["video"].get("broadcast_start_utc") or "",
                        b["video"]["video_id"],
                    )
                )
                multi = len(bindings) > 1
                for idx, b in enumerate(bindings):
                    v = b["video"]
                    if b["custom_label"]:
                        label = b["custom_label"]
                    elif multi:
                        part = v.get("part_label") or f"Part {idx + 1}"
                        label = f"比賽影片 ({part})"
                    else:
                        label = "比賽影片"

                    for m_row in ev.match_rows:
                        conn.execute(
                            """
                            INSERT INTO match_videos (
                                match_id, video_id, label, match_confidence, match_source, sort_order
                            )
                            VALUES (?, ?, ?, ?, ?, ?);
                            """,
                            (
                                m_row["id"],
                                v["video_id"],
                                label,
                                b["confidence"],
                                b["source"],
                                idx,
                            ),
                        )
                        total_links += 1
                        matched_match_ids.add(m_row["id"])
                        matched_video_ids.add(v["video_id"])

        # 輸出完整 Excel 可開報表 CSV (utf-8-sig)
        out_report = Path(report_path)
        out_report.parent.mkdir(parents=True, exist_ok=True)
        with open(out_report, mode="w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "狀態",
                    "信心分數",
                    "頻道",
                    "開播日期",
                    "開播時間",
                    "片長",
                    "影片標題",
                    "影片連結",
                    "video_id",
                    "對應聯賽",
                    "對應日期",
                    "對應時間",
                    "客隊",
                    "主隊",
                    "比數",
                    "boxscore_url",
                    "備註",
                ]
            )
            for rr in report_rows:
                v = rr["video"]
                ev = rr["event"]
                score_text = (
                    f"{ev.away_score}:{ev.home_score}"
                    if ev and ev.away_score is not None and ev.home_score is not None
                    else ""
                )
                writer.writerow(
                    [
                        rr["status"],
                        rr["confidence"],
                        v.get("channel_handle", ""),
                        v.get("broadcast_date_iso") or "",
                        v.get("broadcast_time") or "",
                        _format_duration(v.get("duration_sec")),
                        v.get("title", ""),
                        v.get("url", ""),
                        v["video_id"],
                        ev.league_title if ev else "",
                        ev.match_date_iso or "" if ev else "",
                        ev.match_time or "" if ev else "",
                        ev.away_team if ev else "",
                        ev.home_team if ev else "",
                        score_text,
                        ev.boxscore_url or "" if ev else "",
                        rr["note"],
                    ]
                )

        total_matches_cnt = conn.execute("SELECT COUNT(*) FROM matches;").fetchone()[0]
        ambiguous_cnt = sum(1 for r in report_rows if r["status"] == "ambiguous")
        unmatched_cnt = sum(1 for r in report_rows if r["status"] == "unmatched")
        print(
            f"[+] YouTube 賽程配對完成: 比賽影片 {len(match_videos_list)} 支 | "
            f"成功綁定影片 {len(matched_video_ids)} 支 -> 涵蓋 {len(matched_match_ids)}/{total_matches_cnt} 筆賽程列 "
            f"(共 {total_links} 筆關聯；待確認={ambiguous_cnt}，未匹配={unmatched_cnt}"
            + (f"，人工={manual_bound_count}" if manual_bound_count else "")
            + ")"
        )
        print(f"  - 配對總表已匯出: {out_report}")
        return {
            "total_videos": len(match_videos_list),
            "matched_videos": len(matched_video_ids),
            "matched_matches": len(matched_match_ids),
            "total_matches": total_matches_cnt,
            "ambiguous": ambiguous_cnt,
            "unmatched": unmatched_cnt,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="台中小海盜 YouTube 比賽影片與賽程離線配對器")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB_PATH), help="SQLite 資料庫路徑")
    parser.add_argument("--cache", type=str, default=str(DEFAULT_YT_CACHE_PATH), help="YouTube 快取 JSON 路徑")
    parser.add_argument("--overrides", type=str, default=str(DEFAULT_OVERRIDES_PATH), help="人工覆寫 CSV 路徑")
    parser.add_argument("--report", type=str, default=str(DEFAULT_REPORT_PATH), help="配對報表 CSV 路徑")
    args = parser.parse_args()
    match_and_store_videos(
        db_path=args.db,
        cache_path=args.cache,
        overrides_path=args.overrides,
        report_path=args.report,
    )


if __name__ == "__main__":
    main()
