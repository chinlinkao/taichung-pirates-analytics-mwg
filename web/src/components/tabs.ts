/**
 * 無障礙頁籤元件 (tabs.ts)
 * 嚴格遵循 MWG `tabs` 指南：
 * - role="tablist" / role="tab" / role="tabpanel"
 * - 頁籤使用 <button type="button">，不加多餘的 aria-controls
 * - 支援 ArrowLeft / ArrowRight / Home / End 鍵盤導覽與 scrollIntoView
 * - 面板切換設定 hidden 並確保未選中面板不可視
 */

import type { TabId } from "../types/data";

export interface TabDefinition {
  id: TabId;
  label: string;
}

export const MAIN_TABS: readonly TabDefinition[] = [
  { id: "overview", label: "📊 分隊戰情總覽" },
  { id: "leaderboards", label: "🏆 投打排行榜" },
  { id: "player", label: "👤 球員跨屆成長檔案" },
  { id: "opponents", label: "⚔️ 賽程戰果與對手分析" },
];

export interface TabsOptions {
  tabs?: readonly TabDefinition[];
  initialTab: TabId;
  ariaLabel?: string;
  onTabChange: (tabId: TabId) => void;
}

export interface TabsController {
  element: HTMLElement;
  setActiveTab: (tabId: TabId, focusTab?: boolean) => void;
  getPanelContentElement: (tabId: TabId) => HTMLElement;
}

export function createTabs(options: TabsOptions): TabsController {
  const defs = options.tabs ?? MAIN_TABS;
  let activeTabId: TabId = options.initialTab;

  const root = document.createElement("div");
  root.className = "tabs-root";

  const tablist = document.createElement("div");
  tablist.className = "tablist";
  tablist.setAttribute("role", "tablist");
  tablist.setAttribute("aria-label", options.ariaLabel ?? "戰情看板功能分頁");

  const buttons = new Map<TabId, HTMLButtonElement>();
  const panels = new Map<TabId, HTMLElement>();
  const panelInners = new Map<TabId, HTMLElement>();
  const orderedIds: TabId[] = defs.map((d) => d.id);

  const activate = (tabId: TabId, optionsArg?: { focus?: boolean; emit?: boolean }): void => {
    if (!buttons.has(tabId)) {
      return;
    }
    activeTabId = tabId;

    for (const id of orderedIds) {
      const btn = buttons.get(id);
      const panel = panels.get(id);
      const isSelected = id === tabId;

      if (btn) {
        btn.setAttribute("aria-selected", isSelected ? "true" : "false");
        btn.tabIndex = isSelected ? 0 : -1;
        if (isSelected) {
          btn.scrollIntoView({ block: "nearest", inline: "nearest" });
          if (optionsArg?.focus) {
            btn.focus();
          }
        }
      }

      if (panel) {
        panel.hidden = !isSelected;
      }
    }

    if (optionsArg?.emit) {
      options.onTabChange(tabId);
    }
  };

  for (const def of defs) {
    const tabBtnId = `tab-btn-${def.id}`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = tabBtnId;
    btn.className = "tab-btn";
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected", def.id === activeTabId ? "true" : "false");
    btn.tabIndex = def.id === activeTabId ? 0 : -1;
    btn.textContent = def.label;

    btn.addEventListener("click", () => {
      if (def.id !== activeTabId) {
        activate(def.id, { emit: true });
      }
    });

    btn.addEventListener("keydown", (event) => {
      const idx = orderedIds.indexOf(def.id);
      if (idx === -1) return;

      let nextIdx: number | null = null;
      switch (event.key) {
        case "ArrowRight":
          nextIdx = (idx + 1) % orderedIds.length;
          break;
        case "ArrowLeft":
          nextIdx = (idx - 1 + orderedIds.length) % orderedIds.length;
          break;
        case "Home":
          nextIdx = 0;
          break;
        case "End":
          nextIdx = orderedIds.length - 1;
          break;
        default:
          return;
      }

      event.preventDefault();
      const targetId = orderedIds[nextIdx];
      if (targetId) {
        activate(targetId, { focus: true, emit: true });
      }
    });

    buttons.set(def.id, btn);
    tablist.append(btn);

    const panel = document.createElement("div");
    panel.className = "tab-panel";
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tabBtnId);
    panel.hidden = def.id !== activeTabId;

    // 內距與排版放在內層容器，避免 hidden 狀態與外框衝突
    const inner = document.createElement("div");
    inner.className = "tab-panel-inner";
    panel.append(inner);

    panels.set(def.id, panel);
    panelInners.set(def.id, inner);
  }

  root.append(tablist);
  for (const id of orderedIds) {
    const panel = panels.get(id);
    if (panel) root.append(panel);
  }

  return {
    element: root,
    setActiveTab: (tabId, focusTab = false) => {
      activate(tabId, { focus: focusTab, emit: false });
    },
    getPanelContentElement: (tabId) => {
      const inner = panelInners.get(tabId);
      if (!inner) {
        throw new Error(`未知的頁籤識別碼: ${tabId}`);
      }
      return inner;
    },
  };
}
