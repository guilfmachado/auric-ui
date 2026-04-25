"use client";

import { useEffect, useMemo, useState } from "react";

import { createClient } from "@/lib/supabase/client";

export type MacroFeedRow = {
  macro_score: number;
  market_vibe: string | null;
  bullet_points: unknown;
  created_at: string;
};

function normalizeBulletPoints(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.map((x) => String(x));
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw) as unknown;
      if (Array.isArray(parsed)) return parsed.map((x) => String(x));
    } catch {
      /* ignore */
    }
    return raw.trim() ? [raw] : [];
  }
  return [];
}

function getScoreColor(score: number): string {
  if (score >= 70) return "text-green-400 border-green-900 bg-green-950/20";
  if (score >= 40) return "text-yellow-400 border-yellow-900 bg-yellow-950/20";
  return "text-red-400 border-red-900 bg-red-950/20";
}

export function MacroRadar() {
  const supabase = useMemo(() => createClient(), []);
  const [data, setData] = useState<MacroFeedRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!supabase) {
      setLoading(false);
      setError(
        "Supabase não configurado (NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY)."
      );
      return;
    }

    let cancelled = false;

    const fetchData = async () => {
      setLoading(true);
      setError(null);
      const { data: feed, error: qErr } = await supabase
        .from("macro_feed")
        .select("macro_score, market_vibe, bullet_points, created_at")
        .order("created_at", { ascending: false })
        .limit(1)
        .maybeSingle();

      if (cancelled) return;

      if (qErr) {
        setData(null);
        setError(qErr.message);
        setLoading(false);
        return;
      }

      if (!feed) {
        setData(null);
        setError(null);
        setLoading(false);
        return;
      }

      const score = Number((feed as MacroFeedRow).macro_score);
      setData({
        ...feed,
        macro_score: Number.isFinite(score) ? score : 0,
      } as MacroFeedRow);
      setLoading(false);
    };

    void fetchData();

    const channel = supabase
      .channel("macro_changes")
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "macro_feed" },
        (payload) => {
          const row = payload.new as MacroFeedRow;
          const score = Number(row.macro_score);
          setData({
            ...row,
            macro_score: Number.isFinite(score) ? score : 0,
          });
          setError(null);
        }
      )
      .subscribe();

    return () => {
      cancelled = true;
      void supabase.removeChannel(channel);
    };
  }, [supabase]);

  if (!supabase) {
    return (
      <div className="rounded-xl border border-zinc-800 bg-zinc-950/40 p-6 font-mono text-xs text-zinc-500">
        Radar macro: configure o Supabase no .env.local para ativar.
      </div>
    );
  }

  if (loading) {
    return (
      <div className="animate-pulse rounded-xl border border-zinc-800 p-6 font-mono text-sm text-zinc-500">
        A sintonizar radar macro…
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-xl border border-red-900/60 bg-red-950/20 p-6 font-mono text-xs text-red-300/90">
        Radar macro: {error}
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-xl border border-zinc-800 bg-zinc-950/40 p-6 font-mono text-xs text-zinc-500">
        Sem linhas em{" "}
        <span className="font-mono text-zinc-400">macro_feed</span>. Correr a
        migração e inserir dados a partir do VPS.
      </div>
    );
  }

  const bullets = normalizeBulletPoints(data.bullet_points);
  const statusColor = getScoreColor(data.macro_score);

  return (
    <div
      className={`rounded-xl border p-6 font-mono transition-all duration-500 ${statusColor}`}
    >
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xs font-bold tracking-widest uppercase opacity-80">
          Radar Macro DeepSeek V4
        </h2>
        <span className="text-2xl font-black">{data.macro_score}</span>
      </div>

      <div className="mb-4">
        <p className="text-lg leading-tight font-bold uppercase italic underline">
          &quot;{data.market_vibe ?? "—"}&quot;
        </p>
      </div>

      <ul className="space-y-2 text-sm opacity-90">
        {bullets.map((point, i) => (
          <li key={i} className="flex gap-2">
            <span className="opacity-50">[{i + 1}]</span>
            <span>{point}</span>
          </li>
        ))}
      </ul>

      <div className="mt-4 flex justify-between border-t border-current pt-4 text-[10px] opacity-30">
        <span>SISTEMA ATIVO - CONTABO VPS</span>
        <span>
          ÚLTIMA ATUALIZAÇÃO:{" "}
          {new Date(data.created_at).toLocaleTimeString()}
        </span>
      </div>
    </div>
  );
}
