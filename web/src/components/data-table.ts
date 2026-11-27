/**
 * 可排序響應式數據表元件 (data-table.ts)
 * 嚴格遵循 MWG `sortable-table` 與 `responsive-table` 規範：
 * - <th scope="col"> 內嵌 <button type="button" commandfor="..." command="--sort">
 * - 僅於當前排序 <th> 設定 aria-sort，其餘欄位移除 aria-sort
 * - 數值與 IP 排序一律透過 data-value 讀取原始數值（IP 傳入整數 outs），null 永遠排後
 * - 排序移動既有 <tr>（優先 moveBefore，降級 appendChild），不重建 <tbody>
 * - 單一 role="status" 播報排序狀態，不在 <table> 上加 aria-live
 */

export type SortDirection = "ascending" | "descending";

export interface TableColumn<T> {
  key: string;
  label: string;
  type: "string" | "number";
  sortable?: boolean;
  /** 設為 true 時，該欄於 tbody 使用 <th scope="row"> 並於窄螢幕固定左側 */
  stickyRowHeader?: boolean;
  align?: "start" | "end";
  /** 供排序用之原始值（如 IP 務必傳回整數 outs）；未提供則取 renderCell 純文字 */
  sortValue?: (row: T) => string | number | null;
  renderCell: (row: T) => string | Node;
}

export interface DataTableOptions<T> {
  caption: string;
  columns: readonly TableColumn<T>[];
  rows: readonly T[];
  emptyMessage?: string;
  initialSort?: { key: string; direction: SortDirection };
}

export interface DataTableController<T> {
  element: HTMLElement;
  updateCaption: (nextCaption: string) => void;
  updateRows: (nextRows: readonly T[]) => void;
}

interface InvokerCommandEvent extends Event {
  readonly command: string;
  readonly source: Element | null;
}

type MoveBeforeContainer = HTMLElement & {
  moveBefore?: (node: Node, child: Node | null) => void;
};

const textCollator = new Intl.Collator("zh-Hant", {
  numeric: true,
  sensitivity: "base",
});

let nextTableUid = 1;

function moveRowsInDom(
  tbody: HTMLTableSectionElement,
  orderedRows: HTMLTableRowElement[]
): void {
  const container = tbody as MoveBeforeContainer;
  if (typeof container.moveBefore === "function") {
    for (const row of orderedRows) {
      container.moveBefore(row, null);
    }
    return;
  }
  for (const row of orderedRows) {
    tbody.appendChild(row);
  }
}

export function createDataTable<T>(
  options: DataTableOptions<T>
): DataTableController<T> {
  const uid = nextTableUid++;
  const tableId = `pirates-table-${uid}`;
  const captionId = `pirates-table-caption-${uid}`;

  const card = document.createElement("div");
  card.className = "table-card";

  const wrapper = document.createElement("div");
  wrapper.className = "table-wrapper";
  wrapper.tabIndex = 0;
  wrapper.setAttribute("role", "region");
  wrapper.setAttribute("aria-labelledby", captionId);

  const table = document.createElement("table");
  table.id = tableId;
  table.className = "data-table";

  const caption = document.createElement("caption");
  caption.id = captionId;
  caption.textContent = options.caption;

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  const headerCells: HTMLTableCellElement[] = [];

  for (const col of options.columns) {
    const th = document.createElement("th");
    th.scope = "col";
    th.dataset.key = col.key;
    th.dataset.type = col.type;

    if (col.align === "end") {
      th.classList.add("align-end");
    }
    if (col.stickyRowHeader) {
      th.classList.add("is-sticky-col");
    }

    const sortable = col.sortable !== false;
    if (sortable) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sort-btn";
      if (col.align === "end") {
        btn.classList.add("align-end");
      }
      btn.setAttribute("commandfor", tableId);
      btn.setAttribute("command", "--sort");

      const textSpan = document.createElement("span");
      textSpan.textContent = col.label;

      const iconSpan = document.createElement("span");
      iconSpan.className = "sort-icon";
      iconSpan.setAttribute("aria-hidden", "true");

      btn.append(textSpan, iconSpan);
      th.append(btn);
    } else {
      th.textContent = col.label;
    }

    headerCells.push(th);
    headRow.append(th);
  }

  thead.append(headRow);

  const tbody = document.createElement("tbody");
  const statusEl = document.createElement("div");
  statusEl.className = "sr-only";
  statusEl.setAttribute("role", "status");

  const emptyBox = document.createElement("p");
  emptyBox.className = "status-banner";
  emptyBox.textContent = options.emptyMessage ?? "目前篩選條件下無符合的資料。";
  emptyBox.hidden = true;

  let currentSort: { key: string; direction: SortDirection } | null =
    options.initialSort ?? null;

  const sortRowsDom = (
    colIndex: number,
    colType: "string" | "number",
    direction: SortDirection
  ): number => {
    const rows = Array.from(tbody.rows);
    const factor = direction === "ascending" ? 1 : -1;

    rows.sort((rowA, rowB) => {
      const cellA = rowA.cells[colIndex];
      const cellB = rowB.cells[colIndex];
      const nullA = !cellA || cellA.dataset.null === "1";
      const nullB = !cellB || cellB.dataset.null === "1";

      // 缺值 / '-' 無論升冪或降冪皆固定排在末尾
      if (nullA && nullB) return 0;
      if (nullA) return 1;
      if (nullB) return -1;

      const rawA = cellA.dataset.value ?? cellA.textContent?.trim() ?? "";
      const rawB = cellB.dataset.value ?? cellB.textContent?.trim() ?? "";

      if (colType === "number") {
        const numA = Number(rawA);
        const numB = Number(rawB);
        if (Number.isNaN(numA) && Number.isNaN(numB)) return 0;
        if (Number.isNaN(numA)) return 1;
        if (Number.isNaN(numB)) return -1;
        return factor * (numA - numB);
      }

      return factor * textCollator.compare(rawA, rawB);
    });

    moveRowsInDom(tbody, rows);
    return rows.length;
  };

  const applySortFromHeader = (th: HTMLTableCellElement, announce: boolean): void => {
    const key = th.dataset.key ?? "";
    const colType = th.dataset.type === "number" ? "number" : "string";
    const prevSort = th.getAttribute("aria-sort");
    const direction: SortDirection =
      prevSort === "ascending" ? "descending" : "ascending";

    for (const header of headerCells) {
      header.removeAttribute("aria-sort");
    }
    th.setAttribute("aria-sort", direction);
    currentSort = { key, direction };

    const rowCount = sortRowsDom(th.cellIndex, colType, direction);
    if (announce) {
      const colName = th.textContent?.trim() ?? key;
      const dirLabel = direction === "ascending" ? "升冪" : "降冪";
      statusEl.textContent = `已依「${colName}」${dirLabel}排序，共 ${rowCount} 筆`;
    }
  };

  // 使用 MWG Invoker Commands（--sort）並附帶舊瀏覽器 click fallback
  table.addEventListener("command", (event) => {
    const cmdEvent = event as InvokerCommandEvent;
    if (cmdEvent.command !== "--sort" || !cmdEvent.source) {
      return;
    }
    const th = cmdEvent.source.closest("th");
    if (th && th instanceof HTMLTableCellElement) {
      applySortFromHeader(th, true);
    }
  });

  if (!("commandForElement" in HTMLButtonElement.prototype)) {
    for (const btn of thead.querySelectorAll<HTMLButtonElement>(
      'button[command="--sort"]'
    )) {
      btn.addEventListener("click", () => {
        const th = btn.closest("th");
        if (th && th instanceof HTMLTableCellElement) {
          applySortFromHeader(th, true);
        }
      });
    }
  }

  const renderBody = (rowsData: readonly T[]): void => {
    tbody.replaceChildren();
    if (rowsData.length === 0) {
      wrapper.hidden = true;
      emptyBox.hidden = false;
      return;
    }

    wrapper.hidden = false;
    emptyBox.hidden = true;

    const fragment = document.createDocumentFragment();
    for (const rowItem of rowsData) {
      const tr = document.createElement("tr");

      for (const col of options.columns) {
        const cell = col.stickyRowHeader
          ? document.createElement("th")
          : document.createElement("td");

        if (col.stickyRowHeader && cell instanceof HTMLTableCellElement) {
          cell.scope = "row";
          cell.classList.add("is-sticky-col");
        }
        if (col.align === "end") {
          cell.classList.add("align-end");
        }

        const rendered = col.renderCell(rowItem);
        if (typeof rendered === "string") {
          cell.textContent = rendered;
        } else {
          cell.append(rendered);
        }

        if (col.sortValue) {
          const val = col.sortValue(rowItem);
          if (val === null || (typeof val === "number" && Number.isNaN(val))) {
            cell.dataset.null = "1";
          } else {
            cell.dataset.value = String(val);
          }
        }

        tr.append(cell);
      }

      fragment.append(tr);
    }

    tbody.append(fragment);

    // 套用目前選定的排序狀態（若有）
    if (currentSort) {
      const targetTh = headerCells.find((h) => h.dataset.key === currentSort?.key);
      if (targetTh) {
        for (const header of headerCells) {
          header.removeAttribute("aria-sort");
        }
        targetTh.setAttribute("aria-sort", currentSort.direction);
        const colType = targetTh.dataset.type === "number" ? "number" : "string";
        sortRowsDom(targetTh.cellIndex, colType, currentSort.direction);
      }
    }
  };

  table.append(caption, thead, tbody);
  wrapper.append(table);
  card.append(wrapper, emptyBox, statusEl);

  renderBody(options.rows);

  return {
    element: card,
    updateCaption: (nextCaption) => {
      caption.textContent = nextCaption;
    },
    updateRows: (nextRows) => {
      renderBody(nextRows);
    },
  };
}
