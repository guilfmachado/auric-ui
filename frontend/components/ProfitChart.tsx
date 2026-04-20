"use client";

import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { LogRow } from "@/lib/types/auric";

export type ProfitChartPoint = {
  name: string;
  total: number;
  raw: number;
  id: number;
};

/** % de lucro/prejuízo inferido por linha de log (Supabase ou heurística do bot). */
export function profitPercentFromLog(log: LogRow): number {
  if (
    typeof log.resultado_trade === "number" &&
    !Number.isNaN(log.resultado_trade)
  ) {
    return log.resultado_trade;
  }
  if (typeof log.pnl_pct === "number" && !Number.isNaN(log.pnl_pct)) {
    return log.pnl_pct;
  }
  const ac = (log.acao_tomada ?? "").toUpperCase();
  if (ac === "VENDA_PROFIT") return 2;
  if (ac === "VENDA_STOP") return -1;
  return 0;
}

function buildCumulativeData(data: LogRow[]): ProfitChartPoint[] {
  if (!data.length) return [];

  const chronological = [...data].sort((a, b) => {
    const ta = a.created_at ? new Date(a.created_at).getTime() : a.id;
    const tb = b.created_at ? new Date(b.created_at).getTime() : b.id;
    return ta - tb;
  });

  let total = 0;
  return chronological.map((log) => {
    const raw = profitPercentFromLog(log);
    total += raw;
    const label = log.created_at
      ? new Date(log.created_at).toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        })
      : `#${log.id}`;
    return {
      name: label,
      total,
      raw,
      id: log.id,
    };
  });
}

type TooltipPayload = {
  payload?: ProfitChartPoint;
};

function ChartTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: TooltipPayload[];
}) {
  if (!active || !payload?.length) return null;
  const p = payload[0]?.payload;
  if (!p) return null;
  return (
    <div
      className="rounded-md border border-zinc-800 px-3 py-2 text-xs shadow-xl"
      style={{ backgroundColor: "#09090b" }}
    >
      <p className="font-mono text-zinc-500">{p.name}</p>
      <p className="font-mono text-emerald-400">
        Acumulado: {p.total >= 0 ? "+" : ""}
        {p.total.toFixed(2)}%
      </p>
      <p className="font-mono text-zinc-400">
        Evento: {p.raw >= 0 ? "+" : ""}
        {p.raw.toFixed(2)}%
      </p>
    </div>
  );
}

export function ProfitChart({ data }: { data: LogRow[] }) {
  const chartData = useMemo(() => buildCumulativeData(data), [data]);

  if (chartData.length === 0) {
    return (
      <div className="mt-4 flex h-[300px] w-full items-center justify-center rounded-lg border border-dashed border-zinc-800 bg-zinc-950/40 text-sm text-zinc-500">
        Sem logs suficientes para a curva de lucro.
      </div>
    );
  }

  return (
    <div className="mt-4 h-[300px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="colorTotal" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#10b981" stopOpacity={0.35} />
              <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#27272a" vertical={false} />
          <XAxis
            dataKey="name"
            tick={{ fill: "#71717a", fontSize: 10 }}
            interval="preserveStartEnd"
            minTickGap={32}
          />
          <YAxis
            tick={{ fill: "#71717a", fontSize: 10 }}
            width={48}
            domain={["auto", "auto"]}
            tickFormatter={(v) => `${v}%`}
          />
          <Tooltip content={<ChartTooltip />} />
          <Area
            type="monotone"
            dataKey="total"
            stroke="#10b981"
            fillOpacity={1}
            fill="url(#colorTotal)"
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, fill: "#10b981" }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
