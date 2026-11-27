"""單場文字紀錄解析、同名拆帳與半成品快取偵測單元測試 (tests/test_match_events.py)。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import SCHEMA_SQL
from src.match_events import (
    _detect_stale_partial_match_ids,
    _infer_event_flags,
    _store_match_events_to_db,
    extract_all_players_involvement,
)


class TestMatchEventsFixes(unittest.TestCase):
    def _create_temp_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO leagues VALUES (467, '2026 秋季U10大聯盟', 2026, '秋季', '例行賽', 'U10', '大聯盟')"
        )
        conn.execute(
            "INSERT INTO teams (team_id, league_id, csv_row_no, team_name, team_group, source_url) "
            "VALUES (4030, 467, 1, '台中小海盜', '海盜本隊', 'https://myscore.games/test')"
        )
        return conn

    def test_infer_event_flags_supports_dadao_and_dacheng(self) -> None:
        """備援正則應支援『打到』與『打成』等滾地球開頭動詞，不依賴先發名冊。"""
        ev1 = _infer_event_flags(
            "許〇恩打到投手方向的滾地球，投手接到傳給一壘手刺殺打者出局",
            known_players=set(),
        )
        self.assertTrue(ev1["is_plate_appearance"])
        self.assertEqual(ev1["batter_name"], "許〇恩")

        ev2 = _infer_event_flags(
            "鄭〇勛打到一壘方向的滾地球，一壘手接到後踩一壘刺殺打者出局",
            known_players=set(),
        )
        self.assertTrue(ev2["is_plate_appearance"])
        self.assertEqual(ev2["batter_name"], "鄭〇勛")

        ev3 = _infer_event_flags(
            "林〇傑打成游擊方向滾地球，刺殺出局",
            known_players=set(),
        )
        self.assertTrue(ev3["is_plate_appearance"])
        self.assertEqual(ev3["batter_name"], "林〇傑")

    def test_duplicate_names_disambiguated_by_stats_and_batting_order(self) -> None:
        """同隊同遮罩姓名（含數據不同、數據完全相同、先發+代打）皆應精準拆帳。"""
        conn = self._create_temp_db()
        try:
            players = [
                ("蔡〇宇_2017-12", "蔡〇宇", "2017-12", "2"),
                ("蔡〇宇_2017-06", "蔡〇宇", "2017-06", "5"),
                ("許〇士_2017-01", "許〇士", "2017-01", "8"),
                ("林〇恩_2017-03", "林〇恩", "2017-03", "9"),
                ("黃〇越_2016-11", "黃〇越", "2016-11", "10"),
            ]
            for uid, nm, bym, jn in players:
                conn.execute("INSERT INTO players (player_uid, masked_name, birth_ym) VALUES (?, ?, ?)", (uid, nm, bym))
                conn.execute("INSERT INTO team_rosters (team_id, player_uid, jersey_number) VALUES (4030, ?, ?)", (uid, jn))

            conn.execute(
                "INSERT INTO matches (id, team_id, league_id, match_date, match_date_iso, match_time, away_team, home_team, away_score, home_score) "
                "VALUES (40, 4030, 467, '10/09', '2026-10-09', '10:00', '台中小海盜', '種子棒球學校', 10, 5)"
            )

            # 場景 A：兩位蔡〇宇數據完全相同（PA=2, AB=1, BB=1, H=1），僅靠棒次 1 棒 vs 3 棒拆帳
            match_meta = {"match_id": 40, "team_id": 4030, "team_name": "台中小海盜", "pirate_side": "away"}
            record_data = {
                "batting_by_team": {
                    "台中小海盜": [
                        {"raw_player": "2 蔡〇宇", "jersey_number": "2", "masked_name": "蔡〇宇", "PA": 2, "AB": 1, "H": 1, "2B": 0, "3B": 0, "HR": 0, "BB": 1, "SO": 0, "RBI": 0},
                        {"raw_player": "8 許〇士", "jersey_number": "8", "masked_name": "許〇士", "PA": 2, "AB": 2, "H": 0, "2B": 0, "3B": 0, "HR": 0, "BB": 0, "SO": 2, "RBI": 0},
                        {"raw_player": "5 蔡〇宇", "jersey_number": "5", "masked_name": "蔡〇宇", "PA": 2, "AB": 1, "H": 1, "2B": 0, "3B": 0, "HR": 0, "BB": 1, "SO": 0, "RBI": 0},
                        {"raw_player": "9 林〇恩", "jersey_number": "9", "masked_name": "林〇恩", "PA": 2, "AB": 2, "H": 0, "2B": 0, "3B": 0, "HR": 0, "BB": 0, "SO": 2, "RBI": 0},
                    ]
                },
                "pitching_by_team": {},
            }
            text_data = {
                "lineups": {
                    "台中小海盜": [
                        {"order": "1", "masked_name": "蔡〇宇", "position": "游"},
                        {"order": "2", "masked_name": "許〇士", "position": "捕"},
                        {"order": "3", "masked_name": "蔡〇宇", "position": "三"},
                        {"order": "4", "masked_name": "林〇恩", "position": "一"},
                    ]
                },
                "half_innings": [
                    {
                        "half_label": "1局上",
                        "inning": 1,
                        "half_side": "TOP",
                        "events": [
                            {"event_index": 1, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "蔡〇宇", "outs_delta": 0, "runs_scored": 0, "involved_positions": [], "text": "蔡〇宇打擊出一壘安打"},
                            {"event_index": 2, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "許〇士", "outs_delta": 1, "runs_scored": 0, "involved_positions": [], "text": "許〇士揮棒落空，遭到投手三振"},
                            {"event_index": 3, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "蔡〇宇", "outs_delta": 0, "runs_scored": 0, "involved_positions": [], "text": "蔡〇宇獲得四壞球保送上壘"},
                            {"event_index": 4, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "林〇恩", "outs_delta": 1, "runs_scored": 0, "involved_positions": [], "text": "林〇恩揮棒落空，遭到投手三振"},
                        ],
                    },
                    {
                        "half_label": "2局上",
                        "inning": 2,
                        "half_side": "TOP",
                        "events": [
                            {"event_index": 1, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "蔡〇宇", "outs_delta": 0, "runs_scored": 0, "involved_positions": [], "text": "蔡〇宇獲得四壞球保送上壘"},
                            {"event_index": 2, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "許〇士", "outs_delta": 1, "runs_scored": 0, "involved_positions": [], "text": "許〇士揮棒落空，遭到投手三振"},
                            {"event_index": 3, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "蔡〇宇", "outs_delta": 0, "runs_scored": 0, "involved_positions": [], "text": "蔡〇宇打擊出一壘安打"},
                            {"event_index": 4, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "林〇恩", "outs_delta": 1, "runs_scored": 0, "involved_positions": [], "text": "林〇恩揮棒落空，遭到投手三振"},
                        ],
                    },
                ],
            }

            parsed = extract_all_players_involvement(match_meta, record_data, text_data, conn)
            p1 = parsed["players"]["蔡〇宇_2017-12"]
            p2 = parsed["players"]["蔡〇宇_2017-06"]
            self.assertEqual(p1["starting_position"], "游")
            self.assertEqual(p2["starting_position"], "三")
            self.assertEqual([(pa["half_label"], pa["event_index"]) for pa in p1["plate_appearances"]], [("1局上", 1), ("2局上", 1)])
            self.assertEqual([(pa["half_label"], pa["event_index"]) for pa in p2["plate_appearances"]], [("1局上", 3), ("2局上", 3)])

            _store_match_events_to_db(conn, match_meta, parsed)
            lineup_rows = conn.execute(
                "SELECT batting_order, player_uid FROM match_lineups WHERE match_id=40 ORDER BY batting_order"
            ).fetchall()
            self.assertEqual(dict((r["batting_order"], r["player_uid"]) for r in lineup_rows)["1"], "蔡〇宇_2017-12")
            self.assertEqual(dict((r["batting_order"], r["player_uid"]) for r in lineup_rows)["3"], "蔡〇宇_2017-06")

            ev_uids = [
                (r["inning"], r["event_index"], r["batter_uid"])
                for r in conn.execute(
                    "SELECT inning, event_index, batter_uid FROM match_events WHERE match_id=40 ORDER BY inning, event_index"
                ).fetchall()
            ]
            self.assertIn((1, 1, "蔡〇宇_2017-12"), ev_uids)
            self.assertIn((1, 3, "蔡〇宇_2017-06"), ev_uids)
            self.assertIn((2, 1, "蔡〇宇_2017-12"), ev_uids)
            self.assertIn((2, 3, "蔡〇宇_2017-06"), ev_uids)
        finally:
            conn.close()

    def test_duplicate_names_starter_plus_pinch_hitter(self) -> None:
        """先發 2 棒＋代打同名（不同統計數據）應依 Boxscore 統計精準拆帳。"""
        conn = self._create_temp_db()
        try:
            for uid, nm, bym, jn in [
                ("陳〇翔_2016-03", "陳〇翔", "2016-03", "29"),
                ("陳〇翔_2015-01", "陳〇翔", "2015-01", "14"),
                ("許〇衡_2016-02", "許〇衡", "2016-02", "5"),
            ]:
                conn.execute("INSERT INTO players (player_uid, masked_name, birth_ym) VALUES (?, ?, ?)", (uid, nm, bym))
                conn.execute("INSERT INTO team_rosters (team_id, player_uid, jersey_number) VALUES (4030, ?, ?)", (uid, jn))

            match_meta = {"match_id": 135, "team_id": 4030, "team_name": "台中小海盜", "pirate_side": "home"}
            record_data = {
                "batting_by_team": {
                    "台中小海盜": [
                        {"raw_player": "5 許〇衡", "jersey_number": "5", "masked_name": "許〇衡", "PA": 1, "AB": 1, "H": 0, "2B": 0, "3B": 0, "HR": 0, "BB": 0, "SO": 1, "RBI": 0},
                        {"raw_player": "29 陳〇翔", "jersey_number": "29", "masked_name": "陳〇翔", "PA": 2, "AB": 2, "H": 0, "2B": 0, "3B": 0, "HR": 0, "BB": 0, "SO": 1, "RBI": 0},
                        {"raw_player": "14 陳〇翔", "jersey_number": "14", "masked_name": "陳〇翔", "PA": 1, "AB": 0, "H": 0, "2B": 0, "3B": 0, "HR": 0, "BB": 1, "SO": 0, "RBI": 1},
                    ]
                },
                "pitching_by_team": {},
            }
            text_data = {
                "lineups": {
                    "台中小海盜": [
                        {"order": "1", "masked_name": "許〇衡", "position": "游"},
                        {"order": "2", "masked_name": "陳〇翔", "position": "捕"},
                    ]
                },
                "half_innings": [
                    {
                        "half_label": "1局下",
                        "inning": 1,
                        "half_side": "BOT",
                        "events": [
                            {"event_index": 1, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "許〇衡", "outs_delta": 1, "runs_scored": 0, "involved_positions": [], "text": "許〇衡揮棒落空，遭到投手三振"},
                            {"event_index": 2, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "陳〇翔", "outs_delta": 1, "runs_scored": 0, "involved_positions": [], "text": "陳〇翔揮棒落空，遭到投手三振"},
                            {"event_index": 3, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "陳〇翔", "outs_delta": 0, "runs_scored": 0, "involved_positions": [], "text": "陳〇翔擊出球造成一壘手接球失誤上壘"},
                            {"event_index": 4, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "陳〇翔", "outs_delta": 0, "runs_scored": 0, "involved_positions": [], "text": "陳〇翔獲得四壞球保送上壘，帶有1分打點"},
                        ],
                    }
                ],
            }

            parsed = extract_all_players_involvement(match_meta, record_data, text_data, conn)
            self.assertEqual(len(parsed["players"]["陳〇翔_2016-03"]["plate_appearances"]), 2)
            self.assertEqual(
                [pa["event_index"] for pa in parsed["players"]["陳〇翔_2015-01"]["plate_appearances"]],
                [4],
            )
        finally:
            conn.close()

    def test_global_unique_player_fallback_for_guest_players(self) -> None:
        """未列於當隊 team_rosters 但全庫唯一之跨隊支援球員應正確歸戶；查無此人則忽略。"""
        conn = self._create_temp_db()
        try:
            conn.execute(
                "INSERT INTO players (player_uid, masked_name, birth_ym) VALUES ('許〇恩_2015-03', '許〇恩', '2015-03')"
            )
            match_meta = {"match_id": 134, "team_id": 4030, "team_name": "台中小海盜", "pirate_side": "away"}
            record_data = {"batting_by_team": {"台中小海盜": []}, "pitching_by_team": {}}
            text_data = {
                "lineups": {"台中小海盜": []},
                "half_innings": [
                    {
                        "half_label": "2局上",
                        "inning": 2,
                        "half_side": "TOP",
                        "events": [
                            {"event_index": 1, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "許〇恩", "outs_delta": 1, "runs_scored": 0, "involved_positions": [], "text": "許〇恩沒有出棒，遭到投手三振"},
                            {"event_index": 2, "event_type": "pa", "is_pitching_change": False, "pitcher_name": None, "pitcher_jersey": None, "is_runner_event": False, "runner_name": None, "is_sub_event": False, "is_plate_appearance": True, "batter_name": "黃〇驊", "outs_delta": 1, "runs_scored": 0, "involved_positions": [], "text": "黃〇驊揮棒落空，遭到投手三振"},
                        ],
                    }
                ],
            }
            parsed = extract_all_players_involvement(match_meta, record_data, text_data, conn)
            self.assertIn("許〇恩_2015-03", parsed["players"])
            self.assertEqual(len(parsed["players"]["許〇恩_2015-03"]["plate_appearances"]), 1)
            self.assertEqual(len(parsed["players"]), 1)
        finally:
            conn.close()

    def test_detect_stale_partial_match_ids(self) -> None:
        """僅當賽季 PA > 快取 PA 且單場得分缺漏或快取過期時才標記重抓，避免誤抓已完整場次。"""
        conn = self._create_temp_db()
        try:
            conn.execute("INSERT INTO players (player_uid, masked_name, birth_ym) VALUES ('王〇明_2017-01', '王〇明', '2017-01')")
            conn.execute("INSERT INTO team_batting_stats (team_id, player_uid, g, pa) VALUES (4030, '王〇明_2017-01', 1, 3)")
            conn.execute(
                "INSERT INTO matches (id, team_id, league_id, match_date, match_date_iso, match_time, away_team, home_team, away_score, home_score, boxscore_url, pbp_url) "
                "VALUES (2468, 4030, 467, '11/21', '2026-11-21', '14:00', '台中雷爵', '台中小海盜', 12, 2, 'https://myscore.games/show_record.php?id=999&game_id=467', 'https://myscore.games/show_text.php?id=999&game_id=467')"
            )

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                rec_p = tmp_path / "467_999_record.html"
                txt_p = tmp_path / "467_999_text.html"
                rec_p.write_text(
                    "<table><tr><th>台中雷爵打者成績</th></tr><tr><th>球員</th><th>PA</th><th>R</th></tr><tr><td>1 甲〇</td><td>10</td><td>7</td></tr></table>"
                    "<table><tr><th>台中小海盜打者成績</th></tr><tr><th>球員</th><th>PA</th><th>R</th></tr><tr><td>1 王〇明</td><td>2</td><td>2</td></tr></table>",
                    encoding="utf-8",
                )
                txt_p.write_text("<html></html>", encoding="utf-8")

                match_rows = [
                    {
                        "match_id": 2468,
                        "team_id": 4030,
                        "league_id": 467,
                        "away_team": "台中雷爵",
                        "home_team": "台中小海盜",
                        "away_score": 12,
                        "home_score": 2,
                        "boxscore_url": "https://myscore.games/show_record.php?id=999&game_id=467",
                    }
                ]
                stale = _detect_stale_partial_match_ids(conn, match_rows, tmp_path, tmp_path)
                self.assertEqual(stale, {2468})

                # 若賽季 PA 已與快取 PA 一致 (2 == 2)，即使 R < away_score 也不誤判為過期
                conn.execute("UPDATE team_batting_stats SET pa = 2 WHERE team_id = 4030")
                stale_ok = _detect_stale_partial_match_ids(conn, match_rows, tmp_path, tmp_path)
                self.assertEqual(stale_ok, set())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
