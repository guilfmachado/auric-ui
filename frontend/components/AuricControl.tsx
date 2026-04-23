"use client";

import React, { useState } from "react";
import toast from "react-hot-toast";

const apiSecret = process.env.NEXT_PUBLIC_BOT_COMMAND_API_SECRET;

export default function AuricControl() {
  const [loading, setLoading] = useState(false);
  const [obs, setObs] = useState("");

  const sendCommand = async (value: string, active: boolean) => {
    if (!apiSecret) {
      toast.error("Configura NEXT_PUBLIC_BOT_COMMAND_API_SECRET (.env.local / Vercel)");
      return;
    }
    setLoading(true);
    try {
      const res = await fetch("/api/bot/command", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiSecret}`,
        },
        body: JSON.stringify({ value, active }),
      });
      const j = (await res.json().catch(() => ({}))) as {
        ok?: boolean;
        error?: string;
      };
      if (res.ok && j.ok !== false) {
        toast.success(active ? "Sentimento Enviado!" : "Filtros Limpos!");
        setObs(active ? value : "");
      } else {
        toast.error(j.error ?? `Erro ${res.status}`);
      }
    } catch {
      toast.error("Falha na conexão");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 shadow-2xl">
      <h2 className="mb-4 flex items-center gap-2 font-bold text-white">
        🛰️ Auric War Room
      </h2>

      <div className="mb-4 grid grid-cols-2 gap-3">
        <button
          type="button"
          onClick={() =>
            void sendCommand("Double Top detetado - Veto Long", true)
          }
          disabled={loading}
          className="rounded-lg border border-red-500 bg-red-500/20 p-3 font-semibold text-red-500 transition-all active:scale-95 disabled:opacity-50"
        >
          🛑 Veto: Top
        </button>

        <button
          type="button"
          onClick={() =>
            void sendCommand("Double Bottom detetado - Veto Short", true)
          }
          disabled={loading}
          className="rounded-lg border border-green-500 bg-green-500/20 p-3 font-semibold text-green-500 transition-all active:scale-95 disabled:opacity-50"
        >
          🛡️ Veto: Bottom
        </button>
      </div>

      <textarea
        value={obs}
        onChange={(e) => setObs(e.target.value)}
        placeholder="Nota manual para o Claude..."
        rows={3}
        className="mb-3 w-full rounded-lg border border-slate-700 bg-slate-800 p-3 text-sm text-white placeholder:text-slate-500"
      />

      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => void sendCommand(obs, true)}
          disabled={loading}
          className="flex-1 rounded-lg bg-blue-600 p-2 font-bold text-white disabled:opacity-50"
        >
          Enviar Nota
        </button>
        <button
          type="button"
          onClick={() => void sendCommand("", false)}
          disabled={loading}
          className="rounded-lg bg-slate-700 px-4 py-2 text-slate-300 disabled:opacity-50"
        >
          Limpar
        </button>
      </div>
    </div>
  );
}
