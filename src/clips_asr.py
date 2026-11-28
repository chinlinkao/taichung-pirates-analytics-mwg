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
RESULT_PRE_ROLL_SEC = 18.0

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
_PITCH_CHANGE_CUES = (
    "換投",
    "更換投手",
    "投手換上",
    "登板",
    "接替投球",
    "中繼",
    "後援",
    "上來投",
    "主投",
    "先發投手",
)
_EXPLICIT_PITCH_CHANGE_CUES = ("換投", "更換投手", "投手換上", "接替投球", "更換了投手")
_POS_BROADCAST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "投": ("投手",),
    "捕": ("捕手", "本壘"),
    "一": ("一壘手", "一壘"),
    "二": ("二壘手", "二壘"),
    "三": ("三壘手", "三壘"),
    "游": ("游擊手", "游擊"),
    "左": ("左外野", "左外野手", "左半邊"),
    "中": ("中外野", "中外野手", "中堅手"),
    "右": ("右外野", "右外野手", "右半邊"),
}
_NON_DEFENSE_BASE_RE = re.compile(
    r"(?:[一二三]壘安打|[一二三]壘有人|上[一二三]壘|[一二三]壘跑者|盜上[二三]壘|攻佔[一二三]壘|滿壘)"
)
_CN_INNING_MAP = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
}
_HALF_CUE_RE = re.compile(r"(?:第)?([一二三四五六七1-7])\s*局\s*(?:的)?\s*([上下])(?:半)?")
_HALF_END_CUES = (
    "結束",
    "三人出局",
    "三個出局",
    "三出局",
    "殘壘",
    "攻勢結束",
    "進攻結束",
    "無功而返",
)
_HALF_START_CUES = (
    "來到",
    "進入",
    "開始",
    "回到",
    "輪到",
    "進攻",
    "先攻",
    "首先",
    "首位",
    "首名",
    "第一棒",
)
_HALF_NON_TRANSITION_BEFORE = (
    "同一",
    "前一場",
    "上一場",
    "前場",
    "前一個",
    "前在",
    "剛剛",
    "剛才",
    "剛在",
    "稍早",
    "上次",
    "待會",
    "等一下",
    "先前",
    "前面",
    "上個",
)
_LINEUP_ORDER_RE = re.compile(r"(?:第?\s*([一二三四五六七八九壹貳參肆伍陸柒捌玖1-9])\s*棒|(開路先鋒|首棒))")
_LINEUP_ORDER_MAP = {
    "一": 1,
    "壹": 1,
    "1": 1,
    "二": 2,
    "貳": 2,
    "2": 2,
    "三": 3,
    "參": 3,
    "3": 3,
    "四": 4,
    "肆": 4,
    "4": 4,
    "五": 5,
    "伍": 5,
    "5": 5,
    "六": 6,
    "陸": 6,
    "6": 6,
    "七": 7,
    "柒": 7,
    "7": 7,
    "八": 8,
    "捌": 8,
    "8": 8,
    "九": 9,
    "玖": 9,
    "9": 9,
}
_DEFENSE_INTRO_POS_RE = re.compile(r"(?:先發投手|捕手|一壘手?|二壘手?|三壘手?|游擊手?|左外野|中外野|右外野)")
_PROMPT_VOCAB_TOKENS = (
    "一局上",
    "一局下",
    "二局上",
    "二局下",
    "三局上",
    "三局下",
    "三振",
    "四壞",
    "保送",
    "安打",
    "游擊手",
    "三壘手",
    "四壘手",
    "四壽手",
    "接殺",
    "刺殺",
    "換投",
)
_REAL_ACTION_VERBS = (
    "來到",
    "進入",
    "結束",
    "打到",
    "擊出",
    "揮棒",
    "遭到",
    "獲得",
    "輪到",
    "現在",
)


def _is_prompt_hallucination(text: str) -> bool:
    """偵測並過濾 Whisper 靜音段重複 initial_prompt 的詞表幻覺雜訊。"""
    if not text:
        return True
    if any(k in text for k in ("常見詞彙", "常見背號", "台灣少棒社區棒球轉播")):
        return True
    has_action = any(v in text for v in _REAL_ACTION_VERBS)
    if not has_action:
        if "四壘安打" in text or "四壘手" in text:
            return True
        if len(re.findall(r"(?:^|[、，,：:\s])\d{1,2}(?=[、，,：:\s號。]|$)", text)) >= 5:
            return True
        if len(_HALF_CUE_RE.findall(text)) >= 2:
            return True
    parts = [p.strip() for p in re.split(r"[、，,：:]+", text) if p.strip()]
    if len(parts) >= 3:
        vocab_hits = sum(1 for p in parts if any(tok in p for tok in _PROMPT_VOCAB_TOKENS))
        if vocab_hits >= 3 and not has_action:
            return True
    return False


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
        pat = rf"(?:背號(?:是|為)?\s*{re.escape(jv)}|{re.escape(jv)}\s*號|身穿\s*{re.escape(jv)})"
        for m in re.finditer(pat, text):
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

    # Tier 5: 明確棒次唱名 + 打擊正語境 (基礎分 68)
    if order_vars:
        for ov in order_vars:
            for m in re.finditer(re.escape(ov), text):
                cpos = m.start()
                if any(abs(cpos - sp) <= 4 for sp in seen_pos):
                    continue
                ctx_around = text[max(0, cpos - 10) : min(len(text), m.end() + 10)]
                ctx_before = text[max(0, cpos - 8) : cpos]
                has_pos_cue = any(kw in ctx_around for kw in ("輪到", "打擊", "交給", "面對", "打者", "上場", "代打"))
                has_neg_cue = any(kw in ctx_before for kw in _PA_NEGATIVE_CUES)
                if has_pos_cue and not has_neg_cue:
                    candidates.append({"char_pos": cpos, "reason": "order_only", "score": 68})
                    seen_pos.add(cpos)

    candidates.sort(key=lambda x: (-x["score"], x["char_pos"]))
    return candidates


def _has_defensive_position_mention(text: str, pos: str) -> bool:
    """檢查文本是否出現指定守備位置詞（先剔除安打／跑壘等非守備用語避免誤判）。"""
    kws = _POS_BROADCAST_KEYWORDS.get(pos, ())
    if not kws:
        return False
    cleaned = _NON_DEFENSE_BASE_RE.sub(" ", text)
    return any(kw in cleaned for kw in kws)


def _extract_pos_codes_from_detail(text: str) -> list[str]:
    """從文字敘述萃取守備位置簡碼（如 '游擊手' -> '游'）。"""
    pos_map = (
        ("投", ("投手",)),
        ("捕", ("捕手",)),
        ("一", ("一壘手",)),
        ("二", ("二壘手",)),
        ("三", ("三壘手",)),
        ("游", ("游擊手",)),
        ("左", ("左外野手",)),
        ("中", ("中外野手",)),
        ("右", ("右外野手",)),
    )
    return [code for code, kws in pos_map if any(kw in text for kw in kws)]


def _find_pa_result_matches(text: str, label_short: str, event_detail: str) -> Optional[dict[str, Any]]:
    """Tier 6：依打席結果標籤與明細，於語音逐字稿比對明確播報結果詞（基礎分 62）。"""
    lbl = label_short or ""
    detail = event_detail or ""

    if "全壘打" in lbl:
        if any(kw in text for kw in ("全壘打", "紅不讓", "過大牆", "炸裂")):
            return {"reason": "result_hr", "score": 68}
        return None
    if "三壘安打" in lbl:
        if "三壘安打" in text:
            return {"reason": "result_3b", "score": 66}
        return None
    if "二壘安打" in lbl:
        if "二壘安打" in text:
            return {"reason": "result_2b", "score": 65}
        return None
    if "一壘安打" in lbl or "內野安打" in lbl:
        if any(kw in text for kw in ("一壘安打", "內野安打", "落地形成安打", "穿越安打", "敲出安打", "擊出安打")):
            return {"reason": "result_1b", "score": 62}
        return None
    if "觸身球" in lbl:
        if any(kw in text for kw in ("觸身球", "觸身", "丟到", "砸到")):
            return {"reason": "result_hbp", "score": 65}
        return None
    if "保送" in lbl:
        if any(kw in text for kw in ("四壞球", "四壞保送", "獲得保送", "選到保送", "保送上壘")):
            return {"reason": "result_bb", "score": 63}
        return None
    if "雙殺" in lbl:
        if "雙殺" in text:
            return {"reason": "result_dp", "score": 66}
        return None
    if "三振" in lbl:
        if any(kw in text for kw in ("三振", "揮棒落空", "拉弓", "不死三振", "K掉")):
            return {"reason": "result_so", "score": 62}
        return None

    pos_codes = _extract_pos_codes_from_detail(detail)
    if "接殺" in detail or "飛球" in lbl:
        if any(kw in text for kw in ("接殺", "飛球")):
            if pos_codes and any(_has_defensive_position_mention(text, p) for p in pos_codes):
                return {"reason": "result_fly_pos", "score": 64}
            if not pos_codes:
                return {"reason": "result_fly", "score": 60}
        return None
    if "刺殺" in detail or "滾地" in lbl or "野手選擇" in lbl:
        if any(
            kw in text
            for kw in (
                "傳一壘",
                "傳向一壘",
                "往一壘傳",
                "傳給一壘",
                "傳向伊雷",
                "刺殺",
                "封殺",
                "滾地球",
                "攔住",
            )
        ):
            if pos_codes and any(_has_defensive_position_mention(text, p) for p in pos_codes):
                return {"reason": "result_gb_pos", "score": 63}
        return None
    if "失誤" in lbl:
        if any(kw in text for kw in ("失誤", "漏接", "暴傳")):
            return {"reason": "result_err", "score": 62}
        return None
    return None


def _extract_half_cues_from_segments(
    transcript_segments: list[dict[str, Any]],
    expected_set: set[tuple[str, int]],
) -> list[dict[str, Any]]:
    """從過濾幻覺後的語音段落萃取半局 START / END / MENTION 候選錨點。"""
    cues: list[dict[str, Any]] = []
    for seg in transcript_segments:
        txt = str(seg.get("text") or "").strip()
        if not txt or _is_prompt_hallucination(txt):
            continue
        t_sec = int(round(float(seg.get("start", 0.0))))
        for m in _HALF_CUE_RE.finditer(txt):
            inn = _CN_INNING_MAP.get(m.group(1))
            if inn is None:
                continue
            side = "TOP" if m.group(2) == "上" else "BOT"
            if (side, inn) not in expected_set:
                continue

            before_ctx = txt[max(0, m.start() - 5) : m.start()]
            if any(bad in before_ctx for bad in _HALF_NON_TRANSITION_BEFORE):
                continue
            after_ctx = txt[m.end() : m.end() + 2]
            after_char = after_ctx[:1]
            if after_char == "來" and "半" not in m.group(0):
                continue
            if after_ctx.startswith(("保者", "跑者")):
                continue

            is_end = any(k in txt for k in _HALF_END_CUES)
            is_start = any(k in txt for k in _HALF_START_CUES)
            if is_end:
                kind = "END"
                score = 105.0
                t_val = int(round(float(seg.get("end", t_sec))))
            elif is_start:
                kind = "START"
                score = 100.0
                t_val = t_sec
            else:
                kind = "MENTION"
                score = 65.0
                t_val = t_sec

            cues.append(
                {
                    "half_side": side,
                    "inning": inn,
                    "kind": kind,
                    "t": max(0, t_val),
                    "score": score,
                }
            )
    return cues


def detect_transcript_halves(
    transcript_segments: list[dict[str, Any]],
    expected_halves: list[tuple[str, int]],
) -> set[int]:
    """統計逐字稿中出現之合法半局索引鍵 (inning * 2 + 0/1)，供無 Scorebug 多段影片切分使用。"""
    expected_set = set(expected_halves)
    cues = _extract_half_cues_from_segments(transcript_segments, expected_set)
    return {c["inning"] * 2 + (0 if c["half_side"] == "TOP" else 1) for c in cues}


def _detect_lineup_intro_span_sec(
    transcript_segments: list[dict[str, Any]],
    lineup_matchers: Optional[list[dict[str, Any]]],
    duration_sec: int,
) -> Optional[tuple[int, int]]:
    """嚴格偵測賽前海盜先發打序連續唱名群集 (起點, 終點)，排除比賽中一般打席誤判。"""
    if not lineup_matchers or len(lineup_matchers) < 3:
        return None
    limit_sec = min(float(duration_sec) * 0.48, 3000.0) if duration_sec > 0 else 2400.0
    events: list[tuple[float, float, set[int], set[str], bool]] = []
    for seg in transcript_segments:
        st = float(seg.get("start", 0.0))
        if st > limit_sec:
            break
        txt = str(seg.get("text") or "").strip()
        if not txt or _is_prompt_hallucination(txt):
            continue
        ords: set[int] = set()
        for m in _LINEUP_ORDER_RE.finditer(txt):
            ords.add(_LINEUP_ORDER_MAP[m.group(1)] if m.group(1) else 1)
        uids: set[str] = set()
        for pm in lineup_matchers:
            cands = _find_player_matches_in_text(txt, pm)
            if cands and cands[0]["reason"] in ("name_pinyin", "jersey", "name_fuzzy") and cands[0]["score"] >= 85:
                uids.add(str(pm["player_uid"]))
        has_def = bool(_DEFENSE_INTRO_POS_RE.search(txt))
        if ords or uids or has_def:
            events.append((st, float(seg.get("end", st)), ords, uids, has_def))

    for i, (t0, _, o0, _, _) in enumerate(events):
        if not o0:
            continue
        ords_win: set[int] = set()
        uids_win: set[str] = set()
        max_end = t0
        for j in range(i, len(events)):
            if events[j][0] - t0 > 35.0:
                break
            if events[j][2]:
                ords_win |= events[j][2]
                max_end = max(max_end, events[j][1])
            uids_win |= events[j][3]
        if len(ords_win) >= 4 and len(ords_win & {1, 2, 3, 4}) >= 2 and len(uids_win) >= 2:
            ext_end = max_end
            for j in range(i, len(events)):
                if events[j][0] >= max_end and events[j][0] <= ext_end + 12.0 and (events[j][4] or events[j][2]):
                    ext_end = max(ext_end, events[j][1])
            return int(round(t0)), int(round(ext_end))
    return None


def _detect_lineup_intro_end_sec(
    transcript_segments: list[dict[str, Any]],
    lineup_matchers: Optional[list[dict[str, Any]]],
    duration_sec: int,
) -> Optional[int]:
    """偵測賽前海盜先發打序連續唱名群集結束秒數，作為 1 局上開賽下界。"""
    span = _detect_lineup_intro_span_sec(transcript_segments, lineup_matchers, duration_sec)
    return span[1] if span is not None else None


def infer_half_segments_from_transcript(
    transcript_segments: list[dict[str, Any]],
    expected_halves: list[tuple[str, int]],
    duration_sec: int,
    pa_counts_by_label: Optional[dict[str, int]] = None,
    lineup_matchers: Optional[list[dict[str, Any]]] = None,
) -> Optional[list[dict[str, Any]]]:
    """當 Scorebug 無法辨識時，從語音逐字稿重建單調遞增且無重疊的半局時間軸。"""
    if not expected_halves:
        return None
    valid_segs = [
        s
        for s in transcript_segments
        if str(s.get("text") or "").strip() and not _is_prompt_hallucination(str(s.get("text") or ""))
    ]
    if len(valid_segs) < 20:
        return None

    if duration_sec <= 0:
        duration_sec = int(round(max(float(s.get("end", 0.0)) for s in valid_segs)))

    expected_set = set(expected_halves)
    raw_cues = _extract_half_cues_from_segments(valid_segs, expected_set)
    if expected_halves[0] == ("TOP", 1) and duration_sec >= 2400:
        half_idx_map = {h: idx for idx, h in enumerate(expected_halves)}
        raw_cues = [
            c
            for c in raw_cues
            if not (
                half_idx_map.get((c["half_side"], c["inning"]), 0) >= 2
                and c["t"] < half_idx_map[(c["half_side"], c["inning"])] * 140
            )
        ]
    if not raw_cues:
        return None

    intro_span = _detect_lineup_intro_span_sec(valid_segs, lineup_matchers, duration_sec)
    lineup_intro_end: Optional[int] = None
    if intro_span is not None:
        intro_start, intro_end = intro_span
        has_earlier_inn1 = any(c["inning"] == 1 and c["t"] < intro_start - 15 for c in raw_cues)
        if not has_earlier_inn1:
            lineup_intro_end = intro_end
    pa_counts = pa_counts_by_label or {}

    # 依半局分組並去重 (15 秒內同類保留最高分)
    cues_by_half: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for c in raw_cues:
        if lineup_intro_end is not None and c["t"] < lineup_intro_end - 10:
            c = {**c, "score": c["score"] - 60.0}
        key = (c["half_side"], c["inning"])
        cues_by_half.setdefault(key, []).append(c)

    options_per_half: list[list[dict[str, Any]]] = []
    for side, inn in expected_halves:
        raw_list = sorted(cues_by_half.get((side, inn), []), key=lambda x: (-x["score"], x["t"]))
        deduped: list[dict[str, Any]] = []
        for rc in raw_list:
            if any(kept["kind"] == rc["kind"] and abs(kept["t"] - rc["t"]) <= 15 for kept in deduped):
                continue
            deduped.append(rc)

        starts = [c for c in deduped if c["kind"] == "START"]
        ends = [c for c in deduped if c["kind"] == "END"]
        mentions = [c for c in deduped if c["kind"] == "MENTION"]

        opts: list[dict[str, Any]] = []
        for sc in starts:
            for ec in ends:
                if 60 <= (ec["t"] - sc["t"]) <= 1800:
                    opts.append(
                        {
                            "kind": "START_END",
                            "t_start": sc["t"],
                            "t_end": ec["t"],
                            "start_ref": sc["t"],
                            "end_ref": ec["t"],
                            "score": sc["score"] + ec["score"] + 40.0,
                        }
                    )
        for sc in starts:
            opts.append(
                {
                    "kind": "START",
                    "t_start": sc["t"],
                    "t_end": None,
                    "start_ref": sc["t"],
                    "end_ref": sc["t"] + 60,
                    "score": sc["score"],
                }
            )
        for ec in ends:
            opts.append(
                {
                    "kind": "END",
                    "t_start": None,
                    "t_end": ec["t"],
                    "start_ref": max(0, ec["t"] - 120),
                    "end_ref": ec["t"],
                    "score": ec["score"],
                }
            )
        for mc in mentions:
            opts.append(
                {
                    "kind": "MENTION",
                    "t_start": None,
                    "t_end": None,
                    "t_mid": mc["t"],
                    "start_ref": mc["t"],
                    "end_ref": mc["t"],
                    "score": mc["score"],
                }
            )
        opts.sort(key=lambda o: (-o["score"], o["start_ref"]))
        options_per_half.append(opts[:12])

    h_cnt = len(expected_halves)
    dp: list[list[float]] = []
    prev_ptr: list[list[Optional[tuple[int, int]]]] = []
    for h_idx in range(h_cnt):
        dp.append([opt["score"] for opt in options_per_half[h_idx]])
        prev_ptr.append([None] * len(options_per_half[h_idx]))

    for h_idx in range(h_cnt):
        for k, opt in enumerate(options_per_half[h_idx]):
            for p_idx in range(h_idx):
                gap = h_idx - p_idx
                for pk, prev_opt in enumerate(options_per_half[p_idx]):
                    if opt["start_ref"] < prev_opt["end_ref"] + 40 * gap:
                        continue
                    if (opt["end_ref"] - prev_opt["start_ref"]) > 2100 * (gap + 1):
                        continue
                    avg_dur = (opt["start_ref"] - prev_opt["start_ref"]) / gap
                    dur_bonus = 15.0 if 180.0 <= avg_dur <= 1100.0 else 0.0
                    cand_tot = dp[p_idx][pk] + opt["score"] + dur_bonus
                    if cand_tot > dp[h_idx][k]:
                        dp[h_idx][k] = cand_tot
                        prev_ptr[h_idx][k] = (p_idx, pk)

    best_end: Optional[tuple[int, int]] = None
    best_total = -1e9
    for h_idx in range(h_cnt):
        for k in range(len(options_per_half[h_idx])):
            if dp[h_idx][k] > best_total:
                best_total = dp[h_idx][k]
                best_end = (h_idx, k)

    if best_end is None:
        return None

    selected: dict[int, dict[str, Any]] = {}
    cur: Optional[tuple[int, int]] = best_end
    while cur is not None:
        hi, ki = cur
        selected[hi] = options_per_half[hi][ki]
        cur = prev_ptr[hi][ki]

    if not selected:
        return None

    def _half_label(h_idx: int) -> str:
        sd, inn = expected_halves[h_idx]
        return f"{inn}局{'上' if sd == 'TOP' else '下'}"

    def _pa_weight(h_idx: int) -> int:
        return max(3, int(pa_counts.get(_half_label(h_idx), 4)))

    fixed_start: list[Optional[int]] = [None] * h_cnt
    fixed_end: list[Optional[int]] = [None] * h_cnt

    for h_idx, opt in selected.items():
        if opt.get("t_start") is not None:
            fixed_start[h_idx] = int(opt["t_start"])
        if opt.get("t_end") is not None:
            fixed_end[h_idx] = int(opt["t_end"])

    # 補齊相鄰已知邊界
    for h_idx in range(h_cnt):
        if fixed_end[h_idx] is not None and fixed_start[h_idx] is None:
            if h_idx == 0:
                if lineup_intro_end is not None and lineup_intro_end < fixed_end[0] - 60:
                    fixed_start[0] = lineup_intro_end
                elif fixed_end[0] > 1200:
                    est_dur = max(600, min(900, _pa_weight(0) * 85))
                    fixed_start[0] = max(60, fixed_end[0] - est_dur)
                else:
                    fixed_start[0] = min(60, max(0, fixed_end[0] - 60))
            elif fixed_end[h_idx - 1] is not None and fixed_end[h_idx - 1] + 25 < fixed_end[h_idx] - 40:
                fixed_start[h_idx] = fixed_end[h_idx - 1] + 25

        if fixed_start[h_idx] is not None and fixed_end[h_idx] is None:
            if (
                h_idx + 1 < h_cnt
                and fixed_start[h_idx + 1] is not None
                and fixed_start[h_idx + 1] - 25 > fixed_start[h_idx] + 40
            ):
                fixed_end[h_idx] = fixed_start[h_idx + 1] - 25

    # 紀錄僅有 MENTION 的內部時間點（不切斷連續邊界，僅確保分割後涵蓋該點）
    mention_mids: dict[int, int] = {}
    for h_idx, opt in selected.items():
        if opt["kind"] == "MENTION" and fixed_start[h_idx] is None and fixed_end[h_idx] is None:
            mention_mids[h_idx] = int(opt["t_mid"])

    # 組裝完整 half_segments，對未定起訖的區段依 PA 權重連續切分（無空白斷層）
    segs_out: list[dict[str, Any]] = [{} for _ in range(h_cnt)]
    idx = 0
    while idx < h_cnt:
        if fixed_start[idx] is not None and fixed_end[idx] is not None:
            s_val = int(fixed_start[idx])
            e_val = max(s_val + 30, int(fixed_end[idx]))
            sd, inn = expected_halves[idx]
            segs_out[idx] = {
                "half_key": inn * 2 + (0 if sd == "TOP" else 1),
                "half_label": _half_label(idx),
                "inning": inn,
                "half_side": sd,
                "start_sec": s_val,
                "end_sec": min(duration_sec, e_val),
                "sample_count": 0,
                "is_interpolated": False,
                "source": "asr_anchor",
            }
            idx += 1
            continue

        # 若 idx 自身已有 fixed_end 且無 fixed_start，只處理到 idx 為止；否則往後找到第一個 fixed_start（止於其前）或 fixed_end（止於其後）
        if fixed_end[idx] is not None:
            j = idx + 1
        else:
            j = idx + 1
            while j < h_cnt:
                if fixed_start[j] is not None:
                    break
                if fixed_end[j] is not None:
                    j += 1
                    break
                j += 1

        if fixed_start[idx] is not None:
            t_lo = int(fixed_start[idx])
        elif idx > 0:
            t_lo = int(segs_out[idx - 1]["end_sec"]) + 20
        else:
            if j <= h_cnt and (j - 1) >= idx and fixed_end[j - 1] is not None:
                t_hi_peek = int(fixed_end[j - 1])
            elif j < h_cnt and fixed_start[j] is not None:
                t_hi_peek = int(fixed_start[j]) - 20
            else:
                t_hi_peek = duration_sec - 30
            if lineup_intro_end is not None and lineup_intro_end < t_hi_peek - 40 * (j - idx):
                t_lo = lineup_intro_end
            elif (j - idx) == 1 and t_hi_peek > 1200:
                est_dur = max(600, min(900, _pa_weight(0) * 85))
                if idx in mention_mids:
                    t_lo = max(60, min(mention_mids[idx] - 120, t_hi_peek - est_dur))
                else:
                    t_lo = max(60, t_hi_peek - est_dur)
            else:
                t_lo = min(60, max(0, t_hi_peek - 40 * (j - idx)))

        if j - 1 >= idx and fixed_end[j - 1] is not None:
            t_hi = int(fixed_end[j - 1])
        elif j < h_cnt and fixed_start[j] is not None:
            t_hi = int(fixed_start[j]) - 20
        else:
            t_hi = max(t_lo + 45 * (j - idx), duration_sec - 30)

        t_hi = max(t_lo + 40 * (j - idx), min(duration_sec, t_hi))
        has_both_bounds = (idx > 0 or fixed_start[idx] is not None or lineup_intro_end is not None) and (
            j < h_cnt or fixed_end[j - 1] is not None
        )

        sub_weights = [_pa_weight(k) for k in range(idx, j)]
        cur_t = t_lo
        for offset, k in enumerate(range(idx, j)):
            if k > idx and fixed_start[k] is not None and int(fixed_start[k]) > cur_t + 30:
                cur_t = int(fixed_start[k])
            if k == j - 1:
                seg_e = t_hi
            elif fixed_end[k] is not None and int(fixed_end[k]) > cur_t + 30:
                seg_e = min(t_hi, int(fixed_end[k]))
            elif k + 1 < j and fixed_start[k + 1] is not None and int(fixed_start[k + 1]) - 20 > cur_t + 30:
                seg_e = int(fixed_start[k + 1]) - 20
            else:
                rem_w = max(1, sum(sub_weights[offset:]))
                seg_len = int(round((t_hi - cur_t) * (sub_weights[offset] / rem_w)))
                seg_e = cur_t + seg_len
                if k in mention_mids:
                    seg_e = max(seg_e, mention_mids[k] + 15)
                if (k + 1) in mention_mids and fixed_start[k + 1] is None:
                    seg_e = min(seg_e, mention_mids[k + 1] - 15)
                seg_e = min(t_hi - 35 * (j - 1 - k), max(cur_t + 35, seg_e))

            sd, inn = expected_halves[k]
            is_anchored = (k in selected) or has_both_bounds
            segs_out[k] = {
                "half_key": inn * 2 + (0 if sd == "TOP" else 1),
                "half_label": _half_label(k),
                "inning": inn,
                "half_side": sd,
                "start_sec": cur_t,
                "end_sec": max(cur_t + 30, seg_e),
                "sample_count": 0,
                "is_interpolated": not is_anchored,
                "source": "asr_anchor",
            }
            cur_t = segs_out[k]["end_sec"] + 15
        idx = j

    for h_idx in range(1, h_cnt):
        if segs_out[h_idx]["start_sec"] < segs_out[h_idx - 1]["end_sec"]:
            segs_out[h_idx]["start_sec"] = segs_out[h_idx - 1]["end_sec"]
        if segs_out[h_idx]["end_sec"] <= segs_out[h_idx]["start_sec"] + 20:
            segs_out[h_idx]["end_sec"] = min(duration_sec, segs_out[h_idx]["start_sec"] + 30)

    return segs_out


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
    """從守備文字開頭萃取對手打者或壘上跑者遮罩姓名（如 '王〇彬擊出球...' -> '王〇彬'）。"""
    cleaned = detail.strip()
    m = re.match(
        r"^([^\s，,。：:（(]{2,5}?)(?:沒有出棒|揮棒|打擊|擊出|打到|打成|觸擊|犧牲|遭到|獲得)",
        cleaned,
    )
    if m:
        return m.group(1)
    m_runner = re.match(
        r"^壘上跑者([^\s，,。：:（(]{2,5}?)(?:在|因|試圖|盜|被|遭)",
        cleaned,
    )
    return m_runner.group(1) if m_runner else None


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
    align_only: bool = False,
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

        if missing_wins and not align_only:
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
        g_upgraded, matched_segments_for_db = _refine_clips_for_single_game(
            g=g,
            m_clips=m_clips,
            conn=conn,
            transcripts_by_vid=transcripts_by_vid,
            half_window_map=half_window_map,
            asr_mode=asr_mode,
            effective_pre_roll=effective_pre_roll,
        )
        clips_upgraded += g_upgraded

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


def _solve_batting_dp(
    group: list[dict[str, Any]],
    win_segs: list[dict[str, Any]],
    matchers_by_uid: dict[str, dict[str, Any]],
    all_matchers: list[dict[str, Any]],
    vid: str,
    eff_start: int,
    eff_end: int,
    h_start: int,
    h_end: int,
    asr_mode: str,
    effective_pre_roll: float,
    matched_segments_for_db: list[dict[str, Any]],
) -> int:
    """以半局全局動態規劃（DP）為打席序列找最高總分的單調遞增語音對齊組合。"""
    group.sort(key=lambda x: x["event_index"])
    n_pa = len(group)
    if n_pa == 0:
        return 0

    half_span = max(60.0, float(h_end - h_start))
    cands_per_pa: list[list[dict[str, Any]]] = []

    for idx_c, c in enumerate(group):
        is_speech_pa_batting = asr_mode == "speech_pa"
        if not is_speech_pa_batting and c["confidence"] == "high":
            fixed_t = float(c["start_sec"])
            cands_per_pa.append(
                [
                    {
                        "est_start": fixed_t,
                        "exact_sec": fixed_t,
                        "node_score": 1000.0,
                        "is_fixed": True,
                        "is_result": False,
                        "seg": None,
                    }
                ]
            )
            continue

        matcher = matchers_by_uid.get(c["player_uid"])
        raw_cands: list[dict[str, Any]] = []
        is_interp_clip = c["align_method"] in ("bracket_interp", "asr_half_interp", "scorebug_half_interp")
        orig_t = float(c["start_sec"])
        rem_after = n_pa - 1 - idx_c

        for s in win_segs:
            if _is_prompt_hallucination(str(s.get("text") or "")):
                continue
            s_start = float(s["start"])
            if matcher:
                for m in _find_player_matches_in_text(s["text"], matcher):
                    exact_sec = _resolve_match_timestamp(s, m["char_pos"])
                    if not (eff_start - 5 <= exact_sec <= eff_end + 15):
                        continue
                    est_start = max(0.0, exact_sec - effective_pre_roll)
                    raw_cands.append(
                        {
                            "est_start": est_start,
                            "exact_sec": exact_sec,
                            "raw_score": float(m["score"]),
                            "is_fixed": False,
                            "is_result": False,
                            "seg": s,
                        }
                    )

            res_m = _find_pa_result_matches(s["text"], c.get("label_short", ""), c.get("event_detail", ""))
            if res_m and (eff_start - 5 <= s_start <= eff_end + 15):
                est_start = max(float(max(0, eff_start)), s_start - RESULT_PRE_ROLL_SEC)
                raw_cands.append(
                    {
                        "est_start": est_start,
                        "exact_sec": s_start,
                        "raw_score": float(res_m["score"]),
                        "is_fixed": False,
                        "is_result": True,
                        "seg": s,
                    }
                )

        # 去重：4 秒內僅保留分數較高者
        raw_cands.sort(key=lambda x: (-x["raw_score"], x["est_start"]))
        deduped: list[dict[str, Any]] = []
        for rc in raw_cands:
            if any(abs(rc["est_start"] - kept["est_start"]) < 4.0 for kept in deduped):
                continue
            score = rc["raw_score"]
            if not is_interp_clip and (h_end - h_start) > 30:
                dist = abs(rc["est_start"] - orig_t)
                if dist <= 45.0:
                    score += 8.0
                elif dist <= 90.0:
                    score += 4.0
                elif dist > max(180.0, half_span * 0.65):
                    score -= 15.0
            if rem_after > 0 and rc["est_start"] > (eff_end + 15.0 - rem_after * 8.0):
                score -= 30.0
            if score >= 50.0:
                rc["node_score"] = score
                deduped.append(rc)

        deduped.sort(key=lambda x: (-x["node_score"], x["est_start"]))
        top_cands = sorted(deduped[:12], key=lambda x: (x["est_start"], x["exact_sec"]))
        cands_per_pa.append(top_cands)

    # 動態規劃求解單調遞增最大總分序列
    dp: list[list[float]] = []
    prev_ptr: list[list[Optional[tuple[int, int]]]] = []
    for i in range(n_pa):
        k_len = len(cands_per_pa[i])
        dp.append([cands_per_pa[i][k]["node_score"] for k in range(k_len)])
        prev_ptr.append([None] * k_len)

    for i in range(n_pa):
        for k, ci in enumerate(cands_per_pa[i]):
            for p in range(i):
                gap_idx = i - p
                for pk, cp in enumerate(cands_per_pa[p]):
                    if ci["est_start"] < cp["est_start"] + 8.0 * gap_idx:
                        continue
                    if not ci["is_fixed"] and not cp["is_fixed"]:
                        if ci["exact_sec"] < cp["exact_sec"] + 2.0:
                            continue
                    avg_gap = (ci["est_start"] - cp["est_start"]) / gap_idx
                    gap_bonus = 5.0 if 15.0 <= avg_gap <= 180.0 else 0.0
                    cand_total = dp[p][pk] + ci["node_score"] + gap_bonus
                    if cand_total > dp[i][k]:
                        dp[i][k] = cand_total
                        prev_ptr[i][k] = (p, pk)

    best_end: Optional[tuple[int, int]] = None
    best_total = -1.0
    for i in range(n_pa):
        for k in range(len(cands_per_pa[i])):
            if dp[i][k] > best_total:
                best_total = dp[i][k]
                best_end = (i, k)

    selected: dict[int, dict[str, Any]] = {}
    cur = best_end
    while cur is not None:
        ci_idx, ck_idx = cur
        selected[ci_idx] = cands_per_pa[ci_idx][ck_idx]
        cur = prev_ptr[ci_idx][ck_idx]

    upgraded = 0
    prev_assigned_start = -1
    for i, c in enumerate(group):
        chosen = selected.get(i)
        if chosen is None or chosen["is_fixed"]:
            if c["confidence"] == "high":
                prev_assigned_start = max(prev_assigned_start, int(c["start_sec"]))
            continue

        new_start = max(0, int(round(chosen["est_start"])))
        if chosen["is_result"] and prev_assigned_start >= 0:
            new_start = max(new_start, prev_assigned_start + 6)
        prev_assigned_start = new_start

        if c["confidence"] != "high" or c["align_method"] != "dual_track":
            upgraded += 1
        c["start_sec"] = new_start
        c["align_method"] = "dual_track"
        c["confidence"] = "high"

        hit_seg = chosen["seg"]
        if hit_seg is not None:
            matched_segments_for_db.append(
                {
                    "video_id": vid,
                    "start_sec": round(float(chosen["exact_sec"]), 2),
                    "end_sec": hit_seg["end"],
                    "text_masked": mask_matched_names_in_text(hit_seg["text"], all_matchers),
                    "matched_uid": c["player_uid"],
                }
            )

    # 雙軌夾擠內插：僅對夾在兩筆精準錨點中間的未命中打席升級為 medium (dual_track_bracket)
    anchor_indices = [
        i
        for i, c in enumerate(group)
        if c["align_method"] == "dual_track" or (i in selected and selected[i]["is_fixed"])
    ]
    if len(anchor_indices) >= 2:
        for pos_idx in range(len(anchor_indices) - 1):
            p = anchor_indices[pos_idx]
            q = anchor_indices[pos_idx + 1]
            if q - p <= 1:
                continue
            sp = int(group[p]["start_sec"])
            sq = int(group[q]["start_sec"])
            if sq <= sp + 10:
                continue
            for mid_i in range(p + 1, q):
                mc = group[mid_i]
                if mc["align_method"] == "dual_track" or mc["confidence"] == "high":
                    continue
                interp_sec = max(0, int(round(sp + (sq - sp) * ((mid_i - p) / (q - p)))))
                mc["start_sec"] = interp_sec
                mc["align_method"] = "dual_track_bracket"
                mc["confidence"] = "medium"

    return upgraded


def _refine_pitching_clips(
    pitching_by_vid_half: dict[tuple[str, str], list[dict[str, Any]]],
    transcripts_by_vid: dict[str, dict[str, Any]],
    half_window_map: dict[tuple[str, str], tuple[int, int]],
    matchers_by_uid: dict[str, dict[str, Any]],
    all_matchers: list[dict[str, Any]],
    matched_segments_for_db: list[dict[str, Any]],
) -> int:
    """獨立語音通道：精修低／中信心度投球（先發與中繼換投）時間戳記。"""
    upgraded = 0
    _batting_noise_cues = ("打擊", "打席", "打西", "打者", "棒打", "第一棒", "第二棒", "第三棒", "第四棒", "第五棒", "第六棒", "第七棒", "第八棒", "第九棒")

    for (vid, hlabel), p_group in pitching_by_vid_half.items():
        segs = transcripts_by_vid.get(vid, {}).get("segments", [])
        if not segs:
            continue
        p_group.sort(key=lambda x: x["event_index"])
        h_start = int(p_group[0]["half_start_sec"])
        h_end = int(p_group[0]["half_end_sec"])
        eff_start, eff_end = half_window_map.get((vid, hlabel), (max(0, h_start - 25), h_end + 35))
        win_segs = [s for s in segs if eff_start <= float(s["start"]) <= eff_end]
        if not win_segs:
            continue

        reliever_cnt = sum(1 for pc in p_group if int(pc["event_index"]) > 1)
        min_pitch_sec = float(eff_start - 5)
        half_span = max(60.0, float(h_end - h_start))

        for pc in p_group:
            if pc["confidence"] == "high":
                min_pitch_sec = max(min_pitch_sec, float(pc["start_sec"]) + 8.0)
                continue
            matcher = matchers_by_uid.get(pc["player_uid"])
            is_interp = pc["align_method"] in ("bracket_interp", "asr_half_interp", "asr_half_start", "scorebug_half_interp")
            cur_est = float(pc["start_sec"])
            s_lo, s_hi = min_pitch_sec, float(eff_end + 10)

            candidates: list[dict[str, Any]] = []
            for s in win_segs:
                st = float(s["start"])
                if float(s["end"]) < s_lo or st > s_hi:
                    continue
                txt = s["text"]
                if _is_prompt_hallucination(txt):
                    continue
                has_pitch_cue = any(kw in txt for kw in _PITCH_CHANGE_CUES)
                has_explicit_change = any(kw in txt for kw in _EXPLICIT_PITCH_CHANGE_CUES)

                if matcher:
                    for m in _find_player_matches_in_text(txt, matcher):
                        if m["reason"] == "order_only":
                            continue
                        cpos = int(m["char_pos"])
                        ctx_near = txt[max(0, cpos - 12) : min(len(txt), cpos + 14)]
                        if any(bk in ctx_near for bk in _batting_noise_cues) and not has_pitch_cue:
                            continue
                        ex_sec = _resolve_match_timestamp(s, cpos)
                        if s_lo <= ex_sec <= s_hi:
                            sc = 100.0 if has_pitch_cue else 80.0
                            if not is_interp and half_span > 30:
                                dist = abs(ex_sec - cur_est)
                                if dist <= 60.0:
                                    sc += 8.0
                                elif dist > max(180.0, half_span * 0.65):
                                    sc -= 15.0
                            if sc >= 60.0:
                                candidates.append({"exact_sec": ex_sec, "score": sc, "seg": s})

                if int(pc["event_index"]) > 1 and reliever_cnt == 1 and has_explicit_change:
                    if not is_interp and abs(st - cur_est) <= 60.0 and s_lo <= st <= s_hi:
                        candidates.append({"exact_sec": st, "score": 70.0, "seg": s})

            if candidates:
                best = min(candidates, key=lambda x: (-x["score"], abs(x["exact_sec"] - cur_est)))
                ex_sec = float(best["exact_sec"])
                new_start = max(0, int(round(ex_sec - 5.0)))
                pc["start_sec"] = new_start
                pc["align_method"] = "dual_track"
                pc["confidence"] = "high"
                min_pitch_sec = ex_sec + 8.0
                upgraded += 1

                hit_seg = best["seg"]
                matched_segments_for_db.append(
                    {
                        "video_id": vid,
                        "start_sec": round(ex_sec, 2),
                        "end_sec": hit_seg["end"],
                        "text_masked": mask_matched_names_in_text(hit_seg["text"], all_matchers),
                        "matched_uid": pc["player_uid"],
                    }
                )
    return upgraded


def _extract_fielding_action_keywords(detail: str) -> tuple[str, ...]:
    """依守備事件文字萃取對應的播報動作關鍵字。"""
    if "雙殺" in detail:
        return ("雙殺",)
    if "失誤" in detail:
        return ("失誤", "漏接", "暴傳", "沒接好")
    if "接殺" in detail:
        return ("接殺", "飛球", "美技")
    if "觸殺" in detail or "夾殺" in detail:
        return ("觸殺", "夾殺", "阻殺", "抓到", "出局", "刺殺", "盜壘失敗")
    return ("傳一壘", "傳向一壘", "刺殺", "封殺", "滾地", "接到", "出局")


def _refine_fielding_clips(
    fielding_by_vid_half: dict[tuple[str, str], list[dict[str, Any]]],
    transcripts_by_vid: dict[str, dict[str, Any]],
    half_window_map: dict[tuple[str, str], tuple[int, int]],
    opp_roster: dict[str, dict[str, Optional[str]]],
    all_matchers: list[dict[str, Any]],
    matched_segments_for_db: list[dict[str, Any]],
) -> int:
    """守備雙通道全局 DP 精修：F1（對手打者完整比對）＋ F2（守位詞與守備動作複合比對）。"""
    opp_matcher_cache: dict[str, dict[str, Any]] = {}
    upgraded = 0

    for (vid, hlabel), f_group in fielding_by_vid_half.items():
        segs = transcripts_by_vid.get(vid, {}).get("segments", [])
        if not segs:
            continue
        f_group.sort(key=lambda x: x["event_index"])
        h_start = int(f_group[0]["half_start_sec"])
        h_end = int(f_group[0]["half_end_sec"])
        eff_start, eff_end = half_window_map.get((vid, hlabel), (max(0, h_start - 15), h_end + 15))
        win_segs = [s for s in segs if eff_start <= float(s["start"]) <= eff_end]
        if not win_segs:
            continue

        by_ev_idx: dict[int, list[dict[str, Any]]] = {}
        for c in f_group:
            by_ev_idx.setdefault(int(c["event_index"]), []).append(c)

        ev_list = sorted(by_ev_idx.keys())
        n_ev = len(ev_list)
        half_span = max(60.0, float(h_end - h_start))
        cands_per_ev: list[list[dict[str, Any]]] = []

        for idx_e, ev_idx in enumerate(ev_list):
            ev_clips = by_ev_idx[ev_idx]
            non_high_clips = [c for c in ev_clips if c["confidence"] != "high"]
            if not non_high_clips:
                fixed_t = float(ev_clips[0]["start_sec"])
                cands_per_ev.append(
                    [
                        {
                            "start_sec": fixed_t,
                            "exact_sec": fixed_t,
                            "node_score": 1000.0,
                            "is_fixed": True,
                            "seg": None,
                        }
                    ]
                )
                continue

            rep = non_high_clips[0]
            cur_est = float(rep["start_sec"])
            is_interp = rep["align_method"] in ("bracket_interp", "asr_half_interp", "scorebug_half_interp")
            rem_after = n_ev - 1 - idx_e
            if is_interp:
                search_lo = max(float(eff_start), cur_est - 120.0) if (h_end - h_start) > 180 else float(eff_start)
                search_hi = float(eff_end)
            else:
                search_lo = max(float(eff_start), cur_est - 50.0)
                search_hi = min(float(eff_end), cur_est + 60.0)

            raw_cands: list[dict[str, Any]] = []
            is_runner_fielding = rep["event_detail"].strip().startswith("壘上跑者")

            # 通道 F1：對手打者／跑者遮罩姓名同音＋背號＋先發棒次
            opp_name = _extract_opponent_batter_from_fielding(rep["event_detail"])
            if opp_name:
                opp_m = opp_matcher_cache.get(opp_name)
                if opp_m is None:
                    r_info = opp_roster.get(opp_name, {})
                    opp_m = build_player_matcher(
                        opp_name,
                        opp_name,
                        r_info.get("jersey_number"),
                        batting_order=r_info.get("batting_order"),
                    )
                    opp_matcher_cache[opp_name] = opp_m

                for s in win_segs:
                    st = float(s["start"])
                    if st < search_lo or st > search_hi:
                        continue
                    txt_s = str(s.get("text") or "")
                    if _is_prompt_hallucination(txt_s):
                        continue
                    for ms in _find_player_matches_in_text(s["text"], opp_m):
                        if is_runner_fielding:
                            if ms["reason"] == "order_only":
                                continue
                            has_bat_intro = any(kw in txt_s for kw in ("輪到", "打擊", "棒"))
                            has_play_cue = any(
                                kw in txt_s
                                for kw in ("跑者", "跑壘", "本壘", "盜壘", "盜", "觸殺", "夾殺", "阻殺", "出局", "牽制", "抓到")
                            )
                            if has_bat_intro and not has_play_cue:
                                continue
                        ex_sec = _resolve_match_timestamp(s, ms["char_pos"])
                        est_s = max(0.0, ex_sec - 3.0)
                        raw_cands.append(
                            {
                                "start_sec": est_s,
                                "exact_sec": ex_sec,
                                "raw_score": 95.0,
                                "is_fixed": False,
                                "seg": s,
                            }
                        )

            # 通道 F2：守位詞＋對應守備動作詞在同段或相鄰 8 秒內共現
            pos_codes = set(_extract_pos_codes_from_detail(rep["event_detail"]))
            for c in ev_clips:
                m_pos = re.match(r"^\[([^\]]+)\]", c.get("label_short", ""))
                if m_pos:
                    pos_codes.add(m_pos.group(1))
            action_kws = _extract_fielding_action_keywords(rep["event_detail"])

            if pos_codes:
                for idx_s, s in enumerate(win_segs):
                    st = float(s["start"])
                    if st < search_lo or st > search_hi:
                        continue
                    if _is_prompt_hallucination(str(s.get("text") or "")):
                        continue
                    if not any(_has_defensive_position_mention(s["text"], p) for p in pos_codes):
                        continue
                    merged_txt = s["text"]
                    for nxt_s in win_segs[idx_s + 1 :]:
                        if float(nxt_s["start"]) <= st + 8.0:
                            merged_txt += " " + nxt_s["text"]
                        else:
                            break
                    if any(ak in merged_txt for ak in action_kws):
                        raw_cands.append(
                            {
                                "start_sec": max(0.0, st - (4.0 if is_runner_fielding else 10.0)),
                                "exact_sec": st,
                                "raw_score": 92.0 if is_runner_fielding else 85.0,
                                "is_fixed": False,
                                "seg": s,
                            }
                        )

            raw_cands.sort(key=lambda x: (-x["raw_score"], x["start_sec"]))
            deduped: list[dict[str, Any]] = []
            for rc in raw_cands:
                if any(abs(rc["start_sec"] - k["start_sec"]) < 5.0 for k in deduped):
                    continue
                sc = rc["raw_score"]
                if not is_interp and half_span > 30:
                    dist = abs(rc["start_sec"] - cur_est)
                    if dist <= 40.0:
                        sc += 8.0
                if rem_after > 0 and rc["start_sec"] > (eff_end - rem_after * 8.0):
                    sc -= 25.0
                if sc >= 55.0:
                    rc["node_score"] = sc
                    deduped.append(rc)

            deduped.sort(key=lambda x: (-x["node_score"], x["start_sec"]))
            cands_per_ev.append(sorted(deduped[:10], key=lambda x: x["start_sec"]))

        # 小型全局單調 DP（防止前事件誤抓尾端擠掉後續整排守備）
        dp: list[list[float]] = []
        prev_ptr: list[list[Optional[tuple[int, int]]]] = []
        for i in range(n_ev):
            dp.append([cands_per_ev[i][k]["node_score"] for k in range(len(cands_per_ev[i]))])
            prev_ptr.append([None] * len(cands_per_ev[i]))

        for i in range(n_ev):
            for k, ci in enumerate(cands_per_ev[i]):
                for p in range(i):
                    gap_idx = i - p
                    for pk, cp in enumerate(cands_per_ev[p]):
                        if ci["start_sec"] < cp["start_sec"] + 6.0 * gap_idx:
                            continue
                        cand_total = dp[p][pk] + ci["node_score"]
                        if cand_total > dp[i][k]:
                            dp[i][k] = cand_total
                            prev_ptr[i][k] = (p, pk)

        best_end: Optional[tuple[int, int]] = None
        best_total = -1.0
        for i in range(n_ev):
            for k in range(len(cands_per_ev[i])):
                if dp[i][k] > best_total:
                    best_total = dp[i][k]
                    best_end = (i, k)

        selected: dict[int, dict[str, Any]] = {}
        cur = best_end
        while cur is not None:
            ei, ek = cur
            selected[ei] = cands_per_ev[ei][ek]
            cur = prev_ptr[ei][ek]

        for idx_e, ev_idx in enumerate(ev_list):
            chosen = selected.get(idx_e)
            if chosen is None or chosen["is_fixed"]:
                continue
            ev_clips = by_ev_idx[ev_idx]
            non_high_clips = [c for c in ev_clips if c["confidence"] != "high"]
            if not non_high_clips:
                continue
            new_start = max(0, int(round(chosen["start_sec"])))
            for c in non_high_clips:
                c["start_sec"] = new_start
                c["align_method"] = "dual_track"
                c["confidence"] = "high"
                upgraded += 1
            hit_seg = chosen["seg"]
            if hit_seg is not None:
                matched_segments_for_db.append(
                    {
                        "video_id": vid,
                        "start_sec": round(float(chosen["exact_sec"]), 2),
                        "end_sec": hit_seg["end"],
                        "text_masked": mask_matched_names_in_text(hit_seg["text"], all_matchers),
                        "matched_uid": None,
                    }
                )
    return upgraded


def _refine_clips_for_single_game(
    g: dict[str, Any],
    m_clips: list[dict[str, Any]],
    conn: sqlite3.Connection,
    transcripts_by_vid: dict[str, dict[str, Any]],
    half_window_map: dict[tuple[str, str], tuple[int, int]],
    asr_mode: str,
    effective_pre_roll: float,
) -> tuple[int, list[dict[str, Any]]]:
    """對單場比賽執行打席 DP、投球獨立通道與守備雙通道精修。"""
    match_id = int(g["match_id"])
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

    batting_by_vid_half: dict[tuple[str, str], list[dict[str, Any]]] = {}
    pitching_by_vid_half: dict[tuple[str, str], list[dict[str, Any]]] = {}
    fielding_by_vid_half: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in m_clips:
        key = (c["video_id"], c["half_label"])
        if c["clip_type"] == "batting":
            batting_by_vid_half.setdefault(key, []).append(c)
        elif c["clip_type"] == "pitching":
            pitching_by_vid_half.setdefault(key, []).append(c)
        elif c["clip_type"] == "fielding":
            fielding_by_vid_half.setdefault(key, []).append(c)

    matched_segments_for_db: list[dict[str, Any]] = []
    upgraded = 0

    for (vid, hlabel), group in batting_by_vid_half.items():
        segs = transcripts_by_vid.get(vid, {}).get("segments", [])
        if not segs:
            continue
        h_start = int(group[0]["half_start_sec"])
        h_end = int(group[0]["half_end_sec"])
        if asr_mode == "speech_pa":
            eff_start, eff_end = half_window_map.get((vid, hlabel), (max(0, h_start - 25), h_end + 35))
        else:
            eff_start, eff_end = (max(0, h_start - 15), h_end + 15)
        win_segs = [s for s in segs if eff_start <= float(s["start"]) <= eff_end]
        if not win_segs:
            continue
        upgraded += _solve_batting_dp(
            group=group,
            win_segs=win_segs,
            matchers_by_uid=matchers_by_uid,
            all_matchers=all_matchers,
            vid=vid,
            eff_start=eff_start,
            eff_end=eff_end,
            h_start=h_start,
            h_end=h_end,
            asr_mode=asr_mode,
            effective_pre_roll=effective_pre_roll,
            matched_segments_for_db=matched_segments_for_db,
        )

    upgraded += _refine_pitching_clips(
        pitching_by_vid_half=pitching_by_vid_half,
        transcripts_by_vid=transcripts_by_vid,
        half_window_map=half_window_map,
        matchers_by_uid=matchers_by_uid,
        all_matchers=all_matchers,
        matched_segments_for_db=matched_segments_for_db,
    )
    upgraded += _refine_fielding_clips(
        fielding_by_vid_half=fielding_by_vid_half,
        transcripts_by_vid=transcripts_by_vid,
        half_window_map=half_window_map,
        opp_roster=g.get("opponent_roster", {}),
        all_matchers=all_matchers,
        matched_segments_for_db=matched_segments_for_db,
    )
    return upgraded, matched_segments_for_db
