/**
 * Chart.js 精簡樹搖封裝元件 (charts.ts)
 * - 僅按需註冊折線圖所需模組，極小化打包體積
 * - 主動同步 CSS 亮／暗色主題與 ERA 切換更新，不閃爍重建
 */

import {
  CategoryScale,
  Chart,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  Title,
  Tooltip,
  type ChartConfiguration,
} from "chart.js";

Chart.register(
  LineController,
  LineElement,
  PointElement,
  LinearScale,
  CategoryScale,
  Tooltip,
  Legend,
  Title
);

export interface TrendSeries {
  label: string;
  data: Array<number | null>;
  colorLight: string;
  colorDark: string;
}

export interface TrendChartConfig {
  labels: string[];
  series: TrendSeries[];
  yAxisTitle?: string;
  decimals?: number;
}

export interface TrendChartController {
  update: (nextConfig: TrendChartConfig) => void;
  refreshTheme: () => void;
  destroy: () => void;
}

interface ThemePalette {
  isDark: boolean;
  text: string;
  muted: string;
  grid: string;
  surface: string;
}

function getCurrentPalette(): ThemePalette {
  const metaContent = document
    .querySelector<HTMLMetaElement>('meta[name="color-scheme"]')
    ?.getAttribute("content");
  const systemDark =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const isDark =
    metaContent === "dark" || (metaContent !== "light" && Boolean(systemDark));

  const rootStyle = getComputedStyle(document.documentElement);
  const text =
    rootStyle.getPropertyValue("--color-text").trim() ||
    (isDark ? "#f1f4f9" : "#141821");
  const muted =
    rootStyle.getPropertyValue("--color-text-muted").trim() ||
    (isDark ? "#a6b1c2" : "#4b5565");
  const border =
    rootStyle.getPropertyValue("--color-border").trim() ||
    (isDark ? "#2c3545" : "#d5dce6");
  const surface =
    rootStyle.getPropertyValue("--color-surface").trim() ||
    (isDark ? "#161b24" : "#ffffff");

  return { isDark, text, muted, grid: border, surface };
}

export function createTrendChart(
  canvas: HTMLCanvasElement,
  initialConfig: TrendChartConfig
): TrendChartController {
  let latestConfig = initialConfig;

  const buildDatasets = (palette: ThemePalette) =>
    latestConfig.series.map((s) => {
      const stroke = palette.isDark ? s.colorDark : s.colorLight;
      return {
        label: s.label,
        data: s.data,
        borderColor: stroke,
        backgroundColor: stroke,
        pointBackgroundColor: stroke,
        pointBorderColor: palette.surface,
        pointBorderWidth: 1.5,
        pointRadius: 4,
        pointHoverRadius: 6,
        borderWidth: 2.5,
        tension: 0.25,
        spanGaps: false,
      };
    });

  const palette = getCurrentPalette();
  const decimals = latestConfig.decimals ?? 3;

  const chartConfig: ChartConfiguration<"line", Array<number | null>, string> = {
    type: "line",
    data: {
      labels: latestConfig.labels,
      datasets: buildDatasets(palette),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 200 },
      interaction: {
        mode: "index",
        intersect: false,
      },
      plugins: {
        legend: {
          position: "top",
          labels: {
            color: palette.text,
            usePointStyle: true,
            boxWidth: 10,
          },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const raw = ctx.parsed.y;
              const formatted =
                raw === null || Number.isNaN(raw)
                  ? "-"
                  : raw.toFixed(decimals);
              return `${ctx.dataset.label ?? ""}: ${formatted}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: palette.muted },
          grid: { color: palette.grid },
        },
        y: {
          beginAtZero: true,
          title: {
            display: Boolean(latestConfig.yAxisTitle),
            text: latestConfig.yAxisTitle ?? "",
            color: palette.muted,
          },
          ticks: {
            color: palette.muted,
            callback: (value) => Number(value).toFixed(decimals),
          },
          grid: { color: palette.grid },
        },
      },
    },
  };

  const chart = new Chart(canvas, chartConfig);

  const applyThemeAndData = (useAnimation: boolean): void => {
    const nextPalette = getCurrentPalette();
    const dec = latestConfig.decimals ?? 3;

    chart.data.labels = latestConfig.labels;
    chart.data.datasets = buildDatasets(nextPalette);

    if (chart.options.plugins?.legend?.labels) {
      chart.options.plugins.legend.labels.color = nextPalette.text;
    }
    if (chart.options.scales?.x) {
      if (chart.options.scales.x.ticks) {
        chart.options.scales.x.ticks.color = nextPalette.muted;
      }
      if (chart.options.scales.x.grid) {
        chart.options.scales.x.grid.color = nextPalette.grid;
      }
    }
    if (chart.options.scales?.y) {
      if (chart.options.scales.y.ticks) {
        chart.options.scales.y.ticks.color = nextPalette.muted;
        chart.options.scales.y.ticks.callback = (val) => Number(val).toFixed(dec);
      }
      if (chart.options.scales.y.grid) {
        chart.options.scales.y.grid.color = nextPalette.grid;
      }
      const yScale = chart.options.scales.y as {
        title?: { display?: boolean; text?: string; color?: string };
      };
      if (yScale.title) {
        yScale.title.display = Boolean(latestConfig.yAxisTitle);
        yScale.title.text = latestConfig.yAxisTitle ?? "";
        yScale.title.color = nextPalette.muted;
      }
    }

    chart.update(useAnimation ? undefined : "none");
  };

  return {
    update: (nextConfig) => {
      latestConfig = nextConfig;
      applyThemeAndData(false);
    },
    refreshTheme: () => {
      applyThemeAndData(false);
    },
    destroy: () => {
      chart.destroy();
    },
  };
}
