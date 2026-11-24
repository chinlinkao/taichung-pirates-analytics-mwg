/**
 * 頂部標題列元件 (header.ts)
 * - 顯示海盜主題名稱與台灣時區 (Asia/Taipei) 最後更新時間
 * - 遵循 MWG `dark-mode-toggle` 兩態切換（系統預設 ↔ 使用者手動覆寫）
 * - 行動版提供篩選抽屜開啟按鈕與目前自訂篩選計數徽章
 */

const STORAGE_KEY = "color-scheme";

export type ResolvedTheme = "light" | "dark";

export interface HeaderOptions {
  lastFetchedAt: string | null;
  activeFilterCount: number;
  manualSyncUrl?: string;
  onOpenMobileFilters: () => void;
  onThemeChange?: (resolvedTheme: ResolvedTheme) => void;
}

export interface HeaderController {
  element: HTMLElement;
  updateFilterBadge: (count: number) => void;
  getResolvedTheme: () => ResolvedTheme;
  destroy: () => void;
}

/** 將 ISO 時間格式化為台灣時間 (Asia/Taipei) */
export function formatTaipeiTimestamp(isoString: string | null): string {
  if (!isoString) {
    return "未知";
  }
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) {
    return "未知";
  }
  return new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function readStoredScheme(): ResolvedTheme | null {
  try {
    const val = localStorage.getItem(STORAGE_KEY);
    return val === "light" || val === "dark" ? val : null;
  } catch {
    return null;
  }
}

function writeStoredScheme(value: ResolvedTheme | null): void {
  try {
    if (value === null) {
      localStorage.removeItem(STORAGE_KEY);
    } else {
      localStorage.setItem(STORAGE_KEY, value);
    }
  } catch {
    /* 瀏覽器限制 storage 寫入時靜默降級 */
  }
}

function getSystemScheme(): ResolvedTheme {
  if (typeof window === "undefined" || !window.matchMedia) {
    return "light";
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function setMetaColorScheme(content: "light dark" | ResolvedTheme): void {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="color-scheme"]');
  if (meta) {
    meta.setAttribute("content", content);
  }
}

export function createHeader(options: HeaderOptions): HeaderController {
  const header = document.createElement("header");
  header.className = "site-header";

  const inner = document.createElement("div");
  inner.className = "header-inner";

  // 左側品牌標題區
  const brandGroup = document.createElement("div");
  brandGroup.className = "brand-group";

  const logo = document.createElement("img");
  logo.className = "brand-logo";
  logo.src = "./favicon.svg";
  logo.alt = "台中小海盜隊徽";
  logo.width = 38;
  logo.height = 38;

  const titleWrap = document.createElement("div");
  titleWrap.className = "brand-title-wrap";

  const h1 = document.createElement("h1");
  h1.className = "brand-title";
  h1.textContent = "台中小海盜棒球數據庫";

  const subtitle = document.createElement("p");
  subtitle.className = "brand-subtitle";
  subtitle.textContent = "台中就是棒社區棒球聯賽 2024–2026";

  titleWrap.append(h1, subtitle);
  brandGroup.append(logo, titleWrap);

  // 右側工具區
  const actions = document.createElement("div");
  actions.className = "header-actions";

  const updatedChip = document.createElement("span");
  updatedChip.className = "last-updated-chip";
  updatedChip.title = "資料最後爬取時間（台灣時間 Asia/Taipei）";
  updatedChip.textContent = `更新：${formatTaipeiTimestamp(options.lastFetchedAt)}`;

  // 僅限本機開發環境顯示的手動更新捷徑（正式建置由 Tree-Shaking 完全移除）
  let syncLink: HTMLAnchorElement | null = null;
  if (options.manualSyncUrl) {
    syncLink = document.createElement("a");
    syncLink.className = "btn header-sync-link";
    syncLink.href = options.manualSyncUrl;
    syncLink.target = "_blank";
    syncLink.rel = "noopener noreferrer";
    syncLink.title = "僅限本地端顯示：前往 GitHub Actions 手動觸發賽事數據更新（另開新分頁）";
    syncLink.setAttribute(
      "aria-label",
      "手動更新賽事數據（前往 GitHub Actions，另開新分頁）"
    );

    const syncIcon = document.createElement("span");
    syncIcon.setAttribute("aria-hidden", "true");
    syncIcon.textContent = "🔄";

    const syncText = document.createElement("span");
    syncText.textContent = "手動更新";

    syncLink.append(syncIcon, syncText);
  }

  // 行動版篩選按鈕（含計數徽章）
  const mobileFilterBtn = document.createElement("button");
  mobileFilterBtn.type = "button";
  mobileFilterBtn.className = "btn mobile-filter-trigger";
  mobileFilterBtn.setAttribute("aria-haspopup", "dialog");

  const filterLabel = document.createElement("span");
  filterLabel.textContent = "篩選條件";

  const filterBadge = document.createElement("span");
  filterBadge.className = "badge-count";

  mobileFilterBtn.append(filterLabel, filterBadge);
  mobileFilterBtn.addEventListener("click", () => {
    options.onOpenMobileFilters();
  });

  // 兩態深淺色主題切換按鈕
  const themeBtn = document.createElement("button");
  themeBtn.type = "button";
  themeBtn.className = "btn theme-toggle";

  const lightSpan = document.createElement("span");
  lightSpan.className = "theme-label-light";
  lightSpan.innerHTML = `<span aria-hidden="true">🌙</span><span>深色模式</span>`;

  const darkSpan = document.createElement("span");
  darkSpan.className = "theme-label-dark";
  darkSpan.innerHTML = `<span aria-hidden="true">☀️</span><span>淺色模式</span>`;

  themeBtn.append(lightSpan, darkSpan);

  const mql = window.matchMedia("(prefers-color-scheme: dark)");

  const getResolvedTheme = (): ResolvedTheme => {
    const stored = readStoredScheme();
    return stored ?? getSystemScheme();
  };

  const syncThemeUi = (notify: boolean): void => {
    const stored = readStoredScheme();
    setMetaColorScheme(stored ?? "light dark");
    const resolved = stored ?? getSystemScheme();
    themeBtn.dataset.resolvedTheme = resolved;
    themeBtn.setAttribute(
      "aria-label",
      resolved === "dark" ? "切換至淺色主題" : "切換至深色主題"
    );
    if (notify) {
      options.onThemeChange?.(resolved);
    }
  };

  themeBtn.addEventListener("click", () => {
    const currentlyRendered = getResolvedTheme();
    const target: ResolvedTheme = currentlyRendered === "dark" ? "light" : "dark";
    const systemTheme = getSystemScheme();

    // 與系統設定相同時移除儲存值恢復「跟隨系統」，絕不在 matchMedia change 時清除
    if (target === systemTheme) {
      writeStoredScheme(null);
    } else {
      writeStoredScheme(target);
    }
    syncThemeUi(true);
  });

  const handleSystemChange = (): void => {
    // 僅在目前無手動覆寫時跟隨系統變動並通知圖表重繪
    if (readStoredScheme() === null) {
      syncThemeUi(true);
    }
  };

  const handleStorageSync = (event: StorageEvent): void => {
    if (event.key === STORAGE_KEY || event.key === null) {
      syncThemeUi(true);
    }
  };

  mql.addEventListener("change", handleSystemChange);
  window.addEventListener("storage", handleStorageSync);

  const updateFilterBadge = (count: number): void => {
    if (count > 0) {
      filterBadge.hidden = false;
      filterBadge.textContent = String(count);
      mobileFilterBtn.setAttribute("aria-label", `篩選條件（已套用 ${count} 項自訂篩選）`);
    } else {
      filterBadge.hidden = true;
      filterBadge.textContent = "";
      mobileFilterBtn.setAttribute("aria-label", "篩選條件（目前為全選）");
    }
  };

  updateFilterBadge(options.activeFilterCount);
  syncThemeUi(false);

  if (syncLink) {
    actions.append(updatedChip, syncLink, mobileFilterBtn, themeBtn);
  } else {
    actions.append(updatedChip, mobileFilterBtn, themeBtn);
  }
  inner.append(brandGroup, actions);
  header.append(inner);

  return {
    element: header,
    updateFilterBadge,
    getResolvedTheme,
    destroy: () => {
      mql.removeEventListener("change", handleSystemChange);
      window.removeEventListener("storage", handleStorageSync);
    },
  };
}
