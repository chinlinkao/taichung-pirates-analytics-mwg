"""靜態球員影片集合 JSON 匯出模組 (src/export_clips_json.py)。

從唯讀 SQLite 資料庫 (data/pirates.db) 匯出第五頁籤所需之輕量化靜態 JSON
至 web/public/data/player-clips.json，供前端切換至「球員影片集合」頁籤時 Lazy Load。
嚴格排除 coach_pitch_888，且絕不匯出任何未遮罩逐字稿原文。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.db import DEFAULT_DB_PATH
from src.export_json import get_readonly_connection, resolve_pirate_side

COACH_PITCH_UID = "coach_pitch_888"
CLIPS_SCHEMA_VERSION = "1.0.0"
DEFAULT_CLIPS_OUTPUT_PATH = Path("web/public/data/player-clips.json")


def fmt_mmss(sec: int) -> str:
    """將秒數轉為 mm:ss 或 h:mm:ss 字串。"""
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, sec_rem = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sec_rem:02d}"
    return f"{m:02d}:{sec_rem:02d}"


def build_clips_dataset(conn: sqlite3.Connection) -> dict[str, Any]:
    """組裝第五頁籤靜態 JSON 資料結構（按 player_uid 索引）。"""
    rows = conn.execute(
        """
        SELECT pvc.id, pvc.match_id, pvc.player_uid, pvc.video_id, pvc.clip_type,
               pvc.inning, pvc.half_side, pvc.event_index, pvc.label_short,
               pvc.event_detail, pvc.start_sec, pvc.align_method, pvc.confidence,
               m.team_id, m.league_id, m.match_date, m.match_date_iso, m.match_time,
               m.away_team, m.home_team, m.away_score, m.home_score,
               m.boxscore_url, m.pbp_url,
               t.team_name, l.league_title, l.year, l.season, l.age_group,
               p.masked_name, p.birth_ym,
               COALESCE(tr.jersey_number, '') AS jersey_number
        FROM player_video_clips pvc
        JOIN matches m ON pvc.match_id = m.id
        JOIN teams t ON m.team_id = t.team_id
        JOIN leagues l ON m.league_id = l.league_id
        JOIN players p ON pvc.player_uid = p.player_uid
        LEFT JOIN team_rosters tr ON tr.team_id = m.team_id AND tr.player_uid = pvc.player_uid
        WHERE pvc.player_uid != ?
        ORDER BY m.match_date_iso DESC, m.match_time DESC, pvc.match_id DESC,
                 pvc.inning ASC,
                 CASE WHEN pvc.half_side = 'TOP' THEN 0 ELSE 1 END ASC,
                 pvc.event_index ASC
        """,
        (COACH_PITCH_UID,),
    ).fetchall()

    # 讀取單場先發守位
    lineup_pos_map: dict[tuple[int, str], str] = {}
    for lr in conn.execute(
        """
        SELECT match_id, player_uid, position
        FROM match_lineups
        WHERE player_uid IS NOT NULL AND batting_order != '投手'
        """
    ).fetchall():
        lineup_pos_map[(int(lr["match_id"]), str(lr["player_uid"]))] = str(lr["position"])

    players_by_uid: dict[str, dict[str, Any]] = {}
    match_ids_covered: set[int] = set()
    video_ids_covered: set[str] = set()
    date_candidates: list[str] = []
    conf_counts = {"high": 0, "medium": 0, "low": 0}
    dual_track_count = 0

    for r in rows:
        uid = str(r["player_uid"])
        match_id = int(r["match_id"])
        vid = str(r["video_id"])
        match_ids_covered.add(match_id)
        video_ids_covered.add(vid)
        if r["match_date_iso"]:
            date_candidates.append(str(r["match_date_iso"]))

        conf = str(r["confidence"])
        if conf in conf_counts:
            conf_counts[conf] += 1
        if r["align_method"] == "dual_track":
            dual_track_count += 1

        p_entry = players_by_uid.get(uid)
        if p_entry is None:
            p_entry = {
                "player_uid": uid,
                "masked_name": r["masked_name"],
                "birth_ym": r["birth_ym"],
                "matches_map": {},
            }
            players_by_uid[uid] = p_entry

        m_entry = p_entry["matches_map"].get(match_id)
        if m_entry is None:
            pirate_side, opponent_team = resolve_pirate_side(
                r["team_name"], r["away_team"], r["home_team"]
            )
            m_entry = {
                "match_id": match_id,
                "team_id": int(r["team_id"]),
                "league_id": int(r["league_id"]),
                "league_title": r["league_title"],
                "team_name": r["team_name"],
                "match_date_iso": r["match_date_iso"],
                "match_time": r["match_time"],
                "away_team": r["away_team"],
                "home_team": r["home_team"],
                "away_score": r["away_score"],
                "home_score": r["home_score"],
                "pirate_side": pirate_side,
                "opponent_team": opponent_team,
                "jersey_number": r["jersey_number"] or None,
                "starting_position": lineup_pos_map.get((match_id, uid)),
                "boxscore_url": r["boxscore_url"],
                "pbp_url": r["pbp_url"],
                "batting_clips": [],
                "pitching_clips": [],
                "fielding_clips": [],
            }
            p_entry["matches_map"][match_id] = m_entry

        start_sec = int(r["start_sec"])
        half_label = f"{r['inning']}局{'上' if r['half_side'] == 'TOP' else '下'}"
        clip_obj = {
            "id": int(r["id"]),
            "video_id": vid,
            "inning": int(r["inning"]),
            "half_side": r["half_side"],
            "half_label": half_label,
            "event_index": int(r["event_index"]),
            "label_short": r["label_short"],
            "event_detail": r["event_detail"],
            "start_sec": start_sec,
            "time_display": fmt_mmss(start_sec),
            "youtube_url": f"https://www.youtube.com/watch?v={vid}&t={start_sec}s",
            "align_method": r["align_method"],
            "confidence": conf,
        }

        ctype = r["clip_type"]
        if ctype == "batting":
            m_entry["batting_clips"].append(clip_obj)
        elif ctype == "pitching":
            m_entry["pitching_clips"].append(clip_obj)
        elif ctype == "fielding":
            m_entry["fielding_clips"].append(clip_obj)

    formatted_players: dict[str, dict[str, Any]] = {}
    for uid in sorted(players_by_uid.keys()):
        p = players_by_uid[uid]
        matches_list = list(p["matches_map"].values())
        formatted_players[uid] = {
            "player_uid": uid,
            "masked_name": p["masked_name"],
            "birth_ym": p["birth_ym"],
            "total_matches": len(matches_list),
            "total_batting": sum(len(m["batting_clips"]) for m in matches_list),
            "total_pitching": sum(len(m["pitching_clips"]) for m in matches_list),
            "total_fielding": sum(len(m["fielding_clips"]) for m in matches_list),
            "matches": matches_list,
        }

    return {
        "schema_version": CLIPS_SCHEMA_VERSION,
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "since_date": min(date_candidates) if date_candidates else None,
            "until_date": max(date_candidates) if date_candidates else None,
            "counts": {
                "players_with_clips": len(formatted_players),
                "matches": len(match_ids_covered),
                "videos": len(video_ids_covered),
                "total_clips": len(rows),
                "dual_track_clips": dual_track_count,
                "confidence": conf_counts,
            },
        },
        "players": formatted_players,
    }


def export_clips_json(
    db_path: Path | str = DEFAULT_DB_PATH,
    output_path: Path | str = DEFAULT_CLIPS_OUTPUT_PATH,
) -> Path:
    """從唯讀 SQLite 資料庫匯出 player-clips.json。"""
    conn = get_readonly_connection(db_path)
    try:
        dataset = build_clips_dataset(conn)
    finally:
        conn.close()

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, mode="w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024.0
    c = dataset["meta"]["counts"]
    print(f"已成功匯出球員影片靜態 JSON: {out_path} ({size_kb:.2f} KB)")
    print(
        f"  - 涵蓋範圍: {dataset['meta']['since_date']} ~ {dataset['meta']['until_date']} | "
        f"球員={c['players_with_clips']}, 場次={c['matches']}, 影片={c['videos']}, "
        f"總片段={c['total_clips']} (雙軌精修={c['dual_track_clips']})"
    )
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="匯出球員影片集合靜態 JSON (player-clips.json)")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB_PATH))
    parser.add_argument("--output", type=str, default=str(DEFAULT_CLIPS_OUTPUT_PATH))
    args = parser.parse_args()
    export_clips_json(db_path=args.db, output_path=args.output)


if __name__ == "__main__":
    main()
