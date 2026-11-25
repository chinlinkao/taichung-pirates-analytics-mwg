"""資料庫連線、Schema 與 Views 初始化模組。

遵守台中小海盜數據專案規範 (AGENTS.md)：
- SQLite 連線必須開啟 PRAGMA foreign_keys = ON;
"""

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("data/pirates.db")

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- 1. 聯賽／賽事組別維度表（對應網址 game_id）
CREATE TABLE IF NOT EXISTS leagues (
    league_id       INTEGER PRIMARY KEY,         -- 原網址參數 game_id (如 302, 430, 467)
    league_title    TEXT NOT NULL,               -- 完整賽事名稱
    year            INTEGER NOT NULL,            -- 年份 (2024, 2025, 2026)
    season          TEXT NOT NULL,               -- '春季' | '秋季' | '其他'
    tournament_type TEXT NOT NULL,               -- '例行賽' | '季後賽' | '台灣大賽'
    age_group       TEXT NOT NULL,               -- 'U8' | 'U10' | 'U12' | 'U15'
    division        TEXT NOT NULL                -- '大聯盟' | '小聯盟' | '一般'
);

-- 2. 參賽球隊主表（對應 CSV 41 筆）
CREATE TABLE IF NOT EXISTS teams (
    team_id         INTEGER PRIMARY KEY,         -- 原網址參數 team_id
    league_id       INTEGER NOT NULL REFERENCES leagues(league_id),
    csv_row_no      INTEGER,                     -- CSV 原始編號 (1~41)
    team_name       TEXT NOT NULL,               -- 如 '台中小海盜(白)A', '台中海盜'
    team_group      TEXT NOT NULL,               -- '海盜本隊' | '白隊' | '黑隊' | 'U15海盜'
    leader          TEXT,                        -- 領隊
    head_coach      TEXT,                        -- 總教練
    manager         TEXT,                        -- 管理
    coaches         TEXT,                        -- 教練
    source_url      TEXT NOT NULL,               -- 移除 #team-section 後之網址
    last_fetched_at TEXT                         -- ISO8601 下載時間
);

-- 3. 選手跨屆唯一識別表
CREATE TABLE IF NOT EXISTS players (
    player_uid      TEXT PRIMARY KEY,            -- 優先 'id:{site_player_id}'，無則 '{masked_name}_{birth_ym}'
    site_player_id  INTEGER,                     -- 網站內部 player_id (若無超連結則為 NULL)
    masked_name     TEXT NOT NULL,               -- 遮罩姓名 (如 '蔡〇宇')
    birth_ym        TEXT,                        -- 出生年月 'YYYY-MM' (如 '2015-11')
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(masked_name, birth_ym)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_players_site_id
    ON players(site_player_id)
    WHERE site_player_id IS NOT NULL;

-- 4. 各聯賽球隊登錄名冊
CREATE TABLE IF NOT EXISTS team_rosters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    player_uid      TEXT NOT NULL REFERENCES players(player_uid),
    jersey_number   TEXT,                        -- 文字型別保留前導零 (如 '01')
    position        TEXT,                        -- 登錄守位 (如 'P', 'C', 'IF', 'OF')
    UNIQUE(team_id, player_uid)
);

-- 5. 單一聯賽球隊打者表現
CREATE TABLE IF NOT EXISTS team_batting_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    player_uid      TEXT NOT NULL REFERENCES players(player_uid),
    jersey_number   TEXT,
    g               INTEGER NOT NULL DEFAULT 0,  -- 對應表頭全形 'Ｇ'
    pa              INTEGER NOT NULL DEFAULT 0,
    ab              INTEGER NOT NULL DEFAULT 0,
    h               INTEGER NOT NULL DEFAULT 0,
    doubles         INTEGER NOT NULL DEFAULT 0,  -- 對應 '2B'
    triples         INTEGER NOT NULL DEFAULT 0,  -- 對應 '3B'
    hr              INTEGER NOT NULL DEFAULT 0,
    bb              INTEGER NOT NULL DEFAULT 0,
    so              INTEGER NOT NULL DEFAULT 0,
    rbi             INTEGER NOT NULL DEFAULT 0,
    r               INTEGER NOT NULL DEFAULT 0,
    sb_top10        INTEGER,                     -- 頁尾 Top 10 - Batting 補充盜壘數 (非前十為 NULL)
    site_obp        REAL,                        -- 網站原始顯示 OBP (供驗證對照)
    site_slg        REAL,                        -- 網站原始顯示 SLG (供驗證對照)
    site_avg        REAL,                        -- 網站原始顯示 AVG (供驗證對照)
    UNIQUE(team_id, player_uid)
);

-- 6. 單一聯賽球隊投手表現
CREATE TABLE IF NOT EXISTS team_pitching_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    player_uid      TEXT NOT NULL REFERENCES players(player_uid),
    jersey_number   TEXT,
    w               INTEGER NOT NULL DEFAULT 0,
    l               INTEGER NOT NULL DEFAULT 0,
    sv              INTEGER NOT NULL DEFAULT 0,
    g               INTEGER NOT NULL DEFAULT 0,  -- 對應表頭半形 'G'
    outs            INTEGER NOT NULL DEFAULT 0,  -- round(float(ip_raw) * 3)
    ip_raw          TEXT,                        -- 保留原始字串 (如 '18.67')
    ab              INTEGER NOT NULL DEFAULT 0,
    h               INTEGER NOT NULL DEFAULT 0,
    doubles         INTEGER NOT NULL DEFAULT 0,
    triples         INTEGER NOT NULL DEFAULT 0,
    hr              INTEGER NOT NULL DEFAULT 0,
    bb              INTEGER NOT NULL DEFAULT 0,
    so              INTEGER NOT NULL DEFAULT 0,
    r               INTEGER NOT NULL DEFAULT 0,
    er              INTEGER NOT NULL DEFAULT 0,
    site_opp_avg    REAL,                        -- 網站原始顯示被打擊率 (AVG)
    site_era        REAL,                        -- 網站原始顯示 ERA
    UNIQUE(team_id, player_uid)
);

-- 7. 本次賽事賽程及結果
CREATE TABLE IF NOT EXISTS matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    league_id       INTEGER NOT NULL REFERENCES leagues(league_id),
    match_date      TEXT,                        -- 原始日期 (如 '03/15')
    match_date_iso  TEXT,                        -- 推導日期 (如 '2026-03-15')
    match_time      TEXT,                        -- 如 '08:00'
    away_team       TEXT NOT NULL,
    home_team       TEXT NOT NULL,
    away_score      INTEGER,                     -- 未開賽顯示 ':' 時為 NULL
    home_score      INTEGER,                     -- 未開賽顯示 ':' 時為 NULL
    venue           TEXT,                        -- 場地
    boxscore_url    TEXT,                        -- '攻守數據' 連結 (預留 Phase 2)
    pbp_url         TEXT,                        -- '文字紀錄' 連結
    summary_url     TEXT,                        -- '戰況表' 連結
    UNIQUE(team_id, match_date, match_time, away_team, home_team)
);

-- 8. YouTube 頻道海盜影片總表
CREATE TABLE IF NOT EXISTS youtube_videos (
    video_id            TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL,
    channel_handle      TEXT NOT NULL,
    source_tab          TEXT,                     -- 'streams' | 'videos'
    title               TEXT NOT NULL,
    url                 TEXT NOT NULL,            -- https://www.youtube.com/watch?v={video_id}
    duration_sec        INTEGER,
    is_live_content     INTEGER,                  -- 1 | 0 | NULL
    broadcast_start_utc TEXT,                     -- ISO8601 UTC
    broadcast_date_iso  TEXT,                     -- 台灣時間 (UTC+8) YYYY-MM-DD
    broadcast_time      TEXT,                     -- 台灣時間 (UTC+8) HH:MM
    part_label          TEXT,                     -- 如 'Part 1', 'Part 2' 或 NULL
    is_match_video      INTEGER NOT NULL DEFAULT 1, -- 1=比賽影片, 0=非比賽短片(MVP/SVP等)
    fetched_at          TEXT
);

-- 9. 賽程與比賽影片多對多關聯表（支援單場多段影片與海盜內戰雙視角共用）
CREATE TABLE IF NOT EXISTS match_videos (
    match_id            INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    video_id            TEXT NOT NULL REFERENCES youtube_videos(video_id) ON DELETE CASCADE,
    label               TEXT NOT NULL DEFAULT '比賽影片',
    match_confidence    REAL,
    match_source        TEXT NOT NULL DEFAULT 'auto', -- 'auto' | 'manual'
    sort_order          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (match_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_match_videos_match_id ON match_videos(match_id);
CREATE INDEX IF NOT EXISTS idx_match_videos_video_id ON match_videos(video_id);

-- ============================================================
-- 分析用 Views（支援跨屆動態重算率值，不受單頁四捨五入影響）
-- ============================================================

-- 跨季球員打擊生涯總表 View
CREATE VIEW IF NOT EXISTS v_player_career_batting AS
SELECT
    p.player_uid,
    p.masked_name,
    p.birth_ym,
    COUNT(DISTINCT b.team_id) AS seasons_played,
    GROUP_CONCAT(DISTINCT l.age_group) AS age_groups,
    SUM(b.g) AS g,
    SUM(b.pa) AS pa,
    SUM(b.ab) AS ab,
    SUM(b.h) AS h,
    SUM(b.doubles) AS doubles,
    SUM(b.triples) AS triples,
    SUM(b.hr) AS hr,
    SUM(b.bb) AS bb,
    SUM(b.so) AS so,
    SUM(b.rbi) AS rbi,
    SUM(b.r) AS r,
    SUM(b.h + b.doubles + 2 * b.triples + 3 * b.hr) AS total_bases,
    CASE WHEN SUM(b.ab) > 0
         THEN ROUND(CAST(SUM(b.h) AS REAL) / SUM(b.ab), 3)
         ELSE 0.0 END AS calc_avg,
    CASE WHEN SUM(b.ab + b.bb) > 0
         THEN ROUND(CAST(SUM(b.h + b.bb) AS REAL) / SUM(b.ab + b.bb), 3)
         ELSE 0.0 END AS calc_obp,
    CASE WHEN SUM(b.ab) > 0
         THEN ROUND(CAST(SUM(b.h + b.doubles + 2 * b.triples + 3 * b.hr) AS REAL) / SUM(b.ab), 3)
         ELSE 0.0 END AS calc_slg
FROM players p
JOIN team_batting_stats b ON p.player_uid = b.player_uid
JOIN teams t ON b.team_id = t.team_id
JOIN leagues l ON t.league_id = l.league_id
GROUP BY p.player_uid;

-- 跨季球員投球生涯總表 View（同時提供 7 局制少棒基準與 9 局制對齊網站基準）
CREATE VIEW IF NOT EXISTS v_player_career_pitching AS
SELECT
    p.player_uid,
    p.masked_name,
    p.birth_ym,
    COUNT(DISTINCT pt.team_id) AS seasons_pitched,
    GROUP_CONCAT(DISTINCT l.age_group) AS age_groups,
    SUM(pt.w) AS w,
    SUM(pt.l) AS l,
    SUM(pt.sv) AS sv,
    SUM(pt.g) AS g,
    SUM(pt.outs) AS total_outs,
    printf('%d.%d', SUM(pt.outs) / 3, SUM(pt.outs) % 3) AS ip_baseball,
    ROUND(SUM(pt.outs) / 3.0, 2) AS ip_decimal,
    SUM(pt.ab) AS ab,
    SUM(pt.h) AS h,
    SUM(pt.hr) AS hr,
    SUM(pt.bb) AS bb,
    SUM(pt.so) AS so,
    SUM(pt.r) AS r,
    SUM(pt.er) AS er,
    CASE WHEN SUM(pt.outs) > 0
         THEN ROUND((SUM(pt.er) * 27.0) / SUM(pt.outs), 2)
         ELSE NULL END AS calc_era_9,
    CASE WHEN SUM(pt.outs) > 0
         THEN ROUND((SUM(pt.er) * 21.0) / SUM(pt.outs), 2)
         ELSE NULL END AS calc_era_7,
    CASE WHEN SUM(pt.outs) > 0
         THEN ROUND(((SUM(pt.h) + SUM(pt.bb)) * 3.0) / SUM(pt.outs), 2)
         ELSE NULL END AS calc_whip,
    CASE WHEN SUM(pt.outs) > 0
         THEN ROUND((SUM(pt.so) * 21.0) / SUM(pt.outs), 2)
         ELSE NULL END AS calc_k7
FROM players p
JOIN team_pitching_stats pt ON p.player_uid = pt.player_uid
JOIN teams t ON pt.team_id = t.team_id
JOIN leagues l ON t.league_id = l.league_id
GROUP BY p.player_uid;
"""


def get_db_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """建立 SQLite 連線並開啟外鍵約束。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """初始化資料庫表格與檢視表。"""
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("資料庫初始化成功！")
