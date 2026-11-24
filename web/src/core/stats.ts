/**
 * 棒球純函式計算引擎 (stats.ts)
 * 嚴格對齊 src/db.py SQLite Views 與 AGENTS.md 棒球數據模型鐵則：
 * - 投球局數一律以整數出局數 outs 累計並換算為 X.Y
 * - 0 局責失不可預先濾除，總 outs === 0 時 ERA / WHIP / K7 一律回傳 null
 */

import type { EraInnings } from "../types/data";

/**
 * 固定小數位數四捨五入，補償 IEEE-754 浮點數精度誤差以對齊 SQLite ROUND()
 */
export function roundTo(value: number, decimals: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  const factor = 10 ** decimals;
  const sign = value < 0 ? -1 : 1;
  return (sign * Math.round(Math.abs(value) * factor + 1e-12)) / factor;
}

/** 計算總壘打數 TB = 1B + 2*2B + 3*3B + 4*HR = H + 2B + 2*3B + 3*HR */
export function calcTotalBases(
  h: number,
  doubles: number,
  triples: number,
  hr: number
): number {
  return h + doubles + 2 * triples + 3 * hr;
}

/** 計算打擊率 AVG = H / AB（四捨五入至小數 3 位） */
export function calcAvg(h: number, ab: number): number {
  if (ab <= 0) {
    return 0;
  }
  return roundTo(h / ab, 3);
}

/** 計算上壘率 OBP = (H + BB) / (AB + BB)（四捨五入至小數 3 位） */
export function calcObp(h: number, ab: number, bb: number): number {
  const denom = ab + bb;
  if (denom <= 0) {
    return 0;
  }
  return roundTo((h + bb) / denom, 3);
}

/** 計算長打率 SLG = TB / AB（四捨五入至小數 3 位） */
export function calcSlg(
  h: number,
  doubles: number,
  triples: number,
  hr: number,
  ab: number
): number {
  if (ab <= 0) {
    return 0;
  }
  const tb = calcTotalBases(h, doubles, triples, hr);
  return roundTo(tb / ab, 3);
}

/**
 * 計算整體攻擊指數 OPS = 未四捨五入原始 OBP + 原始 SLG 後再取小數 3 位
 * （完全對齊原 SQL 計算規則）
 */
export function calcOps(
  h: number,
  doubles: number,
  triples: number,
  hr: number,
  ab: number,
  bb: number
): number {
  const obpRaw = ab + bb > 0 ? (h + bb) / (ab + bb) : 0;
  const tb = calcTotalBases(h, doubles, triples, hr);
  const slgRaw = ab > 0 ? tb / ab : 0;
  return roundTo(obpRaw + slgRaw, 3);
}

/** 將整數出局數轉換為棒球局數顯示字串 (例：outs=31 -> "10.1") */
export function formatInnings(outs: number): string {
  const safeOuts = Math.max(0, Math.trunc(outs));
  return `${Math.floor(safeOuts / 3)}.${safeOuts % 3}`;
}

/**
 * 計算防禦率 ERA：
 * 7 局制為 (ER * 21) / outs；9 局制為 (ER * 27) / outs。
 * 當 outs <= 0 時回傳 null（UI 顯示 '-'）。
 */
export function calcEra(
  er: number,
  outs: number,
  eraInnings: EraInnings = 7
): number | null {
  if (outs <= 0) {
    return null;
  }
  const multiplier = eraInnings === 7 ? 21 : 27;
  return roundTo((er * multiplier) / outs, 2);
}

/**
 * 計算每局被上壘率 WHIP = ((H + BB) * 3) / outs。
 * 當 outs <= 0 時回傳 null。
 */
export function calcWhip(h: number, bb: number, outs: number): number | null {
  if (outs <= 0) {
    return null;
  }
  return roundTo(((h + bb) * 3) / outs, 2);
}

/**
 * 計算 7 局三振率 K/7 = (SO * 21) / outs。
 * 當 outs <= 0 時回傳 null。
 */
export function calcK7(so: number, outs: number): number | null {
  if (outs <= 0) {
    return null;
  }
  return roundTo((so * 21) / outs, 2);
}

/** 計算勝率（無出賽場次時回傳 null） */
export function calcWinRate(wins: number, games: number): number | null {
  if (games <= 0) {
    return null;
  }
  return roundTo(wins / games, 3);
}

/** 格式化 3 位小數率值（如 AVG / OBP / SLG / OPS / 勝率） */
export function formatRate3(value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "-";
  }
  return value.toFixed(3);
}

/** 格式化 2 位小數率值（如 ERA / WHIP / K/7） */
export function formatRate2(value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "-";
  }
  return value.toFixed(2);
}
