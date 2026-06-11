import { Component, useMemo, type ReactNode } from "react";
import styles from "../index.module.less";

/* eslint-disable @typescript-eslint/no-explicit-any */

import { Line, Column, Pie, Area, Bar, Scatter, Radar, Gauge } from "@ant-design/plots";

const CHART_MAP: Record<string, React.FC<any>> = {
  line: Line,
  column: Column,
  bar: Bar,
  pie: Pie,
  area: Area,
  scatter: Scatter,
  radar: Radar,
  gauge: Gauge,
};

// Charts that don't use x/y axes
const NO_XY_CHARTS = new Set(["pie", "gauge"]);

interface ChartBlockProps {
  block: {
    chartType?: string;
    data?: any[] | number; // array for most charts, number for gauge
    config?: Record<string, any>;
    title?: string;
    height?: number;
    xField?: string;
    yField?: string;
    colorField?: string;
    angleField?: string;
  };
}

/**
 * Auto-detect x/y field names from the first valid data row.
 * Heuristic: first string-valued key → x, first number-valued key → y.
 * Falls back to first two keys if types don't differentiate.
 */
function guessFields(data: any[]): { x?: string; y?: string } {
  // Find first valid row
  const row = data?.find((r) => r && typeof r === "object" && Object.keys(r).length > 0);
  if (!row) return {};

  const keys = Object.keys(row);
  if (keys.length === 0) return {};

  // If only one key, use it for both (degenerate case)
  if (keys.length === 1) return { x: keys[0], y: keys[0] };

  let x: string | undefined;
  let y: string | undefined;

  for (const k of keys) {
    const v = row[k];
    if (v == null) continue; // skip null/undefined values
    if (!x && typeof v === "string") x = k;
    if (!y && typeof v === "number") y = k;
    if (x && y) break;
  }

  // Fallback: use first two keys regardless of type
  if (!x) x = keys.find((k) => k !== y) ?? keys[0];
  if (!y) y = keys.find((k) => k !== x) ?? keys[1] ?? keys[0];

  return { x, y };
}

/**
 * Set a key on obj only if value is a non-empty string.
 */
function setIfValid(obj: Record<string, any>, key: string, ...candidates: (string | undefined)[]) {
  for (const v of candidates) {
    if (typeof v === "string" && v.length > 0) {
      obj[key] = v;
      return;
    }
  }
  // Don't set the key at all — @ant-design/plots treats missing vs undefined differently
}

/** Error boundary to prevent chart crashes from breaking the whole UI */
class ChartErrorBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(error: Error) {
    return { error: error.message };
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 16, color: "#999", fontSize: 13 }}>
          Chart render error: {this.state.error}
        </div>
      );
    }
    return this.props.children;
  }
}

export default function ChartBlock({ block }: ChartBlockProps) {
  const chartType = block.chartType || "line";
  const ChartComponent = CHART_MAP[chartType];

  const chartConfig = useMemo(() => {
    const base: Record<string, any> = {
      height: block.height || 300,
      autoFit: true,
    };

    if (chartType === "gauge") {
      // Gauge expects data as a single number (0-1), NOT an array.
      // The adaptor wraps it to { value: data } internally.
      const rawData = block.data as any;
      if (typeof rawData === "number") {
        base.data = rawData;
      } else if (Array.isArray(rawData) && rawData.length > 0) {
        // Try to extract a number from the first element
        const first = rawData[0];
        base.data = typeof first === "number" ? first : (first?.value ?? 0);
      } else {
        base.data = 0;
      }
    } else {
      // All other charts use an array of data rows
      const rawData = block.data || [];
      base.data = Array.isArray(rawData)
        ? rawData.filter((r) => r != null && typeof r === "object")
        : [];
    }

    // For x/y charts (line, column, bar, area, scatter, radar), set field mappings
    if (!NO_XY_CHARTS.has(chartType)) {
      const guessed = guessFields(base.data);
      setIfValid(base, "xField", block.xField, block.config?.xField, guessed.x);
      setIfValid(base, "yField", block.yField, block.config?.yField, guessed.y);
      setIfValid(base, "colorField", block.colorField, block.config?.colorField);
    }

    // For pie charts, set angleField / colorField
    if (chartType === "pie") {
      const guessed = guessFields(base.data);
      setIfValid(base, "angleField", block.angleField, block.config?.angleField, guessed.y);
      setIfValid(base, "colorField", block.colorField, block.config?.colorField, guessed.x);
    }

    // Spread user config last — can override anything above
    if (block.config) {
      for (const [k, v] of Object.entries(block.config)) {
        if (v !== undefined) {
          base[k] = v;
        }
      }
    }

    return base;
  }, [block.data, block.config, block.height, block.xField, block.yField, block.colorField, block.angleField, chartType]);

  if (!ChartComponent) {
    return (
      <div className={styles.fallbackBlock}>
        <span>
          Unknown chart type: <code>{chartType}</code>
        </span>
      </div>
    );
  }

  // Don't render if no valid data (skip for gauge which uses a number)
  if (chartType !== "gauge" && Array.isArray(chartConfig.data) && !chartConfig.data.length) {
    return (
      <div className={styles.chartBlock}>
        {block.title && <div className={styles.chartTitle}>{block.title}</div>}
        <div className={styles.chartBody} style={{ padding: 20, color: "#999", textAlign: "center" }}>
          No data
        </div>
      </div>
    );
  }

  return (
    <div className={styles.chartBlock}>
      {block.title && <div className={styles.chartTitle}>{block.title}</div>}
      <div className={styles.chartBody}>
        <ChartErrorBoundary>
          <ChartComponent {...chartConfig} />
        </ChartErrorBoundary>
      </div>
    </div>
  );
}
