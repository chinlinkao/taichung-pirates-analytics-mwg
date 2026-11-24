/**
 * 台中小海盜棒球數據庫 — 應用程式進入點 (main.ts)
 * - 讀取純靜態資料 ./data/pirates.json（零外部寫入）
 * - 協調 Header、FilterBar、Tabs 與四大戰情視圖
 * - 雙向同步 URL Query，支援分享連結直接定位球員成長曲線與 ERA 局數制
 */

import "./styles/reset.css";
import "./styles/tokens.css";
import "./styles/layout.css";
import "./styles/components.css";

import {
  countActiveFilterGroups,
  createFilterBar,
} from "./components/filter-bar";
import { createHeader } from "./components/header";
import { createTabs } from "./components/tabs";
import {
  computeOpponents,
  computeOverview,
  createDatasetIndex,
  getFilteredTeamIds,
} from "./core/filters";
import { AppStore } from "./core/store";
import type { AppState, PiratesDataset } from "./types/data";
import { createLeaderboardsView } from "./views/leaderboards";
import { createOpponentsView } from "./views/opponents";
import { createOverviewView } from "./views/overview";
import { createPlayerProfileView } from "./views/player-profile";

const DATA_URL = "./data/pirates.json";

function renderLoadingSkeleton(root: HTMLElement): void {
  const shell = document.createElement("div");
  shell.className = "skeleton-shell";
  shell.setAttribute("role", "status");
  shell.setAttribute("aria-busy", "true");
  shell.setAttribute("aria-label", "正在載入台中小海盜棒球數據...");

  const headerBar = document.createElement("div");
  headerBar.className = "skeleton-bar";

  const filterBar = document.createElement("div");
  filterBar.className = "skeleton-bar";

  const grid = document.createElement("div");
  grid.className = "skeleton-grid";
  for (let i = 0; i < 5; i += 1) {
    const card = document.createElement("div");
    card.className = "skeleton-card";
    grid.append(card);
  }

  const hint = document.createElement("p");
  hint.className = "brand-subtitle";
  hint.textContent = "正在載入台中小海盜棒球戰情數據...";

  shell.append(headerBar, filterBar, grid, hint);
  root.replaceChildren(shell);
}

function renderErrorBanner(
  root: HTMLElement,
  message: string,
  onRetry: () => void
): void {
  const main = document.createElement("main");
  main.id = "main-content";
  main.className = "app-main";

  const box = document.createElement("section");
  box.className = "status-banner section-stack";
  box.setAttribute("role", "alert");

  const title = document.createElement("h2");
  title.textContent = "無法載入戰情數據";

  const desc = document.createElement("p");
  desc.textContent = message;

  const actions = document.createElement("div");
  const retryBtn = document.createElement("button");
  retryBtn.type = "button";
  retryBtn.className = "btn btn-primary";
  retryBtn.textContent = "重新載入";
  retryBtn.addEventListener("click", onRetry);
  actions.append(retryBtn);

  box.append(title, desc, actions);
  main.append(box);
  root.replaceChildren(main);
}

async function fetchDataset(): Promise<PiratesDataset> {
  const response = await fetch(DATA_URL, { cache: "no-cache" });
  if (!response.ok) {
    throw new Error(`讀取資料檔案失敗（HTTP ${response.status}）`);
  }
  const data = (await response.json()) as PiratesDataset;
  if (!data || data.schema_version !== "1.0.0" || !Array.isArray(data.teams)) {
    throw new Error("資料格式不符或版本不相容");
  }
  return data;
}

function mountApp(root: HTMLElement, dataset: PiratesDataset): void {
  const index = createDatasetIndex(dataset);
  const store = new AppStore(dataset);

  const overviewView = createOverviewView();
  const leaderboardsView = createLeaderboardsView({
    onSelectPlayer: (playerUid) => {
      store.setState({
        tab: "player",
        selectedPlayerUid: playerUid,
      });
    },
  });
  const playerProfileView = createPlayerProfileView({
    onSelectPlayer: (playerUid) => {
      store.setState({ selectedPlayerUid: playerUid });
    },
  });
  const opponentsView = createOpponentsView({
    onSelectOpponent: (opponent) => {
      store.setState({ selectedOpponent: opponent });
    },
  });

  const initialState = store.getState();

  const filterBar = createFilterBar({
    filterOptions: dataset.meta.filter_options,
    initialFilters: initialState.filters,
    initialEraInnings: initialState.eraInnings,
    onFiltersChange: (nextFilters) => store.updateFilters(nextFilters),
    onEraChange: (nextEra) => store.setState({ eraInnings: nextEra }),
    onReset: () => store.resetFilters(),
  });

  const header = createHeader({
    lastFetchedAt: dataset.meta.last_fetched_at,
    activeFilterCount: countActiveFilterGroups(
      initialState.filters,
      dataset.meta.filter_options
    ),
    onOpenMobileFilters: () => filterBar.openDrawer(),
    onThemeChange: () => {
      playerProfileView.refreshTheme();
    },
  });

  const tabs = createTabs({
    initialTab: initialState.tab,
    onTabChange: (tabId) => {
      store.setState({ tab: tabId });
    },
  });

  tabs.getPanelContentElement("overview").append(overviewView.element);
  tabs.getPanelContentElement("leaderboards").append(leaderboardsView.element);
  tabs.getPanelContentElement("player").append(playerProfileView.element);
  tabs.getPanelContentElement("opponents").append(opponentsView.element);

  // 無符合隊伍時之防呆提示
  const emptyBanner = document.createElement("section");
  emptyBanner.className = "status-banner section-stack";
  emptyBanner.hidden = true;

  const emptyTitle = document.createElement("h2");
  emptyTitle.textContent = "目前篩選條件下無符合的球隊資料";

  const emptyDesc = document.createElement("p");
  emptyDesc.textContent =
    "請調整年齡層級、分隊群組、年度、賽季或賽制條件，或點擊下方按鈕恢復預設全選。";

  const emptyActionRow = document.createElement("div");
  const resetAllBtn = document.createElement("button");
  resetAllBtn.type = "button";
  resetAllBtn.className = "btn btn-primary";
  resetAllBtn.textContent = "恢復預設全選篩選";
  resetAllBtn.addEventListener("click", () => store.resetFilters());
  emptyActionRow.append(resetAllBtn);
  emptyBanner.append(emptyTitle, emptyDesc, emptyActionRow);

  const main = document.createElement("main");
  main.id = "main-content";
  main.className = "app-main";
  main.append(filterBar.desktopElement, emptyBanner, tabs.element);

  const footer = document.createElement("footer");
  footer.className = "site-footer";

  const footerLeft = document.createElement("span");
  const counts = dataset.meta.counts;
  footerLeft.textContent = `資料彙整：${counts.leagues} 項聯賽・${counts.teams} 支隊伍紀錄・${counts.players} 位登錄球員・${counts.matches} 場賽程`;

  const footerRight = document.createElement("span");
  footerRight.textContent =
    "純靜態資料檢視｜依 player_uid 跨屆歸戶・投球局數以整數出局數累積";

  footer.append(footerLeft, footerRight);

  root.replaceChildren(
    header.element,
    main,
    footer,
    filterBar.drawerElement
  );

  const renderState = (state: AppState): void => {
    const teamIds = getFilteredTeamIds(dataset, state.filters, index);
    const activeFilterCount = countActiveFilterGroups(
      state.filters,
      dataset.meta.filter_options
    );

    header.updateFilterBadge(activeFilterCount);
    filterBar.sync(state.filters, state.eraInnings, teamIds.size);
    tabs.setActiveTab(state.tab);

    const hasTeams = teamIds.size > 0;
    emptyBanner.hidden = hasTeams;
    tabs.element.hidden = !hasTeams;

    if (!hasTeams) {
      return;
    }

    // 僅更新當前可見頁籤以保持互動即時回應
    switch (state.tab) {
      case "overview":
        overviewView.update(computeOverview(dataset, teamIds, index));
        break;
      case "leaderboards":
        leaderboardsView.update(dataset, teamIds, state.eraInnings, index);
        break;
      case "player":
        playerProfileView.update(
          dataset,
          teamIds,
          state.selectedPlayerUid,
          state.eraInnings,
          index
        );
        break;
      case "opponents":
        opponentsView.update(
          computeOpponents(dataset, teamIds, state.selectedOpponent, index)
        );
        break;
      default:
        break;
    }
  };

  store.subscribe(renderState);
  renderState(store.getState());
}

async function bootstrap(): Promise<void> {
  const appRoot = document.getElementById("app");
  if (!appRoot) {
    return;
  }

  renderLoadingSkeleton(appRoot);
  try {
    const dataset = await fetchDataset();
    mountApp(appRoot, dataset);
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "載入靜態資料時發生未知錯誤。";
    renderErrorBanner(appRoot, message, () => {
      void bootstrap();
    });
  }
}

void bootstrap();
