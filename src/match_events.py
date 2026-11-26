"""官網單場攻守數據與逐打席文字紀錄解析模組 (src/match_events.py)。

職責：
- 遵守爬蟲安全紅線：對 myscore.games 採單線程循序請求，每次請求間隔 >= 1.6 秒。
- 將單場 show_record.php 與 show_text.php 快取至 data/raw_html/matches/。
- 依據 team_rosters 與 show_record.php（背號 + 遮罩姓名）精準歸戶 player_uid，
  嚴格排除 coach_pitch_888，並區分同隊同名選手。
- 將解析後之先發打序與逐半局事件寫入 SQLite (match_lineups, match_events)，
  同時萃取各出場球員的打席 (batting)、投球 (pitching) 與守備 (fielding) 事件清單。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.db import DEFAULT_DB_PATH, get_db_connection
from src.export_json import resolve_pirate_side

COACH_PITCH_UID = "coach_pitch_888"
DEFAULT_MATCH_HTML_DIR = Path("data/raw_html/matches")
REQUEST_INTERVAL_SEC = 1.6

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

POSITION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "投": ("投手",),
    "捕": ("捕手",),
    "一": ("一壘手",),
    "二": ("二壘手",),
    "三": ("三壘手",),
    "游": ("游擊手",),
    "左": ("左外野手",),
    "中": ("中外野手",),
    "右": ("右外野手",),
}


def extract_site_match_id(url: Optional[str]) -> Optional[str]:
    """從 myscore.games 單場網址擷取 id 參數。"""
    if not url:
        return None
    vals = parse_qs(urlparse(url).query).get("id")
    return vals[0] if vals else None


def clean_text(text: str) -> str:
    """正規化空白字元。"""
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()


def normalize_team_name(name: str) -> str:
    """正規化隊名以利比對。"""
    return re.sub(r"\s+", "", name).replace("臺", "台")


def split_jersey_and_name(raw: str) -> tuple[Optional[str], str]:
    """解析 '79 高〇展' 或 '高〇展' 欄位。"""
    txt = clean_text(raw)
    m = re.match(r"^(\d+)\s+(.+)$", txt)
    if m:
        return m.group(1), m.group(2).strip()
    return None, txt


def _find_team_key(mapping: dict[str, Any], target_team_name: str) -> Optional[str]:
    """比對球隊名稱（容許空白與臺/台差異）。"""
    target_norm = normalize_team_name(target_team_name)
    for k in mapping:
        if normalize_team_name(k) == target_norm:
            return k
    return None


def parse_show_record_html(html: str) -> dict[str, Any]:
    """解析 show_record.php（攻守數據頁）兩隊打者與投手成績。"""
    soup = BeautifulSoup(html, "lxml")
    batting_by_team: dict[str, list[dict[str, Any]]] = {}
    pitching_by_team: dict[str, list[dict[str, Any]]] = {}

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        first_cells = [clean_text(c.get_text(" ", strip=True)) for c in rows[0].find_all(["th", "td"])]
        if not first_cells:
            continue
        title = first_cells[0]
        is_bat = title.endswith("打者成績")
        is_pitch = title.endswith("投手成績")
        if not (is_bat or is_pitch):
            continue

        team_label = clean_text(title[: -len("打者成績")] if is_bat else title[: -len("投手成績")])
        headers = [clean_text(c.get_text(" ", strip=True)) for c in rows[1].find_all(["th", "td"])]
        records: list[dict[str, Any]] = []

        for tr in rows[2:]:
            cells = [clean_text(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if len(cells) < 3:
                continue
            if any(k in cells[0] for k in ("滾飛比", "合計", "Total")):
                continue
            jersey, name = split_jersey_and_name(cells[0])
            row_dict: dict[str, Any] = {
                "raw_player": cells[0],
                "jersey_number": jersey,
                "masked_name": name,
            }
            for idx, h in enumerate(headers[1:], start=1):
                if idx < len(cells):
                    val = cells[idx]
                    if h in ("IP", "OBP", "SLG", "AVG", "ERA"):
                        try:
                            row_dict[h] = float(val)
                        except ValueError:
                            row_dict[h] = val
                    else:
                        try:
                            row_dict[h] = int(val)
                        except ValueError:
                            row_dict[h] = val
            records.append(row_dict)

        if is_bat:
            batting_by_team[team_label] = records
        else:
            pitching_by_team[team_label] = records

    return {
        "batting_by_team": batting_by_team,
        "pitching_by_team": pitching_by_team,
    }


def _parse_lineup_table(tbl: Any) -> list[dict[str, str]]:
    """解析單一隊伍的先發打序與守位表。"""
    lineup: list[dict[str, str]] = []
    for tr in tbl.find_all("tr"):
        cells = [clean_text(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        if len(cells) != 3:
            continue
        order_str, name_str, pos_str = cells
        if order_str == "棒次":
            continue
        lineup.append(
            {
                "order": order_str,
                "masked_name": name_str,
                "position": pos_str,
            }
        )
    return lineup


def _infer_event_flags(text: str, known_players: set[str]) -> dict[str, Any]:
    """判定單條文字紀錄的類別、打者、出局增量、得分與涉入守位。"""
    is_pitch_change = text.startswith("先發：投手") or text.startswith("換投：投手")
    pitcher_name: Optional[str] = None
    pitcher_jersey: Optional[str] = None
    if is_pitch_change:
        m_p = re.search(r"投手([^\s(（]+)\s*[（(](\d+)[）)]?", text)
        if m_p:
            pitcher_name = m_p.group(1).strip()
            pitcher_jersey = m_p.group(2).strip()
        else:
            m_p2 = re.search(r"投手(.+)$", text)
            if m_p2:
                pitcher_name = m_p2.group(1).strip()

    is_runner_event = text.startswith("壘上跑者")
    is_sub_event = (
        ("上場代打" in text or "上場代跑" in text or "更換守備" in text or "守備異動" in text)
        and not is_runner_event
    )

    involved_positions: list[str] = []
    for pos_code, kws in POSITION_KEYWORDS.items():
        if any(kw in text for kw in kws):
            involved_positions.append(pos_code)

    outs_delta = 0
    if "雙殺" in text:
        outs_delta = 2
    elif "三殺" in text:
        outs_delta = 3
    elif any(k in text for k in ("出局", "三振", "接殺", "刺殺", "封殺", "觸殺", "盜壘失敗")):
        outs_delta = 1

    runs_scored = 0
    if "回本壘得分" in text:
        runs_scored += 1
    elif "全壘打" in text:
        runs_scored += 1

    batter_name: Optional[str] = None
    is_pa = False
    if not is_pitch_change and not is_runner_event and not is_sub_event:
        for pname in sorted(known_players, key=len, reverse=True):
            if text.startswith(pname):
                batter_name = pname
                is_pa = True
                break
        if batter_name is None:
            m_b = re.match(
                r"^([^\s，,。：:（(]{2,5}?)(?:沒有出棒|揮棒|打擊|擊出|獲得|遭到|選到|觸擊|犧牲|界外|不死)",
                text,
            )
            if m_b:
                batter_name = m_b.group(1)
                is_pa = True

    runner_name: Optional[str] = None
    if is_runner_event:
        for pname in sorted(known_players, key=len, reverse=True):
            if text.startswith(f"壘上跑者{pname}"):
                runner_name = pname
                break
        if runner_name is None:
            m_r = re.match(r"^壘上跑者([^\s，,因盜回上]{2,5})", text)
            if m_r:
                runner_name = m_r.group(1)

    event_type = "other"
    if is_pitch_change:
        event_type = "pitch_change"
    elif is_runner_event:
        event_type = "runner"
    elif is_sub_event:
        event_type = "sub"
    elif is_pa:
        event_type = "pa"

    return {
        "event_type": event_type,
        "is_pitching_change": is_pitch_change,
        "pitcher_name": pitcher_name,
        "pitcher_jersey": pitcher_jersey,
        "is_runner_event": is_runner_event,
        "runner_name": runner_name,
        "is_sub_event": is_sub_event,
        "is_plate_appearance": is_pa,
        "batter_name": batter_name,
        "outs_delta": outs_delta,
        "runs_scored": runs_scored,
        "involved_positions": involved_positions,
    }


def summarize_pa_result(text: str) -> str:
    """將逐打席文字簡化為精簡結果標籤。"""
    if "三振" in text and "不死三振" not in text:
        return "三振"
    if "不死三振" in text:
        return "不死三振"
    if "四壞球" in text:
        return "四壞保送"
    if "故意四壞" in text:
        return "敬遠保送"
    if "觸身球" in text:
        return "觸身球"
    if "全壘打" in text:
        return "全壘打"
    if "三壘安打" in text:
        return "三壘安打"
    if "二壘安打" in text:
        return "二壘安打"
    if "一壘安打" in text or "內野安打" in text:
        return "一壘安打"
    if "雙殺" in text:
        return "雙殺打"
    if "接殺" in text:
        m = re.search(r"遭到([^\s]+?)接殺", text)
        return f"{m.group(1)}飛球" if m else "飛球出局"
    if "失誤" in text:
        return "失誤上壘"
    if "野手選擇" in text:
        return "野手選擇"
    if "刺殺" in text or "滾地球" in text:
        return "滾地出局"
    if "犧牲" in text:
        return "犧牲打"
    return text[:16]


def parse_show_text_html(html: str, extra_known_players: set[str]) -> dict[str, Any]:
    """解析 show_text.php（文字紀錄頁）先發打序與逐半局事件。"""
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")

    lineups: dict[str, list[dict[str, str]]] = {}
    for tbl in tables[1:]:
        if tbl.find("table") is not None:
            continue
        first_row = tbl.find("tr")
        if not first_row:
            continue
        h_cells = [clean_text(c.get_text(" ", strip=True)) for c in first_row.find_all(["th", "td"])]
        if h_cells[:3] != ["棒次", "球員", "守備位置"]:
            continue
        parent_td = tbl.find_parent("td")
        team_title = ""
        if parent_td:
            for child in parent_td.children:
                if getattr(child, "name", None) == "table":
                    break
                t = clean_text(child.get_text(" ", strip=True) if hasattr(child, "get_text") else str(child))
                if t:
                    team_title = t
                    break
        parsed_lu = _parse_lineup_table(tbl)
        if team_title:
            lineups[team_title] = parsed_lu

    known_players = set(extra_known_players)
    for lu in lineups.values():
        for item in lu:
            if item["masked_name"] and item["masked_name"] != "無":
                known_players.add(item["masked_name"])

    half_innings: list[dict[str, Any]] = []
    for div in soup.select("div.col-lg-6.border-bottom"):
        div_copy = BeautifulSoup(str(div), "lxml")
        for br in div_copy.find_all("br"):
            br.replace_with("\n")
        raw_lines = [clean_text(ln) for ln in div_copy.get_text().splitlines() if clean_text(ln)]
        if not raw_lines:
            continue

        half_label = raw_lines[0]
        m_half = re.match(r"^(\d+)\s*局\s*([上下])$", half_label)
        if not m_half:
            continue
        inning_num = int(m_half.group(1))
        half_side = "TOP" if m_half.group(2) == "上" else "BOT"

        events: list[dict[str, Any]] = []
        cum_outs = 0
        for idx, line in enumerate(raw_lines[1:], start=1):
            flags = _infer_event_flags(line, known_players)
            events.append(
                {
                    "event_index": idx,
                    "outs_before": min(2, cum_outs),
                    "text": line,
                    **flags,
                }
            )
            cum_outs = min(3, cum_outs + flags["outs_delta"])

        half_innings.append(
            {
                "half_label": half_label,
                "inning": inning_num,
                "half_side": half_side,
                "events": events,
            }
        )

    return {
        "lineups": lineups,
        "half_innings": half_innings,
    }


def _load_team_roster_lookup(
    conn: sqlite3.Connection,
    team_id: int,
) -> tuple[dict[tuple[str, str], str], dict[str, list[str]], dict[str, str]]:
    """讀取單隊名冊：(jersey, masked_name) -> uid, masked_name -> [uids], uid -> jersey。"""
    rows = conn.execute(
        """
        SELECT COALESCE(tr.jersey_number, '') AS jersey, p.player_uid, p.masked_name
        FROM team_rosters tr
        JOIN players p ON tr.player_uid = p.player_uid
        WHERE tr.team_id = ? AND p.player_uid != ?
        ORDER BY p.player_uid
        """,
        (team_id, COACH_PITCH_UID),
    ).fetchall()
    by_jersey_name: dict[tuple[str, str], str] = {}
    by_name: dict[str, list[str]] = {}
    uid_to_jersey: dict[str, str] = {}
    for r in rows:
        jersey = str(r["jersey"]).strip()
        uid = str(r["player_uid"])
        mname = str(r["masked_name"]).strip()
        if jersey:
            by_jersey_name[(jersey, mname)] = uid
            uid_to_jersey[uid] = jersey
        by_name.setdefault(mname, []).append(uid)
    return by_jersey_name, by_name, uid_to_jersey


def _resolve_uid(
    masked_name: Optional[str],
    jersey: Optional[str],
    by_jersey_name: dict[tuple[str, str], str],
    by_name: dict[str, list[str]],
) -> Optional[str]:
    """依背號與遮罩姓名解析唯一 player_uid，避免同隊同名混淆。"""
    if not masked_name:
        return None
    name_clean = masked_name.strip()
    if jersey:
        j_clean = jersey.strip()
        if (j_clean, name_clean) in by_jersey_name:
            return by_jersey_name[(j_clean, name_clean)]
        # 容許前導零差異 (如 '01' vs '1')
        j_int = j_clean.lstrip("0") or "0"
        for (kj, kn), uid in by_jersey_name.items():
            if kn == name_clean and (kj.lstrip("0") or "0") == j_int:
                return uid
    cands = by_name.get(name_clean, [])
    if len(cands) == 1:
        return cands[0]
    return None


def extract_all_players_involvement(
    match_meta: dict[str, Any],
    record_data: dict[str, Any],
    text_data: dict[str, Any],
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """萃取單場比賽中海盜隊所有出場球員之 player_uid 及打席/投球/守備事件。"""
    team_id = int(match_meta["team_id"])
    team_name = str(match_meta["team_name"])
    pirate_side = match_meta["pirate_side"]
    batting_half = "TOP" if pirate_side == "away" else "BOT"
    fielding_half = "BOT" if pirate_side == "away" else "TOP"

    by_jersey_name, by_name, uid_to_jersey = _load_team_roster_lookup(conn, team_id)

    bat_key = _find_team_key(record_data["batting_by_team"], team_name)
    pitch_key = _find_team_key(record_data["pitching_by_team"], team_name)
    bat_rows = record_data["batting_by_team"].get(bat_key, []) if bat_key else []
    pitch_rows = record_data["pitching_by_team"].get(pitch_key, []) if pitch_key else []

    # 1. 建立當場出場選手 Name -> UID（結合 boxscore 背號優先精準對應）
    game_name_to_uid: dict[str, str] = {}
    boxscore_bat_by_uid: dict[str, dict[str, Any]] = {}
    boxscore_pitch_by_uid: dict[str, dict[str, Any]] = {}

    for r in bat_rows:
        uid = _resolve_uid(r["masked_name"], r.get("jersey_number"), by_jersey_name, by_name)
        if uid:
            boxscore_bat_by_uid[uid] = r
            game_name_to_uid.setdefault(r["masked_name"], uid)

    for r in pitch_rows:
        uid = _resolve_uid(r["masked_name"], r.get("jersey_number"), by_jersey_name, by_name)
        if uid:
            boxscore_pitch_by_uid[uid] = r
            game_name_to_uid.setdefault(r["masked_name"], uid)

    lu_key = _find_team_key(text_data["lineups"], team_name)
    pirate_lineup = text_data["lineups"].get(lu_key, []) if lu_key else []
    for item in pirate_lineup:
        if item["masked_name"] not in game_name_to_uid:
            uid = _resolve_uid(item["masked_name"], None, by_jersey_name, by_name)
            if uid:
                game_name_to_uid[item["masked_name"]] = uid

    # 若名冊裡該姓名唯一而尚未對應（如代跑/代打未列於打者成績表），補充對應
    for mname, uids in by_name.items():
        if len(uids) == 1 and mname not in game_name_to_uid:
            game_name_to_uid[mname] = uids[0]

    # 2. 追蹤每位海盜球員當場守位與產出明細
    player_events: dict[str, dict[str, Any]] = {}

    def ensure_player(uid: str, mname: str) -> dict[str, Any]:
        if uid not in player_events:
            player_events[uid] = {
                "player_uid": uid,
                "masked_name": mname,
                "jersey_number": (
                    (boxscore_bat_by_uid.get(uid) or {}).get("jersey_number")
                    or (boxscore_pitch_by_uid.get(uid) or {}).get("jersey_number")
                    or uid_to_jersey.get(uid)
                ),
                "starting_position": None,
                "plate_appearances": [],
                "pitching_stints": [],
                "fielding_plays": [],
            }
        return player_events[uid]

    active_pos_by_uid: dict[str, Optional[str]] = {}
    pos_uncertain_uids: set[str] = set()
    for item in pirate_lineup:
        uid = game_name_to_uid.get(item["masked_name"])
        if not uid:
            continue
        pe = ensure_player(uid, item["masked_name"])
        if item["order"] != "投手" or pe["starting_position"] is None:
            pe["starting_position"] = item["position"]
        active_pos_by_uid[uid] = item["position"]

    for uid, row in boxscore_bat_by_uid.items():
        ensure_player(uid, row["masked_name"])
    for uid, row in boxscore_pitch_by_uid.items():
        ensure_player(uid, row["masked_name"])

    current_pitcher_uid: Optional[str] = None

    for hi in text_data["half_innings"]:
        hlabel = hi["half_label"]
        inning = hi["inning"]
        half_side = hi["half_side"]
        events = hi["events"]

        if half_side == batting_half:
            for idx, ev in enumerate(events):
                if ev["is_plate_appearance"] and ev["batter_name"]:
                    bname = ev["batter_name"]
                    uid = game_name_to_uid.get(bname)
                    if not uid:
                        continue
                    pe = ensure_player(uid, bname)
                    runner_notes: list[str] = []
                    for nxt in events[idx + 1 :]:
                        if nxt["is_plate_appearance"] or nxt["is_pitching_change"]:
                            break
                        if nxt["is_runner_event"] and nxt["runner_name"] == bname:
                            runner_notes.append(nxt["text"])
                    pe["plate_appearances"].append(
                        {
                            "half_label": hlabel,
                            "inning": inning,
                            "half_side": half_side,
                            "event_index": ev["event_index"],
                            "result_short": summarize_pa_result(ev["text"]),
                            "event_text": ev["text"],
                            "runner_notes": runner_notes,
                        }
                    )

        elif half_side == fielding_half:
            stint_by_uid: dict[str, dict[str, Any]] = {}

            for ev in events:
                if ev["is_pitching_change"]:
                    p_name = ev["pitcher_name"]
                    p_jersey = ev.get("pitcher_jersey")
                    p_uid = _resolve_uid(p_name, p_jersey, by_jersey_name, by_name) or (
                        game_name_to_uid.get(p_name) if p_name else None
                    )
                    if p_uid and p_name:
                        ensure_player(p_uid, p_name)
                        if current_pitcher_uid and current_pitcher_uid != p_uid:
                            if active_pos_by_uid.get(current_pitcher_uid) == "投":
                                pos_uncertain_uids.add(current_pitcher_uid)
                        current_pitcher_uid = p_uid
                        active_pos_by_uid[p_uid] = "投"
                        pos_uncertain_uids.discard(p_uid)
                        if p_uid not in stint_by_uid:
                            stint_by_uid[p_uid] = {
                                "half_label": hlabel,
                                "inning": inning,
                                "half_side": half_side,
                                "start_event_index": ev["event_index"],
                                "batters_faced_in_half": 0,
                            }
                    continue

                if current_pitcher_uid:
                    if current_pitcher_uid not in stint_by_uid:
                        stint_by_uid[current_pitcher_uid] = {
                            "half_label": hlabel,
                            "inning": inning,
                            "half_side": half_side,
                            "start_event_index": ev["event_index"],
                            "batters_faced_in_half": 0,
                        }
                    if ev["is_plate_appearance"]:
                        stint_by_uid[current_pitcher_uid]["batters_faced_in_half"] += 1

                # 比對所有有明確守位之海盜球員
                if ev["involved_positions"]:
                    for uid, pos in active_pos_by_uid.items():
                        if not pos or uid in pos_uncertain_uids or pos not in ev["involved_positions"]:
                            continue
                        if pos == "投" and not any(
                            kw in ev["text"]
                            for kw in (
                                "投手接到",
                                "投手接殺",
                                "投手接球",
                                "投手傳",
                                "投手失誤",
                                "投手方向",
                                "投手前",
                            )
                        ):
                            continue
                        role = "守備處理"
                        if pos == "一" and ("傳向一壘" in ev["text"] or "傳給一壘手" in ev["text"]):
                            if "一壘手接到" not in ev["text"] and "一壘手接殺" not in ev["text"]:
                                role = "刺殺參與(接傳)"
                        player_events[uid]["fielding_plays"].append(
                            {
                                "half_label": hlabel,
                                "inning": inning,
                                "half_side": half_side,
                                "event_index": ev["event_index"],
                                "position": pos,
                                "role": role,
                                "event_text": ev["text"],
                            }
                        )

            for p_uid, stint in stint_by_uid.items():
                player_events[p_uid]["pitching_stints"].append(stint)

    return {
        "game_name_to_uid": game_name_to_uid,
        "lineups": text_data["lineups"],
        "half_innings": text_data["half_innings"],
        "players": player_events,
    }


def _store_match_events_to_db(
    conn: sqlite3.Connection,
    match_meta: dict[str, Any],
    parsed: dict[str, Any],
) -> None:
    """將單場先發打序與逐半局事件冪等寫入 SQLite。"""
    match_id = int(match_meta["match_id"])
    team_id = int(match_meta["team_id"])
    team_name = str(match_meta["team_name"])
    pirate_side = match_meta["pirate_side"]
    batting_half = "TOP" if pirate_side == "away" else "BOT"
    name_to_uid = parsed["game_name_to_uid"]

    with conn:
        conn.execute("DELETE FROM match_lineups WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))

        for lu_team, items in parsed["lineups"].items():
            is_pirate_lu = normalize_team_name(lu_team) == normalize_team_name(team_name)
            for item in items:
                p_uid = name_to_uid.get(item["masked_name"]) if is_pirate_lu else None
                conn.execute(
                    """
                    INSERT OR IGNORE INTO match_lineups (
                        match_id, team_id, player_uid, masked_name, batting_order, position
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        match_id,
                        team_id,
                        p_uid,
                        item["masked_name"],
                        item["order"],
                        item["position"],
                    ),
                )

        for hi in parsed["half_innings"]:
            inning = int(hi["inning"])
            half_side = hi["half_side"]
            for ev in hi["events"]:
                b_uid = name_to_uid.get(ev["batter_name"]) if (half_side == batting_half and ev.get("batter_name")) else None
                p_uid = name_to_uid.get(ev["pitcher_name"]) if (half_side != batting_half and ev.get("pitcher_name")) else None
                conn.execute(
                    """
                    INSERT INTO match_events (
                        match_id, inning, half_side, event_index, event_type,
                        batter_uid, batter_name, pitcher_uid, pitcher_name,
                        outs_before, outs_delta, runs_scored, involved_positions, event_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        match_id,
                        inning,
                        half_side,
                        int(ev["event_index"]),
                        ev["event_type"],
                        b_uid,
                        ev.get("batter_name"),
                        p_uid,
                        ev.get("pitcher_name"),
                        int(ev.get("outs_before", 0)),
                        int(ev.get("outs_delta", 0)),
                        int(ev.get("runs_scored", 0)),
                        json.dumps(ev.get("involved_positions", []), ensure_ascii=False),
                        ev["text"],
                    ),
                )


def fetch_and_parse_matches(
    db_path: Path | str = DEFAULT_DB_PATH,
    since_date: Optional[str] = "2026-10-01",
    match_ids: Optional[list[int]] = None,
    parse_only: bool = False,
    cache_dir: Path = DEFAULT_MATCH_HTML_DIR,
) -> list[dict[str, Any]]:
    """抓取（若無快取）並解析指定範圍之所有比賽，回傳含全部海盜出場球員事件的清單。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    conn = get_db_connection(db_path)

    try:
        where_clauses = ["m.boxscore_url IS NOT NULL", "m.pbp_url IS NOT NULL"]
        params: list[Any] = []
        if since_date:
            where_clauses.append("m.match_date_iso >= ?")
            params.append(since_date)
        if match_ids:
            ph = ",".join("?" for _ in match_ids)
            where_clauses.append(f"m.id IN ({ph})")
            params.extend(match_ids)

        sql = f"""
            SELECT m.id AS match_id, m.team_id, m.league_id, m.match_date, m.match_date_iso,
                   m.match_time, m.away_team, m.home_team, m.away_score, m.home_score,
                   m.venue, m.boxscore_url, m.pbp_url, m.summary_url,
                   t.team_name, l.league_title, l.year, l.season, l.age_group, l.division
            FROM matches m
            JOIN teams t ON m.team_id = t.team_id
            JOIN leagues l ON m.league_id = l.league_id
            WHERE {' AND '.join(where_clauses)}
            ORDER BY m.match_date_iso ASC, m.match_time ASC, m.id ASC
        """
        match_rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        # 蒐集每場關聯 YouTube 影片
        videos_by_match: dict[int, list[dict[str, Any]]] = {}
        for m in match_rows:
            vrows = conn.execute(
                """
                SELECT mv.label, mv.sort_order, yv.video_id, yv.title, yv.url, yv.duration_sec, yv.part_label
                FROM match_videos mv
                JOIN youtube_videos yv ON mv.video_id = yv.video_id
                WHERE mv.match_id = ?
                ORDER BY mv.sort_order ASC, mv.video_id ASC
                """,
                (m["match_id"],),
            ).fetchall()
            videos_by_match[m["match_id"]] = [dict(v) for v in vrows]

        # 下載尚缺之 HTML 快取（間隔 >= 1.6s）
        to_fetch: list[tuple[str, Path]] = []
        seen_paths: set[Path] = set()
        for m in match_rows:
            site_id = extract_site_match_id(m["boxscore_url"]) or str(m["match_id"])
            rec_path = cache_dir / f"{m['league_id']}_{site_id}_record.html"
            txt_path = cache_dir / f"{m['league_id']}_{site_id}_text.html"
            for url, p in ((m["boxscore_url"], rec_path), (m["pbp_url"], txt_path)):
                if p not in seen_paths:
                    seen_paths.add(p)
                    if not p.exists() and url:
                        to_fetch.append((url, p))

        if to_fetch and not parse_only:
            print(f"[*] 下載 {len(to_fetch)} 份未快取之官網單場頁面（間隔 >= {REQUEST_INTERVAL_SEC}s）...", flush=True)
            last_req = 0.0
            with httpx.Client(follow_redirects=True, timeout=20.0) as client:
                for idx, (url, dest) in enumerate(to_fetch, start=1):
                    elapsed = time.monotonic() - last_req
                    if last_req > 0 and elapsed < REQUEST_INTERVAL_SEC:
                        time.sleep(REQUEST_INTERVAL_SEC - elapsed)
                    last_req = time.monotonic()
                    resp = client.get(url, headers=REQUEST_HEADERS)
                    resp.raise_for_status()
                    dest.write_text(resp.text, encoding="utf-8")
                    if idx % 10 == 0 or idx == len(to_fetch):
                        print(f"  - 官網快取進度: [{idx}/{len(to_fetch)}]", flush=True)
        elif to_fetch and parse_only:
            raise RuntimeError(f"--parse-only 模式下仍有 {len(to_fetch)} 份單場快取缺失: {to_fetch[0][1]}")

        # 解析並入庫
        results: list[dict[str, Any]] = []
        for m in match_rows:
            site_id = extract_site_match_id(m["boxscore_url"]) or str(m["match_id"])
            rec_path = cache_dir / f"{m['league_id']}_{site_id}_record.html"
            txt_path = cache_dir / f"{m['league_id']}_{site_id}_text.html"

            pirate_side, opponent_team = resolve_pirate_side(m["team_name"], m["away_team"], m["home_team"])
            m["pirate_side"] = pirate_side
            m["opponent_team"] = opponent_team

            record_data = parse_show_record_html(rec_path.read_text(encoding="utf-8"))
            known_players: set[str] = set()
            for rows in record_data["batting_by_team"].values():
                for r in rows:
                    known_players.add(r["masked_name"])
            for rows in record_data["pitching_by_team"].values():
                for r in rows:
                    known_players.add(r["masked_name"])

            text_data = parse_show_text_html(txt_path.read_text(encoding="utf-8"), known_players)
            if not text_data["half_innings"]:
                print(
                    f"  [SKIP] match_id={m['match_id']} ({m['match_date_iso']} {m['away_team']} vs {m['home_team']}) 官網尚無逐打席文字紀錄",
                    flush=True,
                )
                continue

            parsed_all = extract_all_players_involvement(m, record_data, text_data, conn)
            _store_match_events_to_db(conn, m, parsed_all)

            results.append(
                {
                    **m,
                    "site_match_id": site_id,
                    "youtube_videos": videos_by_match.get(m["match_id"], []),
                    "half_innings": text_data["half_innings"],
                    "players": parsed_all["players"],
                    "lineups": parsed_all["lineups"],
                }
            )

        return results
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="解析官網單場攻守與文字紀錄並入庫")
    parser.add_argument("--since", type=str, default="2026-10-01", help="起始比賽日期 (YYYY-MM-DD)")
    parser.add_argument("--parse-only", action="store_true", help="僅使用本地 HTML 快取解析")
    args = parser.parse_args()

    games = fetch_and_parse_matches(since_date=args.since, parse_only=args.parse_only)
    total_pas = sum(len(p["plate_appearances"]) for g in games for p in g["players"].values())
    total_pits = sum(len(p["pitching_stints"]) for g in games for p in g["players"].values())
    total_flds = sum(len(p["fielding_plays"]) for g in games for p in g["players"].values())
    print(
        f"[+] 解析入庫完成：{len(games)} 場含文字紀錄賽事，"
        f"合計打席={total_pas}, 投球半局={total_pits}, 守備={total_flds}"
    )


if __name__ == "__main__":
    main()
