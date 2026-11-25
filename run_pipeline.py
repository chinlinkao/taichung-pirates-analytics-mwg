"""台中小海盜棒球數據資料庫管線執行入口 (run_pipeline.py)。

支援參數：
  --discover            : 自動連線探索新賽事連結並更新種子 CSV
  --auto-current        : 依台灣時間自動推算當季年度與賽季（含 CSV 防呆回退）
  --refresh-year YEAR   : 重新下載指定年度賽事快取 (如 --refresh-year 2026)
  --refresh-season STR  : 重新下載指定賽季賽事快取 (如 --refresh-season 秋季)
  --parse-only          : 僅讀取現有快取解析入庫，不發出任何網路請求
  --force               : 強制重新下載所有快取
  --db PATH             : 指定 SQLite 資料庫路徑 (預設: data/pirates.db)
  --json-output PATH    : 指定匯出靜態 JSON 路徑 (預設: web/public/data/pirates.json)
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# 確保當前目錄在模組搜尋路徑中
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.db import DEFAULT_DB_PATH
from src.discovery import discover_pirate_links
from src.export_json import DEFAULT_OUTPUT_PATH, export_json
from src.fetch import fetch_all
from src.parse import parse_all
from src.seed_loader import DEFAULT_CSV_PATH, load_seeds
from src.verify import VerificationRunner
from src.youtube_match import match_and_store_videos
from src.youtube_sync import DEFAULT_YT_CACHE_PATH, sync_youtube_videos

TAIPEI_TZ = timezone(timedelta(hours=8))


def resolve_current_year_season(csv_path: Path | str = DEFAULT_CSV_PATH) -> tuple[int, str]:
    """依台灣時區自動推算當季年度與賽季，若尚無賽事則回退至 CSV 最新賽事。"""
    now = datetime.now(TAIPEI_TZ)
    target_year = now.year
    target_season = "春季" if now.month <= 6 else "秋季"

    seeds = load_seeds(csv_path)
    if any(s["year"] == target_year and s.get("season") == target_season for s in seeds):
        return target_year, target_season

    if not seeds:
        return target_year, target_season

    def _season_rank(season: Optional[str]) -> int:
        if season == "秋季":
            return 2
        if season == "春季":
            return 1
        return 0

    latest_seed = max(seeds, key=lambda s: (s["year"], _season_rank(s.get("season"))))
    fallback_year = int(latest_seed["year"])
    fallback_season = str(latest_seed.get("season") or target_season)
    print(
        f"[INFO] CSV 尚無 {target_year} {target_season} 賽事，"
        f"自動回退刷新最新賽事: {fallback_year} {fallback_season}"
    )
    return fallback_year, fallback_season


def run_pipeline(
    discover: bool = False,
    auto_current: bool = False,
    refresh_year: Optional[int] = None,
    refresh_season: Optional[str] = None,
    parse_only: bool = False,
    force: bool = False,
    yt_full: bool = False,
    skip_youtube: bool = False,
    db_path: Path | str = DEFAULT_DB_PATH,
    json_output: Path | str = DEFAULT_OUTPUT_PATH,
) -> bool:
    """執行數據管線全流程，回傳驗證是否成功。"""
    print("=" * 60)
    print("【台中小海盜棒球數據管線】啟動")
    print("=" * 60)

    # 0. 自動探索新賽事 (若指定 --discover)
    if discover:
        print("\n>>> [階段 0/5] 自動探索 myscore.games 全新賽事與種子清單維護 (discovery.py)...")
        discover_pirate_links()

    # 若指定 --auto-current 且未明確指定年度與賽季，於 discover 後推算當季目標
    if auto_current and refresh_year is None and refresh_season is None:
        refresh_year, refresh_season = resolve_current_year_season()
        print(f"[INFO] 自動推算當季目標: {refresh_year} {refresh_season}")

    # 1. 禮貌下載快取階段
    if not parse_only:
        print("\n>>> [階段 1/5] 執行網頁快取檢查與下載 (fetch.py)...")
        fetch_all(refresh_year=refresh_year, refresh_season=refresh_season, force=force)
    else:
        print("\n>>> [階段 1/5] 已指定 --parse-only，跳過網路下載。")

    # 2. 解析與入庫階段
    print("\n>>> [階段 2/5] 執行 HTML 快取解析與資料庫入庫 (parse.py)...")
    parse_all(db_path=db_path)

    # 3. YouTube 比賽影片同步與賽程配對階段
    print("\n>>> [階段 3/5] 執行 YouTube 比賽影片同步與賽程配對...")
    if not parse_only and not skip_youtube:
        sync_youtube_videos(cache_path=DEFAULT_YT_CACHE_PATH, full=yt_full)
    elif parse_only:
        print("  - 已指定 --parse-only，跳過 YouTube 網路同步，僅使用既有快取。")
    else:
        print("  - 已指定 --skip-youtube，跳過 YouTube 網路同步。")
    match_and_store_videos(db_path=db_path, cache_path=DEFAULT_YT_CACHE_PATH)

    # 4. 自動化驗證階段
    print("\n>>> [階段 4/5] 執行數據邏輯與完整性交叉驗算 (verify.py)...")
    runner = VerificationRunner(db_path=db_path)
    success = runner.run_all()

    if not success:
        print("\n[FAILED] 管線驗證失敗，已略過靜態 JSON 匯出，請檢查上述錯誤訊息！")
        return False

    # 5. 匯出前端靜態 JSON 階段
    print("\n>>> [階段 5/5] 匯出前端靜態 JSON (export_json.py)...")
    export_json(db_path=db_path, output_path=json_output)

    print("\n[SUCCESS] 管線執行完畢，資料庫與前端靜態 JSON 皆已就緒！")
    return True


def main():
    parser = argparse.ArgumentParser(description="台中小海盜棒球數據資料庫 一鍵建置管線")
    parser.add_argument("--discover", action="store_true", help="自動探索 myscore.games 全新賽事並增量更新種子 CSV")
    parser.add_argument("--auto-current", action="store_true", help="依台灣時區自動推算當季年度與賽季")
    parser.add_argument("--refresh-year", type=int, help="強制刷新指定年份賽事快取 (如 2026)")
    parser.add_argument("--refresh-season", type=str, help="強制刷新指定賽季賽事快取 (如 '秋季')")
    parser.add_argument("--parse-only", action="store_true", help="跳過網路下載，直接使用既有快取解析入庫")
    parser.add_argument("--force", action="store_true", help="強制重新下載所有頁面快取")
    parser.add_argument("--yt-full", action="store_true", help="完整掃描 YouTube 兩頻道歷史海盜影片")
    parser.add_argument("--skip-youtube", action="store_true", help="跳過 YouTube 網路同步，僅使用本地快取配對")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB_PATH), help="指定 SQLite 資料庫路徑")
    parser.add_argument(
        "--json-output",
        type=str,
        default=str(DEFAULT_OUTPUT_PATH),
        help="指定匯出靜態 JSON 路徑",
    )
    args = parser.parse_args()

    success = run_pipeline(
        discover=args.discover,
        auto_current=args.auto_current,
        refresh_year=args.refresh_year,
        refresh_season=args.refresh_season,
        parse_only=args.parse_only,
        force=args.force,
        yt_full=args.yt_full,
        skip_youtube=args.skip_youtube,
        db_path=args.db,
        json_output=args.json_output,
    )
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
