/**
 * token-usage-plugin – QwenPaw frontend plugin
 *
 * Registers a custom page at /token-usage-dashboard that shows:
 *  - KPI cards  (prompt tokens / completion tokens / API calls)
 *  - Daily trend chart  (pure SVG, no chart-library dependency)
 *  - Per-model breakdown table
 *
 * Build:   npm install && npm run build
 * Install: cp -r . ~/.qwenpaw/plugins/token-usage-plugin
 */

import React from "react";

// ── API types (mirrors backend TokenUsageSummary) ─────────────────────────

interface TokenUsageStats {
  prompt_tokens: number;
  completion_tokens: number;
  call_count: number;
}

interface TokenUsageByModel extends TokenUsageStats {
  provider_id: string;
  model: string;
}

interface TokenUsageSummary {
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_calls: number;
  by_model: Record<string, TokenUsageByModel>;
  by_provider: Record<string, TokenUsageStats>;
  by_date: Record<string, TokenUsageStats>; // key = "YYYY-MM-DD", sorted asc
}

// ── API helper ────────────────────────────────────────────────────────────

function buildApiUrl(path: string): string {
  const base: string =
    typeof (window as any).__VITE_API_BASE_URL === "string"
      ? (window as any).__VITE_API_BASE_URL
      : "";
  const token = localStorage.getItem("qwenpaw_auth_token") ?? "";
  const sep = path.includes("?") ? "&" : "?";
  return token
    ? `${base}/api${path}${sep}token=${encodeURIComponent(token)}`
    : `${base}/api${path}`;
}

async function fetchTokenUsage(
  startDate: string,
  endDate: string,
): Promise<TokenUsageSummary> {
  const url = buildApiUrl(
    `/token-usage?start_date=${startDate}&end_date=${endDate}`,
  );
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Utilities ─────────────────────────────────────────────────────────────

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function isoToday(): string {
  return new Date().toISOString().slice(0, 10);
}

function isoNDaysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

// ── SVG Sparkline / Line Chart ────────────────────────────────────────────

interface LineChartProps {
  byDate: Record<string, TokenUsageStats>;
  startDate: string;
  endDate: string;
}

function LineChart({ byDate, startDate, endDate }: LineChartProps) {
  const W = 600;
  const H = 160;
  const PAD = { top: 12, right: 16, bottom: 36, left: 52 };
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;

  // Build sorted date list between startDate..endDate
  const dates: string[] = [];
  const cur = new Date(startDate);
  const end = new Date(endDate);
  while (cur <= end) {
    dates.push(cur.toISOString().slice(0, 10));
    cur.setDate(cur.getDate() + 1);
  }
  if (dates.length === 0) return null;

  const promptSeries = dates.map((d) => byDate[d]?.prompt_tokens ?? 0);
  const completionSeries = dates.map((d) => byDate[d]?.completion_tokens ?? 0);

  const allValues = [...promptSeries, ...completionSeries];
  const maxVal = Math.max(...allValues, 1);

  const xScale = (i: number) =>
    PAD.left + (dates.length === 1 ? innerW / 2 : (i / (dates.length - 1)) * innerW);
  const yScale = (v: number) =>
    PAD.top + innerH - (v / maxVal) * innerH;

  const polyline = (series: number[]) =>
    series.map((v, i) => `${xScale(i)},${yScale(v)}`).join(" ");

  // Y-axis ticks
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => ({
    y: PAD.top + innerH - f * innerH,
    label: fmt(Math.round(f * maxVal)),
  }));

  // X-axis ticks: show at most 7 labels
  const step = Math.max(1, Math.ceil(dates.length / 7));
  const xTicks = dates
    .map((d, i) => ({ d, i }))
    .filter(({ i }) => i % step === 0 || i === dates.length - 1);

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      style={{ width: "100%", height: H, display: "block" }}
      aria-label="Token usage trend"
    >
      {/* grid lines */}
      {yTicks.map((t) => (
        <line
          key={t.y}
          x1={PAD.left}
          x2={W - PAD.right}
          y1={t.y}
          y2={t.y}
          stroke="#e8e8e8"
          strokeWidth={1}
        />
      ))}

      {/* prompt tokens line */}
      <polyline
        points={polyline(promptSeries)}
        fill="none"
        stroke="#4096ff"
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {/* completion tokens line */}
      <polyline
        points={polyline(completionSeries)}
        fill="none"
        stroke="#52c41a"
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {/* Y-axis labels */}
      {yTicks.map((t) => (
        <text
          key={t.y}
          x={PAD.left - 6}
          y={t.y + 4}
          textAnchor="end"
          fontSize={10}
          fill="#999"
        >
          {t.label}
        </text>
      ))}

      {/* X-axis labels */}
      {xTicks.map(({ d, i }) => (
        <text
          key={d}
          x={xScale(i)}
          y={H - 4}
          textAnchor="middle"
          fontSize={10}
          fill="#999"
        >
          {d.slice(5)} {/* MM-DD */}
        </text>
      ))}

      {/* Legend */}
      <circle cx={PAD.left} cy={H - PAD.bottom + 22} r={4} fill="#4096ff" />
      <text x={PAD.left + 8} y={H - PAD.bottom + 26} fontSize={11} fill="#555">
        Prompt tokens
      </text>
      <circle cx={PAD.left + 110} cy={H - PAD.bottom + 22} r={4} fill="#52c41a" />
      <text x={PAD.left + 118} y={H - PAD.bottom + 26} fontSize={11} fill="#555">
        Completion tokens
      </text>
    </svg>
  );
}

// ── KPI Card ──────────────────────────────────────────────────────────────

interface KpiCardProps {
  label: string;
  value: number;
  color: string;
}

function KpiCard({ label, value, color }: KpiCardProps) {
  return (
    <div style={{ ...s.card, borderTop: `3px solid ${color}` }}>
      <div style={{ ...s.cardVal, color }}>{fmt(value)}</div>
      <div style={s.cardLabel}>{label}</div>
    </div>
  );
}

// ── By-model table ────────────────────────────────────────────────────────

interface ModelTableProps {
  byModel: Record<string, TokenUsageByModel>;
}

function ModelTable({ byModel }: ModelTableProps) {
  const rows = Object.entries(byModel).map(([key, v]) => ({ key, ...v }));
  if (rows.length === 0) return null;

  return (
    <div style={s.tableWrap}>
      <div style={s.sectionTitle}>By Model</div>
      <table style={s.table}>
        <thead>
          <tr>
            {["Provider", "Model", "Prompt tokens", "Completion tokens", "Calls"].map((h) => (
              <th key={h} style={s.th}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} style={s.tr}>
              <td style={s.td}>{r.provider_id || "—"}</td>
              <td style={{ ...s.td, fontWeight: 500 }}>{r.model || r.key}</td>
              <td style={{ ...s.td, ...s.tdNum }}>{fmt(r.prompt_tokens)}</td>
              <td style={{ ...s.td, ...s.tdNum }}>{fmt(r.completion_tokens)}</td>
              <td style={{ ...s.td, ...s.tdNum }}>{fmt(r.call_count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Date range picker (native <input type="date">) ────────────────────────

interface DateRangeProps {
  start: string;
  end: string;
  onStartChange: (v: string) => void;
  onEndChange: (v: string) => void;
  onRefresh: () => void;
  loading: boolean;
}

function DateRange({
  start,
  end,
  onStartChange,
  onEndChange,
  onRefresh,
  loading,
}: DateRangeProps) {
  return (
    <div style={s.toolbar}>
      <label style={s.label}>Start</label>
      <input
        type="date"
        value={start}
        max={end}
        onChange={(e) => onStartChange(e.target.value)}
        style={s.dateInput}
      />
      <label style={s.label}>End</label>
      <input
        type="date"
        value={end}
        min={start}
        max={isoToday()}
        onChange={(e) => onEndChange(e.target.value)}
        style={s.dateInput}
      />
      <button onClick={onRefresh} disabled={loading} style={s.btn}>
        {loading ? "Loading…" : "Refresh"}
      </button>
    </div>
  );
}

// ── Main page component ───────────────────────────────────────────────────

function TokenUsageDashboard() {
  const [startDate, setStartDate] = React.useState(isoNDaysAgo(30));
  const [endDate, setEndDate] = React.useState(isoToday());
  const [data, setData] = React.useState<TokenUsageSummary | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const summary = await fetchTokenUsage(startDate, endDate);
      setData(summary);
    } catch (e: any) {
      setError(e?.message ?? "Failed to load token usage");
    } finally {
      setLoading(false);
    }
  }, [startDate, endDate]);

  React.useEffect(() => {
    load();
  }, []); // load once on mount; user hits Refresh to re-query

  const hasData = data && data.total_calls > 0;

  return (
    <div style={s.page}>
      <div style={s.header}>
        <span style={s.title}>📊 Token Usage</span>
      </div>

      <DateRange
        start={startDate}
        end={endDate}
        onStartChange={setStartDate}
        onEndChange={setEndDate}
        onRefresh={load}
        loading={loading}
      />

      {error && (
        <div style={s.error}>
          ⚠ {error}
          <button onClick={load} style={{ ...s.btn, marginLeft: 12 }}>
            Retry
          </button>
        </div>
      )}

      {loading && !data && (
        <div style={s.placeholder}>Loading…</div>
      )}

      {!loading && !error && data && !hasData && (
        <div style={s.placeholder}>No data for the selected period.</div>
      )}

      {hasData && (
        <>
          {/* KPI cards */}
          <div style={s.kpiRow}>
            <KpiCard
              label="Prompt Tokens"
              value={data.total_prompt_tokens}
              color="#4096ff"
            />
            <KpiCard
              label="Completion Tokens"
              value={data.total_completion_tokens}
              color="#52c41a"
            />
            <KpiCard
              label="Total Tokens"
              value={data.total_prompt_tokens + data.total_completion_tokens}
              color="#722ed1"
            />
            <KpiCard
              label="API Calls"
              value={data.total_calls}
              color="#fa8c16"
            />
          </div>

          {/* Trend chart */}
          {Object.keys(data.by_date).length > 0 && (
            <div style={s.chartCard}>
              <div style={s.sectionTitle}>Daily Trend</div>
              <LineChart
                byDate={data.by_date}
                startDate={startDate}
                endDate={endDate}
              />
            </div>
          )}

          {/* By-model table */}
          <ModelTable byModel={data.by_model} />
        </>
      )}
    </div>
  );
}

// ── Inline styles ─────────────────────────────────────────────────────────

const s: Record<string, React.CSSProperties> = {
  page: {
    padding: "24px 28px",
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    fontSize: 14,
    color: "#222",
    maxWidth: 900,
    margin: "0 auto",
  },
  header: {
    display: "flex",
    alignItems: "center",
    marginBottom: 20,
  },
  title: {
    fontSize: 20,
    fontWeight: 700,
    color: "#1a1a1a",
  },
  toolbar: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    marginBottom: 24,
    flexWrap: "wrap",
  },
  label: {
    fontSize: 13,
    color: "#666",
    whiteSpace: "nowrap",
  },
  dateInput: {
    padding: "5px 8px",
    border: "1px solid #d9d9d9",
    borderRadius: 6,
    fontSize: 13,
    outline: "none",
    cursor: "pointer",
  },
  btn: {
    padding: "6px 16px",
    border: "none",
    borderRadius: 6,
    background: "#4096ff",
    color: "#fff",
    fontSize: 13,
    cursor: "pointer",
    fontWeight: 500,
  },
  error: {
    background: "#fff2f0",
    border: "1px solid #ffccc7",
    borderRadius: 6,
    padding: "10px 16px",
    color: "#cf1322",
    marginBottom: 20,
    display: "flex",
    alignItems: "center",
  },
  placeholder: {
    textAlign: "center",
    color: "#aaa",
    padding: "60px 0",
    fontSize: 15,
  },
  kpiRow: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))",
    gap: 16,
    marginBottom: 24,
  },
  card: {
    background: "#fff",
    borderRadius: 8,
    padding: "16px 20px",
    boxShadow: "0 1px 4px rgba(0,0,0,.08)",
    textAlign: "center",
  },
  cardVal: {
    fontSize: 28,
    fontWeight: 700,
    lineHeight: 1.2,
    marginBottom: 4,
  },
  cardLabel: {
    fontSize: 12,
    color: "#888",
    whiteSpace: "nowrap",
  },
  chartCard: {
    background: "#fff",
    borderRadius: 8,
    padding: "16px 20px",
    boxShadow: "0 1px 4px rgba(0,0,0,.08)",
    marginBottom: 24,
  },
  sectionTitle: {
    fontWeight: 600,
    fontSize: 14,
    color: "#333",
    marginBottom: 12,
  },
  tableWrap: {
    background: "#fff",
    borderRadius: 8,
    padding: "16px 20px",
    boxShadow: "0 1px 4px rgba(0,0,0,.08)",
    overflowX: "auto",
  },
  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: 13,
  },
  th: {
    textAlign: "left",
    padding: "8px 12px",
    background: "#fafafa",
    borderBottom: "1px solid #f0f0f0",
    fontWeight: 600,
    color: "#555",
    whiteSpace: "nowrap",
  },
  tr: {
    borderBottom: "1px solid #f5f5f5",
  },
  td: {
    padding: "9px 12px",
    color: "#333",
  },
  tdNum: {
    textAlign: "right",
    fontVariantNumeric: "tabular-nums",
  },
};

// ── Plugin registration ───────────────────────────────────────────────────

(window as any).__registerPlugin?.(
  {
    name: "token-usage-plugin",
    version: "1.0.0",
    description: "Token Usage dashboard – daily trend + per-model breakdown",
    entry: { frontend: "dist/index.umd.js" },
  },
  {
    routes: [
      {
        path: "/token-usage-dashboard",
        component: TokenUsageDashboard,
        label: "Token Usage",
        icon: "📊",
      },
    ],
  },
);
