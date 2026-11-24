/**
 * 瀏覽器端 CSV 匯出工具 (csv.ts)
 * 嚴格遵守 AGENTS.md：CSV 匯出一律加上 UTF-8 BOM (\uFEFF)，確保 Excel 開啟中文不亂碼。
 */

/** 將單一儲存格內容安全跳脫為 RFC 4180 CSV 格式 */
export function escapeCsvCell(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  const str = String(value);
  if (/[",\r\n]/.test(str)) {
    return `"${str.replaceAll('"', '""')}"`;
  }
  return str;
}

/** 組合含 UTF-8 BOM 的完整 CSV 字串 */
export function buildCsvContent(
  headers: readonly string[],
  rows: ReadonlyArray<ReadonlyArray<unknown>>
): string {
  const headerLine = headers.map((h) => escapeCsvCell(h)).join(",");
  const rowLines = rows.map((row) =>
    row.map((cell) => escapeCsvCell(cell)).join(",")
  );
  // 加上 UTF-8 BOM (utf-8-sig) 確保 Windows Excel 直接開啟不亂碼
  return `\uFEFF${[headerLine, ...rowLines].join("\r\n")}`;
}

/** 觸發瀏覽器下載 UTF-8 BOM CSV 檔案 */
export function downloadCsv(
  filename: string,
  headers: readonly string[],
  rows: ReadonlyArray<ReadonlyArray<unknown>>
): void {
  const csvContent = buildCsvContent(headers, rows);
  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);

  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.append(link);
  link.click();
  link.remove();

  // 延後釋放 Blob URL，確保瀏覽器下載任務已啟動
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 1000);
}
