/**
 * 資料索引、全域篩選、聚合運算與比賽視角計算引擎 (filters.ts)
 * 嚴格遵守 AGENTS.md 棒球數據模型鐵則：
 * - 以 player_uid 作為唯一識別，嚴禁以姓名合併
 * - 投球計算以整數 outs 累加，不提前過濾 outs === 0 && er > 0 的紀錄
 * - 排行榜與球員清單排除 coach_pitch_888
 * - 勝負嚴格依 pirate_side 判定，杜絕海盜內戰誤判
 */

import {
  COACH_PITCH_UID,
  type AgeGroup,
  type BattingLeaderboardRow,
  type BattingStatRecord,
  type EraInnings,
  type FilterState,
  type LeaderboardMode,
  type LeagueRecord,
  type MatchOutcome,
  type MatchRecord,
  type MatchViewRow,
  type OpponentSummary,
  type OverviewSummary,
  type PiratesDataset,
  type PitchingLeaderboardRow,
  type PitchingStatRecord,
  type PlayerRecord,
  type Season,
  type SelectablePlayer,
  type TeamOverviewRow,
  type TeamRecord,
} from "../types/data";
import {
  calcAvg,
  calcEra,
  calcK7,
  calcObp,
  calcOps,
  calcSlg,
  calcTotalBases,
  calcWhip,
  calcWinRate,
  formatInnings,
} from "./stats";

const AGE_GROUP_RANK: Record<AgeGroup, number> = {
  U8: 0,
  U10: 1,
  U12: 2,
  U15: 3,
};

const SEASON_RANK: Record<Season, number> = {
  春季: 0,
  秋季: 1,
  其他: 2,
};

export interface DatasetIndex {
  leagueById: Map<number, LeagueRecord>;
  teamById: Map<number, TeamRecord>;
  playerById: Map<string, PlayerRecord>;
  rosterCountByTeamId: Map<number, number>;
}

/** 建立資料集快速查詢索引，避免 O(N^2) 查詢 */
export function createDatasetIndex(dataset: PiratesDataset): DatasetIndex {
  const leagueById = new Map<number, LeagueRecord>();
  for (const league of dataset.leagues) {
    leagueById.set(league.league_id, league);
  }

  const teamById = new Map<number, TeamRecord>();
  for (const team of dataset.teams) {
    teamById.set(team.team_id, team);
  }

  const playerById = new Map<string, PlayerRecord>();
  for (const player of dataset.players) {
    playerById.set(player.player_uid, player);
  }

  const rosterCountByTeamId = new Map<number, number>();
  for (const roster of dataset.rosters) {
    rosterCountByTeamId.set(
      roster.team_id,
      (rosterCountByTeamId.get(roster.team_id) ?? 0) + 1
    );
  }

  return { leagueById, teamById, playerById, rosterCountByTeamId };
}

function sortAgeGroups(ageGroups: Iterable<AgeGroup>): AgeGroup[] {
  return [...new Set(ageGroups)].sort(
    (a, b) => (AGE_GROUP_RANK[a] ?? 99) - (AGE_GROUP_RANK[b] ?? 99)
  );
}

/** 依全域篩選條件取得符合的 team_id 集合 */
export function getFilteredTeamIds(
  dataset: PiratesDataset,
  filters: FilterState,
  index: DatasetIndex = createDatasetIndex(dataset)
): Set<number> {
  if (
    filters.ageGroups.length === 0 ||
    filters.teamGroups.length === 0 ||
    filters.years.length === 0 ||
    filters.seasons.length === 0 ||
    filters.tournamentTypes.length === 0
  ) {
    return new Set<number>();
  }

  const ageSet = new Set(filters.ageGroups);
  const groupSet = new Set(filters.teamGroups);
  const yearSet = new Set(filters.years);
  const seasonSet = new Set(filters.seasons);
  const tourneySet = new Set(filters.tournamentTypes);

  const matched = new Set<number>();
  for (const team of dataset.teams) {
    if (!groupSet.has(team.team_group)) {
      continue;
    }
    const league = index.leagueById.get(team.league_id);
    if (!league) {
      continue;
    }
    if (
      ageSet.has(league.age_group) &&
      yearSet.has(league.year) &&
      seasonSet.has(league.season) &&
      tourneySet.has(league.tournament_type)
    ) {
      matched.add(team.team_id);
    }
  }
  return matched;
}

/** 依預先解算之 pirate_side 精準判定單場比分與勝負 */
export function evaluateMatchResult(match: MatchRecord): {
  myScore: number;
  oppScore: number;
  outcome: MatchOutcome;
} | null {
  if (
    match.away_score === null ||
    match.home_score === null ||
    match.pirate_side === null
  ) {
    return null;
  }

  const myScore =
    match.pirate_side === "away" ? match.away_score : match.home_score;
  const oppScore =
    match.pirate_side === "away" ? match.home_score : match.away_score;

  let outcome: MatchOutcome = "T";
  if (myScore > oppScore) {
    outcome = "W";
  } else if (myScore < oppScore) {
    outcome = "L";
  }

  return { myScore, oppScore, outcome };
}

/** 計算 Tab 1：分隊戰情總覽 KPI 與各參賽球隊戰績明細 */
export function computeOverview(
  dataset: PiratesDataset,
  teamIds: ReadonlySet<number>,
  index: DatasetIndex = createDatasetIndex(dataset)
): OverviewSummary {
  let totalMatches = 0;
  let wins = 0;
  let losses = 0;
  let ties = 0;
  let runsScored = 0;
  let runsAllowed = 0;

  const teamStatsMap = new Map<
    number,
    { games: number; wins: number; losses: number; ties: number; rs: number; ra: number }
  >();

  for (const match of dataset.matches) {
    if (!teamIds.has(match.team_id)) {
      continue;
    }
    const evaluated = evaluateMatchResult(match);
    if (!evaluated) {
      continue;
    }

    totalMatches += 1;
    runsScored += evaluated.myScore;
    runsAllowed += evaluated.oppScore;

    if (evaluated.outcome === "W") {
      wins += 1;
    } else if (evaluated.outcome === "L") {
      losses += 1;
    } else {
      ties += 1;
    }

    let ts = teamStatsMap.get(match.team_id);
    if (!ts) {
      ts = { games: 0, wins: 0, losses: 0, ties: 0, rs: 0, ra: 0 };
      teamStatsMap.set(match.team_id, ts);
    }
    ts.games += 1;
    ts.rs += evaluated.myScore;
    ts.ra += evaluated.oppScore;
    if (evaluated.outcome === "W") {
      ts.wins += 1;
    } else if (evaluated.outcome === "L") {
      ts.losses += 1;
    } else {
      ts.ties += 1;
    }
  }

  const teams: TeamOverviewRow[] = [];
  for (const team of dataset.teams) {
    if (!teamIds.has(team.team_id)) {
      continue;
    }
    const league = index.leagueById.get(team.league_id);
    if (!league) {
      continue;
    }
    const ts = teamStatsMap.get(team.team_id) ?? {
      games: 0,
      wins: 0,
      losses: 0,
      ties: 0,
      rs: 0,
      ra: 0,
    };
    teams.push({
      teamId: team.team_id,
      leagueId: team.league_id,
      year: league.year,
      season: league.season,
      ageGroup: league.age_group,
      division: league.division,
      teamGroup: team.team_group,
      teamName: team.team_name,
      leagueTitle: league.league_title,
      headCoach: team.head_coach,
      games: ts.games,
      wins: ts.wins,
      losses: ts.losses,
      ties: ts.ties,
      winRate: calcWinRate(ts.wins, ts.games),
      runsScored: ts.rs,
      runsAllowed: ts.ra,
      runDiff: ts.rs - ts.ra,
      rosterCount: index.rosterCountByTeamId.get(team.team_id) ?? 0,
    });
  }

  teams.sort(
    (a, b) =>
      b.year - a.year ||
      (SEASON_RANK[b.season] ?? 0) - (SEASON_RANK[a.season] ?? 0) ||
      (AGE_GROUP_RANK[a.ageGroup] ?? 99) - (AGE_GROUP_RANK[b.ageGroup] ?? 99) ||
      a.teamId - b.teamId
  );

  return {
    teamCount: teams.length,
    totalMatches,
    wins,
    losses,
    ties,
    winRate: calcWinRate(wins, totalMatches),
    runsScored,
    runsAllowed,
    runDiff: runsScored - runsAllowed,
    teams,
  };
}

/** 計算 Tab 2：打者排行榜（跨季合併依 player_uid 或單季分隊） */
export function computeBattingLeaderboard(
  dataset: PiratesDataset,
  teamIds: ReadonlySet<number>,
  mode: LeaderboardMode,
  minPa: number,
  index: DatasetIndex = createDatasetIndex(dataset)
): BattingLeaderboardRow[] {
  const rows: BattingLeaderboardRow[] = [];

  if (mode === "career") {
    interface BattingAcc {
      teamIds: Set<number>;
      ageGroups: Set<AgeGroup>;
      jerseyNumbers: Set<string>;
      g: number;
      pa: number;
      ab: number;
      h: number;
      doubles: number;
      triples: number;
      hr: number;
      rbi: number;
      r: number;
      bb: number;
      so: number;
      sbTop10: number;
    }

    const grouped = new Map<string, BattingAcc>();
    for (const stat of dataset.batting_stats) {
      if (!teamIds.has(stat.team_id) || stat.player_uid === COACH_PITCH_UID) {
        continue;
      }
      const team = index.teamById.get(stat.team_id);
      const league = team ? index.leagueById.get(team.league_id) : undefined;
      if (!team || !league) {
        continue;
      }

      let acc = grouped.get(stat.player_uid);
      if (!acc) {
        acc = {
          teamIds: new Set<number>(),
          ageGroups: new Set<AgeGroup>(),
          jerseyNumbers: new Set<string>(),
          g: 0,
          pa: 0,
          ab: 0,
          h: 0,
          doubles: 0,
          triples: 0,
          hr: 0,
          rbi: 0,
          r: 0,
          bb: 0,
          so: 0,
          sbTop10: 0,
        };
        grouped.set(stat.player_uid, acc);
      }

      acc.teamIds.add(stat.team_id);
      acc.ageGroups.add(league.age_group);
      if (stat.jersey_number) {
        acc.jerseyNumbers.add(stat.jersey_number);
      }
      acc.g += stat.g;
      acc.pa += stat.pa;
      acc.ab += stat.ab;
      acc.h += stat.h;
      acc.doubles += stat.doubles;
      acc.triples += stat.triples;
      acc.hr += stat.hr;
      acc.rbi += stat.rbi;
      acc.r += stat.r;
      acc.bb += stat.bb;
      acc.so += stat.so;
      acc.sbTop10 += stat.sb_top10 ?? 0;
    }

    for (const [playerUid, acc] of grouped.entries()) {
      if (acc.pa < minPa) {
        continue;
      }
      const player = index.playerById.get(playerUid);
      if (!player) {
        continue;
      }
      const jerseys = [...acc.jerseyNumbers].sort();
      rows.push({
        playerUid,
        maskedName: player.masked_name,
        birthYm: player.birth_ym,
        ageGroups: sortAgeGroups(acc.ageGroups),
        jerseyNumbers: jerseys,
        jerseyNumber: jerseys.length > 0 ? jerseys.join("/") : null,
        teamId: null,
        teamName: null,
        leagueTitle: null,
        year: null,
        season: null,
        seasonsPlayed: acc.teamIds.size,
        g: acc.g,
        pa: acc.pa,
        ab: acc.ab,
        h: acc.h,
        doubles: acc.doubles,
        triples: acc.triples,
        hr: acc.hr,
        rbi: acc.rbi,
        r: acc.r,
        bb: acc.bb,
        so: acc.so,
        sbTop10: acc.sbTop10,
        totalBases: calcTotalBases(acc.h, acc.doubles, acc.triples, acc.hr),
        avg: calcAvg(acc.h, acc.ab),
        obp: calcObp(acc.h, acc.ab, acc.bb),
        slg: calcSlg(acc.h, acc.doubles, acc.triples, acc.hr, acc.ab),
        ops: calcOps(acc.h, acc.doubles, acc.triples, acc.hr, acc.ab, acc.bb),
      });
    }
  } else {
    for (const stat of dataset.batting_stats) {
      if (
        !teamIds.has(stat.team_id) ||
        stat.player_uid === COACH_PITCH_UID ||
        stat.pa < minPa
      ) {
        continue;
      }
      const player = index.playerById.get(stat.player_uid);
      const team = index.teamById.get(stat.team_id);
      const league = team ? index.leagueById.get(team.league_id) : undefined;
      if (!player || !team || !league) {
        continue;
      }

      rows.push({
        playerUid: stat.player_uid,
        maskedName: player.masked_name,
        birthYm: player.birth_ym,
        ageGroups: [league.age_group],
        jerseyNumbers: stat.jersey_number ? [stat.jersey_number] : [],
        jerseyNumber: stat.jersey_number,
        teamId: team.team_id,
        teamName: team.team_name,
        leagueTitle: league.league_title,
        year: league.year,
        season: league.season,
        seasonsPlayed: 1,
        g: stat.g,
        pa: stat.pa,
        ab: stat.ab,
        h: stat.h,
        doubles: stat.doubles,
        triples: stat.triples,
        hr: stat.hr,
        rbi: stat.rbi,
        r: stat.r,
        bb: stat.bb,
        so: stat.so,
        sbTop10: stat.sb_top10 ?? 0,
        totalBases: calcTotalBases(stat.h, stat.doubles, stat.triples, stat.hr),
        avg: calcAvg(stat.h, stat.ab),
        obp: calcObp(stat.h, stat.ab, stat.bb),
        slg: calcSlg(stat.h, stat.doubles, stat.triples, stat.hr, stat.ab),
        ops: calcOps(stat.h, stat.doubles, stat.triples, stat.hr, stat.ab, stat.bb),
      });
    }
  }

  rows.sort(
    (a, b) =>
      b.ops - a.ops ||
      b.avg - a.avg ||
      b.h - a.h ||
      b.pa - a.pa ||
      a.playerUid.localeCompare(b.playerUid, "zh-Hant")
  );
  return rows;
}

/**
 * 計算 Tab 2：投手排行榜（跨季合併依 player_uid 或單季分隊）
 * - 嚴格排除 coach_pitch_888
 * - 累加所有紀錄之 outs 與 er（含 outs === 0 && er > 0）後才比對 minOuts
 */
export function computePitchingLeaderboard(
  dataset: PiratesDataset,
  teamIds: ReadonlySet<number>,
  mode: LeaderboardMode,
  minOuts: number,
  eraInnings: EraInnings,
  index: DatasetIndex = createDatasetIndex(dataset)
): PitchingLeaderboardRow[] {
  const rows: PitchingLeaderboardRow[] = [];

  if (mode === "career") {
    interface PitchingAcc {
      teamIds: Set<number>;
      ageGroups: Set<AgeGroup>;
      jerseyNumbers: Set<string>;
      w: number;
      l: number;
      sv: number;
      g: number;
      outs: number;
      ab: number;
      h: number;
      doubles: number;
      triples: number;
      hr: number;
      bb: number;
      so: number;
      r: number;
      er: number;
    }

    const grouped = new Map<string, PitchingAcc>();
    for (const stat of dataset.pitching_stats) {
      if (!teamIds.has(stat.team_id) || stat.player_uid === COACH_PITCH_UID) {
        continue;
      }
      const team = index.teamById.get(stat.team_id);
      const league = team ? index.leagueById.get(team.league_id) : undefined;
      if (!team || !league) {
        continue;
      }

      let acc = grouped.get(stat.player_uid);
      if (!acc) {
        acc = {
          teamIds: new Set<number>(),
          ageGroups: new Set<AgeGroup>(),
          jerseyNumbers: new Set<string>(),
          w: 0,
          l: 0,
          sv: 0,
          g: 0,
          outs: 0,
          ab: 0,
          h: 0,
          doubles: 0,
          triples: 0,
          hr: 0,
          bb: 0,
          so: 0,
          r: 0,
          er: 0,
        };
        grouped.set(stat.player_uid, acc);
      }

      acc.teamIds.add(stat.team_id);
      acc.ageGroups.add(league.age_group);
      if (stat.jersey_number) {
        acc.jerseyNumbers.add(stat.jersey_number);
      }
      acc.w += stat.w;
      acc.l += stat.l;
      acc.sv += stat.sv;
      acc.g += stat.g;
      acc.outs += stat.outs;
      acc.ab += stat.ab;
      acc.h += stat.h;
      acc.doubles += stat.doubles;
      acc.triples += stat.triples;
      acc.hr += stat.hr;
      acc.bb += stat.bb;
      acc.so += stat.so;
      acc.r += stat.r;
      acc.er += stat.er;
    }

    for (const [playerUid, acc] of grouped.entries()) {
      if (acc.outs < minOuts) {
        continue;
      }
      const player = index.playerById.get(playerUid);
      if (!player) {
        continue;
      }
      const jerseys = [...acc.jerseyNumbers].sort();
      rows.push({
        playerUid,
        maskedName: player.masked_name,
        birthYm: player.birth_ym,
        ageGroups: sortAgeGroups(acc.ageGroups),
        jerseyNumbers: jerseys,
        jerseyNumber: jerseys.length > 0 ? jerseys.join("/") : null,
        teamId: null,
        teamName: null,
        leagueTitle: null,
        year: null,
        season: null,
        seasonsPitched: acc.teamIds.size,
        w: acc.w,
        l: acc.l,
        sv: acc.sv,
        g: acc.g,
        outs: acc.outs,
        ipDisplay: formatInnings(acc.outs),
        ab: acc.ab,
        h: acc.h,
        doubles: acc.doubles,
        triples: acc.triples,
        hr: acc.hr,
        bb: acc.bb,
        so: acc.so,
        r: acc.r,
        er: acc.er,
        era: calcEra(acc.er, acc.outs, eraInnings),
        whip: calcWhip(acc.h, acc.bb, acc.outs),
        k7: calcK7(acc.so, acc.outs),
      });
    }
  } else {
    for (const stat of dataset.pitching_stats) {
      if (
        !teamIds.has(stat.team_id) ||
        stat.player_uid === COACH_PITCH_UID ||
        stat.outs < minOuts
      ) {
        continue;
      }
      const player = index.playerById.get(stat.player_uid);
      const team = index.teamById.get(stat.team_id);
      const league = team ? index.leagueById.get(team.league_id) : undefined;
      if (!player || !team || !league) {
        continue;
      }

      rows.push({
        playerUid: stat.player_uid,
        maskedName: player.masked_name,
        birthYm: player.birth_ym,
        ageGroups: [league.age_group],
        jerseyNumbers: stat.jersey_number ? [stat.jersey_number] : [],
        jerseyNumber: stat.jersey_number,
        teamId: team.team_id,
        teamName: team.team_name,
        leagueTitle: league.league_title,
        year: league.year,
        season: league.season,
        seasonsPitched: 1,
        w: stat.w,
        l: stat.l,
        sv: stat.sv,
        g: stat.g,
        outs: stat.outs,
        ipDisplay: formatInnings(stat.outs),
        ab: stat.ab,
        h: stat.h,
        doubles: stat.doubles,
        triples: stat.triples,
        hr: stat.hr,
        bb: stat.bb,
        so: stat.so,
        r: stat.r,
        er: stat.er,
        era: calcEra(stat.er, stat.outs, eraInnings),
        whip: calcWhip(stat.h, stat.bb, stat.outs),
        k7: calcK7(stat.so, stat.outs),
      });
    }
  }

  rows.sort((a, b) => {
    if (a.era === null && b.era === null) {
      return b.outs - a.outs || b.so - a.so || a.playerUid.localeCompare(b.playerUid, "zh-Hant");
    }
    if (a.era === null) {
      return 1;
    }
    if (b.era === null) {
      return -1;
    }
    return (
      a.era - b.era ||
      b.outs - a.outs ||
      b.so - a.so ||
      a.playerUid.localeCompare(b.playerUid, "zh-Hant")
    );
  });

  return rows;
}

/** 計算 Tab 4：賽程戰果與對手分析 */
export function computeOpponents(
  dataset: PiratesDataset,
  teamIds: ReadonlySet<number>,
  selectedOpponent: string | null = null,
  index: DatasetIndex = createDatasetIndex(dataset)
): OpponentSummary {
  const allMatchRows: MatchViewRow[] = [];
  const opponentSet = new Set<string>();

  for (const match of dataset.matches) {
    if (!teamIds.has(match.team_id)) {
      continue;
    }
    const team = index.teamById.get(match.team_id);
    const league = index.leagueById.get(match.league_id);
    if (!team || !league) {
      continue;
    }

    if (match.opponent_team && match.opponent_team.trim() !== "") {
      opponentSet.add(match.opponent_team);
    }

    const evaluated = evaluateMatchResult(match);
    allMatchRows.push({
      id: match.id,
      teamId: match.team_id,
      leagueId: match.league_id,
      year: league.year,
      season: league.season,
      ageGroup: league.age_group,
      leagueTitle: league.league_title,
      teamName: team.team_name,
      matchDate: match.match_date,
      matchDateIso: match.match_date_iso,
      matchTime: match.match_time,
      awayTeam: match.away_team,
      homeTeam: match.home_team,
      awayScore: match.away_score,
      homeScore: match.home_score,
      pirateSide: match.pirate_side,
      opponentTeam: match.opponent_team,
      myScore: evaluated ? evaluated.myScore : null,
      oppScore: evaluated ? evaluated.oppScore : null,
      outcome: evaluated ? evaluated.outcome : null,
      venue: match.venue,
      boxscoreUrl: match.boxscore_url,
      pbpUrl: match.pbp_url,
      summaryUrl: match.summary_url,
      youtubeVideos: match.youtube_videos ?? [],
    });
  }

  const availableOpponents = [...opponentSet].sort((a, b) =>
    a.localeCompare(b, "zh-Hant")
  );

  const normalizedSelection =
    selectedOpponent && selectedOpponent.trim() !== "" ? selectedOpponent : null;

  const filteredMatches = normalizedSelection
    ? allMatchRows.filter((m) => m.opponentTeam === normalizedSelection)
    : allMatchRows;

  filteredMatches.sort((a, b) => {
    const dateA = a.matchDateIso ?? "";
    const dateB = b.matchDateIso ?? "";
    if (dateA !== dateB) {
      return dateB.localeCompare(dateA);
    }
    const timeA = a.matchTime ?? "";
    const timeB = b.matchTime ?? "";
    if (timeA !== timeB) {
      return timeB.localeCompare(timeA);
    }
    return b.id - a.id;
  });

  let completedMatches = 0;
  let wins = 0;
  let losses = 0;
  let ties = 0;
  let runsScored = 0;
  let runsAllowed = 0;

  for (const m of filteredMatches) {
    if (m.outcome === null || m.myScore === null || m.oppScore === null) {
      continue;
    }
    completedMatches += 1;
    runsScored += m.myScore;
    runsAllowed += m.oppScore;
    if (m.outcome === "W") {
      wins += 1;
    } else if (m.outcome === "L") {
      losses += 1;
    } else {
      ties += 1;
    }
  }

  return {
    opponentName: normalizedSelection,
    availableOpponents,
    totalMatches: filteredMatches.length,
    completedMatches,
    wins,
    losses,
    ties,
    winRate: calcWinRate(wins, completedMatches),
    runsScored,
    runsAllowed,
    matches: filteredMatches,
  };
}

/** 取得可供球員搜尋選單顯示的球員清單（嚴格排除 coach_pitch_888） */
export function getSelectablePlayers(
  dataset: PiratesDataset,
  teamIds?: ReadonlySet<number>,
  index: DatasetIndex = createDatasetIndex(dataset)
): SelectablePlayer[] {
  interface Acc {
    teamIds: Set<number>;
    ageGroups: Set<AgeGroup>;
    jerseyNumbers: Set<string>;
    hasBatting: boolean;
    hasPitching: boolean;
  }

  const accMap = new Map<string, Acc>();
  const ensureAcc = (uid: string): Acc | null => {
    if (uid === COACH_PITCH_UID) {
      return null;
    }
    let acc = accMap.get(uid);
    if (!acc) {
      acc = {
        teamIds: new Set<number>(),
        ageGroups: new Set<AgeGroup>(),
        jerseyNumbers: new Set<string>(),
        hasBatting: false,
        hasPitching: false,
      };
      accMap.set(uid, acc);
    }
    return acc;
  };

  const acceptTeam = (teamId: number) =>
    teamIds === undefined ? true : teamIds.has(teamId);

  const registerTeamInfo = (acc: Acc, teamId: number, jersey: string | null) => {
    acc.teamIds.add(teamId);
    const team = index.teamById.get(teamId);
    const league = team ? index.leagueById.get(team.league_id) : undefined;
    if (league) {
      acc.ageGroups.add(league.age_group);
    }
    if (jersey) {
      acc.jerseyNumbers.add(jersey);
    }
  };

  for (const r of dataset.rosters) {
    if (!acceptTeam(r.team_id)) continue;
    const acc = ensureAcc(r.player_uid);
    if (acc) registerTeamInfo(acc, r.team_id, r.jersey_number);
  }
  for (const b of dataset.batting_stats) {
    if (!acceptTeam(b.team_id)) continue;
    const acc = ensureAcc(b.player_uid);
    if (acc) {
      acc.hasBatting = true;
      registerTeamInfo(acc, b.team_id, b.jersey_number);
    }
  }
  for (const p of dataset.pitching_stats) {
    if (!acceptTeam(p.team_id)) continue;
    const acc = ensureAcc(p.player_uid);
    if (acc) {
      acc.hasPitching = true;
      registerTeamInfo(acc, p.team_id, p.jersey_number);
    }
  }

  const players: SelectablePlayer[] = [];
  for (const [playerUid, acc] of accMap.entries()) {
    const player = index.playerById.get(playerUid);
    if (!player) {
      continue;
    }
    players.push({
      playerUid,
      maskedName: player.masked_name,
      birthYm: player.birth_ym,
      ageGroups: sortAgeGroups(acc.ageGroups),
      jerseyNumbers: [...acc.jerseyNumbers].sort(),
      teamCount: acc.teamIds.size,
      hasBatting: acc.hasBatting,
      hasPitching: acc.hasPitching,
    });
  }

  players.sort(
    (a, b) =>
      b.teamCount - a.teamCount ||
      a.maskedName.localeCompare(b.maskedName, "zh-Hant") ||
      a.playerUid.localeCompare(b.playerUid)
  );
  return players;
}

export interface PlayerSeasonBattingRow extends BattingStatRecord {
  year: number;
  season: Season;
  ageGroup: AgeGroup;
  teamName: string;
  leagueTitle: string;
  totalBases: number;
  avg: number;
  obp: number;
  slg: number;
  ops: number;
}

export interface PlayerSeasonPitchingRow extends PitchingStatRecord {
  year: number;
  season: Season;
  ageGroup: AgeGroup;
  teamName: string;
  leagueTitle: string;
  ipDisplay: string;
  era: number | null;
  whip: number | null;
  k7: number | null;
}

export interface PlayerHistorySummary {
  player: PlayerRecord | null;
  ageGroups: AgeGroup[];
  jerseyNumbers: string[];
  positions: string[];
  battingSeasons: PlayerSeasonBattingRow[];
  pitchingSeasons: PlayerSeasonPitchingRow[];
}

/** 取得單一球員的跨隊／跨屆歷程明細（按時間正序排列，供球員成長檔案與趨勢圖使用） */
export function getPlayerHistory(
  dataset: PiratesDataset,
  playerUid: string,
  eraInnings: EraInnings,
  teamIds?: ReadonlySet<number>,
  index: DatasetIndex = createDatasetIndex(dataset)
): PlayerHistorySummary {
  if (playerUid === COACH_PITCH_UID) {
    return {
      player: null,
      ageGroups: [],
      jerseyNumbers: [],
      positions: [],
      battingSeasons: [],
      pitchingSeasons: [],
    };
  }

  const player = index.playerById.get(playerUid) ?? null;
  const acceptTeam = (tid: number) => (teamIds === undefined ? true : teamIds.has(tid));

  const ageGroupSet = new Set<AgeGroup>();
  const jerseySet = new Set<string>();
  const positionSet = new Set<string>();

  for (const r of dataset.rosters) {
    if (r.player_uid !== playerUid || !acceptTeam(r.team_id)) continue;
    const team = index.teamById.get(r.team_id);
    const league = team ? index.leagueById.get(team.league_id) : undefined;
    if (league) ageGroupSet.add(league.age_group);
    if (r.jersey_number) jerseySet.add(r.jersey_number);
    if (r.position) positionSet.add(r.position);
  }

  const sortChronologically = (
    a: { year: number; season: Season; team_id: number },
    b: { year: number; season: Season; team_id: number }
  ) =>
    a.year - b.year ||
    (SEASON_RANK[a.season] ?? 9) - (SEASON_RANK[b.season] ?? 9) ||
    a.team_id - b.team_id;

  const battingSeasons: PlayerSeasonBattingRow[] = [];
  for (const b of dataset.batting_stats) {
    if (b.player_uid !== playerUid || !acceptTeam(b.team_id)) continue;
    const team = index.teamById.get(b.team_id);
    const league = team ? index.leagueById.get(team.league_id) : undefined;
    if (!team || !league) continue;
    ageGroupSet.add(league.age_group);
    if (b.jersey_number) jerseySet.add(b.jersey_number);

    battingSeasons.push({
      ...b,
      year: league.year,
      season: league.season,
      ageGroup: league.age_group,
      teamName: team.team_name,
      leagueTitle: league.league_title,
      totalBases: calcTotalBases(b.h, b.doubles, b.triples, b.hr),
      avg: calcAvg(b.h, b.ab),
      obp: calcObp(b.h, b.ab, b.bb),
      slg: calcSlg(b.h, b.doubles, b.triples, b.hr, b.ab),
      ops: calcOps(b.h, b.doubles, b.triples, b.hr, b.ab, b.bb),
    });
  }
  battingSeasons.sort(sortChronologically);

  const pitchingSeasons: PlayerSeasonPitchingRow[] = [];
  for (const p of dataset.pitching_stats) {
    if (p.player_uid !== playerUid || !acceptTeam(p.team_id)) continue;
    const team = index.teamById.get(p.team_id);
    const league = team ? index.leagueById.get(team.league_id) : undefined;
    if (!team || !league) continue;
    ageGroupSet.add(league.age_group);
    if (p.jersey_number) jerseySet.add(p.jersey_number);

    pitchingSeasons.push({
      ...p,
      year: league.year,
      season: league.season,
      ageGroup: league.age_group,
      teamName: team.team_name,
      leagueTitle: league.league_title,
      ipDisplay: formatInnings(p.outs),
      era: calcEra(p.er, p.outs, eraInnings),
      whip: calcWhip(p.h, p.bb, p.outs),
      k7: calcK7(p.so, p.outs),
    });
  }
  pitchingSeasons.sort(sortChronologically);

  return {
    player,
    ageGroups: sortAgeGroups(ageGroupSet),
    jerseyNumbers: [...jerseySet].sort(),
    positions: [...positionSet].sort(),
    battingSeasons,
    pitchingSeasons,
  };
}
