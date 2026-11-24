"""台中小海盜棒球數據資料庫管線執行入口 (run_pipeline.py)。

支援參數：
  --discover          : 自動連線探索新賽事連結並更新種子 CSV
  --refresh-year YEAR : 重新下載指定年度賽事快取 (如 --refresh-year 2026)
  --parse-only        : 僅讀取現有快取解析入庫，不發出任何網路請求
  --force             : 強制重新下載所有快取
  --db PATH           : 指定 SQLite 資料庫路徑 (預設: data/pirates.db)
  --json-output PATH  : 指定匯出靜態 JSON 路徑 (預設: web/public/data/pirates.json)
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

# 確保當前目錄在模組搜尋路徑中
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.db import DEFAULT_DB_PATH
from src.discovery import discover_pirate_links
from src.export_json import DEFAULT_OUTPUT_PATH, export_json
from src.fetch import fetch_all
from src.parse import parse_all
from src.verify import VerificationRunner


def run_pipeline(
    discover: bool = False,
    refresh_year: Optional[int] = None,
    refresh_season: Optional[str] = None,
    parse_only: bool = False,
    force: bool = False,
    db_path: Path | str = DEFAULT_DB_PATH,
    json_output: Path | str = DEFAULT_OUTPUT_PATH,
) -> bool:
    """執行數據管線全流程，回傳驗證是否成功。"""
    print("=" * 60)
    print("【台中小海盜棒球數據管線】啟動")
    print("=" * 60)

    # 0. 自動探索新賽事 (若指定 --discover)
    if discover:
        print("\n>>> [階段 0/4] 自動探索 myscore.games 全新賽事與種子清單維護 (discovery.py)...")
        discover_pirate_links()

    # 1. 禮貌下載快取階段
    if not parse_only:
        print("\n>>> [階段 1/4] 執行網頁快取檢查與下載 (fetch.py)...")
        fetch_all(refresh_year=refresh_year, refresh_season=refresh_season, force=force)
    else:
        print("\n>>> [階段 1/4] 已指定 --parse-only，跳過網路下載。")

    # 2. 解析與入庫階段
    print("\n>>> [階段 2/4] 執行 HTML 快取解析與資料庫入庫 (parse.py)...")
    parse_all(db_path=db_path)

    # 3. 自動化驗證階段
    print("\n>>> [階段 3/4] 執行數據邏輯與完整性交叉驗算 (verify.py)...")
    runner = VerificationRunner(db_path=db_path)
    success = runner.run_all()

    if not success:
        print("\n[FAILED] 管線驗證失敗，已略過靜態 JSON 匯出，請檢查上述錯誤訊息！")
        return False

    # 4. 匯出前端靜態 JSON 階段
    print("\n>>> [階段 4/4] 匯出前端靜態 JSON (export_json.py)...")
    export_json(db_path=db_path, output_path=json_output)

    print("\n[SUCCESS] 管線執行完畢，資料庫與前端靜態 JSON 皆已就緒！")
    return True


def main():
    parser = argparse.ArgumentParser(description="台中小海盜棒球數據資料庫 一鍵建置管線")
    parser.add_argument("--discover", action="store_true", help="自動探索 myscore.games 全新賽事並增量更新種子 CSV")
    parser.add_argument("--refresh-year", type=int, help="強制刷新指定年份賽事快取 (如 2026)")
    parser.add_argument("--refresh-season", type=str, help="強制刷新指定賽季賽事快取 (如 '秋季')")
    parser.add_argument("--parse-only", action="store_true", help="跳過網路下載，直接使用既有快取解析入庫")
    parser.add_argument("--force", action="store_true", help="強制重新下載所有頁面快取")
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
        refresh_year=args.refresh_year,
        refresh_season=args.refresh_season,
        parse_only=args.parse_only,
        force=args.force,
        db_path=args.db,
        json_output=args.json_output,
    )
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
