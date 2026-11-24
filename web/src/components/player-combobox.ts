/**
 * 球員搜尋選單元件 (player-combobox.ts)
 * - 支援以遮罩姓名、出生年月、曾穿背號即時過濾
 * - 正確處理 IME 組字事件 (compositionstart / compositionend / isComposing)，防止注音選字誤觸
 * - 嚴格綁定 player_uid，避免同隊同名選手混淆
 * - 支援 ArrowUp / ArrowDown / Enter / Escape 完整鍵盤操作
 */

import type { SelectablePlayer } from "../types/data";

export interface PlayerComboboxOptions {
  players: readonly SelectablePlayer[];
  selectedPlayerUid: string | null;
  label?: string;
  onSelect: (playerUid: string) => void;
}

export interface PlayerComboboxController {
  element: HTMLElement;
  updatePlayers: (
    nextPlayers: readonly SelectablePlayer[],
    selectedPlayerUid: string | null
  ) => void;
}

let nextComboboxUid = 1;

/** 格式化球員選項顯示文字：{maskedName}（{birthYm}）｜{ageGroups} */
export function formatPlayerOptionLabel(player: SelectablePlayer): string {
  const birth = player.birthYm ?? "生日未知";
  const ages =
    player.ageGroups.length > 0 ? player.ageGroups.join("/") : "未分組";
  const jerseys =
    player.jerseyNumbers.length > 0
      ? `｜#${player.jerseyNumbers.join("/")}`
      : "";
  return `${player.maskedName}（${birth}）｜${ages}${jerseys}`;
}

export function createPlayerCombobox(
  options: PlayerComboboxOptions
): PlayerComboboxController {
  const uid = nextComboboxUid++;
  const inputId = `player-combobox-input-${uid}`;
  const listboxId = `player-combobox-list-${uid}`;

  const wrap = document.createElement("div");
  wrap.className = "combobox-wrap";

  const label = document.createElement("label");
  label.className = "combobox-label";
  label.htmlFor = inputId;
  label.textContent = options.label ?? "選擇球員（可輸入姓名、出生年月或背號搜尋）";

  const inputRow = document.createElement("div");
  inputRow.className = "combobox-input-row";

  const input = document.createElement("input");
  input.id = inputId;
  input.type = "text";
  input.className = "combobox-input";
  input.placeholder = "輸入姓名、出生年月（如 2015-11）或背號...";
  input.autocomplete = "off";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", listboxId);

  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "combobox-toggle-btn";
  toggleBtn.tabIndex = -1;
  toggleBtn.setAttribute("aria-label", "展開或收合球員清單");
  toggleBtn.textContent = "▾";

  const listbox = document.createElement("ul");
  listbox.id = listboxId;
  listbox.className = "combobox-listbox";
  listbox.setAttribute("role", "listbox");
  listbox.hidden = true;

  inputRow.append(input, toggleBtn);
  wrap.append(label, inputRow, listbox);

  let allPlayers: readonly SelectablePlayer[] = options.players;
  let filteredPlayers: SelectablePlayer[] = [...allPlayers];
  let selectedUid: string | null = options.selectedPlayerUid;
  let activeIndex = -1;
  let isOpen = false;
  let isComposing = false;

  const getSelectedPlayer = (): SelectablePlayer | undefined =>
    allPlayers.find((p) => p.playerUid === selectedUid);

  const syncInputToSelected = (): void => {
    const found = getSelectedPlayer();
    input.value = found ? formatPlayerOptionLabel(found) : "";
  };

  const setOpen = (open: boolean): void => {
    isOpen = open;
    listbox.hidden = !open;
    input.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) {
      activeIndex = -1;
      input.removeAttribute("aria-activedescendant");
    }
  };

  const updateActiveDescendant = (): void => {
    const items = listbox.querySelectorAll<HTMLLIElement>('[role="option"]');
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (!item) continue;
      const active = i === activeIndex;
      item.classList.toggle("is-active", active);
      if (active) {
        input.setAttribute("aria-activedescendant", item.id);
        item.scrollIntoView({ block: "nearest" });
      }
    }
    if (activeIndex < 0) {
      input.removeAttribute("aria-activedescendant");
    }
  };

  const choosePlayer = (player: SelectablePlayer): void => {
    selectedUid = player.playerUid;
    input.value = formatPlayerOptionLabel(player);
    setOpen(false);
    options.onSelect(player.playerUid);
  };

  const renderList = (): void => {
    listbox.replaceChildren();
    if (filteredPlayers.length === 0) {
      const emptyLi = document.createElement("li");
      emptyLi.className = "combobox-empty";
      emptyLi.textContent = "找不到符合的球員";
      listbox.append(emptyLi);
      activeIndex = -1;
      input.removeAttribute("aria-activedescendant");
      return;
    }

    const fragment = document.createDocumentFragment();
    filteredPlayers.forEach((player, idx) => {
      const li = document.createElement("li");
      li.id = `${listboxId}-opt-${idx}`;
      li.className = "combobox-option";
      li.setAttribute("role", "option");
      li.dataset.uid = player.playerUid;
      li.setAttribute(
        "aria-selected",
        player.playerUid === selectedUid ? "true" : "false"
      );
      li.textContent = formatPlayerOptionLabel(player);

      // 使用 mousedown 在 blur 前完成選取
      li.addEventListener("mousedown", (event) => {
        event.preventDefault();
        choosePlayer(player);
      });

      fragment.append(li);
    });

    listbox.append(fragment);
    const currentSelectedIdx = filteredPlayers.findIndex(
      (p) => p.playerUid === selectedUid
    );
    activeIndex = currentSelectedIdx >= 0 ? currentSelectedIdx : 0;
    updateActiveDescendant();
  };

  const applyFilter = (query: string): void => {
    const q = query.trim().toLowerCase();
    if (q === "") {
      filteredPlayers = [...allPlayers];
    } else {
      filteredPlayers = allPlayers.filter((p) => {
        const labelText = formatPlayerOptionLabel(p).toLowerCase();
        return (
          labelText.includes(q) ||
          p.playerUid.toLowerCase().includes(q) ||
          p.jerseyNumbers.some((j) => j.toLowerCase().includes(q))
        );
      });
    }
    renderList();
  };

  // IME 組字事件防護：中文輸入選字期間不重繪清單或觸發確認
  input.addEventListener("compositionstart", () => {
    isComposing = true;
  });

  input.addEventListener("compositionend", () => {
    isComposing = false;
    setOpen(true);
    applyFilter(input.value);
  });

  input.addEventListener("input", (event) => {
    const inputEvent = event as InputEvent;
    if (isComposing || inputEvent.isComposing) {
      return;
    }
    setOpen(true);
    applyFilter(input.value);
  });

  input.addEventListener("focus", () => {
    // 聚焦時全選文字並顯示完整清單，方便直接輸入新關鍵字
    input.select();
    filteredPlayers = [...allPlayers];
    setOpen(true);
    renderList();
  });

  input.addEventListener("blur", () => {
    setTimeout(() => {
      if (!wrap.contains(document.activeElement)) {
        setOpen(false);
        syncInputToSelected();
      }
    }, 120);
  });

  input.addEventListener("keydown", (event) => {
    if (isComposing || event.isComposing) {
      return;
    }

    switch (event.key) {
      case "ArrowDown": {
        event.preventDefault();
        if (!isOpen) {
          filteredPlayers = [...allPlayers];
          setOpen(true);
          renderList();
          return;
        }
        if (filteredPlayers.length > 0) {
          activeIndex = (activeIndex + 1) % filteredPlayers.length;
          updateActiveDescendant();
        }
        break;
      }
      case "ArrowUp": {
        event.preventDefault();
        if (!isOpen) {
          filteredPlayers = [...allPlayers];
          setOpen(true);
          renderList();
          return;
        }
        if (filteredPlayers.length > 0) {
          activeIndex =
            (activeIndex - 1 + filteredPlayers.length) % filteredPlayers.length;
          updateActiveDescendant();
        }
        break;
      }
      case "Enter": {
        if (!isOpen) return;
        event.preventDefault();
        const candidate =
          filteredPlayers[activeIndex] ?? filteredPlayers[0];
        if (candidate) {
          choosePlayer(candidate);
        }
        break;
      }
      case "Escape": {
        if (!isOpen) return;
        event.preventDefault();
        setOpen(false);
        syncInputToSelected();
        break;
      }
      default:
        break;
    }
  });

  toggleBtn.addEventListener("mousedown", (event) => {
    event.preventDefault();
    if (isOpen) {
      setOpen(false);
      syncInputToSelected();
    } else {
      input.focus();
    }
  });

  syncInputToSelected();

  return {
    element: wrap,
    updatePlayers: (nextPlayers, nextSelectedUid) => {
      allPlayers = nextPlayers;
      selectedUid = nextSelectedUid;
      filteredPlayers = [...allPlayers];
      if (!isOpen) {
        syncInputToSelected();
      } else {
        renderList();
      }
    },
  };
}
