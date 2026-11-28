"""軌道 B-0：ASR 語音半局重建與幻覺過濾單元測試 (tests/test_asr_half_anchor.py)。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.clips_asr import (
    _is_prompt_hallucination,
    _solve_batting_dp,
    build_player_matcher,
    infer_half_segments_from_transcript,
)


def _pad_segments(base_segs: list[dict], target_count: int = 24) -> list[dict]:
    """填充無關語音片段以滿足最小語音長度門檻。"""
    out = list(base_segs)
    idx = 0
    while len(out) < target_count:
        t = 10.0 + idx * 5.0
        out.append({"start": t, "end": t + 3.0, "text": f"現場觀眾加油聲浪第{idx}段"})
        idx += 1
    out.sort(key=lambda s: s["start"])
    return out


class TestAsrHalfAnchor(unittest.TestCase):
    def test_prompt_hallucination_filter(self) -> None:
        """應攔截 initial_prompt 重述幻覺（含數字連數、四壘與多半局羅列），且不誤殺真實播報句。"""
        self.assertTrue(_is_prompt_hallucination("常見詞彙１一局下、三振、四壽手、接殺、換投。"))
        self.assertTrue(_is_prompt_hallucination("二局下、三振、四壘手、接殺、換投。"))
        self.assertTrue(_is_prompt_hallucination("接殺：二局下、三壘安打、游擊手、三振、四壘"))
        self.assertTrue(_is_prompt_hallucination("兩局上、二局下：14、56、57、58、59、60號。"))
        self.assertTrue(_is_prompt_hallucination("二局下、四壘安打：二局小失誤，"))
        self.assertTrue(_is_prompt_hallucination("四局下、二局上：二局下１二局後期。"))
        self.assertFalse(_is_prompt_hallucination("比賽來到二局下半，三壘手準備就位"))
        self.assertFalse(_is_prompt_hallucination("林〇徹打到投手方向的滾地球，傳向一壘刺殺出局"))

    def test_tzmq_pregame_idle_and_lineup_intro_anchoring(self) -> None:
        """模擬 TZmQLL82fSQ：片頭 13.5 分鐘賽前空檔 + 先發唱名群集 + 1局上結束，驗證林〇徹定位至 ~17:45。"""
        matchers = [
            build_player_matcher("高〇凡_2017-11", "高〇凡", "3", batting_order="1"),
            build_player_matcher("黃〇鋒_2019-07", "黃〇鋒", "17", batting_order="3"),
            build_player_matcher("楊〇軒_2017-11", "楊〇軒", "96", batting_order="6"),
            build_player_matcher("林〇徹_2017-11", "林〇徹", "99", batting_order="8"),
            build_player_matcher("尤〇翔_2018-01", "尤〇翔", "16", batting_order="9"),
        ]
        m_map = {m["player_uid"]: m for m in matchers}

        segs = _pad_segments(
            [
                {"start": 542.0, "end": 546.0, "text": "今天在青年公園棒球場進行冠軍賽"},
                {"start": 808.0, "end": 810.0, "text": "第一棒是高〇凡"},
                {"start": 810.3, "end": 812.0, "text": "三棒是黃〇鋒"},
                {"start": 813.5, "end": 815.5, "text": "六棒是楊〇軒"},
                {"start": 815.8, "end": 817.5, "text": "八棒林〇徹，九棒是尤〇翔"},
                {"start": 1038.2, "end": 1041.0, "text": "打擊輪到是小海盜第六棒的楊〇軒"},
                {"start": 1059.4, "end": 1062.0, "text": "是一個飛球在中間方向接殺"},
                {"start": 1082.3, "end": 1085.0, "text": "投手攔住傳向一壘拿下出局"},
                {"start": 1109.3, "end": 1112.0, "text": "又是投手正面的滾地球傳向一壘"},
                {"start": 1129.1, "end": 1132.5, "text": "接下來輪到小海盜第九棒尤〇翔"},
                {"start": 1158.0, "end": 1161.0, "text": "就結束了一局上半"},
                {"start": 1213.4, "end": 1216.0, "text": "比賽來到一局的下半"},
                {"start": 2061.0, "end": 2064.0, "text": "也結束了二局上半"},
                {"start": 2173.0, "end": 2176.0, "text": "來到二局下半"},
            ]
        )
        expected = [("TOP", 1), ("BOT", 1), ("TOP", 2), ("BOT", 2)]
        pa_counts = {"1局上": 9, "1局下": 8, "2局上": 6, "2局下": 7}
        half_segs = infer_half_segments_from_transcript(
            segs,
            expected,
            duration_sec=2800,
            pa_counts_by_label=pa_counts,
            lineup_matchers=matchers,
        )
        self.assertIsNotNone(half_segs)
        assert half_segs is not None
        self.assertEqual(len(half_segs), 4)
        top1 = half_segs[0]
        self.assertEqual(top1["half_label"], "1局上")
        self.assertGreaterEqual(top1["start_sec"], 805)
        self.assertLessEqual(top1["start_sec"], 840)
        self.assertGreaterEqual(top1["end_sec"], 1150)
        self.assertLessEqual(top1["end_sec"], 1170)

        # 在重建後的 1局上視窗內執行打席 DP，確認楊〇軒、林〇徹、尤〇翔順序與區間合理
        group = [
            {
                "player_uid": "楊〇軒_2017-11",
                "clip_type": "batting",
                "event_index": 9,
                "label_short": "中外野手飛球",
                "event_detail": "楊〇軒打擊到外野的飛球，遭到中外野手接殺出局",
                "start_sec": 1000,
                "align_method": "asr_half_interp",
                "confidence": "low",
            },
            {
                "player_uid": "林〇徹_2017-11",
                "clip_type": "batting",
                "event_index": 10,
                "label_short": "滾地出局",
                "event_detail": "林〇徹打到投手方向的滾地球，投手接到傳給一壘手刺殺打者出局",
                "start_sec": 1050,
                "align_method": "asr_half_interp",
                "confidence": "low",
            },
            {
                "player_uid": "卓〇霆_2018-09",
                "clip_type": "batting",
                "event_index": 11,
                "label_short": "滾地出局",
                "event_detail": "卓〇霆打到投手方向的滾地球，投手接到傳給一壘手刺殺打者出局",
                "start_sec": 1090,
                "align_method": "asr_half_interp",
                "confidence": "low",
            },
            {
                "player_uid": "尤〇翔_2018-01",
                "clip_type": "batting",
                "event_index": 12,
                "label_short": "滾地出局",
                "event_detail": "尤〇翔打擊到內野的滾地球，三壘手接到後傳向一壘刺殺打者出局",
                "start_sec": 1125,
                "align_method": "asr_half_interp",
                "confidence": "low",
            },
        ]
        win_segs = [s for s in segs if (top1["start_sec"] - 25) <= s["start"] <= (top1["end_sec"] + 35)]
        db_segs: list[dict] = []
        _solve_batting_dp(
            group=group,
            win_segs=win_segs,
            matchers_by_uid=m_map,
            all_matchers=matchers,
            vid="TZmQLL82fSQ",
            eff_start=top1["start_sec"] - 25,
            eff_end=top1["end_sec"] + 35,
            h_start=top1["start_sec"],
            h_end=top1["end_sec"],
            asr_mode="speech_pa",
            effective_pre_roll=1.0,
            matched_segments_for_db=db_segs,
        )
        self.assertGreaterEqual(group[1]["start_sec"], 1050)
        self.assertLessEqual(group[1]["start_sec"], 1095)
        self.assertIn(group[1]["confidence"], ("medium", "high"))
        self.assertLess(group[0]["start_sec"], group[1]["start_sec"])
        self.assertLess(group[1]["start_sec"], group[2]["start_sec"])
        self.assertLess(group[2]["start_sec"], group[3]["start_sec"])

    def test_previous_game_tail_discarded_and_full_game_monotonic(self) -> None:
        """片頭出現前一場『四局下半』雜訊時，全域單調 DP 應捨棄雜訊並鎖定真實 1上~4下。"""
        segs = _pad_segments(
            [
                {"start": 343.0, "end": 347.0, "text": "再來到四局的下半"},
                {"start": 1776.0, "end": 1780.0, "text": "結束力量河馬在一局上半的攻勢"},
                {"start": 1850.0, "end": 1854.0, "text": "再進入一局下半"},
                {"start": 3273.0, "end": 3277.0, "text": "雙殺結束了二局下半"},
                {"start": 3773.0, "end": 3777.0, "text": "來，結束三局上半"},
                {"start": 3896.0, "end": 3900.0, "text": "好比賽現在進入到三局下"},
                {"start": 3947.0, "end": 3951.0, "text": "前一場比賽第四局上來"},
                {"start": 4332.0, "end": 4336.0, "text": "那也結束了三局的下半"},
                {"start": 4520.0, "end": 4524.0, "text": "好比賽現在要準備進入到四局的上半"},
                {"start": 4932.0, "end": 4936.0, "text": "繼續回到比賽現場現在要進入四局的下半"},
            ]
        )
        expected = [
            ("TOP", 1),
            ("BOT", 1),
            ("TOP", 2),
            ("BOT", 2),
            ("TOP", 3),
            ("BOT", 3),
            ("TOP", 4),
            ("BOT", 4),
        ]
        half_segs = infer_half_segments_from_transcript(segs, expected, duration_sec=5900)
        self.assertIsNotNone(half_segs)
        assert half_segs is not None
        self.assertEqual(len(half_segs), 8)
        # 1局上應遠大於片頭 343s 前場殘留，且嚴格遞增無重疊
        self.assertGreater(half_segs[0]["start_sec"], 800)
        self.assertLessEqual(half_segs[0]["end_sec"], 1785)
        self.assertGreaterEqual(half_segs[1]["start_sec"], 1840)
        self.assertGreaterEqual(half_segs[7]["start_sec"], 4920)
        for i in range(1, len(half_segs)):
            self.assertGreaterEqual(half_segs[i]["start_sec"], half_segs[i - 1]["end_sec"])

    def test_silent_video_returns_none(self) -> None:
        """靜音或少於 20 段逐字稿時應安全回傳 None。"""
        res = infer_half_segments_from_transcript(
            [{"start": 10.0, "end": 12.0, "text": "一局上半開始"}],
            [("TOP", 1), ("BOT", 1)],
            duration_sec=4000,
        )
        self.assertIsNone(res)

    def test_mention_continuity_and_huajie_not_treated_as_end(self) -> None:
        """『化解』不應誤判為 END，且僅含 MENTION 的半局應與前後半局連續銜接無空白斷層。"""
        segs = _pad_segments(
            [
                {"start": 698.0, "end": 702.0, "text": "來到一局下半"},
                {"start": 1020.0, "end": 1024.0, "text": "二局上半進攻"},
                {"start": 1512.0, "end": 1516.0, "text": "在二局下半"},
                {"start": 1895.0, "end": 1899.0, "text": "比賽來到三局上半"},
                {"start": 2200.0, "end": 2204.0, "text": "進入到三局下半，成功化解滿壘危機"},
            ]
        )
        expected = [("TOP", 1), ("BOT", 1), ("TOP", 2), ("BOT", 2), ("TOP", 3), ("BOT", 3)]
        half_segs = infer_half_segments_from_transcript(segs, expected, duration_sec=2800)
        self.assertIsNotNone(half_segs)
        assert half_segs is not None
        # 2BOT 應涵蓋 1512s 並連續延伸到 3TOP 起點 (1895s) 附近，不留數百秒斷層
        bot2 = half_segs[3]
        self.assertLessEqual(bot2["start_sec"], 1512)
        self.assertGreaterEqual(bot2["end_sec"], 1850)
        # 3BOT 含『化解』應視為 START (~2200s) 並延伸至片尾附近 (2770s)，不可被切斷在 2204s
        bot3 = half_segs[5]
        self.assertGreaterEqual(bot3["start_sec"], 2190)
        self.assertGreaterEqual(bot3["end_sec"], 2700)

    def test_sparse_start_anchors_preserved_across_unannounced_halves(self) -> None:
        """僅有 1B/2B/3B START 錨點而無 END 時，不可被片尾倒推覆蓋，每個 START 必須精準鎖定。"""
        segs = _pad_segments(
            [
                {"start": 896.0, "end": 900.0, "text": "來到一局下半"},
                {"start": 1759.0, "end": 1763.0, "text": "進入二局下半"},
                {"start": 2773.0, "end": 2777.0, "text": "比賽來到三局下半"},
                {"start": 4690.0, "end": 4694.0, "text": "現在四局下半"},
            ]
        )
        expected = [
            ("TOP", 1),
            ("BOT", 1),
            ("TOP", 2),
            ("BOT", 2),
            ("TOP", 3),
            ("BOT", 3),
            ("TOP", 4),
            ("BOT", 4),
        ]
        half_segs = infer_half_segments_from_transcript(segs, expected, duration_sec=5134)
        self.assertIsNotNone(half_segs)
        assert half_segs is not None
        self.assertLessEqual(half_segs[0]["end_sec"], 890)
        self.assertEqual(half_segs[1]["start_sec"], 896)
        self.assertEqual(half_segs[3]["start_sec"], 1759)
        self.assertEqual(half_segs[5]["start_sec"], 2773)
        self.assertLessEqual(half_segs[7]["start_sec"], 4690)
        self.assertGreaterEqual(half_segs[7]["end_sec"], 4900)

    def test_sparse_late_cues_do_not_shift_top1_late(self) -> None:
        """稀疏後段局錨點與比賽中單打席播報，不得將 1局上推遲到 30 分鐘後。"""
        # 情境 A：模擬 mid=13 (僅 4局上真實播報，且前段 73s 起已有打者上場)
        segs_a = _pad_segments(
            [
                {"start": 73.0, "end": 78.0, "text": "現在輪到第3棒，背號2號黃〇珽上場打擊"},
                {"start": 132.0, "end": 137.0, "text": "現在輪到背號62號張〇泰上場打擊"},
                {"start": 2453.0, "end": 2457.0, "text": "二局下、四壘安打：二局小失誤，"},
                {"start": 3693.0, "end": 3697.0, "text": "四局上輪到背號21號蘇〇琳上場打擊"},
            ]
        )
        expected_a = [
            ("TOP", 1),
            ("BOT", 1),
            ("TOP", 2),
            ("BOT", 2),
            ("TOP", 3),
            ("BOT", 3),
            ("TOP", 4),
            ("BOT", 4),
        ]
        res_a = infer_half_segments_from_transcript(segs_a, expected_a, duration_sec=4860)
        self.assertIsNotNone(res_a)
        assert res_a is not None
        self.assertLessEqual(res_a[0]["start_sec"], 90)

        # 情境 B：模擬 mid=161 (2040s 1局下 MENTION + 3244s 2局下 START，1540s 單打席不應誤觸先發唱名)
        matchers_b = [
            build_player_matcher("王〇敬_2015-01", "王〇敬", "1", batting_order="1"),
            build_player_matcher("莊〇仲_2015-02", "莊〇仲", "68", batting_order="2"),
            build_player_matcher("何〇縉_2015-03", "何〇縉", "5", batting_order="4"),
        ]
        segs_b = _pad_segments(
            [
                {"start": 1389.0, "end": 1392.0, "text": "現在是第二棒的68號莊〇仲打擊"},
                {"start": 1540.0, "end": 1544.0, "text": "接下來輪到第四棒，五號的何〇縉"},
                {"start": 2040.0, "end": 2043.0, "text": "一局下、三壘攻勢滿壘"},
                {"start": 3244.0, "end": 3247.0, "text": "接下來來到二局下"},
            ]
        )
        expected_b = [("TOP", 1), ("BOT", 1), ("TOP", 2), ("BOT", 2), ("TOP", 3), ("BOT", 3)]
        res_b = infer_half_segments_from_transcript(
            segs_b, expected_b, duration_sec=6029, lineup_matchers=matchers_b
        )
        self.assertIsNotNone(res_b)
        assert res_b is not None
        self.assertLessEqual(res_b[0]["start_sec"], 90)

    def test_bot1_lineup_intro_not_treated_as_top1_start(self) -> None:
        """1局上已在前面發生，1局下的先發打序介紹不得將 1局上起點推遲至中後段。"""
        matchers = [
            build_player_matcher("黃〇軒_2015-01", "黃〇軒", "8", batting_order="1"),
            build_player_matcher("蔡〇承_2015-02", "蔡〇承", "52", batting_order="2"),
            build_player_matcher("王〇曄_2015-03", "王〇曄", "41", batting_order="3"),
            build_player_matcher("林〇倫_2015-04", "林〇倫", "11", batting_order="4"),
        ]
        segs = _pad_segments(
            [
                {"start": 241.0, "end": 244.0, "text": "一局上半小海盜敲出安打"},
                {"start": 1318.0, "end": 1320.0, "text": "第一棒是黃〇軒"},
                {"start": 1321.0, "end": 1323.0, "text": "第二棒是52號蔡〇承"},
                {"start": 1324.0, "end": 1326.0, "text": "第三棒王〇曄，第四棒林〇倫"},
                {"start": 1717.0, "end": 1720.0, "text": "比賽來到二局上半"},
                {"start": 2609.0, "end": 2612.0, "text": "進入到二局下半"},
            ]
        )
        expected = [("TOP", 1), ("BOT", 1), ("TOP", 2), ("BOT", 2), ("TOP", 3), ("BOT", 3)]
        res = infer_half_segments_from_transcript(
            segs, expected, duration_sec=4891, lineup_matchers=matchers
        )
        self.assertIsNotNone(res)
        assert res is not None
        self.assertLessEqual(res[0]["start_sec"], 90)

    def test_impossible_early_inning2_cue_and_recap_filtered(self) -> None:
        """超早誤聽的 2局上(171s) 與回顧句『剛在一局上』/『二局上保者』皆應被過濾。"""
        segs = _pad_segments(
            [
                {"start": 171.0, "end": 175.0, "text": "二局上半，對方先攻第一棒"},
                {"start": 1020.0, "end": 1024.0, "text": "二局上保者三壘，滿球數"},
                {"start": 1791.0, "end": 1795.0, "text": "剛在一局上拿到三分領先"},
                {"start": 2514.0, "end": 2518.0, "text": "比賽來到二局下"},
                {"start": 3061.0, "end": 3065.0, "text": "比賽進行來到三局上"},
                {"start": 3509.0, "end": 3513.0, "text": "比賽來到三局下"},
            ]
        )
        expected = [
            ("TOP", 1),
            ("BOT", 1),
            ("TOP", 2),
            ("BOT", 2),
            ("TOP", 3),
            ("BOT", 3),
            ("TOP", 4),
            ("BOT", 4),
        ]
        res = infer_half_segments_from_transcript(segs, expected, duration_sec=6200)
        self.assertIsNotNone(res)
        assert res is not None
        # 若未過濾 171s 的 2TOP，1TOP 會被壓扁在 <=151s；過濾後應正常涵蓋前段
        self.assertGreater(res[0]["end_sec"], 400)
        self.assertGreater(res[2]["start_sec"], 1200)


if __name__ == "__main__":
    unittest.main()
