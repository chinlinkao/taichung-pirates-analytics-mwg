"""軌道 C Targeted VLM Fallback 與 Scorebug 防呆離線單元測試 (tests/test_vlm_fallback.py)。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.clips_scorebug import align_game_all_players, clean_scorebug_timeline
from src.clips_vlm_fallback import (
    PROMPT_VERSION,
    build_contact_sheets,
    detect_blind_windows,
    parse_vlm_response_json,
    refine_clips_with_vlm_fallback,
    refine_event_sec_with_opencv,
)


def _create_synthetic_video(path: Path, duration_sec: int = 26, fps: int = 10) -> None:
    """建立含紅土背景與本壘打擊區人物移動的合成測試影片。"""
    w, h = 320, 180
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, float(fps), (w, h))
    try:
        total = duration_sec * fps
        for i in range(total):
            t = i / fps
            frame = np.full((h, w, 3), (60, 100, 150), dtype=np.uint8)  # 紅土底色
            # 在 t=10~15s 於本壘打擊區 ROI (0.58..0.72, 0.48..0.60) 放入黑色打者輪廓
            if 10.0 <= t <= 15.0:
                cv2.rectangle(
                    frame,
                    (int(w * 0.60), int(h * 0.49)),
                    (int(w * 0.68), int(h * 0.59)),
                    (15, 15, 15),
                    -1,
                )
            writer.write(frame)
    finally:
        writer.release()


class TestVlmFallback(unittest.TestCase):
    def test_clean_scorebug_filters_isolated_single_frame(self) -> None:
        """孤立單幀 OCR 命中（如 t=20s 僅 1 幀）應降級為插值，且無真實出局數不得標為 high。"""
        samples = [{"t": t, "detected": False, "half": None, "inning": None, "outs": None, "bs": None} for t in range(0, 960, 5)]
        # t=20s (index 4) 僅 1 幀誤抓 TOP 1
        samples[4] = {"t": 20, "detected": True, "half": "TOP", "inning": 1, "outs": None, "bs": None}
        # t=860..950s 穩定抓到 TOP 2 與 1 Out
        for idx, s in enumerate(samples):
            if s["t"] >= 860:
                s["detected"] = True
                s["half"] = "TOP"
                s["inning"] = 2
                s["outs"] = 1 if s["t"] >= 910 else 0

        raw_scan = {"video_id": "vid_iso", "duration_sec": 960, "sample_interval_sec": 5, "samples": samples}
        expected = [("TOP", 1), ("BOT", 1), ("TOP", 2)]
        pa_counts = {"1局上": 3, "1局下": 4, "2局上": 3}

        cleaned = clean_scorebug_timeline(raw_scan, expected, pa_counts)
        segs = {s["half_label"]: s for s in cleaned["half_segments"]}
        self.assertTrue(segs["1局上"]["is_interpolated"])
        self.assertTrue(segs["1局下"]["is_interpolated"])
        self.assertFalse(segs["2局上"]["is_interpolated"])

        game = {
            "match_id": 999,
            "youtube_videos": [{"video_id": "vid_iso", "duration_sec": 960}],
            "half_innings": [
                {
                    "half_label": "1局上",
                    "half_side": "TOP",
                    "inning": 1,
                    "events": [
                        {"event_index": 1, "is_plate_appearance": True, "outs_delta": 0, "text": "曾〇瑞安打"},
                        {"event_index": 2, "is_plate_appearance": True, "outs_delta": 1, "text": "高〇展滾地出局"},
                        {"event_index": 3, "is_plate_appearance": True, "outs_delta": 1, "text": "楊〇皓三振"},
                    ],
                },
                {"half_label": "1局下", "half_side": "BOT", "inning": 1, "events": []},
                {
                    "half_label": "2局上",
                    "half_side": "TOP",
                    "inning": 2,
                    "events": [
                        {"event_index": 1, "is_plate_appearance": True, "outs_delta": 1, "text": "林〇恩飛球出局"},
                    ],
                },
            ],
            "players": {
                "曾〇瑞_2015-07": {
                    "masked_name": "曾〇瑞",
                    "jersey_number": "1",
                    "plate_appearances": [
                        {"half_label": "1局上", "inning": 1, "half_side": "TOP", "event_index": 1, "result_short": "一安", "event_text": "曾〇瑞安打", "runner_notes": []}
                    ],
                    "pitching_stints": [],
                    "fielding_plays": [],
                }
            },
        }
        clips = align_game_all_players(game, {"vid_iso": raw_scan})
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["align_method"], "bracket_interp")
        self.assertEqual(clips[0]["confidence"], "low")

    def test_detect_blind_windows_skips_asr_covered_and_normal_halves(self) -> None:
        """模擬 match 157：1上/1下盲區合併輸出，2上/3上正常與 4上已有 ASR 均不觸發。"""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE video_scorebug_segments (
                video_id TEXT, inning INTEGER, half_side TEXT,
                start_sec INTEGER, end_sec INTEGER, sample_count INTEGER, is_interpolated INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO video_scorebug_segments VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("vid157", 1, "TOP", 90, 300, 0, 1),
                ("vid157", 1, "BOT", 300, 858, 0, 1),
                ("vid157", 2, "TOP", 858, 1563, 129, 0),
                ("vid157", 4, "TOP", 2528, 2933, 0, 1),
            ],
        )

        game = {
            "match_id": 157,
            "away_team": "台中小海盜(黑)",
            "home_team": "台中仁美M",
            "youtube_videos": [{"video_id": "vid157", "duration_sec": 4878}],
            "half_innings": [
                {"half_label": "1局上", "inning": 1, "half_side": "TOP", "events": [{"event_index": 2, "is_plate_appearance": True, "text": "曾〇瑞安打"}]},
                {"half_label": "1局下", "inning": 1, "half_side": "BOT", "events": [{"event_index": 2, "is_plate_appearance": True, "text": "黃〇胤接殺"}]},
                {"half_label": "2局上", "inning": 2, "half_side": "TOP", "events": [{"event_index": 1, "is_plate_appearance": True, "text": "林〇恩安打"}]},
                {"half_label": "4局上", "inning": 4, "half_side": "TOP", "events": [{"event_index": 1, "is_plate_appearance": True, "text": "梁〇華出局"}]},
            ],
        }
        clips = [
            {"video_id": "vid157", "half_label": "1局上", "clip_type": "batting", "align_method": "bracket_interp", "confidence": "low"},
            {"video_id": "vid157", "half_label": "1局下", "clip_type": "fielding", "align_method": "bracket_interp", "confidence": "low"},
            {"video_id": "vid157", "half_label": "2局上", "clip_type": "batting", "align_method": "dual_track", "confidence": "high"},
            {"video_id": "vid157", "half_label": "4局上", "clip_type": "batting", "align_method": "dual_track", "confidence": "high"},
        ]

        windows = detect_blind_windows(game, clips, conn=conn)
        self.assertEqual(len(windows), 1)
        self.assertEqual([h["half_label"] for h in windows[0]["halves"]], ["1局上", "1局下"])
        self.assertEqual(windows[0]["win_start"], 0)
        self.assertEqual(windows[0]["win_end"], 858 + 15)

    def test_vlm_fallback_skips_gracefully_without_api_key_but_uses_cache(self) -> None:
        """無 GEMINI_API_KEY 時不報錯、不呼 API；但既有本地快取仍正常套用。"""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE video_scorebug_segments (video_id TEXT, inning INTEGER, half_side TEXT, start_sec INTEGER, end_sec INTEGER, sample_count INTEGER, is_interpolated INTEGER)"
        )
        conn.execute("INSERT INTO video_scorebug_segments VALUES ('vid_cache', 1, 'TOP', 90, 300, 0, 1)")

        game = {
            "match_id": 157,
            "away_team": "台中小海盜(黑)",
            "home_team": "台中仁美M",
            "youtube_videos": [{"video_id": "vid_cache", "duration_sec": 900}],
            "half_innings": [
                {"half_label": "1局上", "inning": 1, "half_side": "TOP", "events": [{"event_index": 2, "is_plate_appearance": True, "text": "曾〇瑞安打"}]}
            ],
        }
        base_clip = {
            "match_id": 157,
            "player_uid": "曾〇瑞_2015-07",
            "video_id": "vid_cache",
            "clip_type": "batting",
            "inning": 1,
            "half_side": "TOP",
            "half_label": "1局上",
            "event_index": 2,
            "start_sec": 50,
            "half_start_sec": 90,
            "half_end_sec": 300,
            "align_method": "bracket_interp",
            "confidence": "low",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "vlm_cache"
            tmp_media = Path(tmpdir) / "tmp_media"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # 情況 A：無 GEMINI_API_KEY 且無快取 -> 安全跳過不報錯
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False), mock.patch(
                "src.clips_vlm_fallback._load_gemini_api_key", return_value=None
            ):
                clips_no_key = [dict(base_clip)]
                stats = refine_clips_with_vlm_fallback(
                    [game],
                    {157: clips_no_key},
                    conn,
                    vlm_mode="blind_only",
                    cache_dir=cache_dir,
                    tmp_dir=tmp_media,
                )
                self.assertEqual(stats["blind_windows"], 1)
                self.assertEqual(stats["vlm_calls"], 0)
                self.assertEqual(clips_no_key[0]["start_sec"], 50)

            # 情況 B：無 GEMINI_API_KEY 但有本地快取 -> 直接命中並升級
            win_key = f"0_900_{PROMPT_VERSION}"
            (cache_dir / "vid_cache.json").write_text(
                json.dumps(
                    {
                        "video_id": "vid_cache",
                        "windows": {
                            win_key: {
                                "game_start_sec": 138,
                                "half_boundaries": {"1局上": {"start_sec": 138, "end_sec": 294}},
                                "events": [
                                    {
                                        "half_label": "1局上",
                                        "event_index": 2,
                                        "start_sec": 138,
                                        "end_sec": 156,
                                        "matched_by_vlm": True,
                                        "opencv_refined": True,
                                        "opencv_refined_sec": 138,
                                    }
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("src.clips_vlm_fallback._load_gemini_api_key", return_value=None):
                clips_cached = [dict(base_clip)]
                stats_c = refine_clips_with_vlm_fallback(
                    [game],
                    {157: clips_cached},
                    conn,
                    vlm_mode="blind_only",
                    cache_dir=cache_dir,
                    tmp_dir=tmp_media,
                )
                self.assertEqual(stats_c["cache_hits"], 1)
                self.assertEqual(stats_c["vlm_calls"], 0)
                self.assertEqual(clips_cached[0]["align_method"], "vlm_cv_refined")
                self.assertEqual(clips_cached[0]["confidence"], "high")
                self.assertEqual(clips_cached[0]["start_sec"], 136)

    def test_vlm_json_parsing_and_monotonic_clamp(self) -> None:
        """驗證 markdown JSON 解析、超界夾緊與單調遞增約束。"""
        raw = """```json
        {
          "game_start_sec": 140,
          "half_boundaries": [{"half_label": "1局上", "start_sec": 136, "end_sec": 295}],
          "events": [
            {"half_label": "1局上", "event_index": 2, "start_sec": 140, "end_sec": 160},
            {"half_label": "1局上", "event_index": 4, "start_sec": 120, "end_sec": 5000}
          ]
        }
        ```"""
        expected = [
            {"half_label": "1局上", "event_index": 2},
            {"half_label": "1局上", "event_index": 4},
        ]
        parsed = parse_vlm_response_json(raw, win_start=0, win_end=350, expected_events=expected)
        self.assertEqual(parsed["game_start_sec"], 140)
        evs = parsed["events"]
        self.assertEqual(evs[0]["start_sec"], 140)
        self.assertGreaterEqual(evs[1]["start_sec"], evs[0]["start_sec"])
        self.assertLessEqual(evs[1]["end_sec"], 350)

    def test_contact_sheet_generation_and_opencv_refine_bounds(self) -> None:
        """驗證合成影片九宮格拼接尺寸 (1278x720) 與 OpenCV 局部微調約束在 ±5s 內。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            vid_path = tmp / "synth.mp4"
            _create_synthetic_video(vid_path, duration_sec=26, fps=10)

            sheets = build_contact_sheets(vid_path, [0, 3, 6, 9, 12, 15, 18], tmp / "sheets", prefix="test")
            self.assertEqual(len(sheets), 1)
            img = cv2.imread(str(sheets[0]["path"]))
            self.assertIsNotNone(img)
            self.assertEqual(img.shape[:2], (720, 1278))

            est = 12
            refined, did_ref = refine_event_sec_with_opencv(vid_path, est, (0, 26))
            self.assertLessEqual(abs(refined - est), 5)
            self.assertTrue(0 <= refined <= 26)


if __name__ == "__main__":
    unittest.main()
