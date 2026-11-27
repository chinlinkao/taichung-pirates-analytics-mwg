"""P1 雙軌對齊演算法單元測試（tests/test_clips_p1.py）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.clips_asr import (
    _refine_fielding_clips,
    _refine_pitching_clips,
    _solve_batting_dp,
    build_player_matcher,
)
from src.clips_scorebug import _partition_halves_across_videos


class TestClipsP1(unittest.TestCase):
    def test_batting_dp_avoids_greedy_tail_pushout(self) -> None:
        """第 1 棒在半局尾端出現同音雜訊時，DP 應選正確前段序列並夾擠第 2 棒為 medium。"""
        m1 = build_player_matcher("林〇安_2015-01", "林〇安", "10", batting_order="1")
        m2 = build_player_matcher("陳〇宏_2015-02", "陳〇宏", "12", batting_order="2")
        m3 = build_player_matcher("王〇宇_2015-03", "王〇宇", "15", batting_order="3")
        matchers = {m["player_uid"]: m for m in (m1, m2, m3)}

        group = [
            {
                "player_uid": "林〇安_2015-01",
                "clip_type": "batting",
                "event_index": 1,
                "label_short": "一壘安打",
                "event_detail": "林〇安擊出一壘安打",
                "start_sec": 100,
                "align_method": "scorebug_anchor_interp",
                "confidence": "medium",
            },
            {
                "player_uid": "陳〇宏_2015-02",
                "clip_type": "batting",
                "event_index": 2,
                "label_short": "滾地出局",
                "event_detail": "陳〇宏擊出內野滾地球出局",
                "start_sec": 150,
                "align_method": "bracket_interp",
                "confidence": "low",
            },
            {
                "player_uid": "王〇宇_2015-03",
                "clip_type": "batting",
                "event_index": 3,
                "label_short": "三振",
                "event_detail": "王〇宇遭到三振",
                "start_sec": 210,
                "align_method": "scorebug_anchor_interp",
                "confidence": "medium",
            },
        ]
        # 105s 正確唱名第1棒，340s 是半局尾端提到第1棒(會擠掉後面)，195s 唱名第3棒
        win_segs = [
            {"start": 105.0, "end": 110.0, "text": "第一棒背號10號林家安打擊"},
            {"start": 160.0, "end": 165.0, "text": "這球打成滾地"},
            {"start": 195.0, "end": 200.0, "text": "現在輪到第三棒王冠宇上場打擊"},
            {"start": 340.0, "end": 345.0, "text": "前面第一棒林家安表現很不錯"},
        ]
        db_segs: list[dict] = []
        upgraded = _solve_batting_dp(
            group=group,
            win_segs=win_segs,
            matchers_by_uid=matchers,
            all_matchers=list(matchers.values()),
            vid="vid_test",
            eff_start=90,
            eff_end=360,
            h_start=100,
            h_end=350,
            asr_mode="speech_pa",
            effective_pre_roll=1.0,
            matched_segments_for_db=db_segs,
        )
        self.assertEqual(upgraded, 2)
        self.assertEqual(group[0]["align_method"], "dual_track")
        self.assertEqual(group[0]["confidence"], "high")
        self.assertLess(group[0]["start_sec"], 130)

        self.assertEqual(group[1]["align_method"], "dual_track_bracket")
        self.assertEqual(group[1]["confidence"], "medium")
        self.assertGreater(group[1]["start_sec"], group[0]["start_sec"])
        self.assertLess(group[1]["start_sec"], group[2]["start_sec"])

        self.assertEqual(group[2]["align_method"], "dual_track")
        self.assertEqual(group[2]["confidence"], "high")

    def test_partition_overlapping_and_sequential_videos(self) -> None:
        """測試雙流重疊影片(match 32型)過濾與前後接續切分(match 72型)。"""
        expected = [("TOP", 1), ("BOT", 1), ("TOP", 2), ("BOT", 2), ("TOP", 3), ("BOT", 3), ("TOP", 4), ("BOT", 4)]

        # 雙流重疊：v1 含 2上~4上，v2 含 2上~4下且樣本更多
        v1 = {
            "video_id": "v_overlap_short",
            "samples": [{"detected": True, "inning": i, "half": h} for i, h in [(2, "TOP"), (2, "BOT"), (3, "TOP"), (3, "BOT"), (4, "TOP")] for _ in range(5)],
        }
        v2 = {
            "video_id": "v_full",
            "samples": [{"detected": True, "inning": i, "half": h} for i, h in [(2, "TOP"), (2, "BOT"), (3, "TOP"), (3, "BOT"), (4, "TOP"), (4, "BOT")] for _ in range(8)],
        }
        res_overlap = _partition_halves_across_videos([v1, v2], expected)
        self.assertEqual(len(res_overlap), 1)
        self.assertEqual(res_overlap[0][0], "v_full")
        self.assertEqual(res_overlap[0][1], expected)

        # 前後接續：Part 1 含 1上~2下，Part 2 含 3上~4下
        p1 = {
            "video_id": "v_p1",
            "samples": [{"detected": True, "inning": i, "half": h} for i, h in [(1, "TOP"), (1, "BOT"), (2, "TOP"), (2, "BOT")] for _ in range(5)],
        }
        p2 = {
            "video_id": "v_p2",
            "samples": [{"detected": True, "inning": i, "half": h} for i, h in [(3, "TOP"), (3, "BOT"), (4, "TOP"), (4, "BOT")] for _ in range(5)],
        }
        res_seq = _partition_halves_across_videos([p1, p2], expected)
        self.assertEqual(len(res_seq), 2)
        self.assertEqual(res_seq[0][1], expected[:4])
        self.assertEqual(res_seq[1][1], expected[4:])

    def test_pitching_and_fielding_channels(self) -> None:
        """測試中繼投球換投語境與守備 F1/F2 複合比對及同 event_index 同步。"""
        mp = build_player_matcher("吳〇叡_2015-05", "吳〇叡", "22")
        matchers = {"吳〇叡_2015-05": mp}
        segs = [
            {"start": 500.0, "end": 506.0, "text": "四局上台中小海盜更換投手背號22號"},
            {"start": 540.0, "end": 545.0, "text": "下一棒背號88號打者打擊"},
            {"start": 552.0, "end": 557.0, "text": "三壘手接到滾地球後傳向一壘刺殺打者"},
        ]
        tr_map = {"vid1": {"segments": segs}}
        win_map = {("vid1", "4局上"): (470, 620)}
        db_segs: list[dict] = []

        pitch_clips = [
            {
                "player_uid": "吳〇叡_2015-05",
                "video_id": "vid1",
                "half_label": "4局上",
                "event_index": 8,
                "start_sec": 510,
                "half_start_sec": 480,
                "half_end_sec": 600,
                "align_method": "scorebug_reliever_entry",
                "confidence": "medium",
            }
        ]
        p_up = _refine_pitching_clips(
            {("vid1", "4局上"): pitch_clips},
            tr_map,
            win_map,
            matchers,
            list(matchers.values()),
            db_segs,
        )
        self.assertEqual(p_up, 1)
        self.assertEqual(pitch_clips[0]["confidence"], "high")
        self.assertEqual(pitch_clips[0]["align_method"], "dual_track")

        # 守備：同 event_index 雙守備員（三壘手與一壘手），且先透過 F2 命中
        fld_clips = [
            {
                "player_uid": "p_3b",
                "video_id": "vid1",
                "half_label": "4局上",
                "event_index": 10,
                "label_short": "[三] 守備處理",
                "event_detail": "張〇凱打擊成三壘滾地球，三壘手接到傳向一壘刺殺出局",
                "start_sec": 560,
                "half_start_sec": 480,
                "half_end_sec": 600,
                "align_method": "scorebug_anchor_interp",
                "confidence": "medium",
            },
            {
                "player_uid": "p_1b",
                "video_id": "vid1",
                "half_label": "4局上",
                "event_index": 10,
                "label_short": "[一] 刺殺參與(接傳)",
                "event_detail": "張〇凱打擊成三壘滾地球，三壘手接到傳向一壘刺殺出局",
                "start_sec": 560,
                "half_start_sec": 480,
                "half_end_sec": 600,
                "align_method": "scorebug_anchor_interp",
                "confidence": "medium",
            },
        ]
        f_up = _refine_fielding_clips(
            {("vid1", "4局上"): fld_clips},
            tr_map,
            win_map,
            {"張〇凱": {"jersey_number": "77", "batting_order": "3"}},
            list(matchers.values()),
            db_segs,
        )
        self.assertEqual(f_up, 2)
        self.assertEqual(fld_clips[0]["confidence"], "high")
        self.assertEqual(fld_clips[1]["confidence"], "high")
        self.assertEqual(fld_clips[0]["start_sec"], fld_clips[1]["start_sec"])


if __name__ == "__main__":
    unittest.main()
