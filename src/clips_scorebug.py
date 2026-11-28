"""軌道 A：Scorebug 視覺狀態機掃描與半局／打席時間軸對齊模組 (src/clips_scorebug.py)。

支援功能：
- 雙樣式（Style A 黑底白框／Style B MEVO 深綠底）與雙位置（左下／右下）自動偵測
- ffmpeg -skip_frame nokey + select>=4s 極速抽幀與 WinRT OCR 辨識
- 3 幀多數決清洗、單調遞增半局鎖定、缺漏半局夾擠補間 (bracket_interp)
- 支援單場多段影片 (Part 1 / Part 2) 自動分段配對
- 將半局時間軸寫入 SQLite (video_scorebug_segments)，並對齊所有海盜球員事件
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

DEFAULT_TIMELINE_DIR = Path("data/video_timelines")
LEGACY_POC_SCOREBUG_DIR = Path("data/poc_scorebug")
TMP_MEDIA_DIR = Path("data/tmp_clips_media")

SAMPLE_INTERVAL_SEC = 4
PRE_ROLL_PA_SEC = 5
PRE_ROLL_PITCH_SEC = 5
PRE_ROLL_FIELD_SEC = 12


class ScorebugOCR:
    """封裝 WinRT 原生 OCR 引擎（相容 winrt 3.x / 4.x）。"""

    def __init__(self) -> None:
        self.eng = (
            OcrEngine.try_create_from_user_profile_languages()
            or OcrEngine.try_create_from_language(Language("zh-Hant-TW"))
            or OcrEngine.try_create_from_language(Language("en-US"))
        )
        if self.eng is None:
            raise RuntimeError("無法初始化 Windows WinRT OCR 引擎")

    async def recognize_lines(self, bgr_img: np.ndarray) -> list[str]:
        if bgr_img is None or bgr_img.size == 0:
            return []
        bgra = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2BGRA)
        h, w = bgra.shape[:2]
        writer = DataWriter()
        writer.write_bytes(bgra.tobytes())
        sb = SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h
        )
        res = await self.eng.recognize_async(sb)
        return [ln.text.strip() for ln in res.lines if ln.text.strip()]


def _find_style_a_score_pair(
    crop: np.ndarray,
) -> Optional[tuple[tuple[int, int, int, int], tuple[int, int, int, int]]]:
    """尋找 Style A 垂直對齊的兩個純白比數方塊 (x, y, w, h)。"""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mask = (gray > 220).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    squares: list[tuple[int, int, int, int]] = []
    ch = crop.shape[0]
    min_side = max(8, int(ch * 0.05))
    max_side = max(45, int(ch * 0.30))
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if min_side <= w <= max_side and min_side <= h <= max_side and 0.6 <= (w / h) <= 1.6:
            if cv2.contourArea(c) / (w * h) > 0.72:
                squares.append((x, y, w, h))
    squares.sort(key=lambda b: b[1])
    for i in range(len(squares)):
        for j in range(i + 1, len(squares)):
            s1, s2 = squares[i], squares[j]
            if abs(s1[0] - s2[0]) <= 8 and abs(s1[2] - s2[2]) <= 8:
                gap = s2[1] - (s1[1] + s1[3])
                if 0 <= gap <= max(18, int(s1[3] * 0.6)):
                    return s1, s2
    return None


def _parse_style_a_bar_text(
    lines: list[str],
) -> tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
    """解析 Style A 底部黑條文字：(half='TOP'|'BOT', inning, outs, bs_str)。"""
    joined = " | ".join(lines)
    half: Optional[str] = None
    inning: Optional[int] = None
    outs: Optional[int] = None
    bs_str: Optional[str] = None

    m_in = re.search(r"\b(TOP|BOT)\s*(\d{1,2})\b", joined, re.IGNORECASE)
    if m_in:
        half = m_in.group(1).upper()
        inning = int(m_in.group(2))

    m_out = re.search(r"\b([012])\s*Out(?:s)?\b", joined, re.IGNORECASE)
    if m_out:
        outs = int(m_out.group(1))

    m_bs = re.search(r"\b([0-3])\s*[-·.\s]\s*([0-2])\b", joined)
    if m_bs:
        bs_str = f"{m_bs.group(1)}-{m_bs.group(2)}"

    return half, inning, outs, bs_str


def _detect_style_a_bases(
    crop: np.ndarray,
    s1: tuple[int, int, int, int],
    s2: tuple[int, int, int, int],
) -> tuple[int, int, int]:
    """透過白色菱形輪廓質心判定一、二、三壘是否亮起。"""
    bx, bw = s1[0], s1[2]
    y_top = max(0, s1[1] - 2)
    y_bot = min(crop.shape[0], s2[1] + s2[3] + 2)
    x_left = bx + bw + 2
    x_right = min(crop.shape[1], bx + int(bw * 4.2))
    if x_right <= x_left or y_bot <= y_top:
        return (0, 0, 0)

    reg = crop[y_top:y_bot, x_left:x_right]
    gray = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)
    mask = (gray > 215).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rh, rw = reg.shape[:2]
    on_1b = on_2b = on_3b = 0
    for c in contours:
        area = cv2.contourArea(c)
        if area < max(20, rh * rw * 0.025):
            continue
        m = cv2.moments(c)
        if m["m00"] <= 0:
            continue
        nx = (m["m10"] / m["m00"]) / rw
        ny = (m["m01"] / m["m00"]) / rh
        if ny < 0.48 and 0.25 <= nx <= 0.75:
            on_2b = 1
        elif ny >= 0.40 and nx > 0.50:
            on_1b = 1
        elif ny >= 0.40 and nx < 0.50:
            on_3b = 1
    return (on_1b, on_2b, on_3b)


def _compute_score_sig(
    crop: np.ndarray,
    s1: tuple[int, int, int, int],
    s2: tuple[int, int, int, int],
) -> str:
    """萃取兩個白底比數方塊內黑色數字像素的特徵簽名。"""
    sigs: list[str] = []
    for x, y, w, h in (s1, s2):
        box = crop[y : y + h, x : x + w]
        if box.size == 0:
            sigs.append("0")
            continue
        g = cv2.cvtColor(box, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(g, (8, 8), interpolation=cv2.INTER_AREA)
        bits = "".join("1" if v < 150 else "0" for v in small.flatten())
        sigs.append(hex(int(bits, 2))[2:])
    return f"{sigs[0]}_{sigs[1]}"


def _find_style_b_box(crop: np.ndarray) -> Optional[tuple[int, int, int, int]]:
    """尋找 MEVO 深綠半透明 Scorebug 主區塊邊界 (x, y, w, h)。"""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = (
        (hsv[:, :, 0] >= 35)
        & (hsv[:, :, 0] <= 88)
        & (hsv[:, :, 1] >= 45)
        & (hsv[:, :, 2] <= 140)
    ).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(c)
    if w < 100 or h < 50:
        return None
    return (x, y, w, h)


def _detect_style_b_geometry(sb: np.ndarray) -> dict[str, Any]:
    """在 Style B 主區塊內以亮黃綠幾何元件判定 ▲/▼、出局圓點與壘包狀態。"""
    bh, bw = sb.shape[:2]
    hsv = cv2.cvtColor(sb, cv2.COLOR_BGR2HSV)
    bright_green = (
        (hsv[:, :, 0] >= 30)
        & (hsv[:, :, 0] <= 80)
        & (hsv[:, :, 1] >= 80)
        & (hsv[:, :, 2] >= 150)
    ).astype(np.uint8) * 255
    contours, _ = cv2.findContours(bright_green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    outs_dots = 0
    arrow_half: Optional[str] = None
    on_1b = on_2b = on_3b = 0

    for c in contours:
        if cv2.contourArea(c) < 8:
            continue
        cx, cy, cw, ch = cv2.boundingRect(c)
        m = cv2.moments(c)
        if m["m00"] <= 0:
            continue
        nx = (m["m10"] / m["m00"]) / bw
        ny = (m["m01"] / m["m00"]) / bh

        if nx < 0.60:
            continue
        if ny < 0.45 and nx >= 0.85:
            outs_dots += 1
        elif ny < 0.50 and 0.65 <= nx < 0.85:
            if ny < 0.25:
                on_2b = 1
            elif nx > 0.78:
                on_1b = 1
            else:
                on_3b = 1
        elif 0.48 <= ny <= 0.80 and 0.65 <= nx <= 0.78:
            patch = bright_green[cy : cy + ch, cx : cx + cw]
            mid = max(1, ch // 2)
            top_px = int(np.sum(patch[:mid, :] > 0))
            bot_px = int(np.sum(patch[ch - mid :, :] > 0))
            arrow_half = "TOP" if bot_px > top_px else "BOT"

    return {
        "outs_dots": min(2, outs_dots),
        "arrow_half": arrow_half,
        "bases": (on_1b, on_2b, on_3b),
    }


def _parse_style_b_ocr(lines: list[str]) -> tuple[Optional[int], Optional[str]]:
    """從 Style B 右下子區 OCR 文字萃取局數與 B-S 球數。"""
    bs_str: Optional[str] = None
    inning: Optional[int] = None

    for ln in lines:
        if any(k in ln for k in ("Taichung", "Clouds", "Pitch", "Rain", "Sunny", "TW")):
            continue
        m_full = re.search(r"([1-9])\s+([0-3])\s*[-·.\s]\s*([0-2])", ln)
        if m_full:
            inning = int(m_full.group(1))
            bs_str = f"{m_full.group(2)}-{m_full.group(3)}"
            return inning, bs_str

    joined = " | ".join(lines)
    m_bs = re.search(r"\b([0-3])\s*[-·.\s]\s*([0-2])\b", joined)
    if m_bs:
        bs_str = f"{m_bs.group(1)}-{m_bs.group(2)}"
    for ln in lines:
        if any(k in ln for k in ("Taichung", "Clouds", "Pitch", "Rain", "Sunny", "TW")):
            continue
        cleaned = re.sub(r"[0-3]\s*[-·.]\s*[0-2]", " ", ln)
        m_in = re.search(r"\b([1-9])\b", cleaned)
        if m_in:
            inning = int(m_in.group(1))
            break
    return inning, bs_str


def download_temp_video(video_id: str, dest: Path) -> None:
    """使用 yt-dlp 多線程下載 360p/480p 低畫質暫存影片。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-f",
        "bestvideo[height<=360][ext=mp4]/best[height<=360][ext=mp4]/bestvideo[height<=480][ext=mp4]/best[height<=480]",
        "--concurrent-fragments",
        "8",
        "--no-part",
        "--quiet",
        "-o",
        str(dest),
        url,
    ]
    subprocess.run(cmd, check=True)


async def scan_video_scorebug(
    video_path: Path,
    video_id: str,
    ocr: ScorebugOCR,
    sample_interval_sec: int = SAMPLE_INTERVAL_SEC,
    tmp_dir: Path = TMP_MEDIA_DIR,
) -> dict[str, Any]:
    """使用 ffmpeg -skip_frame nokey 極速抽取關鍵幀下方記分板區域並執行 Scorebug 掃描。"""
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_sec = int(round(total_frames / fps)) if total_frames > 0 else 0
    cap.release()

    kf_dir = tmp_dir / f"kf_{video_id}"
    shutil.rmtree(kf_dir, ignore_errors=True)
    kf_dir.mkdir(parents=True, exist_ok=True)

    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "info",
            "-skip_frame",
            "nokey",
            "-i",
            str(video_path),
            "-fps_mode",
            "vfr",
            "-vf",
            f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{sample_interval_sec})',crop=iw:ih*0.32:0:ih*0.68,showinfo",
            "-q:v",
            "3",
            str(kf_dir / "%05d.jpg"),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg 抽幀失敗 (rc={res.returncode}): {res.stderr[-400:]}")

        pts_list = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", res.stderr)]
        frame_files = sorted(kf_dir.glob("*.jpg"))
        if not frame_files:
            raise RuntimeError(f"未抽取到任何關鍵幀: {video_path}")

        if len(pts_list) != len(frame_files):
            step = (duration_sec / len(frame_files)) if duration_sec > 0 else 5.0
            pts_list = [i * step for i in range(len(frame_files))]

        probe_indices = sorted(
            {
                min(len(frame_files) - 1, max(0, int(len(frame_files) * r)))
                for r in (0.12, 0.22, 0.35, 0.50, 0.65)
            }
        )
        side_votes: Counter[str] = Counter()
        style_votes: Counter[str] = Counter()
        for pi in probe_indices:
            crop_band = cv2.imread(str(frame_files[pi]))
            if crop_band is None:
                continue
            bw = crop_band.shape[1]
            for side, (x1, x2) in (("left", (0.0, 0.45)), ("right", (0.55, 1.0))):
                sub = crop_band[:, int(bw * x1) : int(bw * x2)]
                if _find_style_a_score_pair(sub) is not None:
                    side_votes[side] += 1
                    style_votes["A"] += 1
                elif _find_style_b_box(sub) is not None:
                    side_votes[side] += 1
                    style_votes["B"] += 1

        preferred_side = side_votes.most_common(1)[0][0] if side_votes else "left"
        preferred_style = style_votes.most_common(1)[0][0] if style_votes else "A"
        actual_interval = round(duration_sec / max(1, len(frame_files)), 2) if duration_sec else 5.0
        print(
            f"  - 偵測樣式: style={preferred_style}, side={preferred_side}, "
            f"keyframes={len(frame_files)} (~{actual_interval}s/幀, duration={duration_sec}s)",
            flush=True,
        )

        samples: list[dict[str, Any]] = []
        last_score_sig: Optional[str] = None

        for fpath, pts in zip(frame_files, pts_list):
            crop_band = cv2.imread(str(fpath))
            if crop_band is None:
                continue
            bw = crop_band.shape[1]
            x1, x2 = (0.0, 0.45) if preferred_side == "left" else (0.55, 1.0)
            crop = crop_band[:, int(bw * x1) : int(bw * x2)]

            entry: dict[str, Any] = {
                "t": int(round(pts)),
                "detected": False,
                "style": None,
                "half": None,
                "inning": None,
                "outs": None,
                "bs": None,
                "bases": [0, 0, 0],
                "score_changed": False,
            }

            pair = _find_style_a_score_pair(crop)
            if pair is not None:
                s1, s2 = pair
                bx, sbw = s1[0], s1[2]
                bot_bar = crop[
                    s2[1] + s2[3] + 1 : min(crop.shape[0], s2[1] + s2[3] + int(s2[3] * 1.15)),
                    max(0, bx - int(sbw * 7.5)) : min(crop.shape[1], bx + int(sbw * 4.0)),
                ]
                if bot_bar.size > 0:
                    bb_up = cv2.resize(bot_bar, None, fx=2.4, fy=2.4, interpolation=cv2.INTER_CUBIC)
                    lines = await ocr.recognize_lines(bb_up)
                    half, inning, outs, bs_str = _parse_style_a_bar_text(lines)
                    bases = _detect_style_a_bases(crop, s1, s2)
                    sig = _compute_score_sig(crop, s1, s2)
                    score_changed = last_score_sig is not None and sig != last_score_sig
                    last_score_sig = sig

                    entry.update(
                        {
                            "detected": half is not None and inning is not None,
                            "style": "A",
                            "half": half,
                            "inning": inning,
                            "outs": outs,
                            "bs": bs_str,
                            "bases": list(bases),
                            "score_changed": score_changed,
                        }
                    )
            elif (sb_box := _find_style_b_box(crop)) is not None:
                bx, by, sbw, sbh = sb_box
                sb = crop[by : by + sbh, bx : bx + sbw]
                geo = _detect_style_b_geometry(sb)
                inn_crop = sb[int(sbh * 0.40) : int(sbh * 0.99), int(sbw * 0.64) : int(sbw * 0.99)]
                up = cv2.resize(inn_crop, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
                lines = await ocr.recognize_lines(up)
                inning, bs_str = _parse_style_b_ocr(lines)
                entry.update(
                    {
                        "detected": geo["arrow_half"] is not None and inning is not None,
                        "style": "B",
                        "half": geo["arrow_half"],
                        "inning": inning,
                        "outs": geo["outs_dots"],
                        "bs": bs_str,
                        "bases": list(geo["bases"]),
                        "score_changed": False,
                    }
                )

            samples.append(entry)

        return {
            "video_id": video_id,
            "duration_sec": duration_sec,
            "sample_interval_sec": actual_interval,
            "preferred_side": preferred_side,
            "preferred_style": preferred_style,
            "samples": samples,
        }
    finally:
        shutil.rmtree(kf_dir, ignore_errors=True)


def _half_sort_key(half: str, inning: int) -> int:
    return inning * 2 + (0 if half == "TOP" else 1)


def clean_scorebug_timeline(
    raw_scan: dict[str, Any],
    expected_halves: list[tuple[str, int]],
    pa_counts_by_label: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    """對原始抽樣時間軸做滑動視窗去噪並鎖定單調遞增的半局邊界 [start_sec, end_sec]。"""
    samples = raw_scan.get("samples", [])
    n = len(samples)
    if n == 0:
        return {"video_id": raw_scan["video_id"], "duration_sec": 0, "half_segments": [], "cleaned_samples": []}

    expected_keys = {_half_sort_key(h, i): f"{i}局{'上' if h == 'TOP' else '下'}" for h, i in expected_halves}
    expected_order = [_half_sort_key(h, i) for h, i in expected_halves]
    pa_counts = pa_counts_by_label or {}

    cleaned: list[dict[str, Any]] = []
    for idx, s in enumerate(samples):
        window = samples[max(0, idx - 1) : min(n, idx + 2)]
        h_votes = [
            _half_sort_key(w["half"], w["inning"])
            for w in window
            if w.get("detected") and w.get("half") in ("TOP", "BOT") and isinstance(w.get("inning"), int)
        ]
        hk = Counter(h_votes).most_common(1)[0][0] if h_votes else None
        if hk is not None and expected_keys and hk not in expected_keys:
            hk = None

        o_votes = [w["outs"] for w in window if w.get("outs") in (0, 1, 2)]
        outs = Counter(o_votes).most_common(1)[0][0] if o_votes else s.get("outs")

        cleaned.append(
            {
                "t": s["t"],
                "half_key": hk,
                "outs": outs,
                "bs": s.get("bs"),
                "bases": s.get("bases", [0, 0, 0]),
                "score_changed": s.get("score_changed", False),
            }
        )

    detected_by_hk: dict[int, dict[str, Any]] = {}
    cursor_idx = 0
    for hk in expected_order:
        indices = [i for i in range(cursor_idx, n) if cleaned[i]["half_key"] == hk]
        if not indices:
            continue
        blocks: list[list[int]] = []
        for i in indices:
            if not blocks or (cleaned[i]["t"] - cleaned[blocks[-1][-1]]["t"]) > 120:
                blocks.append([i])
            else:
                blocks[-1].append(i)
        best_block = max(blocks, key=len)
        if len(best_block) < 2 and len(blocks) > 1:
            best_block = blocks[0]
        start_i, end_i = best_block[0], best_block[-1]
        start_sec = cleaned[start_i]["t"]
        end_sec = cleaned[end_i]["t"] + int(round(raw_scan.get("sample_interval_sec", SAMPLE_INTERVAL_SEC)))
        raw_det_cnt = sum(
            1
            for i in range(start_i, end_i + 1)
            if samples[i].get("detected")
            and samples[i].get("half") in ("TOP", "BOT")
            and isinstance(samples[i].get("inning"), int)
            and _half_sort_key(samples[i]["half"], samples[i]["inning"]) == hk
        )
        # 過濾單幀孤立誤判（如僅 1 幀 OCR 命中經滑動視窗擴張成 15 秒），交由插值邏輯處理
        if n >= 10 and (
            raw_det_cnt < 3
            or (pa_counts.get(expected_keys[hk], 3) >= 3 and (end_sec - start_sec) < 25)
        ):
            continue
        cursor_idx = end_i + 1

        detected_by_hk[hk] = {
            "half_key": hk,
            "half_label": expected_keys[hk],
            "inning": hk // 2,
            "half_side": "TOP" if hk % 2 == 0 else "BOT",
            "start_sec": start_sec,
            "end_sec": end_sec,
            "sample_count": len(best_block),
            "is_interpolated": False,
        }

    duration_sec = int(raw_scan.get("duration_sec") or (cleaned[-1]["t"] if cleaned else 0))

    # 若完全未偵測到任何合法半局（極少見：無 Scorebug 影片），以 PA 權重均分整部影片
    if not detected_by_hk and expected_order:
        t_start = min(90, int(duration_sec * 0.03))
        t_end = max(t_start + 60, duration_sec - 30)
        weights = [max(3, pa_counts.get(expected_keys[hk], 4)) for hk in expected_order]
        tot_w = sum(weights)
        cur_t = t_start
        fallback_segs: list[dict[str, Any]] = []
        for idx_g, (hk, w) in enumerate(zip(expected_order, weights)):
            seg_end = t_end if idx_g == len(expected_order) - 1 else cur_t + int(round((t_end - t_start) * (w / tot_w)))
            fallback_segs.append(
                {
                    "half_key": hk,
                    "half_label": expected_keys[hk],
                    "inning": hk // 2,
                    "half_side": "TOP" if hk % 2 == 0 else "BOT",
                    "start_sec": cur_t,
                    "end_sec": max(cur_t + 20, seg_end),
                    "sample_count": 0,
                    "is_interpolated": True,
                }
            )
            cur_t = fallback_segs[-1]["end_sec"]
        return {
            "video_id": raw_scan["video_id"],
            "duration_sec": duration_sec,
            "half_segments": fallback_segs,
            "cleaned_samples": cleaned,
        }

    half_segments: list[dict[str, Any]] = []
    i = 0
    while i < len(expected_order):
        hk = expected_order[i]
        if hk in detected_by_hk:
            half_segments.append(detected_by_hk[hk])
            i += 1
            continue

        j = i
        while j < len(expected_order) and expected_order[j] not in detected_by_hk:
            j += 1

        missing_hks = expected_order[i:j]
        next_seg = detected_by_hk[expected_order[j]] if j < len(expected_order) else None

        if not half_segments and next_seg is not None:
            t_start = min(90, int(next_seg["start_sec"] * 0.12))
            t_end = next_seg["start_sec"]
            weights = [max(3, pa_counts.get(expected_keys[mhk], 4)) for mhk in missing_hks]
            tot_w = sum(weights)
            cur_t = t_start
            for mhk, w in zip(missing_hks, weights):
                seg_len = int(round((t_end - t_start) * (w / tot_w)))
                half_segments.append(
                    {
                        "half_key": mhk,
                        "half_label": expected_keys[mhk],
                        "inning": mhk // 2,
                        "half_side": "TOP" if mhk % 2 == 0 else "BOT",
                        "start_sec": cur_t,
                        "end_sec": min(t_end, cur_t + max(30, seg_len)),
                        "sample_count": 0,
                        "is_interpolated": True,
                    }
                )
                cur_t = half_segments[-1]["end_sec"]
        elif half_segments:
            prev_seg = half_segments[-1]
            t_start = prev_seg["start_sec"]
            t_end = next_seg["start_sec"] if next_seg is not None else max(prev_seg["end_sec"], duration_sec - 30)
            gap = t_end - prev_seg["end_sec"]

            if gap >= 120 * len(missing_hks):
                cur_t = prev_seg["end_sec"]
                weights = [max(3, pa_counts.get(expected_keys[mhk], 4)) for mhk in missing_hks]
                tot_w = sum(weights)
                for mhk, w in zip(missing_hks, weights):
                    seg_len = int(round((t_end - prev_seg["end_sec"]) * (w / tot_w)))
                    half_segments.append(
                        {
                            "half_key": mhk,
                            "half_label": expected_keys[mhk],
                            "inning": mhk // 2,
                            "half_side": "TOP" if mhk % 2 == 0 else "BOT",
                            "start_sec": cur_t,
                            "end_sec": min(t_end, cur_t + max(30, seg_len)),
                            "sample_count": 0,
                            "is_interpolated": True,
                        }
                    )
                    cur_t = half_segments[-1]["end_sec"]
            else:
                group_hks = [prev_seg["half_key"]] + missing_hks
                weights = [max(3, pa_counts.get(expected_keys[ghk], 4)) for ghk in group_hks]
                tot_w = sum(weights)
                span = max(60, t_end - t_start)
                cur_t = t_start
                new_segs: list[dict[str, Any]] = []
                for idx_g, (ghk, w) in enumerate(zip(group_hks, weights)):
                    seg_end = t_end if idx_g == len(group_hks) - 1 else cur_t + int(round(span * (w / tot_w)))
                    new_segs.append(
                        {
                            "half_key": ghk,
                            "half_label": expected_keys[ghk],
                            "inning": ghk // 2,
                            "half_side": "TOP" if ghk % 2 == 0 else "BOT",
                            "start_sec": cur_t,
                            "end_sec": max(cur_t + 20, seg_end),
                            "sample_count": prev_seg["sample_count"] if idx_g == 0 else 0,
                            "is_interpolated": idx_g > 0,
                        }
                    )
                    cur_t = new_segs[-1]["end_sec"]
                half_segments[-1] = new_segs[0]
                half_segments.extend(new_segs[1:])
        i = j

    return {
        "video_id": raw_scan["video_id"],
        "duration_sec": duration_sec,
        "half_segments": half_segments,
        "cleaned_samples": cleaned,
    }


def _find_bs_reset_near(
    samples: list[dict[str, Any]],
    t_min: int,
    t_max: int,
    target_t: float,
) -> Optional[int]:
    """在時間區間內尋找最接近 target_t 的 B-S 重置點。"""
    candidates: list[int] = []
    prev_bs: Optional[str] = None
    for s in samples:
        t = s["t"]
        if t < t_min or t > t_max:
            prev_bs = s.get("bs")
            continue
        bs = s.get("bs")
        if bs == "0-0" and prev_bs not in (None, "0-0"):
            candidates.append(t)
        prev_bs = bs
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c - target_t))


def _partition_halves_across_videos(
    raw_scans_in_order: list[dict[str, Any]],
    expected_halves: list[tuple[str, int]],
    transcripts_by_vid: Optional[dict[str, dict[str, Any]]] = None,
) -> list[tuple[str, list[tuple[str, int]]]]:
    """當單場比賽有多段影片 (Part 1 / Part 2) 時，依各段實際偵測半局分配半局清單。"""
    if len(raw_scans_in_order) == 1:
        return [(raw_scans_in_order[0]["video_id"], expected_halves)]

    expected_order = [_half_sort_key(h, i) for h, i in expected_halves]
    half_by_hk = {_half_sort_key(h, i): (h, i) for h, i in expected_halves}

    # 統計各影片穩定出現 (>= 2 幀) 的合法半局集合與偵測總幀數
    scan_stats: list[dict[str, Any]] = []
    for order_idx, scan in enumerate(raw_scans_in_order):
        counts: Counter[int] = Counter()
        for s in scan.get("samples", []):
            if s.get("detected") and s.get("half") in ("TOP", "BOT") and isinstance(s.get("inning"), int):
                hk = _half_sort_key(s["half"], s["inning"])
                if hk in half_by_hk:
                    counts[hk] += 1
        valid_set = {hk for hk, c in counts.items() if c >= 2}
        total_det = sum(counts.values())
        if not valid_set and transcripts_by_vid:
            tr_segs = (transcripts_by_vid.get(scan["video_id"]) or {}).get("segments", [])
            if len(tr_segs) >= 20:
                from src.clips_asr import detect_transcript_halves

                valid_set = detect_transcript_halves(tr_segs, expected_halves)
                total_det = len(valid_set)
        scan_stats.append(
            {
                "order_idx": order_idx,
                "scan": scan,
                "vid": scan["video_id"],
                "valid_set": valid_set,
                "total_det": total_det,
            }
        )

    # 過濾雙流／高度重疊且無獨佔半局的冗餘影片（如兩支影片同錄第 2~4 局，由較完整者承載全場）
    active_stats: list[dict[str, Any]] = []
    for st in scan_stats:
        v_set = st["valid_set"]
        is_redundant_overlap = False
        if v_set:
            for other in scan_stats:
                if other["order_idx"] == st["order_idx"]:
                    continue
                o_set = other["valid_set"]
                if len(v_set & o_set) >= 2 and len(v_set - o_set) == 0:
                    if (len(o_set), other["total_det"], -other["order_idx"]) > (
                        len(v_set),
                        st["total_det"],
                        -st["order_idx"],
                    ):
                        is_redundant_overlap = True
                        break
        if not is_redundant_overlap:
            active_stats.append(st)

    if not active_stats:
        active_stats = [scan_stats[0]]
    if len(active_stats) == 1:
        return [(active_stats[0]["vid"], expected_halves)]

    # 用前後接續切分點分配（優先採用後段影片「獨佔」的最早穩定半局，避免交界誤切）
    assignments: list[tuple[str, list[tuple[str, int]]]] = []
    start_idx = 0
    for idx, st in enumerate(active_stats):
        vid = st["vid"]
        if idx == len(active_stats) - 1:
            slice_hks = expected_order[start_idx:]
        else:
            nxt_st = active_stats[idx + 1]
            unique_next = sorted(nxt_st["valid_set"] - st["valid_set"])
            next_valid = unique_next or sorted(nxt_st["valid_set"])
            split_hk = (
                next_valid[0]
                if next_valid
                else expected_order[min(len(expected_order) - 1, len(expected_order) // 2)]
            )
            split_pos = next(
                (k for k, hk in enumerate(expected_order) if hk >= split_hk),
                len(expected_order) // 2,
            )
            split_pos = max(start_idx + 1, split_pos)
            slice_hks = expected_order[start_idx:split_pos]
            start_idx = split_pos
        assignments.append((vid, [half_by_hk[hk] for hk in slice_hks]))
    return assignments


def store_scorebug_segments_to_db(
    conn: sqlite3.Connection,
    video_id: str,
    cleaned_timeline: dict[str, Any],
) -> None:
    """將清洗後 Scorebug 半局時間軸寫入 SQLite video_scorebug_segments。"""
    with conn:
        conn.execute("DELETE FROM video_scorebug_segments WHERE video_id = ?", (video_id,))
        for seg in cleaned_timeline.get("half_segments", []):
            conn.execute(
                """
                INSERT OR REPLACE INTO video_scorebug_segments (
                    video_id, inning, half_side, start_sec, end_sec, sample_count, is_interpolated
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    int(seg["inning"]),
                    seg["half_side"],
                    int(seg["start_sec"]),
                    int(seg["end_sec"]),
                    int(seg.get("sample_count", 0)),
                    1 if seg.get("is_interpolated") else 0,
                ),
            )


def _build_game_lineup_matchers(
    game: dict[str, Any],
    build_matcher_fn: Any,
) -> list[dict[str, Any]]:
    """為單場海盜先發球員建置 matcher 清單，供 ASR 偵測賽前打序介紹群集。"""
    team_name = str(game.get("team_name") or "").strip()
    norm_target = re.sub(r"\s+", "", team_name).replace("臺", "台")
    pirate_lineup: list[dict[str, Any]] = []
    for lu_team, items in (game.get("lineups") or {}).items():
        if re.sub(r"\s+", "", str(lu_team)).replace("臺", "台") == norm_target:
            pirate_lineup = items
            break

    order_by_name: dict[str, str] = {}
    for item in pirate_lineup:
        if str(item.get("order", "")).isdigit():
            order_by_name.setdefault(str(item["masked_name"]).strip(), str(item["order"]))

    matchers: list[dict[str, Any]] = []
    for uid, pdata in (game.get("players") or {}).items():
        mname = str(pdata.get("masked_name") or "").strip()
        b_order = order_by_name.get(mname)
        if pdata.get("starting_position") or b_order:
            matchers.append(
                build_matcher_fn(
                    uid,
                    mname,
                    pdata.get("jersey_number"),
                    batting_order=b_order,
                )
            )
    return matchers


def align_game_all_players(
    game: dict[str, Any],
    raw_scans_by_vid: dict[str, dict[str, Any]],
    conn: Optional[sqlite3.Connection] = None,
    transcripts_by_vid: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """將單場所有海盜球員的打席／投球／守備事件對齊至 Scorebug 時間軸，產出初版 clip 清單。"""
    videos = game.get("youtube_videos", [])
    if not videos:
        return []

    available_scans = [raw_scans_by_vid[v["video_id"]] for v in videos if v["video_id"] in raw_scans_by_vid]
    if not available_scans:
        return []

    expected_halves = [
        ("TOP" if hi["half_side"] == "TOP" else "BOT", int(hi["inning"]))
        for hi in game["half_innings"]
    ]
    pa_counts_by_label = {
        hi["half_label"]: sum(1 for ev in hi["events"] if ev["is_plate_appearance"])
        for hi in game["half_innings"]
    }

    part_assignments = _partition_halves_across_videos(
        available_scans,
        expected_halves,
        transcripts_by_vid=transcripts_by_vid,
    )
    assigned_vids = {vid for vid, sub in part_assignments if sub}
    if conn is not None:
        match_id_cur = int(game["match_id"])
        for scan in available_scans:
            vid_unused = scan["video_id"]
            if vid_unused not in assigned_vids:
                other_cnt = conn.execute(
                    "SELECT COUNT(*) FROM match_videos WHERE video_id = ? AND match_id != ?",
                    (vid_unused, match_id_cur),
                ).fetchone()[0]
                if other_cnt == 0:
                    with conn:
                        conn.execute("DELETE FROM video_scorebug_segments WHERE video_id = ?", (vid_unused,))

    seg_by_label: dict[str, tuple[str, dict[str, Any], list[dict[str, Any]]]] = {}

    for vid, sub_halves in part_assignments:
        if not sub_halves:
            continue
        cleaned = clean_scorebug_timeline(raw_scans_by_vid[vid], sub_halves, pa_counts_by_label)
        detected_halves_cnt = sum(
            1 for seg in cleaned.get("half_segments", []) if not seg.get("is_interpolated")
        )
        weak_or_zero_scorebug = (detected_halves_cnt == 0) or (
            detected_halves_cnt <= 1 and len(sub_halves) >= 3
        )
        if weak_or_zero_scorebug and transcripts_by_vid:
            tr_segs = (transcripts_by_vid.get(vid) or {}).get("segments", [])
            if len(tr_segs) >= 20:
                from src.clips_asr import build_player_matcher, infer_half_segments_from_transcript

                lineup_matchers = _build_game_lineup_matchers(game, build_player_matcher)
                dur_sec = int(raw_scans_by_vid[vid].get("duration_sec") or 0)
                asr_segs = infer_half_segments_from_transcript(
                    transcript_segments=tr_segs,
                    expected_halves=sub_halves,
                    duration_sec=dur_sec,
                    pa_counts_by_label=pa_counts_by_label,
                    lineup_matchers=lineup_matchers,
                )
                if asr_segs:
                    cleaned["half_segments"] = asr_segs
        if conn is not None:
            store_scorebug_segments_to_db(conn, vid, cleaned)
        for seg in cleaned["half_segments"]:
            seg_by_label[seg["half_label"]] = (vid, seg, cleaned["cleaned_samples"])

    # 計算該場每個事件 (half_label, event_index) 的時間窗
    ev_time_map: dict[tuple[str, int], dict[str, Any]] = {}
    half_time_map: dict[str, dict[str, Any]] = {}

    for hi in game["half_innings"]:
        hlabel = hi["half_label"]
        if hlabel not in seg_by_label:
            continue
        vid, seg, samples = seg_by_label[hlabel]
        h_start = int(seg["start_sec"])
        h_end = int(seg["end_sec"])
        is_asr_half = seg.get("source") == "asr_anchor"
        is_interp_half = bool(seg.get("is_interpolated")) or is_asr_half
        half_samples = [] if is_interp_half else [s for s in samples if h_start <= s["t"] <= h_end]
        half_time_map[hlabel] = {
            "video_id": vid,
            "start_sec": h_start,
            "end_sec": h_end,
            "sample_count": int(seg.get("sample_count") or 0),
            "is_interpolated": is_interp_half,
            "is_asr_anchor": is_asr_half,
        }

        t_out1: Optional[int] = None
        t_out2: Optional[int] = None
        for s in half_samples:
            if s["outs"] == 1 and t_out1 is None:
                t_out1 = s["t"]
            elif s["outs"] == 2 and t_out2 is None:
                t_out2 = s["t"]

        events = hi["events"]
        pa_events = [e for e in events if e["is_plate_appearance"]]
        cum_outs = 0
        pa_buckets: dict[int, list[dict[str, Any]]] = {0: [], 1: [], 2: []}
        ev_bucket_map: dict[int, int] = {}

        for ev in events:
            outs_before = min(2, cum_outs)
            ev_bucket_map[ev["event_index"]] = outs_before
            if ev["is_plate_appearance"]:
                pa_buckets[outs_before].append(
                    {
                        "event_index": ev["event_index"],
                        "outs_before": outs_before,
                        "outs_delta": ev["outs_delta"],
                        "text": ev["text"],
                    }
                )
            cum_outs = min(3, cum_outs + ev["outs_delta"])

        has_real_out1 = t_out1 is not None
        has_real_out2 = t_out2 is not None
        span = max(20, h_end - h_start)
        total_pa = max(1, len(pa_events))
        if t_out1 is None and (pa_buckets[1] or pa_buckets[2]):
            t_out1 = int(h_start + span * (max(1, len(pa_buckets[0])) / total_pa))
        if t_out2 is None and pa_buckets[2]:
            t_out2 = int(h_start + span * (max(1, len(pa_buckets[0]) + len(pa_buckets[1])) / total_pa))

        bucket_ranges: dict[int, tuple[int, int]] = {}
        b0_end = t_out1 if t_out1 is not None else h_end
        bucket_ranges[0] = (h_start, max(h_start + 8, b0_end))
        b1_start = bucket_ranges[0][1]
        b1_end = t_out2 if t_out2 is not None else h_end
        bucket_ranges[1] = (b1_start, max(b1_start + 8, b1_end))
        b2_start = bucket_ranges[1][1]
        bucket_ranges[2] = (b2_start, max(b2_start + 8, h_end))

        for outs_b in (0, 1, 2):
            b_pas = pa_buckets[outs_b]
            if not b_pas:
                continue
            r_start, r_end = bucket_ranges[outs_b]
            r_span = max(8, r_end - r_start)
            has_hard_anchor = (
                (outs_b == 0 and has_real_out1)
                or (outs_b == 1 and has_real_out1 and has_real_out2)
                or (outs_b == 2 and has_real_out2)
            )
            k = len(b_pas)

            for pos_i, pa in enumerate(b_pas):
                est_start = r_start + (r_span * pos_i) / k
                est_end = r_start + (r_span * (pos_i + 1)) / k

                bs_snap = None
                if pos_i > 0 and not is_interp_half:
                    bs_snap = _find_bs_reset_near(half_samples, int(est_start - 15), int(est_start + 15), est_start)

                if is_interp_half:
                    pa_start = int(round(est_start))
                    method = "asr_half_interp" if is_asr_half else "bracket_interp"
                    conf = "low"
                elif bs_snap is not None:
                    pa_start = bs_snap
                    method = "scorebug_bs_reset"
                    conf = "high" if has_hard_anchor else "medium"
                else:
                    pa_start = int(round(est_start))
                    if k == 1 and has_hard_anchor:
                        method = "scorebug_anchor"
                        conf = "high"
                    elif has_hard_anchor:
                        method = "scorebug_anchor_interp"
                        conf = "medium"
                    else:
                        method = "scorebug_half_interp"
                        conf = "low"

                pa_end = int(round(est_end))
                ev_time_map[(hlabel, pa["event_index"])] = {
                    "video_id": vid,
                    "half_start_sec": h_start,
                    "half_end_sec": h_end,
                    "pa_start_sec": pa_start,
                    "pa_end_sec": pa_end,
                    "method": method,
                    "confidence": conf,
                }

        last_pa_end = h_start
        for idx_ev, ev in enumerate(events):
            key = (hlabel, ev["event_index"])
            if key in ev_time_map:
                last_pa_end = ev_time_map[key]["pa_end_sec"]
            else:
                ob = ev_bucket_map.get(ev["event_index"], 0)
                r_s, r_e = bucket_ranges[ob]
                has_later_pa_in_bucket = any(
                    e["is_plate_appearance"] and ev_bucket_map.get(e["event_index"], 0) == ob
                    for e in events[idx_ev + 1 :]
                )
                next_pa_start = next(
                    (
                        ev_time_map[(hlabel, e["event_index"])]["pa_start_sec"]
                        for e in events[idx_ev + 1 :]
                        if (hlabel, e["event_index"]) in ev_time_map
                    ),
                    None,
                )
                if ev["outs_delta"] > 0 and not has_later_pa_in_bucket:
                    ev_start = max(r_s, last_pa_end)
                    ev_end = r_e
                else:
                    upper_cap = min(r_e, next_pa_start) if next_pa_start is not None else r_e
                    ev_start = min(upper_cap, max(r_s, last_pa_end))
                    ev_end = min(upper_cap, ev_start + 10)
                    last_pa_end = max(last_pa_end, ev_end)

                if is_asr_half:
                    ev_method, ev_conf = "asr_half_interp", "low"
                elif is_interp_half:
                    ev_method, ev_conf = "bracket_interp", "low"
                else:
                    ev_method, ev_conf = "scorebug_anchor_interp", "medium"

                ev_time_map[key] = {
                    "video_id": vid,
                    "half_start_sec": h_start,
                    "half_end_sec": h_end,
                    "pa_start_sec": ev_start,
                    "pa_end_sec": ev_end,
                    "method": ev_method,
                    "confidence": ev_conf,
                }

    # 組裝全場所有海盜球員的片段
    match_id = int(game["match_id"])
    clips: list[dict[str, Any]] = []

    for uid, p_data in game["players"].items():
        masked_name = p_data["masked_name"]
        jersey_number = p_data.get("jersey_number")

        # 1. 打席片段
        for pa in p_data["plate_appearances"]:
            t_info = ev_time_map.get((pa["half_label"], pa["event_index"]))
            if not t_info:
                continue
            start_sec = max(0, t_info["pa_start_sec"] - PRE_ROLL_PA_SEC)
            detail = pa["event_text"]
            if pa.get("runner_notes"):
                detail = f"{detail}（{'；'.join(pa['runner_notes'])}）"
            clips.append(
                {
                    "match_id": match_id,
                    "player_uid": uid,
                    "masked_name": masked_name,
                    "jersey_number": jersey_number,
                    "video_id": t_info["video_id"],
                    "clip_type": "batting",
                    "inning": int(pa["inning"]),
                    "half_side": pa["half_side"],
                    "half_label": pa["half_label"],
                    "event_index": int(pa["event_index"]),
                    "label_short": pa["result_short"],
                    "event_detail": detail,
                    "start_sec": start_sec,
                    "half_start_sec": t_info["half_start_sec"],
                    "half_end_sec": t_info["half_end_sec"],
                    "align_method": t_info["method"],
                    "confidence": t_info["confidence"],
                }
            )

        # 2. 投球片段
        for idx_p, ps in enumerate(p_data["pitching_stints"], start=1):
            hl = ps["half_label"]
            st_idx = int(ps["start_event_index"])
            h_info = half_time_map.get(hl)
            if not h_info:
                continue

            if st_idx <= 1:
                start_sec = max(0, h_info["start_sec"] - PRE_ROLL_PITCH_SEC)
                if h_info.get("is_asr_anchor"):
                    method = "asr_half_start"
                    conf = "medium"
                elif h_info["is_interpolated"]:
                    method = "bracket_interp"
                    conf = "low"
                else:
                    method = "scorebug_half_start"
                    conf = "high"
                vid = h_info["video_id"]
            else:
                prev_t = None
                for cand in range(st_idx - 1, 0, -1):
                    if (hl, cand) in ev_time_map:
                        prev_t = ev_time_map[(hl, cand)]
                        break
                if prev_t:
                    start_sec = max(0, prev_t["pa_end_sec"] - 2)
                    method = "scorebug_reliever_entry"
                    conf = prev_t["confidence"]
                    vid = prev_t["video_id"]
                else:
                    start_sec = max(0, h_info["start_sec"] - PRE_ROLL_PITCH_SEC)
                    method = "scorebug_half_interp"
                    conf = "low"
                    vid = h_info["video_id"]

            role_txt = "先發" if idx_p == 1 and st_idx <= 1 else "登板"
            clips.append(
                {
                    "match_id": match_id,
                    "player_uid": uid,
                    "masked_name": masked_name,
                    "jersey_number": jersey_number,
                    "video_id": vid,
                    "clip_type": "pitching",
                    "inning": int(ps["inning"]),
                    "half_side": ps["half_side"],
                    "half_label": hl,
                    "event_index": st_idx,
                    "label_short": f"{role_txt}・對{ps['batters_faced_in_half']}打席",
                    "event_detail": f"{hl} {role_txt}投球，該半局面對 {ps['batters_faced_in_half']} 打席",
                    "start_sec": start_sec,
                    "half_start_sec": h_info["start_sec"],
                    "half_end_sec": h_info["end_sec"],
                    "align_method": method,
                    "confidence": conf,
                }
            )

        # 3. 守備片段
        for fp in p_data["fielding_plays"]:
            t_info = ev_time_map.get((fp["half_label"], fp["event_index"]))
            if not t_info:
                continue
            start_sec = max(0, t_info["pa_end_sec"] - PRE_ROLL_FIELD_SEC)
            start_sec = max(start_sec, max(0, t_info["pa_start_sec"] - 4))
            clips.append(
                {
                    "match_id": match_id,
                    "player_uid": uid,
                    "masked_name": masked_name,
                    "jersey_number": jersey_number,
                    "video_id": t_info["video_id"],
                    "clip_type": "fielding",
                    "inning": int(fp["inning"]),
                    "half_side": fp["half_side"],
                    "half_label": fp["half_label"],
                    "event_index": int(fp["event_index"]),
                    "label_short": f"[{fp['position']}] {fp['role']}",
                    "event_detail": fp["event_text"],
                    "start_sec": start_sec,
                    "half_start_sec": t_info["half_start_sec"],
                    "half_end_sec": t_info["half_end_sec"],
                    "align_method": t_info["method"],
                    "confidence": t_info["confidence"],
                }
            )

    return clips


async def ensure_scorebug_scans(
    games: list[dict[str, Any]],
    timeline_dir: Path = DEFAULT_TIMELINE_DIR,
    tmp_dir: Path = TMP_MEDIA_DIR,
    align_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """確保所需影片皆有 Scorebug 時間軸快取，並回傳 video_id -> raw_scan 字典。"""
    timeline_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    unique_videos: dict[str, dict[str, Any]] = {}
    for g in games:
        for v in g.get("youtube_videos", []):
            unique_videos.setdefault(v["video_id"], {"video": v, "game": g})

    raw_scans_by_vid: dict[str, dict[str, Any]] = {}
    ocr: Optional[ScorebugOCR] = None

    total = len(unique_videos)
    for idx, (vid, info) in enumerate(unique_videos.items(), start=1):
        cache_path = timeline_dir / f"{vid}.json"
        if not cache_path.exists():
            legacy_path = LEGACY_POC_SCOREBUG_DIR / f"{vid}.json"
            if legacy_path.exists():
                shutil.copy2(legacy_path, cache_path)

        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            raw_scans_by_vid[vid] = data["raw_scan"]
            print(f"  [Scorebug {idx}/{total}] {vid} 讀取快取", flush=True)
            continue

        if align_only:
            print(f"  [Scorebug {idx}/{total}] [SKIP] {vid} 缺少快取", flush=True)
            continue

        if ocr is None:
            ocr = ScorebugOCR()

        g = info["game"]
        tmp_mp4 = tmp_dir / f"{vid}.mp4"
        t0 = time.perf_counter()
        print(f"  [Scorebug {idx}/{total}] {vid} ({g['match_date_iso']} {g['away_team']} vs {g['home_team']}) 下載並掃描...", flush=True)
        try:
            if not tmp_mp4.exists():
                download_temp_video(vid, tmp_mp4)
            dl_sec = time.perf_counter() - t0
            t1 = time.perf_counter()
            raw_scan = await scan_video_scorebug(tmp_mp4, vid, ocr, tmp_dir=tmp_dir)
            scan_sec = time.perf_counter() - t1
            cache_path.write_text(
                json.dumps({"raw_scan": raw_scan}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            raw_scans_by_vid[vid] = raw_scan
            print(f"    -> 完成: 下載 {dl_sec:.1f}s, 掃描 {scan_sec:.1f}s", flush=True)
        finally:
            if tmp_mp4.exists():
                tmp_mp4.unlink()

    return raw_scans_by_vid
