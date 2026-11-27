"""球員影片集合雙軌定位整合管線 (src/clips_pipeline.py)。

流程：
1. 官網單場紀錄下載與解析入庫 (src/match_events.py)
2. 軌道 A：Scorebug 視覺狀態機掃描與初版對齊 (src/clips_scorebug.py)
3. 軌道 B：本機 faster-whisper 語音逐字稿轉錄與雙軌精修 (src/clips_asr.py)
4. 套用人工秒數覆寫表 data/player_clip_overrides.csv (utf-8-sig)
5. 冪等寫入 SQLite player_video_clips 並匯出 web/public/data/player-clips.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.clips_asr import refine_clips_with_asr
from src.clips_scorebug import (
    TMP_MEDIA_DIR,
    align_game_all_players,
    ensure_scorebug_scans,
)
from src.db import DEFAULT_DB_PATH, get_db_connection, init_db
from src.export_clips_json import DEFAULT_CLIPS_OUTPUT_PATH, export_clips_json
from src.match_events import fetch_and_parse_matches

DEFAULT_OVERRIDES_CSV = Path("data/player_clip_overrides.csv")


def ensure_overrides_template(csv_path: Path = DEFAULT_OVERRIDES_CSV) -> None:
    """若人工微調覆寫表尚不存在，建立含 UTF-8 BOM 之空範本。"""
    if csv_path.exists():
        return
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "match_id",
                "player_uid",
                "clip_type",
                "inning",
                "half_side",
                "event_index",
                "video_id",
                "start_sec",
                "note",
            ]
        )


def apply_manual_overrides(
    clips_by_match: dict[int, list[dict[str, Any]]],
    csv_path: Path = DEFAULT_OVERRIDES_CSV,
) -> int:
    """套用人工微調覆寫秒數。"""
    if not csv_path.exists():
        return 0
    overrides: dict[tuple[int, str, str, int, str, int], dict[str, Any]] = {}
    with open(csv_path, mode="r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not (row.get("match_id") and row.get("player_uid") and row.get("start_sec")):
                continue
            try:
                key = (
                    int(row["match_id"]),
                    row["player_uid"].strip(),
                    row["clip_type"].strip(),
                    int(row["inning"]),
                    row["half_side"].strip().upper(),
                    int(row["event_index"]),
                )
                overrides[key] = {
                    "video_id": (row.get("video_id") or "").strip(),
                    "start_sec": max(0, int(row["start_sec"])),
                }
            except ValueError:
                continue

    if not overrides:
        return 0

    applied = 0
    for mid, clist in clips_by_match.items():
        for c in clist:
            k = (mid, c["player_uid"], c["clip_type"], c["inning"], c["half_side"], c["event_index"])
            if k in overrides:
                ov = overrides[k]
                if ov["video_id"]:
                    c["video_id"] = ov["video_id"]
                c["start_sec"] = ov["start_sec"]
                c["align_method"] = "manual"
                c["confidence"] = "high"
                applied += 1
    return applied


def store_player_clips_to_db(
    conn: sqlite3.Connection,
    clips_by_match: dict[int, list[dict[str, Any]]],
) -> int:
    """將對齊結果冪等寫入 SQLite player_video_clips（保留既有流水號 id）。"""
    total = 0
    with conn:
        for match_id, clist in clips_by_match.items():
            mid = int(match_id)
            existing_rows = conn.execute(
                """
                SELECT id, player_uid, clip_type, inning, half_side, event_index
                FROM player_video_clips
                WHERE match_id = ?
                """,
                (mid,),
            ).fetchall()

            new_keys = {
                (
                    str(c["player_uid"]),
                    str(c["clip_type"]),
                    int(c["inning"]),
                    str(c["half_side"]),
                    int(c["event_index"]),
                )
                for c in clist
            }

            stale_ids = [
                (int(r[0]),)
                for r in existing_rows
                if (str(r[1]), str(r[2]), int(r[3]), str(r[4]), int(r[5])) not in new_keys
            ]
            if stale_ids:
                conn.executemany(
                    "DELETE FROM player_video_clips WHERE id = ?",
                    stale_ids,
                )

            for c in clist:
                conn.execute(
                    """
                    INSERT INTO player_video_clips (
                        match_id, player_uid, video_id, clip_type,
                        inning, half_side, event_index,
                        label_short, event_detail, start_sec,
                        align_method, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id, player_uid, clip_type, inning, half_side, event_index)
                    DO UPDATE SET
                        video_id = excluded.video_id,
                        label_short = excluded.label_short,
                        event_detail = excluded.event_detail,
                        start_sec = excluded.start_sec,
                        align_method = excluded.align_method,
                        confidence = excluded.confidence
                    WHERE
                        video_id IS NOT excluded.video_id
                        OR label_short IS NOT excluded.label_short
                        OR event_detail IS NOT excluded.event_detail
                        OR start_sec IS NOT excluded.start_sec
                        OR align_method IS NOT excluded.align_method
                        OR confidence IS NOT excluded.confidence
                    """,
                    (
                        mid,
                        c["player_uid"],
                        c["video_id"],
                        c["clip_type"],
                        int(c["inning"]),
                        c["half_side"],
                        int(c["event_index"]),
                        c["label_short"],
                        c["event_detail"],
                        int(c["start_sec"]),
                        c["align_method"],
                        c["confidence"],
                    ),
                )
                total += 1
    return total


async def run_clips_pipeline(
    since_date: Optional[str] = "2026-10-01",
    match_ids: Optional[list[int]] = None,
    video_id_filter: Optional[str] = None,
    asr_mode: str = "targeted",
    model_size: str = "base",
    pa_pre_roll: Optional[float] = None,
    force_asr: bool = False,
    parse_only: bool = False,
    align_only: bool = False,
    refresh_html: bool = False,
    db_path: Path | str = DEFAULT_DB_PATH,
    json_output: Path | str = DEFAULT_CLIPS_OUTPUT_PATH,
) -> None:
    print("=" * 60)
    print(f"【球員影片集合雙軌定位管線】啟動 (since={since_date}, asr={asr_mode})")
    print("=" * 60)

    init_db(db_path)
    ensure_overrides_template()

    # 1. 官網單場解析
    print("\n>>> [1/5] 解析官網單場攻守與逐打席紀錄...", flush=True)
    games = fetch_and_parse_matches(
        db_path=db_path,
        since_date=since_date,
        match_ids=match_ids,
        parse_only=parse_only or align_only,
        force_refresh=refresh_html,
    )
    if video_id_filter:
        games = [
            g for g in games
            if any(v["video_id"] == video_id_filter for v in g.get("youtube_videos", []))
        ]
    print(f"  - 取得 {len(games)} 場含逐打席紀錄賽事", flush=True)

    # 2. 軌道 A：Scorebug 掃描與初版對齊
    print("\n>>> [2/5] 軌道 A：Scorebug 視覺狀態機掃描與對齊...", flush=True)
    raw_scans = await ensure_scorebug_scans(games, align_only=align_only)

    conn = get_db_connection(db_path)
    try:
        clips_by_match: dict[int, list[dict[str, Any]]] = {}
        for g in games:
            clist = align_game_all_players(g, raw_scans, conn=conn)
            clips_by_match[int(g["match_id"])] = clist

        track_a_total = sum(len(v) for v in clips_by_match.values())
        print(f"  - 軌道 A 初版對齊完成：共 {track_a_total} 筆片段", flush=True)

        # 3. 軌道 B：本機 faster-whisper ASR 逐字稿精修
        print("\n>>> [3/5] 軌道 B：本機 faster-whisper ASR 語音精修...", flush=True)
        asr_stats = refine_clips_with_asr(
            games=games,
            clips_by_match=clips_by_match,
            conn=conn,
            asr_mode=asr_mode,
            model_size=model_size,
            pa_pre_roll=pa_pre_roll,
            force_asr=force_asr,
            align_only=align_only,
        )
        print(
            f"  - 軌道 B 完成：新轉錄影片 {asr_stats['videos_transcribed']} 支，"
            f"雙軌精修升級 {asr_stats['clips_upgraded']} 筆片段為 high",
            flush=True,
        )

        # 4. 人工覆寫與入庫
        print("\n>>> [4/5] 套用人工覆寫並寫入 player_video_clips...", flush=True)
        ov_cnt = apply_manual_overrides(clips_by_match)
        stored_cnt = store_player_clips_to_db(conn, clips_by_match)
        print(f"  - 入庫完成：共 {stored_cnt} 筆 (含人工覆寫 {ov_cnt} 筆)", flush=True)
    finally:
        conn.close()
        shutil.rmtree(TMP_MEDIA_DIR, ignore_errors=True)

    # 5. 匯出靜態 JSON
    print("\n>>> [5/5] 匯出靜態前端 player-clips.json...", flush=True)
    export_clips_json(db_path=db_path, output_path=json_output)


def main() -> None:
    parser = argparse.ArgumentParser(description="台中小海盜 球員影片集合雙軌定位管線")
    parser.add_argument("--since", type=str, default="2026-10-01", help="起始比賽日期 (YYYY-MM-DD)")
    parser.add_argument("--match-ids", type=str, help="僅處理指定之 match_id 清單 (逗號分隔，例如 154,155,156,157)")
    parser.add_argument("--video-id", type=str, help="僅處理指定單支 video_id (測試用)")
    parser.add_argument(
        "--asr-mode",
        choices=["targeted", "low_medium", "speech_pa", "full", "off"],
        default="targeted",
        help="ASR 語音精修模式 (預設 targeted；speech_pa 為打席優先採主播語音唱名)",
    )
    parser.add_argument("--model", type=str, default="base", help="faster-whisper 模型尺寸 (預設 base)")
    parser.add_argument("--pa-pre-roll", type=float, default=None, help="打席命中唱名後往前預留秒數 (speech_pa 預設 1.0，其餘 5.0)")
    parser.add_argument("--force-asr", action="store_true", help="忽略既有語音轉錄快取，針對目標視窗強制重跑 Whisper")
    parser.add_argument("--parse-only", action="store_true", help="不連網抓 myscore.games")
    parser.add_argument("--align-only", action="store_true", help="僅使用既有本地快取重新對齊")
    parser.add_argument("--refresh-html", action="store_true", help="強制重新下載範圍內單場 HTML 快取")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB_PATH))
    parser.add_argument("--output", type=str, default=str(DEFAULT_CLIPS_OUTPUT_PATH))
    args = parser.parse_args()

    parsed_match_ids: Optional[list[int]] = None
    if args.match_ids:
        parsed_match_ids = [int(x.strip()) for x in args.match_ids.split(",") if x.strip()]

    asyncio.run(
        run_clips_pipeline(
            since_date=args.since,
            match_ids=parsed_match_ids,
            video_id_filter=args.video_id,
            asr_mode=args.asr_mode,
            model_size=args.model,
            pa_pre_roll=args.pa_pre_roll,
            force_asr=args.force_asr,
            parse_only=args.parse_only,
            align_only=args.align_only,
            refresh_html=args.refresh_html,
            db_path=args.db,
            json_output=args.output,
        )
    )


if __name__ == "__main__":
    main()
