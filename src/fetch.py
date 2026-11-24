"""禮貌下載與本地快取模組。

遵守台中小海盜數據專案規範 (AGENTS.md)：
- 對 myscore.games 採單線程循序請求，每次請求間隔必須 >= 1.5 秒，嚴禁高併發。
- 下載之原始網頁一律先快取至 data/raw_html/，重跑解析時只能讀取本地快取。
"""

import argparse
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# 支援直接執行腳本時能匯入根目錄模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.seed_loader import DEFAULT_CSV_PATH, load_seeds

DEFAULT_CACHE_DIR = Path("data/raw_html")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    reraise=True,
)
def fetch_url(client: httpx.Client, url: str) -> str:
    """單次發送 HTTP GET 請求並回傳文字內容。"""
    resp = client.get(url)
    resp.raise_for_status()
    # myscore 網頁多為 UTF-8
    resp.encoding = "utf-8"
    return resp.text


def fetch_and_cache(
    seed: Dict[str, Any],
    client: httpx.Client,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh_year: Optional[int] = None,
    refresh_season: Optional[str] = None,
    force: bool = False,
    target_team_id: Optional[int] = None,
    total_count: Optional[int] = None,
) -> tuple[Path, bool]:
    """下載並快取單一球隊網頁。

    回傳: (快取路徑, 是否有實際發出請求)
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{seed['league_id']}_{seed['team_id']}.html"

    need_download = force
    if not need_download:
        if target_team_id is not None and seed["team_id"] == target_team_id:
            need_download = True
        elif refresh_year is not None and seed["year"] == refresh_year:
            # 若有指定季節 (如 '秋季')，只刷新該季節；若無指定則刷新該年份所有頁面
            if refresh_season is None or seed.get("season") == refresh_season:
                need_download = True
        elif not cache_file.exists() or cache_file.stat().st_size == 0:
            need_download = True

    if not need_download:
        return cache_file, False

    progress = (
        f"[{seed['csv_row_no']}/{total_count}]"
        if total_count is not None
        else f"[{seed['csv_row_no']}]"
    )
    print(f"[*] 正在下載: {progress} {seed['team_name']} ({seed['source_url']})...")
    html_text = fetch_url(client, seed["source_url"])

    # 內容比對：僅在內容有實質變動或無快取時才寫入，避免無新賽果時變更 mtime
    should_write = True
    if cache_file.exists():
        try:
            existing_text = cache_file.read_text(encoding="utf-8")
            if existing_text == html_text:
                should_write = False
                print("    (內容無異動，保留既有快取時間)")
        except OSError:
            should_write = True

    if should_write:
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write(html_text)

    # 禮貌等待 1.5 ~ 2.5 秒（每次發出請求皆必須等待）
    sleep_sec = random.uniform(1.5, 2.5)
    time.sleep(sleep_sec)

    return cache_file, True


def fetch_all(
    seeds: Optional[List[Dict[str, Any]]] = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh_year: Optional[int] = None,
    refresh_season: Optional[str] = None,
    force: bool = False,
    target_team_id: Optional[int] = None,
) -> List[tuple[Dict[str, Any], Path]]:
    """批量循序下載所有種子頁面。"""
    if seeds is None:
        seeds = load_seeds(DEFAULT_CSV_PATH)

    total_count = len(seeds)
    cache_dir.mkdir(parents=True, exist_ok=True)
    results: List[tuple[Dict[str, Any], Path]] = []

    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
        download_count = 0
        skip_count = 0
        for seed in seeds:
            cache_file, downloaded = fetch_and_cache(
                seed=seed,
                client=client,
                cache_dir=cache_dir,
                refresh_year=refresh_year,
                refresh_season=refresh_season,
                force=force,
                target_team_id=target_team_id,
                total_count=total_count,
            )
            results.append((seed, cache_file))
            if downloaded:
                download_count += 1
            else:
                skip_count += 1

    print(f"[+] 快取檢查完成！新下載: {download_count} 頁，讀取既有快取: {skip_count} 頁。")
    return results


def main():
    parser = argparse.ArgumentParser(description="台中小海盜網頁快取下載器")
    parser.add_argument("--refresh-year", type=int, help="強制刷新指定年份賽事快取 (如 2026)")
    parser.add_argument("--refresh-season", type=str, help="強制刷新指定賽季賽事快取 (如 '秋季')")
    parser.add_argument("--force", action="store_true", help="強制重新下載所有頁面")
    parser.add_argument("--team-id", type=int, help="僅重新下載特定 team_id")
    args = parser.parse_args()

    fetch_all(
        refresh_year=args.refresh_year,
        refresh_season=args.refresh_season,
        force=args.force,
        target_team_id=args.team_id,
    )


if __name__ == "__main__":
    main()
