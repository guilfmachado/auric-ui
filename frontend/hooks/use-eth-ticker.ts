"use client";

import { useCallback, useEffect, useState } from "react";

const BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDC";

export type EthTickerState = {
  price: number | null;
  changePct: number | null;
  loading: boolean;
  refetch: () => void;
};

export function useEthTicker(pollMs = 5_000): EthTickerState {
  const [price, setPrice] = useState<number | null>(null);
  const [changePct, setChangePct] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchTicker = useCallback(async () => {
    try {
      const res = await fetch(BINANCE_24H);
      if (!res.ok) return;
      const data: { lastPrice?: string; priceChangePercent?: string } =
        await res.json();
      const p = parseFloat(data.lastPrice ?? "");
      const c = parseFloat(data.priceChangePercent ?? "");
      if (!Number.isNaN(p)) setPrice(p);
      if (!Number.isNaN(c)) setChangePct(c);
    } catch {
      /* mantém último valor */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchTicker();
    const id = window.setInterval(() => void fetchTicker(), pollMs);
    return () => window.clearInterval(id);
  }, [fetchTicker, pollMs]);

  const refetch = useCallback(() => {
    void fetchTicker();
  }, [fetchTicker]);

  return { price, changePct, loading, refetch };
}
