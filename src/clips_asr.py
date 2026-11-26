"""軌道 B：本機 faster-whisper ASR 語音含時間戳記逐字稿與雙軌精修模組 (src/clips_asr.py)。

設計重點：
- 使用本機 faster-whisper (int8 / cpu_threads) 進行零 API 費用語音轉錄與字詞級時間戳記 (word_timestamps=True)
- 支援 targeted（僅 low 半局）、low_medium、full、off 與 speech_pa（所有打席半局優先採主播語音唱名時間）
- 使用 pypinyin 進行「背號 + 同音姓名 + 模糊拼音 + 棒次語境」評分比對並套用時間單調遞增約束
- 入庫 video_transcript_segments 前自動遮罩球員姓名中間字為 '〇'，保護未成年隱私
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any, Optional

import numpy as np
from pypinyin import Style, lazy_pinyin

DEFAULT_TRANSCRIPT_DIR = Path("data/transcripts")
TMP_MEDIA_DIR = Path("data/tmp_clips_media")

_CN_DIGITS = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
_PA_POSITIVE_CUES = (
    "輪到",
    "打擊",
    "交給",
    "面對",
    "棒",
    "打者",
    "上場",
    "代打",
    "背號",
    "首位",
    "首名",
)
_PA_NEGATIVE_CUES = (
    "剛剛",
    "前一次",
    "上次",
    "跑者",
    "壘上",
    "盜",
    "回來",
    "保送了",
    "三振了",
    "三振掉",
    "接殺",
    "刺殺",
)
_FUZZY_CTX_CUES = ("棒", "打擊", "輪到", "交給", "上場", "打者", "面對", "代打", "號")


def _normalize_pinyin_token(py: str) -> str:
    """正規化中文拼音以容忍台灣常見口音/同音字差異（如 zh/z, eng/en, f/h, l/n）。"""
    s = py.lower().strip()
    for a, b in (("zh", "z"), ("ch", "c"), ("sh", "s")):
        if s.startswith(a):
            s = b + s[len(a) :]
            break
    if s.startswith("f"):
        s = "h" + s[1:]
    if s.startswith("l"):
        s = "n" + s[1:]
    s = s.replace("iang", "ian").replace("uang", "uan")
    s = s.replace("eng", "en").replace("ing", "in").replace("ang", "an")
    return s


def _pinyin_initial(py: str) -> str:
    """取得正規化拼音之聲母。"""
    if not py:
        return ""
    for dig in ("zh", "ch", "sh"):
        if py.startswith(dig):
            return dig
    if py[0] in "bpmfdtnlgkhjqxrzcsyw":
        return py[0]
    return ""


def _jersey_to_variants(jersey: Optional[str]) -> list[str]:
    """產生球衣背號在中文主播口語中的常見字樣（如 79 -> ['79', '七十九', '七九']）。"""
    if not jersey or not jersey.isdigit():
        return []
    n = int(jersey)
    variants = [str(n)]
    if jersey != str(n):
        variants.append(jersey)
    if 0 <= n <= 9:
        variants.append(_CN_DIGITS[n])
    elif 10 <= n <= 99:
        tens, ones = divmod(n, 10)
        if tens == 1:
            cn = "十" + (_CN_DIGITS[ones] if ones else "")
        else:
            cn = _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")
        variants.append(cn)
        if tens > 0:
            variants.append(_CN_DIGITS[tens] + _CN_DIGITS[ones])
    return variants


def _order_to_variants(order: Optional[str]) -> list[str]:
    """將先發棒次（如 '2'）轉為主播常見棒次詞（如 ['第二棒', '第2棒', '二棒', '2棒']）。"""
    if not order:
        return []
    s = str(order).strip()
    if not s.isdigit():
        return []
    n = int(s)
    if n < 1 or n > 12:
        return []
    if n <= 9:
        cn = _CN_DIGITS[n]
    elif n == 10:
        cn = "十"
    else:
        cn = f"十{_CN_DIGITS[n - 10]}"
    variants = [f"第{cn}棒", f"第{n}棒", f"{cn}棒", f"{n}棒"]
    if n == 1:
        variants.extend(["首棒", "第一名打者", "首位打者"])
    return variants


def build_player_matcher(
    player_uid: str,
    masked_name: str,
    jersey_number: Optional[str],
    batting_order: Optional[str] = None,
) -> dict[str, Any]:
    """為單一球員建立同音姓名、模糊拼音、背號與棒次比對器。"""
    clean_name = masked_name.strip()
    first_char = clean_name[0] if clean_name else ""
    last_char = clean_name[-1] if len(clean_name) >= 2 and clean_name[-1] not in ("〇", "O", "○") else ""

    first_py = _normalize_pinyin_token(lazy_pinyin(first_char, style=Style.NORMAL)[0]) if first_char else ""
    last_py = _normalize_pinyin_token(lazy_pinyin(last_char, style=Style.NORMAL)[0]) if last_char else ""
    last_prefix2 = last_py[:2] if len(last_py) >= 2 else last_py
    last_initial = _pinyin_initial(last_py)
    jersey_vars = _jersey_to_variants(jersey_number)
    order_vars = _order_to_variants(batting_order)

    return {
        "player_uid": player_uid,
        "masked_name": clean_name,
        "first_char": first_char,
        "last_char": last_char,
        "first_py": first_py,
        "last_py": last_py,
        "last_prefix2": last_prefix2,
        "last_initial": last_initial,
        "jersey_variants": jersey_vars,
        "batting_order": batting_order,
        "order_variants": order_vars,
    }


def _apply_context_score(text: str, char_pos: int, base_score: int, matcher: dict[str, Any]) -> int:
    """依據命中位置前後文評分（唱名語境加分、事後評述／跑者語境扣分）。"""
    score = base_score
    ctx14 = text[max(0, char_pos - 14) : char_pos]
    ctx8 = text[max(0, char_pos - 8) : char_pos]
    if any(kw in ctx14 for kw in _PA_POSITIVE_CUES) or any(ov in ctx14 for ov in matcher.get("order_variants", [])):
        score += 15
    if any(kw in ctx8 for kw in _PA_NEGATIVE_CUES):
        score -= 35
    return score


def _find_player_matches_in_text(text: str, matcher: dict[str, Any]) -> list[dict[str, Any]]:
    """搜尋片段文字中所有命中的候選位置，回傳含 char_pos、reason 與 score 的清單。"""
    candidates: list[dict[str, Any]] = []
    seen_pos: set[int] = set()

    first_py = matcher["first_py"]
    last_py = matcher["last_py"]
    last_prefix2 = matcher.get("last_prefix2", "")
    last_initial = matcher.get("last_initial", "")
    order_vars = matcher.get("order_variants", [])

    cjk_indices = [idx for idx, ch in enumerate(text) if "\u4e00" <= ch <= "\u9fff"]
    char_pys = [_normalize_pinyin_token(lazy_pinyin(text[idx], style=Style.NORMAL)[0]) for idx in cjk_indices]

    # Tier 1: 同音姓氏 + 尾字 (基礎分 100)
    if first_py and last_py and len(cjk_indices) >= 2:
        for i, py_i in enumerate(char_pys):
            if py_i != first_py:
                continue
            for j in range(i + 1, min(len(char_pys), i + 4)):
                if char_pys[j] == last_py:
                    cpos = cjk_indices[i]
                    sc = _apply_context_score(text, cpos, 100, matcher)
                    if sc >= 55:
                        candidates.append({"char_pos": cpos, "reason": "name_pinyin", "score": sc})
                        seen_pos.add(cpos)
                    break

    # Tier 2: 背號比對 (基礎分 90)
    for jv in matcher["jersey_variants"]:
        for m in re.finditer(rf"(?:背號\s*{re.escape(jv)}|{re.escape(jv)}\s*號)", text):
            cpos = m.start()
            sc = _apply_context_score(text, cpos, 90, matcher)
            if sc >= 55 and cpos not in seen_pos:
                candidates.append({"char_pos": cpos, "reason": "jersey", "score": sc})
                seen_pos.add(cpos)

    # Tier 3: 模糊拼音比對 (基礎分 85) - 如「高硬仔」(za+i) 對齊「高〇展」(zan)
    if first_py and last_prefix2 and len(cjk_indices) >= 3:
        for i in range(len(char_pys) - 2):
            if char_pys[i] != first_py:
                continue
            cpos = cjk_indices[i]
            if cpos in seen_pos:
                continue
            py_last = char_pys[i + 2]
            prefix_match = (len(py_last) >= 2 and py_last[:2] == last_prefix2) or (
                len(last_py) == 1 and last_initial and _pinyin_initial(py_last) == last_initial
            )
            if not prefix_match:
                continue
            ctx14 = text[max(0, cpos - 14) : cpos]
            has_bat_ctx = any(kw in ctx14 for kw in _FUZZY_CTX_CUES) or any(ov in ctx14 for ov in order_vars)
            if not has_bat_ctx:
                continue
            sc = _apply_context_score(text, cpos, 85, matcher)
            if sc >= 55:
                candidates.append({"char_pos": cpos, "reason": "name_fuzzy", "score": sc})
                seen_pos.add(cpos)

    # Tier 4: 明確棒次 + 後 6 字內姓氏拼音 (基礎分 80)
    if first_py and order_vars and cjk_indices:
        for ov in order_vars:
            for m in re.finditer(re.escape(ov), text):
                ov_end = m.end()
                for cidx, py in zip(cjk_indices, char_pys):
                    if ov_end <= cidx <= ov_end + 8 and py == first_py:
                        if cidx not in seen_pos:
                            sc = _apply_context_score(text, cidx, 80, matcher)
                            if sc >= 55:
                                candidates.append({"char_pos": cidx, "reason": "order_surname", "score": sc})
                                seen_pos.add(cidx)
                        break

    candidates.sort(key=lambda x: (-x["score"], x["char_pos"]))
    return candidates


def _match_text_to_player(text: str, matcher: dict[str, Any]) -> Optional[str]:
    """檢查轉錄片段文字是否命中目標球員（背號或同音姓名），命中則回傳最高分原因標籤。"""
    cands = _find_player_matches_in_text(text, matcher)
    return cands[0]["reason"] if cands else None


def _resolve_match_timestamp(seg: dict[str, Any], char_pos: int) -> float:
    """利用 faster-whisper word_timestamps 定位字元索引對應的精確發聲秒數，無字詞時退回線性內插。"""
    text = seg.get("text", "")
    words = seg.get("words") or []
    if words and text:
        non_ws_idx = len(re.sub(r"\s+", "", text[: max(0, char_pos)]))
        cum = 0
        last_w_start = float(seg["start"])
        for w in words:
            if not isinstance(w, (list, tuple)) or len(w) < 3:
                continue
            w_start = float(w[0])
            wt_clean = re.sub(r"\s+", "", str(w[2]))
            last_w_start = w_start
            if cum + len(wt_clean) > non_ws_idx:
                return w_start
            cum += len(wt_clean)
        return last_w_start

    s0 = float(seg["start"])
    s1 = float(seg["end"])
    ratio = min(1.0, max(0.0, char_pos / max(1, len(text))))
    return round(s0 + (s1 - s0) * ratio, 2)


def mask_matched_names_in_text(text: str, matchers: list[dict[str, Any]]) -> str:
    """將逐字稿段落中可能出現的選手姓名中間字遮罩為 '〇'（含完全同音與模糊同音三字名）。"""
    chars = list(text)
    cjk_indices = [idx for idx, ch in enumerate(chars) if "\u4e00" <= ch <= "\u9fff"]
    if len(cjk_indices) < 3:
        return text

    cjk_pys = [_normalize_pinyin_token(lazy_pinyin(chars[idx], style=Style.NORMAL)[0]) for idx in cjk_indices]
    for m in matchers:
        f_py, l_py = m["first_py"], m["last_py"]
        l_pref2 = m.get("last_prefix2", "")
        if not f_py or not l_py:
            continue
        for i in range(len(cjk_pys) - 2):
            if cjk_pys[i] != f_py:
                continue
            py_last = cjk_pys[i + 2]
            if py_last == l_py or (l_pref2 and len(py_last) >= 2 and py_last[:2] == l_pref2):
                chars[cjk_indices[i + 1]] = "〇"
    return "".join(chars)


def _merge_windows(windows: list[tuple[int, int]], max_dur: Optional[int] = None, gap_sec: int = 20) -> list[tuple[int, int]]:
    """合併重疊或相近的時間視窗。"""
    if not windows:
        return []
    clipped: list[tuple[int, int]] = []
    for s, e in windows:
        s_c = max(0, int(s))
        e_c = int(e)
        if max_dur and max_dur > 0:
            e_c = min(max_dur, e_c)
        if e_c > s_c + 5:
            clipped.append((s_c, e_c))
    if not clipped:
        return []
    clipped.sort()
    merged: list[tuple[int, int]] = [clipped[0]]
    for s, e in clipped[1:]:
        ps, pe = merged[-1]
        if s <= pe + gap_sec:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _window_covered(win: tuple[int, int], covered: list[tuple[int, int]]) -> bool:
    """檢查目標視窗是否已被既有轉錄快取覆蓋。"""
    ws, we = win
    for cs, ce in covered:
        if cs <= ws + 5 and ce >= we - 5:
            return True
    return False


def download_temp_audio(video_id: str, dest: Path) -> Path:
    """使用 yt-dlp 下載純音軌暫存檔。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    # 移除固定副檔名，讓 yt-dlp 保留原始容器 (.m4a / .webm) 以利 ffmpeg 直接讀取
    stem_out = dest.with_suffix("")
    for cand in dest.parent.glob(f"{stem_out.name}.*"):
        return cand

    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-f",
        "bestaudio[ext=m4a]/bestaudio/best",
        "--concurrent-fragments",
        "8",
        "--no-part",
        "--quiet",
        "-o",
        str(stem_out) + ".%(ext)s",
        url,
    ]
    subprocess.run(cmd, check=True)
    matches = list(dest.parent.glob(f"{stem_out.name}.*"))
    if not matches:
        raise RuntimeError(f"找不到下載完成之音軌檔案: {video_id}")
    return matches[0]


def _get_whisper_model(model_size: str = "base"):
    """延遲載入本機 faster-whisper 模型。"""
    from faster_whisper import WhisperModel

    threads = min(8, os.cpu_count() or 4)
    print(f"  [*] 載入本機 faster-whisper 模型 ({model_size}, int8, threads={threads})...", flush=True)
    return WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=threads)


def _read_wav_float32(wav_path: Path) -> np.ndarray:
    """使用標準庫 wave 讀取 16kHz PCM WAV 為 float32 ndarray（繞過 av 19+ metadata_errors 相容問題）。"""
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    audio_i16 = np.frombuffer(frames, dtype=np.int16)
    return audio_i16.astype(np.float32) / 32768.0


def _split_long_windows(
    windows: list[tuple[int, int]],
    max_chunk_sec: int = 300,
    overlap_sec: int = 2,
) -> list[tuple[int, int]]:
    """將超過 max_chunk_sec 的大視窗切分為 <= 300 秒的子視窗，避免 Whisper 長音段退化或重複迴圈。"""
    chunks: list[tuple[int, int]] = []
    for ws, we in windows:
        if we - ws <= max_chunk_sec:
            chunks.append((ws, we))
            continue
        cur = ws
        while cur < we:
            nxt = min(we, cur + max_chunk_sec)
            chunks.append((cur, nxt))
            if nxt >= we:
                break
            cur = max(cur + 1, nxt - overlap_sec)
    return chunks


def _transcribe_audio_windows(
    model: Any,
    audio_path: Path,
    video_id: str,
    windows: list[tuple[int, int]],
    initial_prompt: str,
    tmp_dir: Path,
) -> list[dict[str, Any]]:
    """對指定時間視窗切片成 16kHz mono WAV 並執行 faster-whisper 轉錄（含 word_timestamps）。"""
    sub_windows = _split_long_windows(windows, max_chunk_sec=300, overlap_sec=2)
    segments_out: list[dict[str, Any]] = []
    for w_start, w_end in sub_windows:
        w_dur = w_end - w_start
        if w_dur <= 5:
            continue
        wav_path = tmp_dir / f"{video_id}_win_{w_start}_{w_end}.wav"
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(w_start),
                "-t",
                str(w_dur),
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                str(wav_path),
            ]
            subprocess.run(cmd, check=True)

            audio_arr = _read_wav_float32(wav_path)
            if audio_arr.size < 1600:
                continue

            segs, _ = model.transcribe(
                audio_arr,
                language="zh",
                beam_size=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500, "threshold": 0.35},
                word_timestamps=True,
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                repetition_penalty=1.15,
                no_repeat_ngram_size=3,
                initial_prompt=initial_prompt,
            )
            for s in segs:
                txt = s.text.strip()
                if not txt:
                    continue
                words_compact: list[list[Any]] = []
                if getattr(s, "words", None):
                    for w in s.words:
                        wt = (w.word or "").strip()
                        if wt:
                            words_compact.append(
                                [
                                    round(w_start + float(w.start), 2),
                                    round(w_start + float(w.end), 2),
                                    wt,
                                ]
                            )
                segments_out.append(
                    {
                        "start": round(w_start + float(s.start), 2),
                        "end": round(w_start + float(s.end), 2),
                        "text": txt,
                        "words": words_compact,
                    }
                )
        finally:
            if wav_path.exists():
                wav_path.unlink()
    return segments_out


def _extract_opponent_batter_from_fielding(detail: str) -> Optional[str]:
    """從守備文字開頭萃取對手打者遮罩姓名（如 '王〇彬擊出球...' -> '王〇彬'）。"""
    m = re.match(
        r"^([^\s，,。：:（(]{2,5}?)(?:沒有出棒|揮棒|打擊|擊出|打到|打成|觸擊|犧牲|遭到|獲得)",
        detail.strip(),
    )
    return m.group(1) if m else None


def _build_half_window_map(
    conn: sqlite3.Connection,
    video_ids: set[str],
) -> dict[tuple[str, str], tuple[int, int]]:
    """從 video_scorebug_segments 推算每個半局的延伸轉錄與搜尋視窗（修補中斷缺口與過短半局）。"""
    window_map: dict[tuple[str, str], tuple[int, int]] = {}
    for vid in video_ids:
        rows = conn.execute(
            """
            SELECT inning, half_side, start_sec, end_sec, is_interpolated
            FROM video_scorebug_segments
            WHERE video_id = ?
            ORDER BY inning ASC, CASE WHEN half_side = 'TOP' THEN 0 ELSE 1 END ASC
            """,
            (vid,),
        ).fetchall()
        for idx, r in enumerate(rows):
            h_start = int(r["start_sec"])
            h_end = int(r["end_sec"])
            eff_start = max(0, h_start - 25)
            eff_end = h_end + 35

            next_r = rows[idx + 1] if idx + 1 < len(rows) else None
            if next_r is not None:
                next_start = int(next_r["start_sec"])
                if next_start - h_end > 40:
                    eff_end = min(next_start + 15, h_end + 650)
                if (h_end - h_start) < 60 and bool(next_r["is_interpolated"]):
                    target_sec = int(rows[-1]["end_sec"])
                    for sub_r in rows[idx + 1 :]:
                        if not bool(sub_r["is_interpolated"]):
                            target_sec = int(sub_r["start_sec"])
                            break
                    eff_end = max(eff_end, target_sec + 15)

            hlabel = f"{r['inning']}局{'上' if r['half_side'] == 'TOP' else '下'}"
            window_map[(vid, hlabel)] = (eff_start, eff_end)
    return window_map


def refine_clips_with_asr(
    games: list[dict[str, Any]],
    clips_by_match: dict[int, list[dict[str, Any]]],
    conn: sqlite3.Connection,
    asr_mode: str = "targeted",
    model_size: str = "base",
    pa_pre_roll: Optional[float] = None,
    force_asr: bool = False,
    transcript_dir: Path = DEFAULT_TRANSCRIPT_DIR,
    tmp_dir: Path = TMP_MEDIA_DIR,
) -> dict[str, int]:
    """以本機 faster-whisper 對目標半局轉錄並精修時間戳記（speech_pa 模式優先採語音唱名）。"""
    if asr_mode == "off":
        return {"videos_transcribed": 0, "clips_upgraded": 0}

    transcript_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    effective_pre_roll = (1.0 if asr_mode == "speech_pa" else 5.0) if pa_pre_roll is None else float(pa_pre_roll)

    # 1. 依 video_id 彙整需要轉錄的時間視窗與當場上下文 prompt
    windows_by_vid: dict[str, list[tuple[int, int]]] = {}
    prompt_info_by_vid: dict[str, dict[str, Any]] = {}
    duration_by_vid: dict[str, int] = {}

    for g in games:
        for v in g.get("youtube_videos", []):
            vid = v["video_id"]
            if v.get("duration_sec"):
                duration_by_vid[vid] = int(v["duration_sec"])
            p_info = prompt_info_by_vid.setdefault(vid, {"teams": set(), "jerseys": set()})
            p_info["teams"].add(g["away_team"])
            p_info["teams"].add(g["home_team"])
            for pdata in g["players"].values():
                if pdata.get("jersey_number"):
                    p_info["jerseys"].add(str(pdata["jersey_number"]))

    half_window_map = _build_half_window_map(conn, set(prompt_info_by_vid.keys()))

    for g in games:
        m_clips = clips_by_match.get(int(g["match_id"]), [])
        for c in m_clips:
            vid = c["video_id"]
            if asr_mode == "full":
                dur = duration_by_vid.get(vid, 7200)
                windows_by_vid.setdefault(vid, []).append((0, dur))
            elif asr_mode == "speech_pa":
                if c["clip_type"] == "batting" or c["confidence"] == "low":
                    default_win = (max(0, int(c["half_start_sec"]) - 25), int(c["half_end_sec"]) + 35)
                    hs, he = half_window_map.get((vid, c["half_label"]), default_win)
                    windows_by_vid.setdefault(vid, []).append((hs, he))
            elif asr_mode == "low_medium" and c["confidence"] in ("low", "medium"):
                hs = max(0, int(c["half_start_sec"]) - 15)
                he = int(c["half_end_sec"]) + 15
                windows_by_vid.setdefault(vid, []).append((hs, he))
            elif asr_mode == "targeted" and c["confidence"] == "low":
                hs = max(0, int(c["half_start_sec"]) - 15)
                he = int(c["half_end_sec"]) + 15
                windows_by_vid.setdefault(vid, []).append((hs, he))

    # 2. 檢查快取，只對尚未涵蓋（或 force_asr）的視窗下載純音軌並執行 Whisper
    model: Any = None
    transcripts_by_vid: dict[str, dict[str, Any]] = {}
    transcribed_count = 0

    all_vids = sorted(prompt_info_by_vid.keys())
    for idx, vid in enumerate(all_vids, start=1):
        t_cache = transcript_dir / f"{vid}.json"
        if t_cache.exists():
            cached = json.loads(t_cache.read_text(encoding="utf-8"))
        else:
            cached = {"video_id": vid, "windows": [], "segments": []}

        raw_wins = windows_by_vid.get(vid, [])
        merged_req = _merge_windows(raw_wins, max_dur=duration_by_vid.get(vid), gap_sec=10)
        if force_asr:
            missing_wins = merged_req
        else:
            missing_wins = [w for w in merged_req if not _window_covered(w, [tuple(x) for x in cached.get("windows", [])])]

        if missing_wins:
            if model is None:
                model = _get_whisper_model(model_size)
            p_info = prompt_info_by_vid[vid]
            teams_str = "、".join(sorted(p_info["teams"]))
            jerseys_str = "、".join(sorted(p_info["jerseys"]))
            prompt = (
                f"台灣少棒社區棒球轉播，對戰隊伍：{teams_str}。"
                f"常見背號：{jerseys_str}號。"
                "常見詞彙：一局上、二局下、三振、四壞球保送、一壘安打、二壘安打、游擊手、三壘手、接殺、刺殺、換投。"
            )
            t0 = time.perf_counter()
            print(f"  [ASR {idx}/{len(all_vids)}] {vid} 轉錄 {len(missing_wins)} 個半局視窗...", flush=True)
            audio_tmp = tmp_dir / f"{vid}.audio"
            actual_audio: Optional[Path] = None
            try:
                actual_audio = download_temp_audio(vid, audio_tmp)
                new_segs = _transcribe_audio_windows(model, actual_audio, vid, missing_wins, prompt, tmp_dir)
                old_segs = [
                    s
                    for s in cached.get("segments", [])
                    if not any((ws - 1) <= float(s["start"]) <= (we + 1) for ws, we in missing_wins)
                ]
                old_segs.extend(new_segs)
                old_segs.sort(key=lambda s: (s["start"], s["end"]))
                cached["segments"] = old_segs
                cached["windows"] = [list(x) for x in _merge_windows([tuple(x) for x in cached.get("windows", [])] + missing_wins)]
                cached["model"] = model_size
                cached["word_timestamps"] = True
                t_cache.write_text(json.dumps(cached, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                transcribed_count += 1
                print(f"    -> 完成轉錄 {len(new_segs)} 段語音 (耗時 {time.perf_counter() - t0:.1f}s)", flush=True)
            except Exception as exc:
                print(f"    -> [WARN] {vid} ASR 轉錄略過 ({exc})", flush=True)
            finally:
                if actual_audio and actual_audio.exists():
                    actual_audio.unlink()
                for leftover in tmp_dir.glob(f"{vid}.*"):
                    if leftover.suffix != ".mp4":
                        leftover.unlink(missing_ok=True)
        transcripts_by_vid[vid] = cached

    # 3. 以轉錄逐字稿對各場目標打席／投球／守備進行語音唱名定位，並遮罩入庫
    clips_upgraded = 0
    stored_vids: set[str] = set()

    for g in games:
        match_id = int(g["match_id"])
        m_clips = clips_by_match.get(match_id, [])
        if not m_clips:
            continue

        order_by_uid = {
            str(r["player_uid"]): str(r["batting_order"])
            for r in conn.execute(
                """
                SELECT player_uid, batting_order
                FROM match_lineups
                WHERE match_id = ? AND player_uid IS NOT NULL AND batting_order != '投手'
                """,
                (match_id,),
            ).fetchall()
        }

        matchers_by_uid = {
            uid: build_player_matcher(
                uid,
                pdata["masked_name"],
                pdata.get("jersey_number"),
                batting_order=order_by_uid.get(uid),
            )
            for uid, pdata in g["players"].items()
        }
        all_matchers = list(matchers_by_uid.values())

        # 按 (video_id, half_label) 分組打席/投球事件，並依 event_index 保持時間單調遞增
        by_vid_half: dict[tuple[str, str], list[dict[str, Any]]] = {}
        fielding_by_vid_half: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for c in m_clips:
            if c["clip_type"] in ("batting", "pitching"):
                by_vid_half.setdefault((c["video_id"], c["half_label"]), []).append(c)
            elif c["clip_type"] == "fielding":
                fielding_by_vid_half.setdefault((c["video_id"], c["half_label"]), []).append(c)

        matched_segments_for_db: list[dict[str, Any]] = []

        for (vid, hlabel), group in by_vid_half.items():
            segs = transcripts_by_vid.get(vid, {}).get("segments", [])
            if not segs:
                continue
            group.sort(key=lambda x: x["event_index"])
            h_start = int(group[0]["half_start_sec"])
            h_end = int(group[0]["half_end_sec"])
            if asr_mode == "speech_pa":
                eff_start, eff_end = half_window_map.get((vid, hlabel), (max(0, h_start - 25), h_end + 35))
            else:
                eff_start, eff_end = (max(0, h_start - 15), h_end + 15)

            win_segs = [s for s in segs if eff_start <= float(s["start"]) <= eff_end]
            if not win_segs:
                continue

            min_allowed_sec = float(eff_start - 5)
            for idx_c, c in enumerate(group):
                is_speech_pa_batting = asr_mode == "speech_pa" and c["clip_type"] == "batting"
                if not is_speech_pa_batting and c["confidence"] == "high":
                    min_allowed_sec = max(min_allowed_sec, float(c["start_sec"]) + 8.0)
                    continue

                matcher = matchers_by_uid.get(c["player_uid"])
                if not matcher:
                    continue

                if is_speech_pa_batting:
                    max_allowed_sec = float(eff_end + 15)
                else:
                    max_allowed_sec = float(h_end + 15)
                    for nxt in group[idx_c + 1 :]:
                        if nxt["confidence"] == "high":
                            max_allowed_sec = min(max_allowed_sec, float(nxt["start_sec"]) - 4.0)
                            break

                candidates: list[dict[str, Any]] = []
                for s in win_segs:
                    if float(s["end"]) < min_allowed_sec or float(s["start"]) > max_allowed_sec:
                        continue
                    matches = _find_player_matches_in_text(s["text"], matcher)
                    for m in matches:
                        exact_sec = _resolve_match_timestamp(s, m["char_pos"])
                        if min_allowed_sec <= exact_sec <= max_allowed_sec:
                            candidates.append(
                                {
                                    "exact_sec": exact_sec,
                                    "score": m["score"],
                                    "seg": s,
                                }
                            )

                if candidates:
                    best = min(candidates, key=lambda x: (-x["score"], x["exact_sec"]))
                    exact_sec = float(best["exact_sec"])
                    hit_seg = best["seg"]
                    new_start = max(0, int(round(exact_sec - effective_pre_roll)))
                    if c["confidence"] != "high" or c["align_method"] != "dual_track":
                        clips_upgraded += 1
                    c["start_sec"] = new_start
                    c["align_method"] = "dual_track"
                    c["confidence"] = "high"
                    min_allowed_sec = exact_sec + 6.0

                    matched_segments_for_db.append(
                        {
                            "video_id": vid,
                            "start_sec": round(exact_sec, 2),
                            "end_sec": hit_seg["end"],
                            "text_masked": mask_matched_names_in_text(hit_seg["text"], all_matchers),
                            "matched_uid": c["player_uid"],
                        }
                    )

        # 對低／中信心度守備事件，以對方打者遮罩姓名同音比對主播唱名精修
        opp_matcher_cache: dict[str, dict[str, Any]] = {}
        for (vid, hlabel), f_group in fielding_by_vid_half.items():
            segs = transcripts_by_vid.get(vid, {}).get("segments", [])
            if not segs:
                continue
            f_group.sort(key=lambda x: x["event_index"])
            h_start = int(f_group[0]["half_start_sec"])
            h_end = int(f_group[0]["half_end_sec"])
            win_segs = [s for s in segs if (h_start - 15) <= s["start"] <= (h_end + 15)]
            if not win_segs:
                continue

            for c in f_group:
                if c["confidence"] == "high":
                    continue
                opp_name = _extract_opponent_batter_from_fielding(c["event_detail"])
                if not opp_name:
                    continue
                opp_m = opp_matcher_cache.get(opp_name)
                if opp_m is None:
                    opp_m = build_player_matcher(opp_name, opp_name, None)
                    opp_matcher_cache[opp_name] = opp_m

                cur_est = int(c["start_sec"])
                search_lo = max(h_start - 15, cur_est - 90) if c["align_method"] == "bracket_interp" else max(h_start - 15, cur_est - 35)
                search_hi = min(h_end + 15, cur_est + 90) if c["align_method"] == "bracket_interp" else min(h_end + 15, cur_est + 45)
                hit_seg = None
                for s in win_segs:
                    if s["start"] < search_lo or s["start"] > search_hi:
                        continue
                    if _match_text_to_player(s["text"], opp_m) is not None:
                        hit_seg = s
                        break
                if hit_seg is not None:
                    # 守備處理通常發生於唱名打者之後數秒，以打者唱名點起算即可完整看到該打席守備
                    c["start_sec"] = max(0, int(round(hit_seg["start"])) - 3)
                    c["align_method"] = "dual_track"
                    c["confidence"] = "high"
                    clips_upgraded += 1

        # 將命中的逐字稿遮罩分段寫入 SQLite（每次僅覆蓋相關影片）
        if matched_segments_for_db:
            with conn:
                for ms in matched_segments_for_db:
                    vid_m = ms["video_id"]
                    if vid_m not in stored_vids:
                        conn.execute("DELETE FROM video_transcript_segments WHERE video_id = ?", (vid_m,))
                        stored_vids.add(vid_m)
                    conn.execute(
                        """
                        INSERT INTO video_transcript_segments (
                            video_id, start_sec, end_sec, text_masked, matched_uid
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            ms["video_id"],
                            ms["start_sec"],
                            ms["end_sec"],
                            ms["text_masked"],
                            ms["matched_uid"],
                        ),
                    )

    return {
        "videos_transcribed": transcribed_count,
        "clips_upgraded": clips_upgraded,
    }
