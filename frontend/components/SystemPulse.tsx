"use client";

import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { createClient, type SupabaseClient } from "@supabase/supabase-js";

export type SystemLogRow = {
  id?: number | string | null;
  created_at: string;
  source: string;
  message: string;
  level?: string | null;
};

export type SystemPulseProps = {
  logs: SystemLogRow[];
  className?: string;
};

function sourceLabelClass(source: string): string {
  const s = (source || "").toUpperCase();
  if (s === "BOT" || s === "MAIN") return "text-blue-400";
  if (s === "AI") return "text-cyan-400";
  return "text-purple-400";
}

/**
 * Feed lateral de telemetria: recebe `logs` já populados (ex. pai com Supabase Realtime).
 * Animação de entrada com deslize suave (framer-motion).
 */
export function SystemPulse({ logs, className = "" }: SystemPulseProps) {
  return (
    <div
      className={`w-full bg-black/20 border border-white/5 rounded-lg p-4 font-mono text-[10px] h-[300px] overflow-hidden flex flex-col ${className}`}
    >
      <div className="flex justify-between items-center mb-3 border-b border-white/10 pb-2 shrink-0">
        <span className="text-amber-500/80 font-bold uppercase tracking-widest">
          Live Telemetry
        </span>
        <div className="flex gap-1 items-center">
          <div className="w-1 h-1 rounded-full bg-red-500 animate-ping" />
          <span className="text-[8px] text-red-500 opacity-80 uppercase">Active</span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto space-y-2 min-h-0 [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden">
        <AnimatePresence initial={false}>
          {logs.map((log, index) => (
            <motion.div
              key={
                log.id !== undefined && log.id !== null && log.id !== ""
                  ? String(log.id)
                  : `row-${index}-${log.created_at}`
              }
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -6 }}
              transition={{ duration: 0.3 }}
              className="flex gap-3 items-start border-b border-white/[0.02] pb-1"
            >
              <span className="text-gray-600 shrink-0">
                {new Date(log.created_at).toLocaleTimeString([], {
                  hour12: false,
                })}
              </span>
              <span
                className={`font-bold shrink-0 ${sourceLabelClass(log.source)}`}
              >
                [{log.source}]
              </span>
              <span className="text-gray-300 leading-tight tracking-tight min-w-0 break-words">
                {log.message}
              </span>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}

const MAX_LOGS = 80;

export type SystemPulseRealtimeProps = {
  supabaseUrl?: string;
  supabaseAnonKey?: string;
  className?: string;
};

/**
 * Variante com Supabase: fetch inicial + canal Realtime INSERT em `system_logs`.
 * Renderiza o mesmo painel {@link SystemPulse}.
 */
export function SystemPulseRealtime({
  supabaseUrl,
  supabaseAnonKey,
  className = "",
}: SystemPulseRealtimeProps) {
  const url =
    supabaseUrl?.trim() ||
    (typeof process !== "undefined" && process.env.NEXT_PUBLIC_SUPABASE_URL) ||
    "";
  const anon =
    supabaseAnonKey?.trim() ||
    (typeof process !== "undefined" &&
      process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY) ||
    "";

  const client = useMemo<SupabaseClient | null>(() => {
    if (!url || !anon) return null;
    return createClient(url, anon, {
      realtime: { params: { eventsPerSecond: 10 } },
    });
  }, [url, anon]);

  const [logs, setLogs] = useState<SystemLogRow[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">(
    "idle",
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!client) {
      setStatus("error");
      setError("Supabase URL ou anon key em falta.");
      return;
    }

    let cancelled = false;
    const channelName = `system_logs_pulse_${Math.random().toString(36).slice(2, 9)}`;

    async function bootstrap() {
      setStatus("loading");
      setError(null);
      const { data, error: qErr } = await client
        .from("system_logs")
        .select("id, created_at, source, message, level")
        .order("created_at", { ascending: false })
        .limit(40);

      if (cancelled) return;
      if (qErr) {
        setError(qErr.message);
        setStatus("error");
        return;
      }
      setLogs((data || []) as SystemLogRow[]);
      setStatus("ready");
    }

    void bootstrap();

    const channel = client
      .channel(channelName)
      .on(
        "postgres_changes",
        {
          event: "INSERT",
          schema: "public",
          table: "system_logs",
        },
        (payload) => {
          const row = payload.new as SystemLogRow;
          if (!row?.created_at) return;
          setLogs((prev) => {
            const id = row.id;
            const next = [
              row,
              ...prev.filter(
                (l) =>
                  id === undefined ||
                  id === null ||
                  String(l.id) !== String(id),
              ),
            ];
            return next.slice(0, MAX_LOGS);
          });
        },
      )
      .subscribe((subState) => {
        if (subState === "CHANNEL_ERROR" || subState === "TIMED_OUT") {
          setError(`Realtime: ${subState}`);
        }
      });

    return () => {
      cancelled = true;
      void client.removeChannel(channel);
    };
  }, [client]);

  return (
    <div className={className}>
      {status === "loading" && (
        <p className="text-[10px] text-gray-500 font-mono mb-1">A carregar logs…</p>
      )}
      {error && (
        <p className="text-[10px] text-red-400 font-mono mb-1" role="alert">
          {error}
        </p>
      )}
      <SystemPulse logs={logs} />
    </div>
  );
}
