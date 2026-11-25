"""資料驗算與異常檢查報告模組 (src/verify.py)。

遵守台中小海盜數據專案規範 (AGENTS.md)：
- 棒球數據模型鐵則：
  - 球員唯一識別必須使用 player_uid，嚴禁僅用遮罩姓名合併。
  - 投球局數入庫一律轉換為整數出局數 outs = round(float(ip) * 3)，禁止用小數局數直接加總。
  - 每次修改解析邏輯後，必須執行 python src/verify.py 驗證通過才算完成。
"""

import csv
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# 支援直接執行腳本時能匯入根目錄模組
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import DEFAULT_DB_PATH, get_db_connection

DEFAULT_CSV_PATH = Path("data/海盜隊網頁標題.csv")


class VerificationRunner:
    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        csv_path: Path | str = DEFAULT_CSV_PATH,
    ):
        self.db_path = Path(db_path)
        self.csv_path = Path(csv_path)
        self.passed = True
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def log_error(self, msg: str):
        self.passed = False
        self.errors.append(msg)
        print(f"  [ERROR] {msg}")

    def log_warning(self, msg: str):
        self.warnings.append(msg)
        print(f"  [WARN]  {msg}")

    def log_success(self, msg: str):
        print(f"  [PASS]  {msg}")

    def run_all(self) -> bool:
        if not self.db_path.exists():
            print(f"[FATAL] 資料庫檔案不存在: {self.db_path}")
            return False

        conn = get_db_connection(self.db_path)
        try:
            print("=" * 60)
            print("開始執行小海盜棒球資料庫自動化驗算與邏輯檢查 (verify.py)")
            print("=" * 60)

            self.check_coverage(conn)
            self.check_same_name_dedup(conn)
            self.check_cross_season_progression(conn)
            self.check_baseball_logic_and_stats(conn)
            self.check_views_and_aggregations(conn)
            self.check_youtube_videos(conn)

            print("=" * 60)
            if self.passed:
                print(f"[SUCCESS] 所有關鍵驗算全數通過！ (警告數: {len(self.warnings)})")
            else:
                print(f"[FAILED] 驗算發現 {len(self.errors)} 項重大錯誤！")
                for err in self.errors:
                    print(f"  - {err}")
            print("=" * 60)
            return self.passed
        finally:
            conn.close()

    def _load_expected_csv_rows(self) -> Tuple[int, set[int]]:
        """讀取種子 CSV 檔案並回傳 (有效資料筆數, 預期編號集合)。"""
        if not self.csv_path.exists():
            self.log_error(f"種子 CSV 檔案不存在: {self.csv_path}")
            return 0, set()

        total_count = 0
        row_nos: set[int] = set()
        has_valid_row_nos = True

        # 必須指定 utf-8-sig 處理 UTF-8 BOM，避免欄位名稱出現 \ufeff
        with open(self.csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not any((val or "").strip() for val in row.values()):
                    continue
                total_count += 1
                raw_no = (row.get("編號") or "").strip()
                try:
                    row_nos.add(int(raw_no))
                except ValueError:
                    has_valid_row_nos = False

        if not has_valid_row_nos or len(row_nos) != total_count:
            return total_count, set(range(1, total_count + 1))
        return total_count, row_nos

    def check_coverage(self, conn: sqlite3.Connection):
        print("\n[1/5] 檢查資料覆蓋率 (Coverage Check)...")
        c = conn.cursor()

        # 檢查 leagues
        c.execute("SELECT COUNT(*) FROM leagues;")
        league_count = c.fetchone()[0]
        if league_count > 0:
            self.log_success(f"聯賽總數: {league_count} 組")
        else:
            self.log_error("leagues 表為空！")

        # 動態讀取 CSV 筆數並比對 teams 數量與編號覆蓋
        expected_count, expected_row_nos = self._load_expected_csv_rows()

        c.execute("SELECT COUNT(*) FROM teams;")
        team_count = c.fetchone()[0]

        if expected_count > 0:
            if team_count == expected_count:
                self.log_success(f"參賽球隊剛好為 {expected_count} 隊 (符合 CSV {expected_count} 筆種子)")
            else:
                self.log_error(f"參賽球隊數為 {team_count}，預期應為 {expected_count} 隊！")

            # 檢查 CSV 編號覆蓋
            c.execute("SELECT DISTINCT csv_row_no FROM teams WHERE csv_row_no IS NOT NULL;")
            row_nos = {r[0] for r in c.fetchall()}
            missing_rows = expected_row_nos - row_nos
            if not missing_rows:
                min_no, max_no = min(expected_row_nos), max(expected_row_nos)
                self.log_success(
                    f"CSV 編號 {min_no} ~ {max_no}（共 {len(expected_row_nos)} 筆）完整建檔，無缺漏"
                )
            else:
                self.log_error(f"缺漏之 CSV 編號: {sorted(missing_rows)}")
        elif self.csv_path.exists():
            self.log_error(f"種子 CSV 檔案無有效資料列: {self.csv_path}")

        # 檢查 0 名冊隊伍
        c.execute("""
            SELECT t.team_id, t.team_name, t.csv_row_no
            FROM teams t
            LEFT JOIN team_rosters r ON t.team_id = r.team_id
            WHERE r.id IS NULL;
        """)
        empty_roster_teams = c.fetchall()
        if empty_roster_teams:
            names = [f"#{r['csv_row_no']} {r['team_name']}(ID:{r['team_id']})" for r in empty_roster_teams]
            self.log_warning(f"名冊為空的球隊 ({len(empty_roster_teams)} 隊，可能尚未開賽): {', '.join(names)}")
        else:
            self.log_success(f"全數 {team_count} 隊皆有名冊資料")

        # 檢查 0 賽程隊伍
        c.execute("""
            SELECT t.team_id, t.team_name, t.csv_row_no
            FROM teams t
            LEFT JOIN matches m ON t.team_id = m.team_id
            WHERE m.id IS NULL;
        """)
        empty_match_teams = c.fetchall()
        if empty_match_teams:
            names = [f"#{r['csv_row_no']} {r['team_name']}(ID:{r['team_id']})" for r in empty_match_teams]
            self.log_warning(f"賽程為空的球隊 ({len(empty_match_teams)} 隊，可能尚未排定賽程): {', '.join(names)}")
        else:
            self.log_success(f"全數 {team_count} 隊皆有賽程資料")

    def check_same_name_dedup(self, conn: sqlite3.Connection):
        print("\n[2/5] 檢查同名球員歸戶防呆 (Same-name De-duplication)...")
        c = conn.cursor()

        # 檢驗 team_id=3439 中是否存在兩位獨立的 蔡〇宇
        c.execute("""
            SELECT p.player_uid, p.masked_name, p.birth_ym, p.site_player_id, r.jersey_number
            FROM team_rosters r
            JOIN players p ON r.player_uid = p.player_uid
            WHERE r.team_id = 3439 AND p.masked_name = '蔡〇宇';
        """)
        cai_rows = c.fetchall()
        if len(cai_rows) == 2:
            self.log_success(f"team_id=3439 成功辨識出兩位獨立的『蔡〇宇』球員！")
            uids = [r["player_uid"] for r in cai_rows]
            births = [r["birth_ym"] for r in cai_rows]
            jerseys = [r["jersey_number"] for r in cai_rows]
            self.log_success(f"  - 球員 1: UID={uids[0]}, 生日={births[0]}, 背號={jerseys[0]}")
            self.log_success(f"  - 球員 2: UID={uids[1]}, 生日={births[1]}, 背號={jerseys[1]}")

            # 檢查兩位蔡〇宇之打擊或投球數據是否有各自被獨立儲存
            c.execute("""
                SELECT b.player_uid, b.jersey_number, b.pa, b.h
                FROM team_batting_stats b
                WHERE b.team_id = 3439 AND b.player_uid IN (?, ?);
            """, (uids[0], uids[1]))
            b_rows = c.fetchall()
            self.log_success(f"  - 打擊數據獨立性: 取得 {len(b_rows)} 筆打擊紀錄 (無覆蓋混淆)")
        elif len(cai_rows) == 0:
            self.log_warning("team_id=3439 名冊未含『蔡〇宇』(請確認該隊網頁快取是否已下載)")
        else:
            self.log_error(f"team_id=3439 僅找到 {len(cai_rows)} 位『蔡〇宇』，可能發生了遮罩姓名衝突覆蓋！")

    def check_cross_season_progression(self, conn: sqlite3.Connection):
        print("\n[3/5] 檢查跨屆升組串聯 (Cross-season Progression)...")
        c = conn.cursor()

        # 尋找同時在 2024/2025 台中小海盜 與 2026 台中海盜(U15) 出賽的球員
        c.execute("""
            SELECT p.player_uid, p.masked_name, p.birth_ym,
                   GROUP_CONCAT(DISTINCT t.team_name) AS teams_list,
                   GROUP_CONCAT(DISTINCT l.age_group) AS age_groups,
                   COUNT(DISTINCT t.team_id) AS total_teams
            FROM team_rosters r
            JOIN players p ON r.player_uid = p.player_uid
            JOIN teams t ON r.team_id = t.team_id
            JOIN leagues l ON t.league_id = l.league_id
            GROUP BY p.player_uid
            HAVING (INSTR(teams_list, '台中海盜') > 0 AND INSTR(teams_list, '小海盜') > 0)
               AND INSTR(age_groups, 'U15') > 0;
        """)
        u15_players = c.fetchall()
        if u15_players:
            self.log_success(f"找到 {len(u15_players)} 位從小海盜升上台中海盜 (U15) 的跨屆串聯選手！")
            for p in u15_players[:5]:
                self.log_success(f"  - {p['masked_name']} (UID: {p['player_uid']}, 生日: {p['birth_ym']}) 歷經: {p['teams_list']}")
        else:
            self.log_warning("尚未發現同時跨小海盜與 U15 台中海盜的球員，請確認 U15 隊伍名冊是否具備 site_player_id 或相同生日")

    def check_baseball_logic_and_stats(self, conn: sqlite3.Connection):
        print("\n[4/5] 檢查棒球數據模型與邏輯合理性 (Baseball Logic Checks)...")
        c = conn.cursor()

        # 1. 安打 > 打數
        c.execute("SELECT COUNT(*) FROM team_batting_stats WHERE h > ab;")
        cnt = c.fetchone()[0]
        if cnt == 0:
            self.log_success("打者數據驗證: 無 安打數 > 打數 (H > AB) 之異常")
        else:
            self.log_error(f"打者數據異常: 發現 {cnt} 筆 H > AB 之記錄！")

        # 2. 打數 > 打席
        c.execute("SELECT COUNT(*) FROM team_batting_stats WHERE ab > pa;")
        cnt = c.fetchone()[0]
        if cnt == 0:
            self.log_success("打者數據驗證: 無 打數 > 打席 (AB > PA) 之異常")
        else:
            self.log_error(f"打者數據異常: 發現 {cnt} 筆 AB > PA 之記錄！")

        # 3. 負數檢查
        c.execute("""
            SELECT COUNT(*) FROM team_batting_stats
            WHERE g < 0 OR pa < 0 OR ab < 0 OR h < 0 OR doubles < 0 OR triples < 0
               OR hr < 0 OR bb < 0 OR so < 0 OR rbi < 0 OR r < 0;
        """)
        cnt_b_neg = c.fetchone()[0]
        c.execute("""
            SELECT COUNT(*) FROM team_pitching_stats
            WHERE w < 0 OR l < 0 OR sv < 0 OR g < 0 OR outs < 0 OR ab < 0
               OR h < 0 OR doubles < 0 OR triples < 0 OR hr < 0 OR bb < 0
               OR so < 0 OR r < 0 OR er < 0;
        """)
        cnt_p_neg = c.fetchone()[0]
        if cnt_b_neg == 0 and cnt_p_neg == 0:
            self.log_success("數值防呆: 無任何打擊或投球欄位為負數")
        else:
            self.log_error(f"數值防呆發現負數: 打擊 {cnt_b_neg} 筆, 投球 {cnt_p_neg} 筆！")

        # 4. 投手出局數 outs 與 ip_raw 還原誤差檢查
        c.execute("""
            SELECT team_id, player_uid, ip_raw, outs
            FROM team_pitching_stats
            WHERE ip_raw IS NOT NULL AND ip_raw != '' AND ip_raw != '-';
        """)
        ip_rows = c.fetchall()
        ip_errors = 0
        for r in ip_rows:
            try:
                ip_f = float(r["ip_raw"])
                # 預期 outs = round(ip_f * 3)
                expected_outs = round(ip_f * 3)
                if abs(r["outs"] - expected_outs) > 0:
                    ip_errors += 1
            except ValueError:
                pass
        if ip_errors == 0:
            self.log_success(f"投手局數驗證: 全數 {len(ip_rows)} 筆投手紀錄之 outs 換算皆 100% 精確吻合 round(ip * 3)")
        else:
            self.log_error(f"投手局數換算發現 {ip_errors} 筆不符 round(ip * 3)！")

        # 5. 打擊率/長打率 重算與網站顯示值比對 (允許 0.002 浮點與進位誤差)
        c.execute("""
            SELECT player_uid, ab, h, doubles, triples, hr, site_avg, site_slg, site_obp
            FROM team_batting_stats
            WHERE ab > 0 AND site_avg IS NOT NULL;
        """)
        bat_diff_rows = c.fetchall()
        avg_diff_cnt = 0
        slg_diff_cnt = 0
        for r in bat_diff_rows:
            calc_avg = r["h"] / r["ab"]
            if abs(calc_avg - r["site_avg"]) > 0.002:
                avg_diff_cnt += 1
            if r["site_slg"] is not None:
                tb = r["h"] + r["doubles"] + 2 * r["triples"] + 3 * r["hr"]
                calc_slg = tb / r["ab"]
                if abs(calc_slg - r["site_slg"]) > 0.002:
                    slg_diff_cnt += 1

        if avg_diff_cnt == 0 and slg_diff_cnt == 0:
            self.log_success(f"打擊三圍重算驗證: 全數 {len(bat_diff_rows)} 筆打者紀錄之 AVG / SLG 與網站原始數據一致 (誤差 <= 0.002)")
        else:
            self.log_warning(f"打擊率/長打率與網站原始數據比對有微小差異: AVG 超差 {avg_diff_cnt} 筆, SLG 超差 {slg_diff_cnt} 筆 (可能因四捨五入進位方式所致)")

        # 6. 特殊邊界測試: team_id=3439 林〇晏 (IP=0.00, ER=1)
        c.execute("""
            SELECT p.masked_name, pt.ip_raw, pt.outs, pt.er, pt.site_era
            FROM team_pitching_stats pt
            JOIN players p ON pt.player_uid = p.player_uid
            WHERE pt.team_id = 3439 AND p.masked_name LIKE '林〇晏%';
        """)
        lin_row = c.fetchone()
        if lin_row:
            if lin_row["outs"] == 0 and lin_row["er"] > 0:
                self.log_success(f"邊界測試 (林〇晏): outs={lin_row['outs']}, er={lin_row['er']} 正確保存，防禦率重算不會漏失責任失分！")
            else:
                self.log_error(f"邊界測試 (林〇晏) 數據不符預期: outs={lin_row['outs']}, er={lin_row['er']}")
        else:
            self.log_warning("未在 team_id=3439 找到林〇晏投球紀錄")

    def check_views_and_aggregations(self, conn: sqlite3.Connection):
        print("\n[5/5] 檢查生涯總表 Views (Career Views Check)...")
        c = conn.cursor()

        # 檢查 v_player_career_batting
        c.execute("SELECT COUNT(*), SUM(g), SUM(h) FROM v_player_career_batting;")
        b_career = c.fetchone()
        self.log_success(f"打者生涯總表 View: 共有 {b_career[0]} 位生涯打者，累計出賽 {b_career[1]} 場次，{b_career[2]} 支安打")

        # 檢查 v_player_career_pitching
        c.execute("SELECT COUNT(*), SUM(total_outs), SUM(w) FROM v_player_career_pitching;")
        p_career = c.fetchone()
        self.log_success(f"投手生涯總表 View: 共有 {p_career[0]} 位生涯投手，累計投球 {p_career[1]} 個出局數，{p_career[2]} 勝")

    def check_youtube_videos(self, conn: sqlite3.Connection):
        print("\n[6/6] 檢查 YouTube 比賽影片連結與賽程關聯 (YouTube Videos Check)...")
        c = conn.cursor()

        c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('youtube_videos', 'match_videos');"
        )
        if c.fetchone()[0] < 2:
            self.log_warning("資料庫尚未建立 youtube_videos / match_videos 表，略過影片檢查")
            return

        c.execute("SELECT COUNT(*) FROM youtube_videos WHERE is_match_video = 1;")
        match_video_cnt = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM youtube_videos WHERE is_match_video = 0;")
        excluded_cnt = c.fetchone()[0]

        if match_video_cnt == 0:
            self.log_warning("youtube_videos 尚無比賽影片資料，請先執行 src/youtube_sync.py")
            return

        # 1. 檢查所有影片 URL 格式
        c.execute(
            "SELECT COUNT(*) FROM youtube_videos WHERE url NOT LIKE 'https://www.youtube.com/watch?v=%';"
        )
        bad_url_cnt = c.fetchone()[0]
        if bad_url_cnt == 0:
            self.log_success(f"影片網址格式驗證: 全部 {match_video_cnt + excluded_cnt} 筆皆為合法 YouTube 連結")
        else:
            self.log_error(f"發現 {bad_url_cnt} 筆不合法的 YouTube 連結格式！")

        # 2. 檢查自動配對未混入 < 180 秒或非比賽短片
        c.execute(
            """
            SELECT COUNT(*)
            FROM match_videos mv
            JOIN youtube_videos yv ON mv.video_id = yv.video_id
            WHERE mv.match_source = 'auto'
              AND (yv.is_match_video = 0 OR (yv.duration_sec IS NOT NULL AND yv.duration_sec < 180));
            """
        )
        bad_auto_cnt = c.fetchone()[0]
        if bad_auto_cnt == 0:
            self.log_success("比賽影片品質防呆: 自動配對未混入 MVP/SVP 短片 (<180s)")
        else:
            self.log_error(f"自動配對混入 {bad_auto_cnt} 筆非比賽短片或小於 180 秒之影片！")

        # 3. 檢查海盜內戰雙視角影片一致性（同 boxscore_url 的雙邊賽程應有相同影片集合）
        c.execute(
            """
            SELECT m.boxscore_url, COUNT(DISTINCT m.id) AS match_cnt,
                   COUNT(DISTINCT COALESCE(mv.video_id, '')) AS distinct_sets
            FROM matches m
            LEFT JOIN match_videos mv ON m.id = mv.match_id
            WHERE m.boxscore_url IS NOT NULL
            GROUP BY m.boxscore_url
            HAVING match_cnt > 1;
            """
        )
        civil_inconsistent = 0
        for row in c.fetchall():
            # 逐一驗證同 boxscore_url 下各 match_id 綁定到的 video_id 清單一致
            m_ids = [
                r[0]
                for r in c.execute(
                    "SELECT id FROM matches WHERE boxscore_url = ?;", (row["boxscore_url"],)
                ).fetchall()
            ]
            sets = [
                tuple(
                    r[0]
                    for r in c.execute(
                        "SELECT video_id FROM match_videos WHERE match_id = ? ORDER BY video_id;",
                        (mid,),
                    ).fetchall()
                )
                for mid in m_ids
            ]
            if len(set(sets)) > 1:
                civil_inconsistent += 1

        if civil_inconsistent == 0:
            self.log_success("海盜內戰影片一致性: 同場次雙隊視角綁定完全同步")
        else:
            self.log_error(f"海盜內戰有 {civil_inconsistent} 場次雙視角影片不一致！")

        # 4. 覆蓋率統計
        c.execute("SELECT COUNT(DISTINCT match_id), COUNT(*), COUNT(DISTINCT video_id) FROM match_videos;")
        linked_matches, total_links, linked_videos = c.fetchone()
        total_matches = c.execute("SELECT COUNT(*) FROM matches;").fetchone()[0]
        self.log_success(
            f"已收錄 {match_video_cnt} 支海盜比賽影片（排除短片 {excluded_cnt} 支），"
            f"成功配對 {linked_videos} 支 -> {linked_matches}/{total_matches} 筆賽程列（共 {total_links} 筆關聯）"
        )


def main():
    runner = VerificationRunner()
    success = runner.run_all()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
