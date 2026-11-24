import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { buildCsvContent, escapeCsvCell } from "../core/csv";
import {
  computeBattingLeaderboard,
  computeOpponents,
  computeOverview,
  computePitchingLeaderboard,
  createDatasetIndex,
  evaluateMatchResult,
  getFilteredTeamIds,
  getPlayerHistory,
  getSelectablePlayers,
} from "../core/filters";
import {
  calcAvg,
  calcEra,
  calcK7,
  calcObp,
  calcOps,
  calcSlg,
  calcTotalBases,
  calcWhip,
  formatInnings,
  formatRate2,
  formatRate3,
} from "../core/stats";
import {
  createDefaultState,
  parseStateFromSearch,
  serializeStateToSearch,
} from "../core/store";
import { COACH_PITCH_UID, type MatchRecord, type PiratesDataset } from "../types/data";

const jsonPath = resolve(process.cwd(), "public/data/pirates.json");
const dataset = JSON.parse(readFileSync(jsonPath, "utf-8")) as PiratesDataset;
const index = createDatasetIndex(dataset);
const allTeamIds = new Set(dataset.teams.map((t) => t.team_id));

describe("棒球純函式計算與真實 SQLite Views 基準對齊", () => {
  it("前端打擊計算結果與 SQLite v_player_career_batting 基準誤差 === 0", () => {
    expect(dataset.benchmarks.career_batting.length).toBeGreaterThan(0);

    const rows = computeBattingLeaderboard(dataset, allTeamIds, "career", 0, index);
    const byUid = new Map(rows.map((r) => [r.playerUid, r]));

    for (const bench of dataset.benchmarks.career_batting) {
      const row = byUid.get(bench.player_uid);
      expect(row, `缺少打擊基準球員: ${bench.player_uid}`).toBeDefined();
      if (!row) continue;

      expect(row.pa).toBe(bench.pa);
      expect(row.ab).toBe(bench.ab);
      expect(row.h).toBe(bench.h);
      expect(row.avg).toBe(bench.calc_avg);
      expect(row.obp).toBe(bench.calc_obp);
      expect(row.slg).toBe(bench.calc_slg);
    }
  });

  it("前端投球計算結果（7局制與9局制）與 SQLite v_player_career_pitching 基準誤差 === 0", () => {
    expect(dataset.benchmarks.career_pitching.length).toBeGreaterThan(0);

    const rows7 = computePitchingLeaderboard(dataset, allTeamIds, "career", 0, 7, index);
    const rows9 = computePitchingLeaderboard(dataset, allTeamIds, "career", 0, 9, index);
    const byUid7 = new Map(rows7.map((r) => [r.playerUid, r]));
    const byUid9 = new Map(rows9.map((r) => [r.playerUid, r]));

    for (const bench of dataset.benchmarks.career_pitching) {
      const row7 = byUid7.get(bench.player_uid);
      const row9 = byUid9.get(bench.player_uid);
      expect(row7, `缺少投球基準球員(7局): ${bench.player_uid}`).toBeDefined();
      expect(row9, `缺少投球基準球員(9局): ${bench.player_uid}`).toBeDefined();
      if (!row7 || !row9) continue;

      expect(row7.outs).toBe(bench.total_outs);
      expect(row7.er).toBe(bench.er);
      expect(row7.ipDisplay).toBe(bench.ip_baseball);
      expect(row7.era).toBe(bench.calc_era_7);
      expect(row9.era).toBe(bench.calc_era_9);
      expect(row7.whip).toBe(bench.calc_whip);
      expect(row7.k7).toBe(bench.calc_k7);
    }
  });

  it("投球局數整數化換算與 OPS 原始未進位相加規則正確", () => {
    expect(formatInnings(0)).toBe("0.0");
    expect(formatInnings(1)).toBe("0.1");
    expect(formatInnings(2)).toBe("0.2");
    expect(formatInnings(31)).toBe("10.1");
    expect(formatInnings(56)).toBe("18.2");

    // 驗證 OPS = round(obp_raw + slg_raw, 3)，而非 round(obp) + round(slg)
    expect(calcTotalBases(5, 1, 1, 1)).toBe(11);
    expect(calcAvg(3, 9)).toBe(0.333);
    expect(calcObp(3, 9, 1)).toBe(0.4);
    expect(calcSlg(3, 0, 0, 1, 9)).toBe(0.667);
    expect(calcOps(3, 0, 0, 1, 9, 1)).toBe(1.067);
    expect(formatRate3(null)).toBe("-");
    expect(formatRate2(null)).toBe("-");
  });
});

describe("棒球數據模型鐵則與邊界案例", () => {
  it("蔡〇宇_2015-11 與 蔡〇宇_2017-06 各自獨立計算絕不合併", () => {
    const careerRows = computeBattingLeaderboard(dataset, allTeamIds, "career", 0, index);
    const caiOld = careerRows.find((r) => r.playerUid === "蔡〇宇_2015-11");
    const caiYoung = careerRows.find((r) => r.playerUid === "蔡〇宇_2017-06");

    expect(caiOld).toBeDefined();
    expect(caiYoung).toBeDefined();
    expect(caiOld?.pa).not.toBe(caiYoung?.pa);

    // 驗證單一球隊 team_id=3439 兩人各自獨立存在
    const team3439 = new Set([3439]);
    const t3439Rows = computeBattingLeaderboard(dataset, team3439, "season", 0, index);
    const t3439CaiOld = t3439Rows.find((r) => r.playerUid === "蔡〇宇_2015-11");
    const t3439CaiYoung = t3439Rows.find((r) => r.playerUid === "蔡〇宇_2017-06");

    expect(t3439CaiOld?.jerseyNumber).toBe("66");
    expect(t3439CaiOld?.pa).toBe(10);
    expect(t3439CaiOld?.h).toBe(3);

    expect(t3439CaiYoung?.jerseyNumber).toBe("96");
    expect(t3439CaiYoung?.pa).toBe(14);
    expect(t3439CaiYoung?.h).toBe(3);
  });

  it("0 局責失防呆：單獨 0 局回傳 null，跨季合併累加所有責失分", () => {
    // 單獨篩選 team_id=3439 時，林〇晏_2017-11 為 outs=0, er=1
    const team3439Rows = computePitchingLeaderboard(
      dataset,
      new Set([3439]),
      "career",
      0,
      7,
      index
    );
    const linSingle = team3439Rows.find((r) => r.playerUid === "林〇晏_2017-11");
    expect(linSingle).toBeDefined();
    expect(linSingle?.outs).toBe(0);
    expect(linSingle?.er).toBe(1);
    expect(linSingle?.ipDisplay).toBe("0.0");
    expect(linSingle?.era).toBeNull();
    expect(linSingle?.whip).toBeNull();
    expect(linSingle?.k7).toBeNull();
    expect(formatRate2(linSingle?.era ?? null)).toBe("-");

    // 跨隊全資料合併時，必須計入 team_id=3439 的 1 分責失（不可先過濾 0 局紀錄）
    const careerRows7 = computePitchingLeaderboard(dataset, allTeamIds, "career", 0, 7, index);
    const careerRows9 = computePitchingLeaderboard(dataset, allTeamIds, "career", 0, 9, index);
    const linCareer7 = careerRows7.find((r) => r.playerUid === "林〇晏_2017-11");
    const linCareer9 = careerRows9.find((r) => r.playerUid === "林〇晏_2017-11");

    expect(linCareer7?.outs).toBe(6);
    expect(linCareer7?.er).toBe(1);
    expect(linCareer7?.era).toBe(calcEra(1, 6, 7));
    expect(linCareer7?.era).toBe(3.5);
    expect(linCareer9?.era).toBe(calcEra(1, 6, 9));
    expect(linCareer9?.era).toBe(4.5);
    expect(linCareer7?.whip).toBe(calcWhip(linCareer7?.h ?? 0, linCareer7?.bb ?? 0, 6));
    expect(linCareer7?.k7).toBe(calcK7(linCareer7?.so ?? 0, 6));
  });

  it("投手排行榜與球員選單嚴格排除 coach_pitch_888", () => {
    const careerPitchers = computePitchingLeaderboard(dataset, allTeamIds, "career", 0, 7, index);
    const seasonPitchers = computePitchingLeaderboard(dataset, allTeamIds, "season", 0, 7, index);
    const selectable = getSelectablePlayers(dataset, allTeamIds, index);
    const coachHistory = getPlayerHistory(dataset, COACH_PITCH_UID, 7, allTeamIds, index);

    expect(careerPitchers.some((r) => r.playerUid === COACH_PITCH_UID)).toBe(false);
    expect(seasonPitchers.some((r) => r.playerUid === COACH_PITCH_UID)).toBe(false);
    expect(selectable.some((p) => p.playerUid === COACH_PITCH_UID)).toBe(false);
    expect(coachHistory.player).toBeNull();
    expect(coachHistory.pitchingSeasons).toHaveLength(0);
  });

  it("海盜白隊 vs 黑隊內戰勝負判定正確，不發生勝負反置", () => {
    // 1. 檢查真實資料中所有海盜內戰場次皆有精準 pirate_side 且可正確計分
    const civilMatches = dataset.matches.filter(
      (m) => m.away_team.includes("海盜") && m.home_team.includes("海盜")
    );
    expect(civilMatches.length).toBeGreaterThan(0);

    for (const m of civilMatches) {
      const team = index.teamById.get(m.team_id);
      expect(team).toBeDefined();
      expect(m.pirate_side).not.toBeNull();
      if (m.pirate_side === "away") {
        expect(m.away_team).toBe(team?.team_name);
      } else if (m.pirate_side === "home") {
        expect(m.home_team).toBe(team?.team_name);
      }
    }

    // 2. 合成白隊(客) 7 : 3 黑隊(主) 的雙向視角測試
    const whitePerspective: MatchRecord = {
      id: 99001,
      team_id: 2968,
      league_id: 364,
      match_date: "10/10",
      match_date_iso: "2025-10-10",
      match_time: "09:00",
      away_team: "台中小海盜(白)",
      home_team: "台中小海盜(黑)",
      away_score: 7,
      home_score: 3,
      pirate_side: "away",
      opponent_team: "台中小海盜(黑)",
      venue: "測試球場",
      boxscore_url: null,
      pbp_url: null,
      summary_url: null,
    };
    const blackPerspective: MatchRecord = {
      ...whitePerspective,
      id: 99002,
      team_id: 2969,
      pirate_side: "home",
      opponent_team: "台中小海盜(白)",
    };

    const wRes = evaluateMatchResult(whitePerspective);
    const bRes = evaluateMatchResult(blackPerspective);
    expect(wRes).toEqual({ myScore: 7, oppScore: 3, outcome: "W" });
    expect(bRes).toEqual({ myScore: 3, oppScore: 7, outcome: "L" });
  });

  it("未開賽／零資料隊伍 (含 3650/3651/4037、模擬空資料 team_id=4046 與空篩選) 不拋例外", () => {
    // 1. 真實資料庫中目前無任何名冊/投打/賽程之未開賽隊伍 (3650, 3651)
    const emptyTeamIds = new Set([3650, 3651]);
    const overview = computeOverview(dataset, emptyTeamIds, index);
    const batting = computeBattingLeaderboard(dataset, emptyTeamIds, "career", 0, index);
    const pitching = computePitchingLeaderboard(dataset, emptyTeamIds, "career", 0, 7, index);
    const opponents = computeOpponents(dataset, emptyTeamIds, null, index);
    const players = getSelectablePlayers(dataset, emptyTeamIds, index);

    expect(overview.teamCount).toBe(2);
    expect(overview.totalMatches).toBe(0);
    expect(overview.winRate).toBeNull();
    expect(batting).toEqual([]);
    expect(pitching).toEqual([]);
    expect(opponents.totalMatches).toBe(0);
    expect(opponents.winRate).toBeNull();
    expect(players).toEqual([]);

    // 2. 有名冊但無賽程與攻守數據之隊伍 (4037)
    const rosterOnlyOverview = computeOverview(dataset, new Set([4037]), index);
    expect(rosterOnlyOverview.teamCount).toBe(1);
    expect(rosterOnlyOverview.totalMatches).toBe(0);
    expect(rosterOnlyOverview.teams[0]?.rosterCount).toBeGreaterThan(0);
    expect(computeBattingLeaderboard(dataset, new Set([4037]), "career", 0, index)).toEqual([]);

    // 3. 模擬早期 team_id=4046 尚未開賽零資料狀態
    const empty4046Dataset: PiratesDataset = {
      ...dataset,
      rosters: dataset.rosters.filter((r) => r.team_id !== 4046),
      batting_stats: dataset.batting_stats.filter((b) => b.team_id !== 4046),
      pitching_stats: dataset.pitching_stats.filter((p) => p.team_id !== 4046),
      matches: dataset.matches.filter((m) => m.team_id !== 4046),
    };
    const empty4046Overview = computeOverview(empty4046Dataset, new Set([4046]));
    expect(empty4046Overview.teamCount).toBe(1);
    expect(empty4046Overview.totalMatches).toBe(0);
    expect(empty4046Overview.winRate).toBeNull();
    expect(
      computeBattingLeaderboard(empty4046Dataset, new Set([4046]), "career", 0)
    ).toEqual([]);

    // 4. 空篩選集合
    const noMatchedTeamIds = getFilteredTeamIds(
      dataset,
      {
        ageGroups: [],
        teamGroups: ["海盜本隊"],
        years: [2026],
        seasons: ["春季"],
        tournamentTypes: ["例行賽"],
      },
      index
    );
    expect(noMatchedTeamIds.size).toBe(0);
    expect(computeOverview(dataset, noMatchedTeamIds, index).teamCount).toBe(0);
  });
});

describe("CSV 匯出與 URL Query 狀態同步", () => {
  it("CSV 匯出具備 UTF-8 BOM 與完整跳脫處理", () => {
    expect(escapeCsvCell(null)).toBe("");
    expect(escapeCsvCell('a,"b"')).toBe('"a,""b"""');

    const csv = buildCsvContent(
      ["姓名", "備註"],
      [["蔡〇宇", "U10, 白隊"]]
    );
    expect(csv.startsWith("\uFEFF姓名,備註\r\n")).toBe(true);
    expect(csv).toContain('蔡〇宇,"U10, 白隊"');
  });

  it("URL Query 可精準還原並序列化球員成長頁面與篩選狀態", () => {
    const parsed = parseStateFromSearch(
      "?tab=player&uid=%E8%94%A1%E3%80%87%E5%AE%87_2015-11&era=7",
      dataset
    );
    expect(parsed.tab).toBe("player");
    expect(parsed.selectedPlayerUid).toBe("蔡〇宇_2015-11");
    expect(parsed.eraInnings).toBe(7);

    const search = serializeStateToSearch(parsed, dataset);
    const reparsed = parseStateFromSearch(search, dataset);
    expect(reparsed).toEqual(parsed);

    // 預設狀態應產出精簡空字串
    const def = createDefaultState(dataset);
    expect(serializeStateToSearch(def, dataset)).toBe("");

    // 空篩選條件需正確保留空陣列，不可退回全選
    const emptyAge = parseStateFromSearch("?age=", dataset);
    expect(emptyAge.filters.ageGroups).toEqual([]);
    expect(serializeStateToSearch(emptyAge, dataset)).toContain("age=");
  });
});
