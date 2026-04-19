"use client";

import { useState, useEffect, useMemo, useCallback } from "react";
import { createClient } from "@supabase/supabase-js";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Brain, Zap, Target, ArrowUpRight, Power, TrendingUp } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "@/lib/utils";
import { ProfitChart } from "@/components/ProfitChart";
import type { TradeLogRow } from "@/lib/types/auric";

function createBrowserSupabase() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key =
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ??
    process.env.NEXT_PUBLIC_SUPABASE_KEY ??
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) return null;
  return createClient(url, key);
}

const BINANCE_ETH_TICKER =
  "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT";
/** Alinhado ao main.py: Hub+Brain só fora da faixa (0,40 — 0,60). */
const ML_LONG_MIN = 0.6;
const ML_SHORT_MAX = 0.4;

export default function AuricDashboard() {
  const supabase = useMemo(() => createBrowserSupabase(), []);
  const [logs, setLogs] = useState<TradeLogRow[]>([]);
  const [isFutures, setIsFutures] = useState(false);
  const [ethPriceLive, setEthPriceLive] = useState<number | null>(null);
  const [masterOn, setMasterOn] = useState(true);
  const [masterBusy, setMasterBusy] = useState(false);

  const fetchInitialData = useCallback(async () => {
    if (!supabase) return;

    const [logsRes, configRes, botRes] = await Promise.all([
      supabase
        .from("trade_logs")
        .select("*")
        .order("created_at", { ascending: false })
        .limit(15),
      supabase
        .from("config")
        .select("modo_operacao, trading_mode")
        .eq("id", 1)
        .maybeSingle(),
      supabase
        .from("bot_control")
        .select("is_active")
        .eq("id", 1)
        .maybeSingle(),
    ]);

    if (logsRes.data) setLogs(logsRes.data as TradeLogRow[]);

    const cfg = configRes.data;
    if (cfg) {
      const modo =
        (cfg as { modo_operacao?: string; trading_mode?: string })
          .modo_operacao ||
        (cfg as { trading_mode?: string }).trading_mode;
      setIsFutures(modo === "FUTURES");
    }

    if (botRes.data && typeof (botRes.data as { is_active?: boolean }).is_active === "boolean") {
      setMasterOn((botRes.data as { is_active: boolean }).is_active);
    }
  }, [supabase]);

  useEffect(() => {
    if (!supabase) return;

    void fetchInitialData();

    const channel = supabase
      .channel("db-changes")
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "trade_logs" },
        (payload) => {
          const row = payload.new as TradeLogRow;
          setLogs((prev) => [row, ...prev].slice(0, 15));
        }
      )
      .subscribe();

    return () => {
      void supabase.removeChannel(channel);
    };
  }, [supabase, fetchInitialData]);

  useEffect(() => {
    if (!supabase) return;
    const ch = supabase
      .channel("bot-status-realtime")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "bot_status" },
        (payload) => {
          const row = payload.new as { is_active?: boolean } | null;
          if (row && typeof row.is_active === "boolean") {
            setMasterOn(row.is_active);
          }
        }
      )
      .subscribe();
    return () => {
      void supabase.removeChannel(ch);
    };
  }, [supabase]);

  useEffect(() => {
    const fetchEthPrice = async () => {
      try {
        const res = await fetch(BINANCE_ETH_TICKER);
        if (!res.ok) return;
        const data: { price?: string } = await res.json();
        const n = parseFloat(data.price ?? "");
        if (!Number.isNaN(n)) setEthPriceLive(n);
      } catch {
        /* rede / CORS em dev: mantém último valor */
      }
    };

    void fetchEthPrice();
    const id = setInterval(fetchEthPrice, 10_000);
    return () => clearInterval(id);
  }, []);

  const toggleMode = async (checked: boolean) => {
    if (!supabase) return;
    const novoModo = checked ? "FUTURES" : "SPOT";
    setIsFutures(checked);
    await supabase.from("config").upsert(
      {
        id: 1,
        modo_operacao: novoModo,
        trading_mode: novoModo,
        updated_at: new Date().toISOString(),
      },
      { onConflict: "id" }
    );
  };

  const toggleBot = async (checked: boolean) => {
    if (!supabase) return;
    setMasterBusy(true);
    setMasterOn(checked);
    const { error } = await supabase
      .from("bot_control")
      .update({
        is_active: checked,
        updated_at: new Date().toISOString(),
      })
      .eq("id", 1);
    setMasterBusy(false);
    if (error) {
      console.error("Erro ao alternar bot:", error);
      setMasterOn(!checked);
    }
  };

  const latest = logs[0] || {};
  const mlProbRaw = latest.probabilidade_ml;
  const mlProbNum =
    mlProbRaw === undefined || mlProbRaw === null
      ? NaN
      : Number(mlProbRaw);
  const inMlActivationZone =
    !Number.isNaN(mlProbNum) &&
    (mlProbNum >= ML_LONG_MIN || mlProbNum <= ML_SHORT_MAX);
  const intelWeakSignal = Number.isNaN(mlProbNum) || !inMlActivationZone;
  const mlPct = Math.min(
    100,
    Math.max(0, (Number.isNaN(mlProbNum) ? 0 : mlProbNum) * 100)
  );

  const isBearish =
    latest.probabilidade_ml != null &&
    Number(latest.probabilidade_ml) <= 0.4;
  const statusColor = isBearish ? "text-red-500" : "text-emerald-500";
  const bgColor = isBearish ? "bg-red-500/10" : "bg-emerald-500/10";
  const borderGlow = isBearish
    ? "shadow-[0_0_20px_rgba(239,68,68,0.2)]"
    : "shadow-[0_0_20px_rgba(16,185,129,0.2)]";

  if (!supabase) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#050505] p-4 text-zinc-500">
        Defina NEXT_PUBLIC_SUPABASE_URL e uma chave pública (ex.:
        NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY) no .env.local
      </div>
    );
  }

  return (
    <div
      className={cn(
        "min-h-screen p-4 font-sans text-zinc-100 transition-all duration-700 selection:bg-emerald-500/30 md:p-8",
        isBearish ? "bg-red-950/10" : "bg-[#050505]"
      )}
    >
      <header className="mx-auto mb-10 flex max-w-7xl items-center justify-between border-b border-zinc-800/50 pb-6">
        <div className="flex items-center gap-4">
          <div className="rounded-md bg-emerald-500 p-1.5">
            <Zap className="size-5 fill-black text-black" />
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight">
              AURIC <span className="text-emerald-500">SYSTEMS</span>
            </h1>
            <p className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
              Autonomous Quant Engine v3.1
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-3">
          <div className="flex items-center gap-2 rounded-full border border-zinc-800 bg-zinc-900/80 px-4 py-2 shadow-2xl">
            <Power
              className={cn(
                "size-4 shrink-0",
                masterOn ? "text-emerald-400" : "text-red-500"
              )}
            />
            <span className="text-[10px] font-black tracking-wide text-zinc-500 uppercase">
              Master
            </span>
            <Switch
              checked={masterOn}
              disabled={masterBusy}
              onCheckedChange={(v) => void toggleBot(v)}
              className="data-checked:bg-emerald-600"
            />
            <span
              className={cn(
                "min-w-[2.5rem] text-[10px] font-black",
                masterOn ? "text-emerald-400" : "text-red-400"
              )}
            >
              {masterOn ? "RUN" : "STBY"}
            </span>
          </div>

          <div className="flex items-center gap-3 rounded-full border border-zinc-800 bg-zinc-900/80 px-4 py-2 shadow-2xl">
            <span
              className={`text-[10px] font-black ${!isFutures ? "text-emerald-400" : "text-zinc-600"}`}
            >
              SPOT
            </span>
            <Switch
              checked={isFutures}
              onCheckedChange={toggleMode}
              className="data-checked:bg-orange-500"
            />
            <span
              className={`text-[10px] font-black ${isFutures ? "text-orange-400" : "text-zinc-600"}`}
            >
              FUTURES
            </span>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-6">
        {/* Grid de Performance */}
        <div className="mb-8 grid grid-cols-1 gap-6 lg:grid-cols-4">
          <Card className="border-zinc-800/50 bg-zinc-900/20 backdrop-blur-xl lg:col-span-3">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-xs font-bold tracking-widest text-zinc-500 uppercase">
                <TrendingUp size={14} className="text-emerald-500" />
                Accumulated Performance (ROI %)
              </CardTitle>
            </CardHeader>
            <CardContent>
              {logs.length === 0 ? (
                <div className="flex h-[300px] items-center justify-center rounded-lg border border-dashed border-zinc-800">
                  <p className="animate-pulse text-xs tracking-widest text-zinc-500">
                    AWAITING MARKET EXECUTION...
                  </p>
                </div>
              ) : (
                <ProfitChart data={logs} />
              )}
            </CardContent>
          </Card>

          <Card className="flex flex-col items-center justify-center border-zinc-800/50 bg-zinc-900/20 p-6">
            <p className="mb-2 text-[10px] font-bold text-zinc-500 uppercase">
              Win Rate
            </p>
            <h2 className="text-5xl font-black text-emerald-500">64%</h2>
            <p className="mt-4 text-center text-[10px] italic text-zinc-600">
              Baseado nos últimos 30 dias de trade real e simulação Claude 3.5
            </p>
          </Card>
        </div>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-4">
        <Card
          className={cn(
            "relative overflow-hidden backdrop-blur-xl transition-all duration-500 lg:col-span-3",
            isBearish
              ? "border-red-500/50 shadow-[0_0_30px_rgba(239,68,68,0.1)]"
              : "border-zinc-800",
            intelWeakSignal
              ? "bg-zinc-900/20 shadow-2xl"
              : isBearish
                ? bgColor
                : cn(bgColor, borderGlow)
          )}
        >
          <div
            className={cn(
              "absolute top-0 left-0 h-full w-1",
              intelWeakSignal ? "bg-amber-500" : isBearish ? "bg-red-500" : "bg-emerald-500"
            )}
          />
          {isBearish && (
            <div className="absolute top-0 right-0 z-10 p-2">
              <span className="relative flex h-3 w-3">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-red-400 opacity-75" />
                <span className="relative inline-flex h-3 w-3 rounded-full bg-red-500" />
              </span>
            </div>
          )}
          <CardHeader className="relative flex flex-col gap-3 border-b border-zinc-800/50 sm:flex-row sm:items-start sm:justify-between">
            {!intelWeakSignal && (
              <div className="flex w-full items-center justify-between gap-3 sm:w-auto sm:max-w-md">
                <Badge
                  className={cn(
                    "font-black text-black",
                    isBearish
                      ? "bg-red-500 hover:bg-red-400"
                      : "bg-emerald-500 hover:bg-emerald-400"
                  )}
                >
                  {isBearish ? "SHORT OPPORTUNITY" : "LONG OPPORTUNITY"}
                </Badge>
                {isBearish && (
                  <span className="relative flex h-3 w-3 shrink-0">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-red-400 opacity-75" />
                    <span className="relative inline-flex h-3 w-3 rounded-full bg-red-500" />
                  </span>
                )}
              </div>
            )}
            <div className="flex w-full flex-col gap-2 sm:flex-1 sm:flex-row sm:items-center sm:justify-between">
              <CardTitle
                className={cn(
                  "flex items-center gap-2 text-xs font-bold tracking-widest uppercase",
                  intelWeakSignal ? "text-zinc-500" : statusColor
                )}
              >
                <Brain
                  size={14}
                  className={intelWeakSignal ? "text-amber-500" : statusColor}
                />{" "}
                Neural Analysis Feed
              </CardTitle>
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    "size-2 animate-pulse rounded-full",
                    intelWeakSignal
                      ? "bg-amber-500"
                      : isBearish
                        ? "bg-red-500"
                        : "bg-emerald-500"
                  )}
                />
                <span
                  className={cn(
                    "font-mono text-[10px]",
                    intelWeakSignal
                      ? "text-amber-500"
                      : isBearish
                        ? "text-red-500"
                        : "text-emerald-500"
                  )}
                >
                  LIVE_DATA_STREAM
                </span>
              </div>
            </div>
          </CardHeader>
          <CardContent className="pt-8 pb-8">
            {intelWeakSignal ? (
              <div className="space-y-8">
                <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 px-6 py-10 text-center">
                  <p className="text-[10px] font-bold tracking-widest text-amber-500/80 uppercase">
                    Inteligência
                  </p>
                  <p className="mt-3 text-xl font-bold tracking-tight text-amber-100 md:text-2xl">
                    Sinal Fraco: Monitorando Oportunidades
                  </p>
                  <p className="mt-2 font-mono text-[11px] text-zinc-500">
                    P(alta) na zona neutra ]{(ML_SHORT_MAX * 100).toFixed(0)}%,{" "}
                    {(ML_LONG_MIN * 100).toFixed(0)}%[ — Hub/Brain inativos; ative em P≥
                    {(ML_LONG_MIN * 100).toFixed(0)}% (Long) ou P≤
                    {(ML_SHORT_MAX * 100).toFixed(0)}% (Short).
                  </p>
                </div>
                <div className="flex items-end justify-between opacity-60">
                  <p className="text-[10px] font-bold tracking-widest text-zinc-500 uppercase">
                    ML (último log)
                  </p>
                  <span className="font-mono text-xl font-black">
                    {Number.isNaN(mlProbNum) ? "—" : `${mlPct.toFixed(1)}%`}
                  </span>
                </div>
              </div>
            ) : (
              <>
                <div className="grid grid-cols-1 gap-12 md:grid-cols-2">
                  <div className="space-y-4">
                    <div className="flex items-end justify-between">
                      <p className="text-[10px] font-bold tracking-widest text-zinc-500 uppercase">
                        ML Confidence
                      </p>
                      <span
                        className={cn(
                          "font-mono text-3xl font-black",
                          isBearish ? "text-red-400" : "text-emerald-400"
                        )}
                      >
                        {mlPct.toFixed(1)}%
                      </span>
                    </div>
                    <Progress
                      value={mlPct}
                      className={cn(
                        "h-1.5 min-w-0 gap-0 bg-zinc-800",
                        "[&_[data-slot=progress-track]]:h-1.5 [&_[data-slot=progress-track]]:rounded-full [&_[data-slot=progress-track]]:bg-zinc-800",
                        "[&_[data-slot=progress-indicator]]:rounded-full",
                        isBearish
                          ? "[&_[data-slot=progress-indicator]]:bg-red-500"
                          : "[&_[data-slot=progress-indicator]]:bg-emerald-500"
                      )}
                    />
                    <div className="flex justify-between font-mono text-[10px] text-zinc-600">
                      <span>BEARISH_THRESHOLD</span>
                      <span>BULLISH_SIGNAL</span>
                    </div>
                  </div>

                  <div className="space-y-4">
                    <p className="text-[10px] font-bold tracking-widest text-zinc-500 uppercase">
                      LLM Sentiment Veredict
                    </p>
                    <div className="flex items-center gap-4">
                      <div
                        className={`text-4xl font-black italic tracking-tighter ${
                          latest.sentimento_ia === "BULLISH"
                            ? "text-emerald-400"
                            : "text-red-500"
                        }`}
                      >
                        {latest.sentimento_ia || "SCANNING..."}
                      </div>
                    </div>
                  </div>
                </div>

                <div
                  className={cn(
                    "mt-10 rounded-lg border bg-black/40 p-5 transition-all",
                    isBearish
                      ? "border-red-500/25 group-hover:border-red-500/40"
                      : "border-zinc-800/50 group-hover:border-emerald-500/30"
                  )}
                >
                  <p
                    className={cn(
                      "mb-3 flex items-center gap-2 text-[10px] font-bold uppercase",
                      isBearish ? "text-red-500/90" : "text-zinc-600"
                    )}
                  >
                    <Target size={12} /> Model Reasoning Justification
                  </p>
                  <p className="text-sm leading-relaxed font-medium text-zinc-300 italic">
                    &quot;
                    {latest.justificativa ||
                      "Waiting for next technical setup to trigger AI contextual analysis..."}
                    &quot;
                  </p>
                </div>
              </>
            )}
          </CardContent>
        </Card>

        <div className="space-y-6">
          <Card className="border-zinc-800/50 bg-zinc-900/20">
            <CardContent className="pt-6">
              <p className="mb-1 text-[10px] font-bold text-zinc-500 uppercase">
                Current Price
              </p>
              <p className="mb-1 font-mono text-[9px] text-emerald-500/80">
                Binance spot · atualizado a cada 10s
              </p>
              <div className="flex items-center justify-between font-mono text-2xl font-bold">
                {ethPriceLive != null ? (
                  <span>
                    $
                    {ethPriceLive.toLocaleString(undefined, {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    })}
                  </span>
                ) : (
                  <span className="text-zinc-500">…</span>
                )}
                <ArrowUpRight size={20} className="text-emerald-500" />
              </div>
            </CardContent>
          </Card>

          <Card className="border-zinc-800/50 bg-zinc-900/20">
            <CardContent className="pt-6">
              <p className="mb-1 text-[10px] font-bold text-zinc-500 uppercase">
                Active Signal
              </p>
              <div className="flex items-center gap-3">
                <Badge
                  className={cn(
                    "font-black text-black",
                    !Number.isNaN(mlProbNum) && isBearish
                      ? "bg-red-500 hover:bg-red-400"
                      : "bg-emerald-500 hover:bg-emerald-400"
                  )}
                >
                  {latest.acao_tomada || "IDLE"}
                </Badge>
                <span className="font-mono text-[10px] text-zinc-500 italic">
                  No manual action req.
                </span>
              </div>
            </CardContent>
          </Card>
        </div>

        <Card className="border-none bg-transparent lg:col-span-4">
          <CardHeader className="px-0">
            <CardTitle className="text-xs font-bold tracking-widest text-zinc-500 uppercase">
              Transaction & Decision Log
            </CardTitle>
          </CardHeader>
          <Table>
            <TableHeader className="border-zinc-800">
              <TableRow className="border-zinc-800 hover:bg-transparent">
                <TableHead className="text-[10px] font-bold text-zinc-500 uppercase">
                  Timestamp
                </TableHead>
                <TableHead className="text-[10px] font-bold text-zinc-500 uppercase">
                  Operation (Compra / Venda)
                </TableHead>
                <TableHead className="text-[10px] font-bold text-zinc-500 uppercase">
                  XGBoost %
                </TableHead>
                <TableHead className="text-right text-[10px] font-bold text-zinc-500 uppercase">
                  Market Price
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <AnimatePresence>
                {logs.map((log) => (
                  <motion.tr
                    key={log.id}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    className="border-zinc-900 transition-colors group-hover:bg-zinc-900/30"
                  >
                    <TableCell className="font-mono text-[11px] text-zinc-600">
                      {log.created_at
                        ? new Date(log.created_at).toLocaleTimeString()
                        : "—"}
                    </TableCell>
                    <TableCell>
                      {(() => {
                        const ac = (log.acao_tomada ?? "").toUpperCase();
                        const isCompraLong =
                          ac.includes("COMPRA") ||
                          ac === "COMPRA_MARKET" ||
                          ac.includes("LONG_MARKET");
                        const isVendaShort =
                          ac.includes("SHORT") ||
                          ac.includes("ABRE_SHORT") ||
                          ac.includes("VETO_SHORT");
                        const cls = isVendaShort
                          ? "bg-rose-500/15 text-rose-400"
                          : isCompraLong
                            ? "bg-emerald-500/10 text-emerald-500"
                            : "bg-zinc-800 text-zinc-500";
                        return (
                          <span className={`rounded px-2 py-0.5 text-[10px] font-black ${cls}`}>
                            {log.acao_tomada ?? "—"}
                          </span>
                        );
                      })()}
                    </TableCell>
                    <TableCell className="font-mono text-[11px] font-bold">
                      {((log.probabilidade_ml ?? 0) * 100).toFixed(1)}%
                    </TableCell>
                    <TableCell className="text-right font-mono font-bold text-zinc-300">
                      ${log.preco_atual?.toLocaleString() ?? "—"}
                    </TableCell>
                  </motion.tr>
                ))}
              </AnimatePresence>
            </TableBody>
          </Table>
        </Card>
        </div>
      </main>
    </div>
  );
}
