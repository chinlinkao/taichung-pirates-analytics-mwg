/**
 * 台中小海盜棒球數據庫 — 前端資料契約與狀態型別定義
 * 嚴格對應 src/export_json.py 匯出的 web/public/data/pirates.json Schema
 */

/** 教練拋打虛擬球員識別碼（排行榜與球員檔案必須排除） */
export const COACH_PITCH_UID = "coach_pitch_888" as const;

export type AgeGroup = "U8" | "U10" | "U12" | "U15";
export type TeamGroup = "海盜本隊" | "白隊" | "黑隊" | "U15海盜";
export type Season = "春季" | "秋季" | "其他";
export type TournamentType = "例行賽" | "季後賽" | "台灣大賽";
export type Division = "大聯盟" | "小聯盟" | "一般";

export type EraInnings = 7 | 9;
export type TabId = "overview" | "leaderboards" | "player" | "opponents";
export type LeaderboardMode = "career" | "season";

export interface LeagueRecord {
  league_id: number;
  league_title: string;
  year: number;
  season: Season;
  tournament_type: TournamentType;
  age_group: AgeGroup;
  division: Division;
}

export interface TeamRecord {
  team_id: number;
  league_id: number;
  csv_row_no: number | null;
  team_name: string;
  team_group: TeamGroup;
  leader: string | null;
  head_coach: string | null;
  manager: string | null;
  coaches: string | null;
  source_url: string;
  last_fetched_at: string | null;
}

export interface PlayerRecord {
  player_uid: string;
  site_player_id: number | null;
  masked_name: string;
  birth_ym: string | null;
}

export interface RosterRecord {
  team_id: number;
  player_uid: string;
  jersey_number: string | null;
  position: string | null;
}

export interface BattingStatRecord {
  team_id: number;
  player_uid: string;
  jersey_number: string | null;
  g: number;
  pa: number;
  ab: number;
  h: number;
  doubles: number;
  triples: number;
  hr: number;
  bb: number;
  so: number;
  rbi: number;
  r: number;
  sb_top10: number | null;
}

export interface PitchingStatRecord {
  team_id: number;
  player_uid: string;
  jersey_number: string | null;
  w: number;
  l: number;
  sv: number;
  g: number;
  /** 整數出局數（鐵則：嚴禁小數局數直接相加） */
  outs: number;
  ip_raw: string | null;
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

export interface MatchRecord {
  id: number;
  team_id: number;
  league_id: number;
  match_date: string | null;
  match_date_iso: string | null;
  match_time: string | null;
  away_team: string;
  home_team: string;
  away_score: number | null;
  home_score: number | null;
  /** 由 export_json.py 精準判定，避免海盜內戰勝負誤判 */
  pirate_side: "away" | "home" | null;
  opponent_team: string | null;
  venue: string | null;
  boxscore_url: string | null;
  pbp_url: string | null;
  summary_url: string | null;
}

export interface CareerBattingBenchmark {
  player_uid: string;
  pa: number;
  ab: number;
  h: number;
  calc_avg: number;
  calc_obp: number;
  calc_slg: number;
}

export interface CareerPitchingBenchmark {
  player_uid: string;
  total_outs: number;
  er: number;
  ip_baseball: string;
  calc_era_7: number | null;
  calc_era_9: number | null;
  calc_whip: number | null;
  calc_k7: number | null;
}

export interface PiratesDataset {
  schema_version: "1.0.0";
  meta: {
    generated_at: string;
    last_fetched_at: string | null;
    counts: {
      leagues: number;
      teams: number;
      players: number;
      matches: number;
    };
    filter_options: {
      age_groups: AgeGroup[];
      team_groups: TeamGroup[];
      years: number[];
      seasons: Season[];
      tournament_types: TournamentType[];
    };
  };
  leagues: LeagueRecord[];
  teams: TeamRecord[];
  players: PlayerRecord[];
  rosters: RosterRecord[];
  batting_stats: BattingStatRecord[];
  pitching_stats: PitchingStatRecord[];
  matches: MatchRecord[];
  benchmarks: {
    career_batting: CareerBattingBenchmark[];
    career_pitching: CareerPitchingBenchmark[];
  };
}

export interface FilterState {
  ageGroups: AgeGroup[];
  teamGroups: TeamGroup[];
  years: number[];
  seasons: Season[];
  tournamentTypes: TournamentType[];
}

export interface AppState {
  tab: TabId;
  eraInnings: EraInnings;
  filters: FilterState;
  selectedPlayerUid: string | null;
  selectedOpponent: string | null;
}

export interface TeamOverviewRow {
  teamId: number;
  leagueId: number;
  year: number;
  season: Season;
  ageGroup: AgeGroup;
  division: Division;
  teamGroup: TeamGroup;
  teamName: string;
  leagueTitle: string;
  headCoach: string | null;
  games: number;
  wins: number;
  losses: number;
  ties: number;
  winRate: number | null;
  runsScored: number;
  runsAllowed: number;
  runDiff: number;
  rosterCount: number;
}

export interface OverviewSummary {
  teamCount: number;
  totalMatches: number;
  wins: number;
  losses: number;
  ties: number;
  winRate: number | null;
  runsScored: number;
  runsAllowed: number;
  runDiff: number;
  teams: TeamOverviewRow[];
}

export interface BattingLeaderboardRow {
  playerUid: string;
  maskedName: string;
  birthYm: string | null;
  ageGroups: AgeGroup[];
  jerseyNumbers: string[];
  jerseyNumber: string | null;
  teamId: number | null;
  teamName: string | null;
  leagueTitle: string | null;
  year: number | null;
  season: Season | null;
  seasonsPlayed: number;
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
  totalBases: number;
  avg: number;
  obp: number;
  slg: number;
  ops: number;
}

export interface PitchingLeaderboardRow {
  playerUid: string;
  maskedName: string;
  birthYm: string | null;
  ageGroups: AgeGroup[];
  jerseyNumbers: string[];
  jerseyNumber: string | null;
  teamId: number | null;
  teamName: string | null;
  leagueTitle: string | null;
  year: number | null;
  season: Season | null;
  seasonsPitched: number;
  w: number;
  l: number;
  sv: number;
  g: number;
  outs: number;
  ipDisplay: string;
  ab: number;
  h: number;
  doubles: number;
  triples: number;
  hr: number;
  bb: number;
  so: number;
  r: number;
  er: number;
  era: number | null;
  whip: number | null;
  k7: number | null;
}

export type MatchOutcome = "W" | "L" | "T";

export interface MatchViewRow {
  id: number;
  teamId: number;
  leagueId: number;
  year: number;
  season: Season;
  ageGroup: AgeGroup;
  leagueTitle: string;
  teamName: string;
  matchDate: string | null;
  matchDateIso: string | null;
  matchTime: string | null;
  awayTeam: string;
  homeTeam: string;
  awayScore: number | null;
  homeScore: number | null;
  pirateSide: "away" | "home" | null;
  opponentTeam: string | null;
  myScore: number | null;
  oppScore: number | null;
  outcome: MatchOutcome | null;
  venue: string | null;
  boxscoreUrl: string | null;
  pbpUrl: string | null;
  summaryUrl: string | null;
}

export interface OpponentSummary {
  opponentName: string | null;
  availableOpponents: string[];
  totalMatches: number;
  completedMatches: number;
  wins: number;
  losses: number;
  ties: number;
  winRate: number | null;
  runsScored: number;
  runsAllowed: number;
  matches: MatchViewRow[];
}

export interface SelectablePlayer {
  playerUid: string;
  maskedName: string;
  birthYm: string | null;
  ageGroups: AgeGroup[];
  jerseyNumbers: string[];
  teamCount: number;
  hasBatting: boolean;
  hasPitching: boolean;
}
