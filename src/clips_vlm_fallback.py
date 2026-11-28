"""軌道 C：Targeted VLM Fallback 與 OpenCV 局部微調模組 (src/clips_vlm_fallback.py)。

定位與觸發原則：
1. 僅針對「Scorebug 不可靠（is_interpolated=True 或樣本不足）且 ASR 語音未覆蓋」之盲區半局觸發，
   已由 Scorebug 或 Whisper ASR 精準定位之半局 100% 略過。
2. 兩階段九宮格抽幀（10s 粗掃 ＋ 針對緊鄰事件 3s 細掃）連同官網逐打席文字送交 Gemini Vision，
   產出各事件與半局時間區間，並搭配 OpenCV 本壘／內野局部動態 ±2~5 秒微調。
3. 結果寫入受 .gitignore 保護之 data/vlm_cache/{video_id}.json 快取；未設定 GEMINI_API_KEY 時
   仍可套用既有快取，對無快取新盲區則安全略過不中斷主流程（或於 --vlm-mode cv_only 走本機 CV）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import numpy as np

from src.clips_scorebug import PRE_ROLL_FIELD_SEC, PRE_ROLL_PA_SEC, PRE_ROLL_PITCH_SEC, TMP_MEDIA_DIR

DEFAULT_VLM_CACHE_DIR = Path("data/vlm_cache")
DEFAULT_VLM_MODEL = "gemini-3.5-flash-lite"
FALLBACK_VLM_MODELS: tuple[str, ...] = (
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
)
PROMPT_VERSION = "v1"

# 局部 OpenCV 微調預設 ROI（與主鏡頭本壘構圖相容）
_ROI_DIRT_REF = (0.35, 0.72, 0.55, 0.88)
_ROI_LEFT_OCC = (0.00, 0.25, 0.35, 0.70)
_ROI_BATTER_UPPER = (0.58, 0.48, 0.72, 0.60)
_ROI_INFIELD = (0.14, 0.47, 0.62, 0.63)


def _fmt_mmss(sec: int) -> str:
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{ss:02d}"
    return f"{m:02d}:{ss:02d}"


def _load_gemini_api_key(env_path: Path = Path(".env")) -> Optional[str]:
    """由環境變數或本地 .env 讀取 GEMINI_API_KEY；未設定時回傳 None。"""
    val = os.environ.get("GEMINI_API_KEY", "").strip()
    if val:
        return val
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "GEMINI_API_KEY":
                cleaned = v.strip().strip('"').strip("'").strip()
                if cleaned:
                    return cleaned
    return None


def _rect_px(norm_rect: tuple[float, float, float, float], w: int, h: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = norm_rect
    return (
        max(0, int(round(x1 * w))),
        max(0, int(round(y1 * h))),
        min(w, int(round(x2 * w))),
        min(h, int(round(y2 * h))),
    )


def _load_scorebug_segments_map(
    conn: Optional[sqlite3.Connection],
    video_ids: set[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """讀取 video_scorebug_segments 作為半局可靠度判斷依據。"""
    if conn is None or not video_ids:
        return {}
    placeholders = ",".join("?" for _ in video_ids)
    try:
        rows = conn.execute(
            f"""
            SELECT video_id, inning, half_side, start_sec, end_sec, sample_count, is_interpolated
            FROM video_scorebug_segments
            WHERE video_id IN ({placeholders})
            """,
            tuple(sorted(video_ids)),
        ).fetchall()
    except sqlite3.Error:
        return {}

    res: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        vid = str(r[0])
        inn = int(r[1])
        hs = str(r[2])
        hlabel = f"{inn}局{'上' if hs == 'TOP' else '下'}"
        res[(vid, hlabel)] = {
            "video_id": vid,
            "inning": inn,
            "half_side": hs,
            "half_label": hlabel,
            "start_sec": int(r[3]),
            "end_sec": int(r[4]),
            "sample_count": int(r[5]),
            "is_interpolated": bool(r[6]),
        }
    return res


def detect_blind_windows(
    game: dict[str, Any],
    clips: list[dict[str, Any]],
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    """識別單場賽事中「Scorebug 不可靠 且 ASR 未覆蓋」的盲區半局，並合併為連續時間窗。"""
    videos = game.get("youtube_videos", [])
    if not videos:
        return []
    duration_by_vid = {str(v["video_id"]): int(v.get("duration_sec") or 0) for v in videos}
    default_vid = str(videos[0]["video_id"])
    seg_map = _load_scorebug_segments_map(conn, set(duration_by_vid.keys()))

    half_statuses: list[dict[str, Any]] = []
    for idx_hi, hi in enumerate(game.get("half_innings", [])):
        hlabel = str(hi["half_label"])
        h_clips = [c for c in clips if c.get("half_label") == hlabel]

        # 決定該半局歸屬的 video_id 與既定起訖
        vid = str(h_clips[0]["video_id"]) if h_clips else None
        seg = None
        if vid is not None:
            seg = seg_map.get((vid, hlabel))
        else:
            for cand_vid in duration_by_vid:
                if (cand_vid, hlabel) in seg_map:
                    vid = cand_vid
                    seg = seg_map[(cand_vid, hlabel)]
                    break
        if vid is None:
            vid = default_vid

        if seg is not None:
            h_start = int(seg["start_sec"])
            h_end = int(seg["end_sec"])
            sample_count = int(seg["sample_count"])
            is_interp = bool(seg["is_interpolated"])
        elif h_clips:
            h_start = int(h_clips[0].get("half_start_sec") or 0)
            h_end = int(h_clips[0].get("half_end_sec") or h_start + 60)
            sample_count = 0 if any(c.get("align_method") in ("bracket_interp", "asr_half_interp", "scorebug_half_interp") for c in h_clips) else 10
            is_interp = any(c.get("align_method") in ("bracket_interp", "asr_half_interp") for c in h_clips)
        else:
            continue

        pa_clips = [c for c in h_clips if c.get("clip_type") == "batting"]
        if pa_clips:
            dual_ratio = sum(1 for c in pa_clips if c.get("align_method") == "dual_track") / len(pa_clips)
        elif h_clips:
            dual_ratio = sum(1 for c in h_clips if c.get("align_method") == "dual_track") / len(h_clips)
        else:
            dual_ratio = 0.0

        low_ratio = (
            sum(1 for c in h_clips if c.get("confidence") == "low") / len(h_clips)
            if h_clips
            else 1.0
        )

        scorebug_unreliable = (seg is None and is_interp) or is_interp or (sample_count < 6) or (low_ratio > 0.5)
        asr_uncovered = dual_ratio < 0.5
        is_blind = bool(scorebug_unreliable and asr_uncovered)

        half_statuses.append(
            {
                "idx": idx_hi,
                "half_label": hlabel,
                "inning": int(hi["inning"]),
                "half_side": str(hi["half_side"]),
                "video_id": vid,
                "start_sec": h_start,
                "end_sec": h_end,
                "is_blind": is_blind,
                "events": hi.get("events", []),
            }
        )

    # 將同一支影片內相鄰的盲區半局合併成單一視窗
    windows: list[dict[str, Any]] = []
    i = 0
    total_h = len(half_statuses)
    while i < total_h:
        if not half_statuses[i]["is_blind"]:
            i += 1
            continue
        vid = half_statuses[i]["video_id"]
        j = i
        while j < total_h and half_statuses[j]["is_blind"] and half_statuses[j]["video_id"] == vid:
            j += 1

        group_halves = half_statuses[i:j]
        dur = duration_by_vid.get(vid, 0)

        # 決定視窗下界：若為該影片第一半局則從 0s 掃描以涵蓋開賽前熱身；否則取前一非盲區半局結尾前 10s
        if i == 0 or half_statuses[i - 1]["video_id"] != vid:
            win_start = 0
            prev_anchor = None
        else:
            prev_anchor = int(half_statuses[i - 1]["end_sec"])
            win_start = max(0, prev_anchor - 10)

        # 決定視窗上界：若後面仍有同影片非盲區半局則取其起點後 15s；否則取影片總長或半局結束後 30s
        if j < total_h and half_statuses[j]["video_id"] == vid:
            next_anchor = int(half_statuses[j]["start_sec"])
            win_end = next_anchor + 15
        else:
            next_anchor = None
            raw_end = int(group_halves[-1]["end_sec"]) + 30
            win_end = dur if dur > raw_end else raw_end

        if dur > 0:
            win_end = min(dur, win_end)
        win_end = max(win_start + 30, win_end)

        windows.append(
            {
                "video_id": vid,
                "win_start": int(win_start),
                "win_end": int(win_end),
                "prev_anchor_sec": prev_anchor,
                "next_anchor_sec": next_anchor,
                "halves": group_halves,
            }
        )
        i = j

    return windows


def build_contact_sheets(
    video_path: Path,
    timestamps: list[int],
    out_dir: Path,
    prefix: str,
    tile_w: int = 426,
    tile_h: int = 240,
) -> list[dict[str, Any]]:
    """將指定時間戳記抽幀並拼接為 3x3 九宮格 JPEG（含 MM:SS (XXXs) 高對比標籤）。"""
    if not timestamps:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"無法開啟影片檔：{video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = (total_frames / fps) if (total_frames > 0 and fps > 0) else 0.0

    # 若為裁切過之區段檔，嘗試由檔名判定起始時間偏移；若為完整檔則偏移為 0
    offset_sec = 0
    m_off = re.search(r"_sec_(\d+)_(\d+)\.mp4$", video_path.name)
    if m_off:
        offset_sec = int(m_off.group(1))

    sheets: list[dict[str, Any]] = []
    try:
        for chunk_idx in range(0, len(timestamps), 9):
            chunk_ts = timestamps[chunk_idx : chunk_idx + 9]
            tiles: list[np.ndarray] = []
            for t in chunk_ts:
                rel_t = max(0.0, float(t - offset_sec))
                if duration > 0:
                    rel_t = min(max(0.0, duration - 0.1), rel_t)
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(rel_t * fps)))
                ok, frame = cap.read()
                if not ok or frame is None:
                    tile = np.zeros((tile_h, tile_w, 3), dtype=np.uint8)
                else:
                    tile = cv2.resize(frame, (tile_w, tile_h))
                label = f"{_fmt_mmss(t)} ({t}s)"
                cv2.rectangle(tile, (4, 4), (175, 32), (0, 0, 0), -1)
                cv2.putText(
                    tile,
                    label,
                    (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.62,
                    (0, 255, 255),
                    2,
                )
                tiles.append(tile)

            while len(tiles) < 9:
                tiles.append(np.zeros((tile_h, tile_w, 3), dtype=np.uint8))

            rows = [np.hstack(tiles[r : r + 3]) for r in range(0, 9, 3)]
            grid = np.vstack(rows)
            out_path = out_dir / f"{prefix}_{chunk_idx // 9 + 1:02d}.jpg"
            cv2.imwrite(str(out_path), grid, [cv2.IMWRITE_JPEG_QUALITY, 88])
            sheets.append({"path": out_path, "timestamps": list(chunk_ts)})
    finally:
        cap.release()

    return sheets


def parse_vlm_response_json(
    raw_text: str,
    win_start: int,
    win_end: int,
    expected_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """解析 VLM 回傳之 JSON（含 markdown code fence 剝離、範圍夾緊與單調遞增防呆）。"""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    m_obj = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m_obj:
        text = m_obj.group(0)

    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("VLM 回應根節點必須為 JSON Object")

    def _clamp(val: Any, fallback: int) -> int:
        try:
            v = int(round(float(val)))
        except (TypeError, ValueError):
            return fallback
        return max(win_start, min(win_end, v))

    game_start_sec = _clamp(data.get("game_start_sec"), win_start) if data.get("game_start_sec") is not None else None

    half_bounds: dict[str, dict[str, int]] = {}
    for hb in data.get("half_boundaries", []) or []:
        if not isinstance(hb, dict) or not hb.get("half_label"):
            continue
        hl = str(hb["half_label"]).strip()
        hs = _clamp(hb.get("start_sec"), win_start)
        he = _clamp(hb.get("end_sec"), win_end)
        if he < hs:
            he = min(win_end, hs + 20)
        half_bounds[hl] = {"start_sec": hs, "end_sec": he}

    raw_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for ev in data.get("events", []) or []:
        if not isinstance(ev, dict):
            continue
        hl = str(ev.get("half_label") or "").strip()
        try:
            ev_idx = int(ev["event_index"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_by_key[(hl, ev_idx)] = ev

    normalized_events: list[dict[str, Any]] = []
    prev_t = win_start
    for exp in expected_events:
        key = (str(exp["half_label"]), int(exp["event_index"]))
        raw_ev = raw_by_key.get(key)
        hb = half_bounds.get(key[0])
        low_b = hb["start_sec"] if hb else win_start
        high_b = hb["end_sec"] if hb else win_end

        if raw_ev is not None:
            st = _clamp(raw_ev.get("start_sec"), max(prev_t, low_b))
            st = max(prev_t, st)
            en = _clamp(raw_ev.get("end_sec"), min(high_b, st + 15))
            en = max(st, en)
            needs_fine = bool(raw_ev.get("needs_fine_scan", False))
            cue = str(raw_ev.get("visual_cue") or "").strip()
            has_match = True
        else:
            st = max(prev_t, low_b)
            en = min(high_b, st + 12)
            needs_fine = False
            cue = ""
            has_match = False

        prev_t = st
        normalized_events.append(
            {
                "half_label": key[0],
                "event_index": key[1],
                "start_sec": st,
                "end_sec": en,
                "needs_fine_scan": needs_fine,
                "visual_cue": cue,
                "matched_by_vlm": has_match,
            }
        )

    return {
        "game_start_sec": game_start_sec,
        "half_boundaries": half_bounds,
        "events": normalized_events,
    }


def _build_vlm_prompt(
    game: dict[str, Any],
    window: dict[str, Any],
    expected_events: list[dict[str, Any]],
    is_fine_pass: bool = False,
) -> str:
    """組合交給 Gemini Vision 的繁體中文結構化定位 Prompt。"""
    halves_desc = "、".join(h["half_label"] for h in window["halves"])
    prev_hint = (
        f"上一已知半局結束於約 {window['prev_anchor_sec']} 秒（{_fmt_mmss(window['prev_anchor_sec'])}）。"
        if window.get("prev_anchor_sec") is not None
        else "此區段涵蓋影片開頭，請務必排除賽前列隊敬禮與捕手／投手熱身時間，找出第一位打者站進打擊區開打的秒數。"
    )
    next_hint = (
        f"下一已知半局開始於約 {window['next_anchor_sec']} 秒（{_fmt_mmss(window['next_anchor_sec'])}）。"
        if window.get("next_anchor_sec") is not None
        else ""
    )

    ev_lines = []
    for ev in expected_events:
        kind = "打席" if ev.get("is_plate_appearance") else "非打席異動"
        ev_lines.append(
            f"- [{ev['half_label']} event_index={ev['event_index']} | {kind}] {ev['event_text']}"
        )
    ev_block = "\n".join(ev_lines)

    pass_note = (
        "這是針對緊湊交錯事件的「2~3 秒高密度細掃九宮格」，請更精準校準事件切換秒數。"
        if is_fine_pass
        else "這是每 10 秒一格的「粗掃九宮格」，每格左上角標示 `MM:SS (XXXs)`。"
    )

    return (
        f"你是台灣少棒比賽轉播影像時間軸定位專家。本場對戰為「{game['away_team']}（先攻／上）vs {game['home_team']}（後攻／下）」。\n"
        f"{pass_note}\n"
        f"盲區涵蓋半局：{halves_desc}（影片區間 {window['win_start']}s ~ {window['win_end']}s）。\n"
        f"{prev_hint}{next_hint}\n\n"
        "請對照圖片左上角時間碼與畫面語意（打者站進左/右打擊區、揮棒跑壘、牽制、主審手勢、攻守交換跑離場內、以及左下角記分板若有顯示的局數/出局數），"
        "為以下官網事件依時序定位出對應秒數區間：\n"
        f"{ev_block}\n\n"
        "請只輸出嚴格 JSON，格式如下：\n"
        "{\n"
        '  "game_start_sec": 136,\n'
        '  "half_boundaries": [\n'
        '    {"half_label": "1局上", "start_sec": 136, "end_sec": 294}\n'
        "  ],\n"
        '  "events": [\n'
        '    {"half_label": "1局上", "event_index": 2, "start_sec": 136, "end_sec": 156, "needs_fine_scan": false, "visual_cue": "右打者站進打擊區並擊出安打"}\n'
        "  ]\n"
        "}\n"
        "規則：所有 start_sec 必須單調遞增且落在區間內；若兩事件交替緊湊不易區分，請將 needs_fine_scan 設為 true。"
    )


def call_vlm_on_sheets(
    sheet_paths: list[Path],
    prompt_text: str,
    model_name: str = DEFAULT_VLM_MODEL,
    api_key: Optional[str] = None,
) -> tuple[str, str]:
    """呼叫 Gemini Vision API（含模型 404 自動備援清單），回傳 (回應文字, 實際使用模型)。"""
    from google import genai
    from google.genai import types

    key = api_key or _load_gemini_api_key()
    if not key:
        raise RuntimeError("未設定 GEMINI_API_KEY")

    client = genai.Client(api_key=key)
    contents: list[Any] = [prompt_text]
    for p in sheet_paths:
        contents.append(types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg"))

    candidates: list[str] = [model_name]
    for fb in FALLBACK_VLM_MODELS:
        if fb not in candidates:
            candidates.append(fb)

    last_exc: Optional[Exception] = None
    for cand in candidates:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=cand,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
                txt = getattr(resp, "text", None) or ""
                if txt.strip():
                    if cand != model_name:
                        print(f"    -> [VLM] 模型 {model_name} 不可用，已自動切換為 {cand}", flush=True)
                    return txt, cand
                break
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                is_model_missing = any(tok in msg for tok in ("404", "not_found", "not found", "unsupported", "invalid argument"))
                if is_model_missing:
                    break
                is_transient = any(tok in msg for tok in ("429", "resource_exhausted", "503", "unavailable", "rate limit"))
                if is_transient:
                    if attempt < 2:
                        wait_s = 15 * (attempt + 1)
                        print(f"    -> [VLM] 遭遇暫時性限流/高負載 ({cand})，{wait_s} 秒後重試 ({attempt + 1}/2)...", flush=True)
                        time.sleep(wait_s)
                        continue
                    print(f"    -> [VLM] {cand} 持續暫時不可用，改試下一備援模型...", flush=True)
                    break
                raise

    raise RuntimeError(f"所有候選 VLM 模型皆呼叫失敗 ({candidates}): {last_exc}")


def refine_event_sec_with_opencv(
    video_path: Path,
    est_sec: int,
    win_bounds: tuple[int, int],
    is_pitching: bool = False,
) -> tuple[int, bool]:
    """在 [est_sec - 12, est_sec + 12] 內執行抗雲影 OpenCV 局部微調（偏移上限 ±5s）。"""
    w_start, w_end = int(win_bounds[0]), int(win_bounds[1])
    scan_s = max(w_start, int(est_sec) - 12)
    scan_e = min(w_end, int(est_sec) + 12)
    if scan_e - scan_s < 4 or not video_path.exists():
        return int(est_sec), False

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return int(est_sec), False

    offset_sec = 0
    m_off = re.search(r"_sec_(\d+)_(\d+)\.mp4$", video_path.name)
    if m_off:
        offset_sec = int(m_off.group(1))

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w <= 0 or h <= 0 or total_frames <= 0:
            return int(est_sec), False

        # 取局部窗內抽樣影格建立參考背景
        frames: list[tuple[float, np.ndarray]] = []
        t = float(scan_s)
        while t <= scan_e + 1e-6:
            rel_t = max(0.0, t - offset_sec)
            idx = min(total_frames - 1, int(round(rel_t * fps)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, fr = cap.read()
            if ok and fr is not None:
                frames.append((t, fr))
            t += 0.5
        if len(frames) < 6:
            return int(est_sec), False

        med = np.median(np.stack([fr for _, fr in frames], axis=0), axis=0).astype(np.uint8)
        dx1, dy1, dx2, dy2 = _rect_px(_ROI_DIRT_REF, w, h)
        ox1, oy1, ox2, oy2 = _rect_px(_ROI_LEFT_OCC, w, h)
        bx1, by1, bx2, by2 = _rect_px(_ROI_BATTER_UPPER, w, h)
        ix1, iy1, ix2, iy2 = _rect_px(_ROI_INFIELD, w, h)

        bg_dirt = np.mean(med[dy1:dy2, dx1:dx2].astype(np.float32), axis=(0, 1)) + 1e-3

        occ_cnt = 0
        cand_scores: list[tuple[float, float]] = []
        prev_inf: Optional[np.ndarray] = None

        for cur_t, fr in frames:
            cur_dirt = np.mean(fr[dy1:dy2, dx1:dx2].astype(np.float32), axis=(0, 1)) + 1e-3
            gain = np.clip(bg_dirt / cur_dirt, 0.65, 1.55)
            fr_norm = np.clip(fr.astype(np.float32) * gain, 0, 255).astype(np.uint8)
            fg = np.max(cv2.absdiff(fr_norm, med), axis=2) > 30

            left_occ = float(np.mean(fg[oy1:oy2, ox1:ox2]))
            if left_occ > 0.25:
                occ_cnt += 1

            inf_gray = cv2.cvtColor(fr_norm[iy1:iy2, ix1:ix2], cv2.COLOR_BGR2GRAY).astype(np.float32)
            inf_mot = float(np.mean(np.abs(inf_gray - prev_inf))) if prev_inf is not None else 0.0
            prev_inf = inf_gray
            bat_fg = float(np.mean(fg[by1:by2, bx1:bx2]))

            if abs(cur_t - est_sec) <= 5.0 and left_occ <= 0.25:
                score = inf_mot if is_pitching else (bat_fg * 20.0 + inf_mot * 0.4)
                cand_scores.append((cur_t, score))

        if occ_cnt / len(frames) > 0.40 or not cand_scores:
            return int(est_sec), False

        # 取局部最佳點並約束偏移在 ±5s 內
        best_t, best_score = max(cand_scores, key=lambda x: x[1])
        if best_score < 2.0:
            return int(est_sec), False
        refined = int(round(best_t))
        refined = max(int(est_sec) - 5, min(int(est_sec) + 5, refined))
        refined = max(w_start, min(w_end, refined))
        return refined, (refined != int(est_sec))
    finally:
        cap.release()


def _download_blind_section(
    video_id: str,
    win_start: int,
    win_end: int,
    tmp_dir: Path = TMP_MEDIA_DIR,
) -> Path:
    """取得涵蓋盲區之本地影片檔：優先多線程下載整支 360p 暫存檔共用，失敗再退回區段下載。"""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    full_tmp = tmp_dir / f"{video_id}.mp4"
    if full_tmp.exists() and full_tmp.stat().st_size > 100_000:
        return full_tmp

    if win_end <= 900:
        poc_clip = Path("data/tmp_poc_videos") / f"{video_id}_0_900.mp4"
        if poc_clip.exists() and poc_clip.stat().st_size > 100_000:
            return poc_clip

    sec_path = tmp_dir / f"{video_id}_sec_{win_start}_{win_end}.mp4"
    if sec_path.exists() and sec_path.stat().st_size > 50_000:
        return sec_path

    url = f"https://www.youtube.com/watch?v={video_id}"
    fmt = (
        "bestvideo[height<=360][vcodec^=avc1][ext=mp4]/best[height<=360][ext=mp4]/"
        "bestvideo[height<=360][ext=mp4]/bestvideo[height<=480][ext=mp4]/best[height<=480]"
    )

    # 優先使用 --concurrent-fragments 8 多線程整檔下載 360p（通常 15~30s 即可完成，且同片多盲區共用）
    full_cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-f",
        fmt,
        "--concurrent-fragments",
        "8",
        "--no-part",
        "--quiet",
        "-o",
        str(full_tmp),
        url,
    ]
    res_full = subprocess.run(full_cmd, capture_output=True, text=True)
    if res_full.returncode == 0 and full_tmp.exists() and full_tmp.stat().st_size > 100_000:
        return full_tmp
    full_tmp.unlink(missing_ok=True)

    # 備援：退回 --download-sections 單段下載
    section_arg = f"*{_fmt_mmss(win_start)}-{_fmt_mmss(win_end)}"
    sec_cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-f",
        fmt,
        "--download-sections",
        section_arg,
        "--force-keyframes-at-cuts",
        "--no-part",
        "--quiet",
        "-o",
        str(sec_path),
        url,
    ]
    subprocess.run(sec_cmd, check=True)
    return sec_path


def _apply_vlm_alignment_to_clips(
    clips: list[dict[str, Any]],
    vid: str,
    vlm_result: dict[str, Any],
    use_opencv_refine: bool,
) -> int:
    """將 VLM（與選擇性 OpenCV 微調）時間軸套用至單場片段清單，回傳升級筆數。"""
    ev_map: dict[tuple[str, int], dict[str, Any]] = {
        (str(e["half_label"]), int(e["event_index"])): e
        for e in vlm_result.get("events", [])
    }
    hb_map: dict[str, dict[str, int]] = vlm_result.get("half_boundaries", {})
    upgraded = 0

    for c in clips:
        if c.get("video_id") != vid:
            continue
        if c.get("align_method") in ("dual_track", "manual", "manual_override"):
            continue

        hl = str(c["half_label"])
        ev_idx = int(c["event_index"])
        hb = hb_map.get(hl)
        if hb:
            c["half_start_sec"] = int(hb["start_sec"])
            c["half_end_sec"] = int(hb["end_sec"])

        ev_res = ev_map.get((hl, ev_idx))
        if ev_res is not None:
            raw_start = int(ev_res["start_sec"])
            raw_end = int(ev_res.get("end_sec") or raw_start + 12)
            refined_sec = ev_res.get("opencv_refined_sec")

            if c.get("clip_type") == "fielding":
                base_sec = refined_sec if (use_opencv_refine and refined_sec is not None) else max(0, raw_end - PRE_ROLL_FIELD_SEC)
                base_sec = max(base_sec, max(0, raw_start - 4))
            elif c.get("clip_type") == "pitching" and ev_idx <= 1:
                h_st = hb["start_sec"] if hb else raw_start
                base_sec = max(0, int(h_st) - PRE_ROLL_PITCH_SEC)
            else:
                # 若 VLM 已回傳打者進場起點，僅微留 2 秒緩衝避免過度倒退至上一棒
                if use_opencv_refine and refined_sec is not None:
                    base_sec = max(0, int(refined_sec) - 2)
                else:
                    base_sec = max(0, raw_start - min(2, PRE_ROLL_PA_SEC))

            c["start_sec"] = int(base_sec)
            was_refined = bool(use_opencv_refine and ev_res.get("opencv_refined"))
            c["align_method"] = "vlm_cv_refined" if was_refined else "vlm_fallback"
            c["confidence"] = "high" if (was_refined or ev_res.get("matched_by_vlm")) else "medium"
            upgraded += 1
        elif c.get("clip_type") == "pitching" and ev_idx <= 1 and hb:
            c["start_sec"] = max(0, int(hb["start_sec"]) - PRE_ROLL_PITCH_SEC)
            c["align_method"] = "vlm_fallback"
            c["confidence"] = "medium"
            upgraded += 1

    return upgraded


def estimate_blind_window_cv_only(
    window: dict[str, Any],
    video_path: Path,
) -> dict[str, Any]:
    """純本機 OpenCV 備援估測（無 API Key 且 --vlm-mode cv_only 時使用）。"""
    win_start = int(window["win_start"])
    win_end = int(window["win_end"])
    halves = window["halves"]
    total_pas = max(1, sum(sum(1 for e in h["events"] if e.get("is_plate_appearance")) for h in halves))

    cur_t = float(win_start)
    span = max(30.0, float(win_end - win_start))
    half_bounds: dict[str, dict[str, int]] = {}
    events_out: list[dict[str, Any]] = []

    for h in halves:
        h_ev = h["events"]
        h_pas = max(1, sum(1 for e in h_ev if e.get("is_plate_appearance")))
        h_dur = span * (h_pas / total_pas)
        h_s = int(round(cur_t))
        h_e = int(round(min(win_end, cur_t + h_dur)))
        half_bounds[h["half_label"]] = {"start_sec": h_s, "end_sec": h_e}

        ev_cnt = max(1, len(h_ev))
        for idx_e, ev in enumerate(h_ev):
            est = int(round(h_s + (h_e - h_s) * (idx_e / ev_cnt)))
            ref_t, _ = refine_event_sec_with_opencv(
                video_path, est, (h_s, h_e), is_pitching=(not ev.get("is_plate_appearance"))
            )
            events_out.append(
                {
                    "half_label": h["half_label"],
                    "event_index": int(ev["event_index"]),
                    "start_sec": ref_t,
                    "end_sec": min(h_e, ref_t + 15),
                    "needs_fine_scan": False,
                    "visual_cue": "cv_only",
                    "matched_by_vlm": False,
                    "opencv_refined": True,
                    "opencv_refined_sec": ref_t,
                }
            )
        cur_t += h_dur

    return {"game_start_sec": win_start, "half_boundaries": half_bounds, "events": events_out}


def refine_clips_with_vlm_fallback(
    games: list[dict[str, Any]],
    clips_by_match: dict[int, list[dict[str, Any]]],
    conn: Optional[sqlite3.Connection],
    vlm_mode: str = "blind_only",
    model_name: str = DEFAULT_VLM_MODEL,
    use_opencv_refine: bool = True,
    align_only: bool = False,
    cache_dir: Path = DEFAULT_VLM_CACHE_DIR,
    tmp_dir: Path = TMP_MEDIA_DIR,
    vlm_caller: Optional[Callable[[list[Path], str, str, Optional[str]], tuple[str, str]]] = None,
) -> dict[str, int]:
    """軌道 C 主入口：僅對盲區觸發 VLM 九宮格定位與 OpenCV 微調。"""
    if vlm_mode == "off":
        return {"blind_windows": 0, "vlm_calls": 0, "cache_hits": 0, "clips_upgraded": 0}

    cache_dir.mkdir(parents=True, exist_ok=True)
    api_key = _load_gemini_api_key()
    caller = vlm_caller or call_vlm_on_sheets

    stats = {"blind_windows": 0, "vlm_calls": 0, "cache_hits": 0, "clips_upgraded": 0}

    for idx_g, g in enumerate(games):
        mid = int(g["match_id"])
        m_clips = clips_by_match.get(mid, [])
        if not m_clips:
            continue

        windows = detect_blind_windows(g, m_clips, conn=conn)
        if not windows:
            continue
        stats["blind_windows"] += len(windows)
        future_vids = {
            str(v["video_id"])
            for fg in games[idx_g + 1 :]
            for v in fg.get("youtube_videos", [])
        }
        processed_vids_in_game: set[str] = set()

        for w_idx, win in enumerate(windows):
            vid = str(win["video_id"])
            processed_vids_in_game.add(vid)
            win_key = f"{win['win_start']}_{win['win_end']}_{PROMPT_VERSION}"
            cache_path = cache_dir / f"{vid}.json"
            cached_doc: dict[str, Any] = {}
            if cache_path.exists():
                try:
                    cached_doc = json.loads(cache_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    cached_doc = {}

            windows_cache = cached_doc.setdefault("windows", {})
            vlm_result: Optional[dict[str, Any]] = None

            if vlm_mode != "force" and win_key in windows_cache:
                vlm_result = windows_cache[win_key]
                stats["cache_hits"] += 1
                print(f"  [VLM 快取命中] {vid} 盲區 {win['win_start']}s~{win['win_end']}s，直接套用快取", flush=True)
            elif vlm_mode == "cv_only":
                if align_only and not (Path("data/tmp_poc_videos") / f"{vid}_0_900.mp4").exists() and not (tmp_dir / f"{vid}.mp4").exists():
                    continue
                sec_video = _download_blind_section(vid, win["win_start"], win["win_end"], tmp_dir=tmp_dir)
                vlm_result = estimate_blind_window_cv_only(win, sec_video)
            else:
                # blind_only / force 模式需要金鑰與 google-genai
                if vlm_caller is None:
                    if not api_key:
                        print(f"  [VLM] 未設定 GEMINI_API_KEY，略過新盲區 API 定位 ({vid} {win['win_start']}s~{win['win_end']}s)", flush=True)
                        continue
                    try:
                        import google.genai  # noqa: F401
                    except ImportError:
                        print("  [VLM] 尚未安裝 google-genai，略過新盲區 API 定位", flush=True)
                        continue

                expected_events: list[dict[str, Any]] = []
                for h in win["halves"]:
                    for ev in h["events"]:
                        expected_events.append(
                            {
                                "half_label": h["half_label"],
                                "event_index": int(ev["event_index"]),
                                "is_plate_appearance": bool(ev.get("is_plate_appearance", True)),
                                "event_text": str(ev.get("text") or ev.get("event_text") or ""),
                            }
                        )

                sec_video = _download_blind_section(vid, win["win_start"], win["win_end"], tmp_dir=tmp_dir)
                sheet_dir = tmp_dir / f"vlm_sheets_{vid}_{w_idx}"
                coarse_ts = list(range(win["win_start"], win["win_end"] + 1, 10))
                coarse_sheets = build_contact_sheets(sec_video, coarse_ts, sheet_dir, prefix="coarse")
                coarse_prompt = _build_vlm_prompt(g, win, expected_events, is_fine_pass=False)

                raw_resp, actual_model = caller(
                    [s["path"] for s in coarse_sheets],
                    coarse_prompt,
                    model_name,
                    api_key,
                )
                stats["vlm_calls"] += 1
                vlm_result = parse_vlm_response_json(raw_resp, win["win_start"], win["win_end"], expected_events)

                # 若模型標記 needs_fine_scan 或相鄰事件緊湊 (< 35s)，針對區間補做 3s 細掃
                fine_ts_set: set[int] = set()
                evs = vlm_result.get("events", [])
                for idx_e, ev_r in enumerate(evs):
                    gap_prev = (ev_r["start_sec"] - evs[idx_e - 1]["start_sec"]) if idx_e > 0 else 999
                    if ev_r.get("needs_fine_scan") or gap_prev < 35:
                        f_s = max(win["win_start"], ev_r["start_sec"] - 15)
                        f_e = min(win["win_end"], ev_r["end_sec"] + 15)
                        for ft in range(f_s, f_e + 1, 3):
                            fine_ts_set.add(ft)

                if fine_ts_set:
                    fine_ts = sorted(fine_ts_set)[:27]  # 最多 3 張細掃九宮格
                    fine_sheets = build_contact_sheets(sec_video, fine_ts, sheet_dir, prefix="fine")
                    fine_prompt = _build_vlm_prompt(g, win, expected_events, is_fine_pass=True)
                    raw_fine, actual_model = caller(
                        [s["path"] for s in fine_sheets],
                        fine_prompt,
                        actual_model,
                        api_key,
                    )
                    stats["vlm_calls"] += 1
                    vlm_result = parse_vlm_response_json(raw_fine, win["win_start"], win["win_end"], expected_events)

                # OpenCV ±2~5s 局部微調
                if use_opencv_refine and sec_video.exists():
                    for ev_r in vlm_result.get("events", []):
                        hb = vlm_result.get("half_boundaries", {}).get(ev_r["half_label"])
                        bounds = (
                            (int(hb["start_sec"]), int(hb["end_sec"]))
                            if hb
                            else (win["win_start"], win["win_end"])
                        )
                        ref_sec, did_refine = refine_event_sec_with_opencv(
                            sec_video, int(ev_r["start_sec"]), bounds
                        )
                        ev_r["opencv_refined"] = did_refine
                        ev_r["opencv_refined_sec"] = ref_sec

                vlm_result["actual_model"] = actual_model
                windows_cache[win_key] = vlm_result
                cached_doc["video_id"] = vid
                cache_path.write_text(
                    json.dumps(cached_doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(
                    f"  [VLM 完成] {vid} 盲區 {win['win_start']}s~{win['win_end']}s "
                    f"(模型={actual_model}，已寫入快取)",
                    flush=True,
                )

            if vlm_result is not None:
                up = _apply_vlm_alignment_to_clips(
                    m_clips, vid=vid, vlm_result=vlm_result, use_opencv_refine=use_opencv_refine
                )
                stats["clips_upgraded"] += up

        # 本場次處理完畢後，立即清理不再被後續場次使用的影片與九宮格暫存
        for vid_done in processed_vids_in_game:
            if vid_done not in future_vids:
                (tmp_dir / f"{vid_done}.mp4").unlink(missing_ok=True)
                for sec_f in tmp_dir.glob(f"{vid_done}_sec_*.mp4"):
                    sec_f.unlink(missing_ok=True)
            for s_dir in tmp_dir.glob(f"vlm_sheets_{vid_done}_*"):
                shutil.rmtree(s_dir, ignore_errors=True)

    return stats
