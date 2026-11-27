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
from collections import Counter
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
                r"^([^\s，,。：:（(]{2,5}?)(?:沒有出棒|揮棒|打擊|擊出|打到|打成|獲得|遭到|選到|觸擊|犧牲|界外|不死)",
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


def _load_global_unique_players(conn: sqlite3.Connection) -> dict[str, str]:
    """讀取全庫僅對應單一 player_uid 之遮罩姓名對照表（供跨隊支援球員備援歸戶）。"""
    rows = conn.execute(
        """
        SELECT masked_name, MIN(player_uid) AS player_uid, COUNT(*) AS cnt
        FROM players
        WHERE player_uid != ?
        GROUP BY masked_name
        HAVING cnt = 1
        """,
        (COACH_PITCH_UID,),
    ).fetchall()
    return {str(r["masked_name"]).strip(): str(r["player_uid"]) for r in rows}


def _classify_pa_boxscore_stats(text: str) -> dict[str, int]:
    """依單一打席文字推算對應 Boxscore 統計增量（供同隊同名拆帳比對）。"""
    is_bb = ("四壞球" in text) or ("故意四壞" in text) or ("觸身球" in text)
    is_sac = "犧牲" in text
    is_ab = 0 if (is_bb or is_sac) else 1
    is_hr = 1 if "全壘打" in text else 0
    is_3b = 1 if "三壘安打" in text else 0
    is_2b = 1 if "二壘安打" in text else 0
    is_1b = 1 if (("一壘安打" in text or "內野安打" in text) and not (is_hr or is_3b or is_2b)) else 0
    is_h = 1 if (is_hr or is_3b or is_2b or is_1b) else 0
    is_so = 1 if "三振" in text else 0
    m_rbi = re.search(r"帶有(\d+)分打點", text)
    rbi = int(m_rbi.group(1)) if m_rbi else 0
    return {
        "PA": 1,
        "AB": is_ab,
        "H": is_h,
        "2B": is_2b,
        "3B": is_3b,
        "HR": is_hr,
        "BB": 1 if is_bb else 0,
        "SO": is_so,
        "RBI": rbi,
    }


def _disambiguate_duplicate_batters(
    all_batting_pas: list[tuple[str, int, str, str]],
    bat_candidates_by_name: dict[str, list[tuple[str, dict[str, Any]]]],
    pirate_lineup: list[dict[str, str]],
) -> dict[tuple[str, int], str]:
    """當同隊同場有多位同遮罩姓名打者時，依 Boxscore 統計、棒次輪轉與出現順序精準拆帳歸戶。"""
    resolved: dict[tuple[str, int], str] = {}
    if not all_batting_pas:
        return resolved

    slot_names: dict[int, str] = {}
    slot_name_counts: Counter[str] = Counter()
    for item in pirate_lineup:
        if item["order"].isdigit():
            slot_num = int(item["order"])
            sname = item["masked_name"].strip()
            if sname and sname != "無":
                slot_names[slot_num] = sname
                slot_name_counts[sname] += 1

    lineup_len = max(slot_names.keys()) if slot_names else 9
    if lineup_len <= 0:
        lineup_len = 9

    unique_name_to_slot = {
        sname: snum
        for snum, sname in slot_names.items()
        if slot_name_counts[sname] == 1 and len(bat_candidates_by_name.get(sname, [])) <= 1
    }

    for nm, cands in bat_candidates_by_name.items():
        if len(cands) < 2:
            continue
        dup_pas = [(g_idx, pa) for g_idx, pa in enumerate(all_batting_pas) if pa[2] == nm]
        if not dup_pas:
            continue

        dup_slots = sorted(snum for snum, sname in slot_names.items() if sname == nm)
        cand_slot: dict[int, Optional[int]] = {
            ci: (dup_slots[ci] if ci < len(dup_slots) else None)
            for ci in range(len(cands))
        }

        pa_inferred_slot: list[Optional[int]] = []
        for g_idx, _ in dup_pas:
            votes: Counter[int] = Counter()
            for delta in (-2, -1, 1, 2):
                ni = g_idx + delta
                if 0 <= ni < len(all_batting_pas):
                    nb_name = all_batting_pas[ni][2]
                    if nb_name in unique_name_to_slot:
                        inferred = ((unique_name_to_slot[nb_name] - 1 - delta) % lineup_len) + 1
                        votes[inferred] += 2 if abs(delta) == 1 else 1
            pa_inferred_slot.append(votes.most_common(1)[0][0] if votes else None)

        target_pas = [
            int(r["PA"]) if isinstance(r.get("PA"), int) else 0
            for _, r in cands
        ]
        m_pa = len(dup_pas)
        n_c = len(cands)

        if sum(target_pas) == m_pa and m_pa <= 12:
            pa_stats = [_classify_pa_boxscore_stats(pa[3]) for _, pa in dup_pas]
            best_cost: Optional[int] = None
            best_assign: Optional[list[int]] = None

            def _backtrack(pos: int, remaining: list[int], cur: list[int]) -> None:
                nonlocal best_cost, best_assign
                if pos == m_pa:
                    stat_err = 0
                    for ci, (_, row) in enumerate(cands):
                        assigned_idxs = [pi for pi, c_idx in enumerate(cur) if c_idx == ci]
                        for k in ("AB", "H", "2B", "3B", "HR", "BB", "SO", "RBI"):
                            exp = row.get(k)
                            if isinstance(exp, int):
                                got = sum(pa_stats[pi][k] for pi in assigned_idxs)
                                stat_err += abs(got - exp)

                    first_pos = [
                        next((pi for pi, c_idx in enumerate(cur) if c_idx == ci), 999)
                        for ci in range(n_c)
                    ]
                    order_pen = 0 if first_pos == sorted(first_pos) else 100
                    slot_pen = 0
                    for pi, c_idx in enumerate(cur):
                        exp_s = cand_slot[c_idx]
                        inf_s = pa_inferred_slot[pi]
                        if exp_s is not None and inf_s is not None and exp_s != inf_s:
                            slot_pen += 10
                    consec_pen = sum(
                        5
                        for pi in range(1, m_pa)
                        if cur[pi] == cur[pi - 1] and cand_slot[cur[pi]] is not None
                    )
                    cost = stat_err * 50 + order_pen + slot_pen + consec_pen
                    if best_cost is None or cost < best_cost:
                        best_cost = cost
                        best_assign = list(cur)
                    return

                for ci in range(n_c):
                    if remaining[ci] > 0:
                        remaining[ci] -= 1
                        cur.append(ci)
                        _backtrack(pos + 1, remaining, cur)
                        cur.pop()
                        remaining[ci] += 1

            _backtrack(0, list(target_pas), [])
            if best_assign is not None:
                for pi, c_idx in enumerate(best_assign):
                    hl, ev_idx, _, _ = dup_pas[pi][1]
                    resolved[(hl, ev_idx)] = cands[c_idx][0]
                continue

        # 備援分配（PA 總數不一致或超過 12 打席）：優先比對推斷棒次並按剩餘 PA 配額輪轉
        rem_quota = [max(0, tp) for tp in target_pas]
        for pi, (_, (hl, ev_idx, _, _)) in enumerate(dup_pas):
            inf_s = pa_inferred_slot[pi]
            chosen_ci: Optional[int] = None
            if inf_s is not None:
                for ci in range(n_c):
                    if cand_slot[ci] == inf_s and rem_quota[ci] > 0:
                        chosen_ci = ci
                        break
            if chosen_ci is None:
                avail = [ci for ci in range(n_c) if rem_quota[ci] > 0]
                chosen_ci = avail[0] if avail else (pi % n_c)
            if rem_quota[chosen_ci] > 0:
                rem_quota[chosen_ci] -= 1
            resolved[(hl, ev_idx)] = cands[chosen_ci][0]

    return resolved


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
    bat_candidates_by_name: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    seen_bat_uids_by_name: dict[str, set[str]] = {}
    starting_pitcher_uid: Optional[str] = None

    for r in bat_rows:
        uid = _resolve_uid(r["masked_name"], r.get("jersey_number"), by_jersey_name, by_name)
        if uid:
            boxscore_bat_by_uid[uid] = r
            game_name_to_uid.setdefault(r["masked_name"], uid)
            seen_uids = seen_bat_uids_by_name.setdefault(r["masked_name"], set())
            if uid not in seen_uids:
                seen_uids.add(uid)
                bat_candidates_by_name.setdefault(r["masked_name"], []).append((uid, r))

    for r in pitch_rows:
        uid = _resolve_uid(r["masked_name"], r.get("jersey_number"), by_jersey_name, by_name)
        if uid:
            if starting_pitcher_uid is None:
                starting_pitcher_uid = uid
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

    global_unique_by_name = _load_global_unique_players(conn)

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
    lineup_uid_map: dict[tuple[str, str], str] = {}
    consumed_cand_idx: dict[str, int] = {}

    for item in pirate_lineup:
        mname = item["masked_name"]
        if item["order"] == "投手":
            if starting_pitcher_uid and (
                (boxscore_pitch_by_uid.get(starting_pitcher_uid) or {}).get("masked_name") == mname
            ):
                uid = starting_pitcher_uid
            else:
                uid = game_name_to_uid.get(mname) or global_unique_by_name.get(mname)
        else:
            cands = bat_candidates_by_name.get(mname, [])
            if len(cands) >= 2 and item["order"].isdigit():
                c_idx = consumed_cand_idx.get(mname, 0)
                uid = cands[c_idx][0] if c_idx < len(cands) else cands[-1][0]
                consumed_cand_idx[mname] = c_idx + 1
            else:
                uid = game_name_to_uid.get(mname) or global_unique_by_name.get(mname)
        if not uid:
            continue
        lineup_uid_map[(item["order"], mname)] = uid
        pe = ensure_player(uid, mname)
        if item["order"] != "投手" or pe["starting_position"] is None:
            pe["starting_position"] = item["position"]
        active_pos_by_uid[uid] = item["position"]

    for uid, row in boxscore_bat_by_uid.items():
        ensure_player(uid, row["masked_name"])
    for uid, row in boxscore_pitch_by_uid.items():
        ensure_player(uid, row["masked_name"])

    all_batting_pas: list[tuple[str, int, str, str]] = []
    for hi in text_data["half_innings"]:
        if hi["half_side"] == batting_half:
            for ev in hi["events"]:
                if ev["is_plate_appearance"] and ev["batter_name"]:
                    all_batting_pas.append(
                        (hi["half_label"], int(ev["event_index"]), ev["batter_name"], ev["text"])
                    )

    disambiguated_pa_uid = _disambiguate_duplicate_batters(
        all_batting_pas,
        bat_candidates_by_name,
        pirate_lineup,
    )

    event_batter_uids: dict[tuple[str, int], str] = {}
    event_pitcher_uids: dict[tuple[str, int], str] = {}
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
                    ev_idx = int(ev["event_index"])
                    uid = (
                        disambiguated_pa_uid.get((hlabel, ev_idx))
                        or game_name_to_uid.get(bname)
                        or global_unique_by_name.get(bname)
                    )
                    if not uid:
                        continue
                    event_batter_uids[(hlabel, ev_idx)] = uid
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
                    p_uid = (
                        _resolve_uid(p_name, p_jersey, by_jersey_name, by_name)
                        or (game_name_to_uid.get(p_name) if p_name else None)
                        or (global_unique_by_name.get(p_name) if p_name else None)
                    )
                    if p_uid and p_name:
                        event_pitcher_uids[(hlabel, int(ev["event_index"]))] = p_uid
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
        "lineup_uid_map": lineup_uid_map,
        "event_batter_uids": event_batter_uids,
        "event_pitcher_uids": event_pitcher_uids,
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
    lineup_uid_map = parsed.get("lineup_uid_map", {})
    event_batter_uids = parsed.get("event_batter_uids", {})
    event_pitcher_uids = parsed.get("event_pitcher_uids", {})

    with conn:
        conn.execute("DELETE FROM match_lineups WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))

        for lu_team, items in parsed["lineups"].items():
            is_pirate_lu = normalize_team_name(lu_team) == normalize_team_name(team_name)
            for item in items:
                p_uid = (
                    (
                        lineup_uid_map.get((item["order"], item["masked_name"]))
                        or name_to_uid.get(item["masked_name"])
                    )
                    if is_pirate_lu
                    else None
                )
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
            hlabel = hi["half_label"]
            for ev in hi["events"]:
                ev_idx = int(ev["event_index"])
                b_uid = (
                    (
                        event_batter_uids.get((hlabel, ev_idx))
                        or name_to_uid.get(ev["batter_name"])
                    )
                    if (half_side == batting_half and ev.get("batter_name"))
                    else None
                )
                p_uid = (
                    (
                        event_pitcher_uids.get((hlabel, ev_idx))
                        or name_to_uid.get(ev["pitcher_name"])
                    )
                    if (half_side != batting_half and ev.get("pitcher_name"))
                    else None
                )
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
                        ev_idx,
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


def _detect_stale_partial_match_ids(
    conn: sqlite3.Connection,
    match_rows: list[dict[str, Any]],
    cache_dir: Path,
    raw_team_html_dir: Path = Path("data/raw_html"),
) -> set[int]:
    """偵測賽季總打席大於快取加總且單場得分缺漏／快取過期之半成品場次。"""
    stale_ids: set[int] = set()
    target_team_ids = {int(m["team_id"]) for m in match_rows}
    rows_by_team: dict[int, list[dict[str, Any]]] = {}
    for m in match_rows:
        rows_by_team.setdefault(int(m["team_id"]), []).append(m)

    for tid in target_team_ids:
        season_pa_row = conn.execute(
            """
            SELECT COALESCE(SUM(pa), 0) AS total_pa
            FROM team_batting_stats
            WHERE team_id = ? AND player_uid != ?
            """,
            (tid, COACH_PITCH_UID),
        ).fetchone()
        season_pa = int(season_pa_row["total_pa"]) if season_pa_row else 0
        if season_pa <= 0:
            continue

        all_team_matches = conn.execute(
            """
            SELECT m.id AS match_id, m.league_id, m.boxscore_url, t.team_name
            FROM matches m
            JOIN teams t ON m.team_id = t.team_id
            WHERE m.team_id = ? AND m.boxscore_url IS NOT NULL
            """,
            (tid,),
        ).fetchall()

        cached_team_pa = 0
        for tm in all_team_matches:
            site_id = extract_site_match_id(tm["boxscore_url"]) or str(tm["match_id"])
            rec_path = cache_dir / f"{tm['league_id']}_{site_id}_record.html"
            if not rec_path.exists():
                continue
            rec_data = parse_show_record_html(rec_path.read_text(encoding="utf-8"))
            bat_key = _find_team_key(rec_data["batting_by_team"], tm["team_name"])
            bat_rows = rec_data["batting_by_team"].get(bat_key, []) if bat_key else []
            cached_team_pa += sum(r["PA"] for r in bat_rows if isinstance(r.get("PA"), int))

        if season_pa <= cached_team_pa:
            continue

        for m in rows_by_team.get(tid, []):
            if m.get("away_score") is None or m.get("home_score") is None:
                continue
            site_id = extract_site_match_id(m["boxscore_url"]) or str(m["match_id"])
            rec_path = cache_dir / f"{m['league_id']}_{site_id}_record.html"
            txt_path = cache_dir / f"{m['league_id']}_{site_id}_text.html"
            if not (rec_path.exists() and txt_path.exists()):
                continue

            rec_data = parse_show_record_html(rec_path.read_text(encoding="utf-8"))
            away_key = _find_team_key(rec_data["batting_by_team"], m["away_team"])
            home_key = _find_team_key(rec_data["batting_by_team"], m["home_team"])
            away_box_r = (
                sum(r["R"] for r in rec_data["batting_by_team"].get(away_key, []) if isinstance(r.get("R"), int))
                if away_key
                else None
            )
            home_box_r = (
                sum(r["R"] for r in rec_data["batting_by_team"].get(home_key, []) if isinstance(r.get("R"), int))
                if home_key
                else None
            )
            score_incomplete = (
                (away_box_r is not None and away_box_r < int(m["away_score"]))
                or (home_box_r is not None and home_box_r < int(m["home_score"]))
            )
            team_html_path = raw_team_html_dir / f"{m['league_id']}_{tid}.html"
            cache_older_than_team = (
                team_html_path.exists()
                and txt_path.stat().st_mtime < team_html_path.stat().st_mtime
            )
            if score_incomplete or cache_older_than_team:
                stale_ids.add(int(m["match_id"]))

    return stale_ids


def fetch_and_parse_matches(
    db_path: Path | str = DEFAULT_DB_PATH,
    since_date: Optional[str] = "2026-10-01",
    match_ids: Optional[list[int]] = None,
    parse_only: bool = False,
    force_refresh: bool = False,
    cache_dir: Path = DEFAULT_MATCH_HTML_DIR,
    raw_team_html_dir: Path = Path("data/raw_html"),
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

        # 下載尚缺或已開賽但仍為空紀錄／半成品之 HTML 快取（間隔 >= 1.6s）
        stale_partial_ids = (
            set()
            if parse_only
            else _detect_stale_partial_match_ids(
                conn,
                match_rows,
                cache_dir=cache_dir,
                raw_team_html_dir=raw_team_html_dir,
            )
        )
        to_fetch: list[tuple[str, Path]] = []
        seen_paths: set[Path] = set()
        for m in match_rows:
            site_id = extract_site_match_id(m["boxscore_url"]) or str(m["match_id"])
            rec_path = cache_dir / f"{m['league_id']}_{site_id}_record.html"
            txt_path = cache_dir / f"{m['league_id']}_{site_id}_text.html"

            is_stale_empty = False
            if not parse_only and m.get("away_score") is not None and txt_path.exists():
                if not parse_show_text_html(txt_path.read_text(encoding="utf-8"), set())["half_innings"]:
                    is_stale_empty = True

            need_refresh = (
                not parse_only
                and (force_refresh or is_stale_empty or int(m["match_id"]) in stale_partial_ids)
            )
            for url, p in ((m["boxscore_url"], rec_path), (m["pbp_url"], txt_path)):
                if p not in seen_paths:
                    seen_paths.add(p)
                    if url and (not p.exists() or need_refresh):
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

            opp_roster: dict[str, dict[str, Optional[str]]] = {}
            opp_bat_key = _find_team_key(record_data["batting_by_team"], opponent_team)
            if opp_bat_key:
                for row in record_data["batting_by_team"].get(opp_bat_key, []):
                    oname = str(row.get("masked_name") or "").strip()
                    if oname:
                        opp_roster[oname] = {
                            "jersey_number": row.get("jersey_number"),
                            "batting_order": None,
                        }
            opp_lu_key = _find_team_key(text_data["lineups"], opponent_team)
            if opp_lu_key:
                for item in text_data["lineups"].get(opp_lu_key, []):
                    oname = str(item.get("masked_name") or "").strip()
                    if not oname or oname == "無":
                        continue
                    entry = opp_roster.setdefault(oname, {"jersey_number": None, "batting_order": None})
                    if item.get("order") != "投手" and entry["batting_order"] is None:
                        entry["batting_order"] = str(item["order"])

            results.append(
                {
                    **m,
                    "site_match_id": site_id,
                    "youtube_videos": videos_by_match.get(m["match_id"], []),
                    "half_innings": text_data["half_innings"],
                    "players": parsed_all["players"],
                    "lineups": parsed_all["lineups"],
                    "opponent_roster": opp_roster,
                }
            )

        return results
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="解析官網單場攻守與文字紀錄並入庫")
    parser.add_argument("--since", type=str, default="2026-10-01", help="起始比賽日期 (YYYY-MM-DD)")
    parser.add_argument("--parse-only", action="store_true", help="僅使用本地 HTML 快取解析")
    parser.add_argument("--refresh-html", action="store_true", help="強制重新下載範圍內單場 HTML 快取")
    args = parser.parse_args()

    games = fetch_and_parse_matches(
        since_date=args.since,
        parse_only=args.parse_only,
        force_refresh=args.refresh_html,
    )
    total_pas = sum(len(p["plate_appearances"]) for g in games for p in g["players"].values())
    total_pits = sum(len(p["pitching_stints"]) for g in games for p in g["players"].values())
    total_flds = sum(len(p["fielding_plays"]) for g in games for p in g["players"].values())
    print(
        f"[+] 解析入庫完成：{len(games)} 場含文字紀錄賽事，"
        f"合計打席={total_pas}, 投球半局={total_pits}, 守備={total_flds}"
    )


if __name__ == "__main__":
    main()
