"""靜態前端資料匯出模組 (src/export_json.py)。

從本地唯讀 SQLite 資料庫 (data/pirates.db) 匯出結構化靜態 JSON
至 web/public/data/pirates.json，供純靜態 Vanilla TypeScript 前端讀取，
並附帶 SQLite Views 抽樣基準 (benchmarks) 供 Vitest 自動化對齊測試。
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# 支援直接執行腳本時能匯入根目錄模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import DEFAULT_DB_PATH

SCHEMA_VERSION = "1.0.0"
DEFAULT_OUTPUT_PATH = Path("web/public/data/pirates.json")

AGE_GROUP_ORDER = ["U8", "U10", "U12", "U15"]
TEAM_GROUP_ORDER = ["海盜本隊", "白隊", "黑隊", "U15海盜"]
SEASON_ORDER = ["春季", "秋季", "其他"]
TOURNAMENT_TYPE_ORDER = ["例行賽", "季後賽", "台灣大賽"]
KEY_BENCHMARK_UIDS = ("蔡〇宇_2015-11", "蔡〇宇_2017-06", "林〇晏_2017-11")


def get_readonly_connection(db_path: Path | str) -> sqlite3.Connection:
    """以唯讀模式開啟 SQLite 資料庫並啟用外鍵約束。"""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到資料庫檔案: {path}")

    # 使用 URI mode=ro 確保匯出過程絕不寫入資料庫
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def resolve_pirate_side(
    team_name: str, away_team: str, home_team: str
) -> tuple[Optional[str], Optional[str]]:
    """依所屬球隊名稱精準判定海盜方視角與對手名稱，避免海盜內戰誤判。

    判定優先順序：
    1. 精準比對 team_name（且另一側不同名）
    2. 去除所有空白後比對
    3. 僅當單側包含「海盜」時才以關鍵字判定
    """
    if away_team == team_name and home_team != team_name:
        return ("away", home_team)
    if home_team == team_name and away_team != team_name:
        return ("home", away_team)

    team_norm = "".join(team_name.split())
    away_norm = "".join(away_team.split())
    home_norm = "".join(home_team.split())
    if away_norm == team_norm and home_norm != team_norm:
        return ("away", home_team)
    if home_norm == team_norm and away_norm != team_norm:
        return ("home", away_team)

    away_has = "海盜" in away_team
    home_has = "海盜" in home_team
    if away_has != home_has:
        return ("away", home_team) if away_has else ("home", away_team)

    return (None, None)


def _sort_options(values: Iterable[Optional[str]], preferred_order: list[str]) -> list[str]:
    """依偏好順序排序篩選選項，未知新值依字典序附於末尾。"""
    unique_vals = {v for v in values if v is not None and v != ""}
    order_index = {val: idx for idx, val in enumerate(preferred_order)}
    return sorted(unique_vals, key=lambda v: (order_index.get(v, len(preferred_order)), v))


def _rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    """將 sqlite3.Row 列表轉換為純字典列表。"""
    return [dict(row) for row in rows]


def _fetch_benchmarks(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """抽取 SQLite Views 計算基準，供前端 Vitest 驗證算式一致性。"""
    placeholders = ",".join("?" for _ in KEY_BENCHMARK_UIDS)

    # 1. 打擊基準：必選關鍵測試球員 + 打席數前 10 名
    batting_sql = f"""
        SELECT player_uid, pa, ab, h, calc_avg, calc_obp, calc_slg
        FROM v_player_career_batting
        WHERE player_uid IN ({placeholders})
        UNION
        SELECT player_uid, pa, ab, h, calc_avg, calc_obp, calc_slg
        FROM (
            SELECT player_uid, pa, ab, h, calc_avg, calc_obp, calc_slg
            FROM v_player_career_batting
            ORDER BY pa DESC, player_uid ASC
            LIMIT 10
        )
        ORDER BY player_uid ASC;
    """
    career_batting = _rows_to_dicts(conn.execute(batting_sql, KEY_BENCHMARK_UIDS).fetchall())

    # 2. 投球基準：必選關鍵球員與 0 局案例 + 出局數前 10 名（嚴格排除教練拋打）
    pitching_sql = f"""
        SELECT player_uid, total_outs, er, ip_baseball, calc_era_7, calc_era_9, calc_whip, calc_k7
        FROM v_player_career_pitching
        WHERE player_uid != 'coach_pitch_888'
          AND (player_uid IN ({placeholders}) OR total_outs = 0)
        UNION
        SELECT player_uid, total_outs, er, ip_baseball, calc_era_7, calc_era_9, calc_whip, calc_k7
        FROM (
            SELECT player_uid, total_outs, er, ip_baseball, calc_era_7, calc_era_9, calc_whip, calc_k7
            FROM v_player_career_pitching
            WHERE player_uid != 'coach_pitch_888'
            ORDER BY total_outs DESC, player_uid ASC
            LIMIT 10
        )
        ORDER BY player_uid ASC;
    """
    career_pitching = _rows_to_dicts(conn.execute(pitching_sql, KEY_BENCHMARK_UIDS).fetchall())

    return {
        "career_batting": career_batting,
        "career_pitching": career_pitching,
    }


def build_dataset(conn: sqlite3.Connection) -> dict[str, Any]:
    """從資料庫讀取所有必要資料表與基準並組裝為前端 Schema。"""
    leagues = _rows_to_dicts(
        conn.execute(
            """
            SELECT league_id, league_title, year, season, tournament_type, age_group, division
            FROM leagues
            ORDER BY league_id ASC;
            """
        ).fetchall()
    )

    teams = _rows_to_dicts(
        conn.execute(
            """
            SELECT team_id, league_id, csv_row_no, team_name, team_group,
                   leader, head_coach, manager, coaches, source_url, last_fetched_at
            FROM teams
            ORDER BY team_id ASC;
            """
        ).fetchall()
    )

    players = _rows_to_dicts(
        conn.execute(
            """
            SELECT player_uid, site_player_id, masked_name, birth_ym
            FROM players
            ORDER BY player_uid ASC;
            """
        ).fetchall()
    )

    rosters = _rows_to_dicts(
        conn.execute(
            """
            SELECT team_id, player_uid, jersey_number, position
            FROM team_rosters
            ORDER BY team_id ASC, player_uid ASC;
            """
        ).fetchall()
    )

    batting_stats = _rows_to_dicts(
        conn.execute(
            """
            SELECT team_id, player_uid, jersey_number, g, pa, ab, h,
                   doubles, triples, hr, bb, so, rbi, r, sb_top10
            FROM team_batting_stats
            ORDER BY team_id ASC, player_uid ASC;
            """
        ).fetchall()
    )

    pitching_stats = _rows_to_dicts(
        conn.execute(
            """
            SELECT team_id, player_uid, jersey_number, w, l, sv, g, outs, ip_raw,
                   ab, h, doubles, triples, hr, bb, so, r, er
            FROM team_pitching_stats
            ORDER BY team_id ASC, player_uid ASC;
            """
        ).fetchall()
    )

    raw_matches = conn.execute(
        """
        SELECT m.id, m.team_id, m.league_id, m.match_date, m.match_date_iso, m.match_time,
               m.away_team, m.home_team, m.away_score, m.home_score,
               m.venue, m.boxscore_url, m.pbp_url, m.summary_url,
               t.team_name
        FROM matches m
        JOIN teams t ON m.team_id = t.team_id
        ORDER BY m.id ASC;
        """
    ).fetchall()

    videos_by_match_id: dict[int, list[dict[str, Any]]] = {}
    has_mv_table = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='match_videos';"
    ).fetchone()[0]
    matched_video_ids: set[str] = set()
    if has_mv_table:
        mv_rows = conn.execute(
            """
            SELECT mv.match_id, yv.video_id, yv.url, yv.title, mv.label
            FROM match_videos mv
            JOIN youtube_videos yv ON mv.video_id = yv.video_id
            ORDER BY mv.match_id ASC, mv.sort_order ASC, mv.video_id ASC;
            """
        ).fetchall()
        for mv in mv_rows:
            matched_video_ids.add(mv["video_id"])
            videos_by_match_id.setdefault(mv["match_id"], []).append(
                {
                    "video_id": mv["video_id"],
                    "url": mv["url"],
                    "title": mv["title"],
                    "label": mv["label"],
                }
            )

    matches: list[dict[str, Any]] = []
    matches_with_video = 0
    for row in raw_matches:
        pirate_side, opponent_team = resolve_pirate_side(
            row["team_name"], row["away_team"], row["home_team"]
        )
        yt_list = videos_by_match_id.get(row["id"], [])
        if yt_list:
            matches_with_video += 1
        matches.append(
            {
                "id": row["id"],
                "team_id": row["team_id"],
                "league_id": row["league_id"],
                "match_date": row["match_date"],
                "match_date_iso": row["match_date_iso"],
                "match_time": row["match_time"],
                "away_team": row["away_team"],
                "home_team": row["home_team"],
                "away_score": row["away_score"],
                "home_score": row["home_score"],
                "pirate_side": pirate_side,
                "opponent_team": opponent_team,
                "venue": row["venue"],
                "boxscore_url": row["boxscore_url"],
                "pbp_url": row["pbp_url"],
                "summary_url": row["summary_url"],
                "youtube_videos": yt_list,
            }
        )

    last_fetched_row = conn.execute("SELECT MAX(last_fetched_at) AS ts FROM teams;").fetchone()
    last_fetched_at = last_fetched_row["ts"] if last_fetched_row else None

    benchmarks = _fetch_benchmarks(conn)

    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_fetched_at": last_fetched_at,
            "counts": {
                "leagues": len(leagues),
                "teams": len(teams),
                "players": len(players),
                "matches": len(matches),
                "youtube_videos": len(matched_video_ids),
                "matches_with_video": matches_with_video,
            },
            "filter_options": {
                "age_groups": _sort_options((r["age_group"] for r in leagues), AGE_GROUP_ORDER),
                "team_groups": _sort_options((r["team_group"] for r in teams), TEAM_GROUP_ORDER),
                "years": sorted({int(r["year"]) for r in leagues}),
                "seasons": _sort_options((r["season"] for r in leagues), SEASON_ORDER),
                "tournament_types": _sort_options(
                    (r["tournament_type"] for r in leagues), TOURNAMENT_TYPE_ORDER
                ),
            },
        },
        "leagues": leagues,
        "teams": teams,
        "players": players,
        "rosters": rosters,
        "batting_stats": batting_stats,
        "pitching_stats": pitching_stats,
        "matches": matches,
        "benchmarks": benchmarks,
    }


def export_json(
    db_path: Path | str = DEFAULT_DB_PATH,
    output_path: Path | str = DEFAULT_OUTPUT_PATH,
) -> Path:
    """從 SQLite 唯讀資料庫匯出壓縮 JSON 至指定路徑。"""
    conn = get_readonly_connection(db_path)
    try:
        dataset = build_dataset(conn)
    finally:
        conn.close()

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, mode="w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024.0
    counts = dataset["meta"]["counts"]
    benchmarks = dataset["benchmarks"]
    print(f"已成功匯出靜態 JSON: {out_path} ({size_kb:.2f} KB)")
    print(
        f"  - 主表筆數: leagues={counts['leagues']}, teams={counts['teams']}, "
        f"players={counts['players']}, matches={counts['matches']}"
    )
    print(
        f"  - 明細筆數: rosters={len(dataset['rosters'])}, "
        f"batting_stats={len(dataset['batting_stats'])}, "
        f"pitching_stats={len(dataset['pitching_stats'])}"
    )
    print(
        f"  - 基準快照: career_batting={len(benchmarks['career_batting'])}, "
        f"career_pitching={len(benchmarks['career_pitching'])}"
    )
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="匯出台中小海盜棒球數據庫靜態 JSON")
    parser.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_DB_PATH),
        help="指定 SQLite 資料庫路徑 (預設: data/pirates.db)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_OUTPUT_PATH),
        help="指定輸出 JSON 路徑 (預設: web/public/data/pirates.json)",
    )
    args = parser.parse_args()
    export_json(db_path=args.db, output_path=args.output)


if __name__ == "__main__":
    main()
