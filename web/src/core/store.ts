/**
 * 全域應用狀態與 URL Query 雙向同步管理 (store.ts)
 * 支援分享網址格式，例如：?tab=player&uid=蔡〇宇_2015-11&era=7
 */

import {
  COACH_PITCH_UID,
  type AgeGroup,
  type AppState,
  type EraInnings,
  type FilterState,
  type PiratesDataset,
  type Season,
  type TabId,
  type TeamGroup,
  type TournamentType,
} from "../types/data";

const VALID_TABS: readonly TabId[] = [
  "overview",
  "leaderboards",
  "player",
  "opponents",
];

const DEFAULT_HIGHLIGHT_PLAYER_UID = "蔡〇宇_2015-11";

/** 取得資料集預設之全選篩選狀態 */
export function createDefaultFilters(dataset: PiratesDataset): FilterState {
  const opts = dataset.meta.filter_options;
  return {
    ageGroups: [...opts.age_groups],
    teamGroups: [...opts.team_groups],
    years: [...opts.years],
    seasons: [...opts.seasons],
    tournamentTypes: [...opts.tournament_types],
  };
}

/** 尋找預設選中球員（優先蔡〇宇_2015-11，否則第一位非教練球員） */
export function resolveDefaultPlayerUid(dataset: PiratesDataset): string | null {
  const hasDefault = dataset.players.some(
    (p) => p.player_uid === DEFAULT_HIGHLIGHT_PLAYER_UID
  );
  if (hasDefault) {
    return DEFAULT_HIGHLIGHT_PLAYER_UID;
  }
  const firstValid = dataset.players.find(
    (p) => p.player_uid !== COACH_PITCH_UID
  );
  return firstValid ? firstValid.player_uid : null;
}

/** 建立初始全域狀態 */
export function createDefaultState(dataset: PiratesDataset): AppState {
  return {
    tab: "overview",
    eraInnings: 7,
    filters: createDefaultFilters(dataset),
    selectedPlayerUid: resolveDefaultPlayerUid(dataset),
    selectedOpponent: null,
  };
}

function parseListParam<T extends string>(
  params: URLSearchParams,
  key: string,
  allowed: readonly T[]
): T[] {
  if (!params.has(key)) {
    return [...allowed];
  }
  const raw = params.get(key) ?? "";
  if (raw.trim() === "") {
    return [];
  }
  const allowedSet = new Set<string>(allowed);
  const picked = raw
    .split(",")
    .map((s) => s.trim())
    .filter((s): s is T => allowedSet.has(s));
  return [...new Set(picked)];
}

function parseYearParam(
  params: URLSearchParams,
  key: string,
  allowed: readonly number[]
): number[] {
  if (!params.has(key)) {
    return [...allowed];
  }
  const raw = params.get(key) ?? "";
  if (raw.trim() === "") {
    return [];
  }
  const allowedSet = new Set<number>(allowed);
  const picked = raw
    .split(",")
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isInteger(n) && allowedSet.has(n));
  return [...new Set(picked)].sort((a, b) => a - b);
}

/** 從 URL search 字串還原 AppState */
export function parseStateFromSearch(
  search: string,
  dataset: PiratesDataset
): AppState {
  const defaults = createDefaultState(dataset);
  const query = search.startsWith("?") ? search.slice(1) : search;
  const params = new URLSearchParams(query);
  const opts = dataset.meta.filter_options;

  const rawTab = params.get("tab");
  const tab: TabId = VALID_TABS.includes(rawTab as TabId)
    ? (rawTab as TabId)
    : defaults.tab;

  const rawEra = params.get("era");
  const eraInnings: EraInnings = rawEra === "9" ? 9 : 7;

  const rawUid = params.get("uid");
  let selectedPlayerUid = defaults.selectedPlayerUid;
  if (rawUid && rawUid !== COACH_PITCH_UID) {
    const exists = dataset.players.some((p) => p.player_uid === rawUid);
    if (exists) {
      selectedPlayerUid = rawUid;
    }
  }

  const rawOpp = params.get("opp");
  const selectedOpponent =
    rawOpp && rawOpp.trim() !== "" ? rawOpp.trim() : null;

  const filters: FilterState = {
    ageGroups: parseListParam<AgeGroup>(params, "age", opts.age_groups),
    teamGroups: parseListParam<TeamGroup>(params, "group", opts.team_groups),
    years: parseYearParam(params, "year", opts.years),
    seasons: parseListParam<Season>(params, "season", opts.seasons),
    tournamentTypes: parseListParam<TournamentType>(
      params,
      "tourney",
      opts.tournament_types
    ),
  };

  return {
    tab,
    eraInnings,
    filters,
    selectedPlayerUid,
    selectedOpponent,
  };
}

function sameStringSet(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false;
  const setB = new Set(b);
  return a.every((v) => setB.has(v));
}

function sameNumberSet(a: readonly number[], b: readonly number[]): boolean {
  if (a.length !== b.length) return false;
  const setB = new Set(b);
  return a.every((v) => setB.has(v));
}

/** 將 AppState 序列化為精簡的 URL Query String（含前導 '?'；全預設則回傳 ''） */
export function serializeStateToSearch(
  state: AppState,
  dataset: PiratesDataset
): string {
  const params = new URLSearchParams();
  const opts = dataset.meta.filter_options;

  if (state.tab !== "overview") {
    params.set("tab", state.tab);
  }
  if (state.tab === "player" && state.selectedPlayerUid) {
    params.set("uid", state.selectedPlayerUid);
  }
  if (state.eraInnings !== 7 || state.tab === "player") {
    params.set("era", String(state.eraInnings));
  }
  if (state.tab === "opponents" && state.selectedOpponent) {
    params.set("opp", state.selectedOpponent);
  }

  if (!sameStringSet(state.filters.ageGroups, opts.age_groups)) {
    params.set("age", state.filters.ageGroups.join(","));
  }
  if (!sameStringSet(state.filters.teamGroups, opts.team_groups)) {
    params.set("group", state.filters.teamGroups.join(","));
  }
  if (!sameNumberSet(state.filters.years, opts.years)) {
    params.set("year", state.filters.years.join(","));
  }
  if (!sameStringSet(state.filters.seasons, opts.seasons)) {
    params.set("season", state.filters.seasons.join(","));
  }
  if (!sameStringSet(state.filters.tournamentTypes, opts.tournament_types)) {
    params.set("tourney", state.filters.tournamentTypes.join(","));
  }

  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

export type StateListener = (state: AppState) => void;

/** 輕量響應式全域狀態儲存庫（自動雙向同步 URL Query） */
export class AppStore {
  private state: AppState;
  private readonly dataset: PiratesDataset;
  private readonly listeners = new Set<StateListener>();
  private readonly syncUrl: boolean;

  constructor(
    dataset: PiratesDataset,
    initialSearch?: string,
    syncUrl = typeof window !== "undefined"
  ) {
    this.dataset = dataset;
    this.syncUrl = syncUrl;
    const search =
      initialSearch ??
      (typeof window !== "undefined" ? window.location.search : "");
    this.state = parseStateFromSearch(search, dataset);

    if (this.syncUrl && typeof window !== "undefined") {
      window.addEventListener("popstate", this.handlePopState);
    }
  }

  private readonly handlePopState = (): void => {
    this.state = parseStateFromSearch(window.location.search, this.dataset);
    this.emit();
  };

  getState(): AppState {
    return this.state;
  }

  setState(updater: Partial<AppState> | ((prev: AppState) => AppState)): void {
    const next =
      typeof updater === "function"
        ? updater(this.state)
        : { ...this.state, ...updater };
    this.state = next;

    if (this.syncUrl && typeof window !== "undefined") {
      const nextSearch = serializeStateToSearch(this.state, this.dataset);
      const nextUrl = `${window.location.pathname}${nextSearch}${window.location.hash}`;
      const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      if (nextUrl !== currentUrl) {
        window.history.replaceState(null, "", nextUrl);
      }
    }
    this.emit();
  }

  updateFilters(partial: Partial<FilterState>): void {
    this.setState((prev) => ({
      ...prev,
      filters: { ...prev.filters, ...partial },
    }));
  }

  resetFilters(): void {
    this.setState((prev) => ({
      ...prev,
      filters: createDefaultFilters(this.dataset),
    }));
  }

  subscribe(listener: StateListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  destroy(): void {
    if (this.syncUrl && typeof window !== "undefined") {
      window.removeEventListener("popstate", this.handlePopState);
    }
    this.listeners.clear();
  }

  private emit(): void {
    for (const listener of this.listeners) {
      listener(this.state);
    }
  }
}
