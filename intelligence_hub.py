"""
Intelligence Hub — agregação de contexto de mercado (institucional vs. varejo).

CoinDesk + subreddits via RSS (feedparser). Twitter: Nitter (RSS) com rotação de instâncias,
texto limpo para o Claude. Fallback: SOCIAL_TWITTER_SNIPPET no .env.
"""

from __future__ import annotations

import asyncio
import base64
import html as html_stdlib
import io
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_CONTEXTO_AGREGADO_CHARS = 1500
TRUNCATION_SUFFIX = "... [TRUNCADO PARA POUPAR TOKENS]"

import feedparser
import mplfinance as mpf
import replicate
from dotenv import load_dotenv

load_dotenv()

# Instâncias Nitter (ordem = prioridade de tentativa).
NITTER_INSTANCES: list[str] = [
    "nitter.net",
    "nitter.cz",
    "nitter.it",
    "nitter.privacydev.net",
    "nitter.at",
]

USER_AGENT_NITTER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Handles reais no X (URLs Nitter: https://instancia/handle/rss)
TWITTER_ALPHA_ACCOUNTS: tuple[tuple[str, str], ...] = (
    ("VitalikButerin", "@VitalikButerin"),
    ("whale_alert", "@WhaleAlert"),
    ("WatcherGuru", "@WatcherGuru"),
)

_RE_HTML_TAGS = re.compile(r"<[^>]+>", re.DOTALL)
_RE_URLS = re.compile(r"https?://[^\s\]\)<>\"']+")
_RE_ESPACOS = re.compile(r"\s+")

# Notícias com mais idade que isto são descartadas (ruído em cripto).
NOTICIAS_MAX_IDADE_HORAS = 2
RSS_SENTIMENT_FEEDS: tuple[str, ...] = (
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
)
REPLICATE_FINBERT_MODEL = (
    os.getenv("REPLICATE_FINBERT_MODEL", "anthropic/claude-4.5-sonnet").strip()
)
REPLICATE_TS_FORECAST_MODEL = (
    os.getenv("REPLICATE_TS_FORECAST_MODEL", "amazon/chronos-forecasting-large").strip()
)
REPLICATE_VISION_MODEL = (
    os.getenv("REPLICATE_VISION_MODEL", "yorickvp/llava-13b").strip()
)


def _timestamp_da_entrada_feed(ent: Any) -> datetime | None:
    """Data UTC da entrada RSS/Atom (feedparser), ou None se não for parseável."""
    get = getattr(ent, "get", None)
    if not callable(get):
        return None
    pp = get("published_parsed") or get("updated_parsed")
    if not pp:
        return None
    try:
        return datetime(
            pp.tm_year,
            pp.tm_mon,
            pp.tm_mday,
            pp.tm_hour,
            pp.tm_min,
            pp.tm_sec,
            tzinfo=timezone.utc,
        )
    except (TypeError, ValueError, AttributeError):
        return None


def filtrar_noticias_recentes(
    noticias: list[dict[str, Any]],
    *,
    horas: int = NOTICIAS_MAX_IDADE_HORAS,
) -> list[dict[str, Any]]:
    """
    Remove itens com mais de `horas` de idade (UTC). Exige `timestamp` timezone-aware.
    Itens sem `timestamp` são descartados (idade desconhecida = não confiável para «freshness»).
    """
    agora = datetime.now(timezone.utc)
    limite = agora - timedelta(hours=horas)
    noticias_filtradas = [
        n
        for n in noticias
        if n.get("timestamp") is not None and n["timestamp"] > limite
    ]
    if noticias:
        descartadas = len(noticias) - len(noticias_filtradas)
        if descartadas:
            print(
                f"[INTELLIGENCE_HUB] 🧹 [SENTIMENT DECAY] {descartadas} notícias antigas "
                f"descartadas (janela {horas}h UTC)."
            )
    return noticias_filtradas


def limpar_texto_feed_bruto(texto: str, max_chars: int = 380) -> str:
    """
    Remove tags HTML, decodifica entidades, substitui URLs por marcador curto,
    compacta espaços — menos tokens para o Claude 3.5 Sonnet.
    """
    if not texto:
        return ""
    t = html_stdlib.unescape(texto)
    t = _RE_HTML_TAGS.sub(" ", t)
    t = _RE_URLS.sub("[link]", t)
    t = _RE_ESPACOS.sub(" ", t).strip()
    if len(t) > max_chars:
        corte = t[: max_chars - 1]
        t = corte.rsplit(" ", 1)[0] + "…"
    return t


def _aliases_simbolo_para_noticias(simbolo: str) -> set[str]:
    """Aliases para filtrar manchetes (ticker e nome canónico)."""
    s = str(simbolo or "").strip().upper()
    if not s:
        s = "BTCUSDT"
    s = s.replace("/", "").replace(":USDT", "").replace(":USDC", "")
    base = s
    for q in ("USDT", "USDC", "BUSD", "USD"):
        if base.endswith(q) and len(base) > len(q):
            base = base[: -len(q)]
            break
    aliases = {base}
    mapa = {
        "BTC": {"BTC", "BITCOIN"},
        "ETH": {"ETH", "ETHEREUM"},
        "SOL": {"SOL", "SOLANA"},
        "BNB": {"BNB", "BINANCE COIN", "BINANCECOIN"},
        "XRP": {"XRP", "RIPPLE"},
    }
    aliases.update(mapa.get(base, {base}))
    return {a.upper() for a in aliases if a}


def _headline_menciona_alias(headline: str, aliases: set[str]) -> bool:
    t = str(headline or "").upper()
    if not t:
        return False
    for a in aliases:
        if re.search(rf"\b{re.escape(a)}\b", t):
            return True
    return False


def _score_finbert_replicate_sync(manchetes: list[str]) -> int:
    """
    Classifica sentimento agregado via Replicate (modelo FinBERT-like configurável).
    """
    if not manchetes:
        return 0
    token = (os.getenv("REPLICATE_API_TOKEN") or "").strip()
    if not token:
        return 0
    prompt = (
        "You are a financial-news sentiment classifier. "
        "Return ONLY JSON in schema "
        '{"score": -1|0|1, "label": "bearish|neutral|bullish"} '
        "for these crypto headlines.\n\nHEADLINES:\n- "
        + "\n- ".join(manchetes[:20])
    )
    try:
        out = replicate.run(
            REPLICATE_FINBERT_MODEL,
            input={"prompt": prompt, "temperature": 0.1, "max_tokens": 120},
        )
        txt = out if isinstance(out, str) else "".join(out)
        txt = str(txt or "").strip()
        if not txt:
            return 0
        try:
            obj = json.loads(txt)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", txt)
            if not m:
                return 0
            obj = json.loads(m.group(0))
        score = int(float(obj.get("score", 0)))
        if score < 0:
            return -1
        if score > 0:
            return 1
        return 0
    except Exception:
        return 0


async def analisar_sentimento_noticias(simbolo: str | None = None) -> int:
    """
    Lê RSS públicos (Cointelegraph + CoinDesk), filtra manchetes da última hora por símbolo
    e devolve score final {-1,0,1}. Em falha, retorna 0 por segurança.
    """
    try:
        sym = simbolo or os.getenv("SYMBOL", "BTCUSDT")
        aliases = _aliases_simbolo_para_noticias(sym)
        agora = datetime.now(timezone.utc)
        limite = agora - timedelta(hours=1)

        headlines: list[str] = []
        for feed_url in RSS_SENTIMENT_FEEDS:
            try:
                parsed = await asyncio.to_thread(feedparser.parse, feed_url)
                for ent in list(getattr(parsed, "entries", []) or []):
                    ts = _timestamp_da_entrada_feed(ent)
                    if ts is None or ts < limite:
                        continue
                    title = limpar_texto_feed_bruto((ent.get("title") or "").strip(), max_chars=220)
                    if not title:
                        continue
                    if _headline_menciona_alias(title, aliases):
                        headlines.append(title)
            except Exception:
                continue

        if not headlines:
            return 0
        score = await asyncio.to_thread(_score_finbert_replicate_sync, headlines)
        if score < 0:
            return -1
        if score > 0:
            return 1
        return 0
    except Exception:
        return 0


def _safe_float_from_any(v: Any) -> float | None:
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _extract_forecast_values(payload: Any) -> list[float]:
    """
    Extrai lista de floats de respostas heterogéneas do Replicate.
    """
    vals: list[float] = []
    if isinstance(payload, (int, float)):
        f = _safe_float_from_any(payload)
        return [f] if f is not None else []
    if isinstance(payload, list):
        for item in payload:
            vals.extend(_extract_forecast_values(item))
        return vals
    if isinstance(payload, dict):
        for k in ("forecast", "predictions", "prediction", "output", "data", "values"):
            if k in payload:
                vals.extend(_extract_forecast_values(payload.get(k)))
        return vals
    if isinstance(payload, str):
        s = payload.strip()
        if not s:
            return []
        try:
            obj = json.loads(s)
            return _extract_forecast_values(obj)
        except json.JSONDecodeError:
            nums = re.findall(r"-?\d+(?:\.\d+)?", s)
            out: list[float] = []
            for n in nums:
                f = _safe_float_from_any(n)
                if f is not None:
                    out.append(f)
            return out
    return []


def _replicate_forecast_sync(serie_close: list[float]) -> list[float]:
    """
    Chamada síncrona ao Replicate para previsão de série temporal.
    Executar via `asyncio.to_thread`.
    """
    if not serie_close or len(serie_close) < 10:
        return []
    token = (os.getenv("REPLICATE_API_TOKEN") or "").strip()
    if not token:
        return []
    try:
        out = replicate.run(
            REPLICATE_TS_FORECAST_MODEL,
            input={
                "series": serie_close,
                "horizon": 8,
            },
        )
    except Exception:
        return []
    if isinstance(out, str):
        return _extract_forecast_values(out)
    if isinstance(out, dict):
        return _extract_forecast_values(out)
    if isinstance(out, list):
        return _extract_forecast_values(out)
    try:
        txt = "".join(out)
        return _extract_forecast_values(txt)
    except Exception:
        return []


async def prever_proximos_candles(df_historico: Any) -> dict[str, Any]:
    """
    Recebe OHLCV (DataFrame-like), envia últimos 100 closes ao Replicate (Chronos),
    calcula média das próximas 3 velas previstas e compara com o preço atual.
    """
    try:
        if df_historico is None:
            return {"tendencia_alta": None, "preco_alvo": None}
        close_series = getattr(df_historico, "get", lambda *_: None)("close")
        if close_series is None:
            return {"tendencia_alta": None, "preco_alvo": None}
        if hasattr(close_series, "tolist"):
            closes_raw = close_series.tolist()
        else:
            closes_raw = list(close_series)
        closes = [float(x) for x in closes_raw if x is not None]
        if len(closes) < 5:
            return {"tendencia_alta": None, "preco_alvo": None}
        ultimos_100 = closes[-100:]
        preco_atual = float(ultimos_100[-1])
        previsoes = await asyncio.to_thread(_replicate_forecast_sync, ultimos_100)
        if len(previsoes) < 3:
            return {"tendencia_alta": None, "preco_alvo": None}
        alvo = float(sum(previsoes[:3]) / 3.0)
        return {
            "tendencia_alta": bool(alvo > preco_atual),
            "preco_alvo": float(alvo),
        }
    except Exception:
        return {"tendencia_alta": None, "preco_alvo": None}


def _dataframe_para_candle_png_data_uri(df_historico: Any) -> str | None:
    """
    Gera candle chart + volume dos últimos 50 períodos em memória (sem gravar em disco).
    Retorna data URI base64 para envio multimodal.
    """
    fig = None
    buf: io.BytesIO | None = None
    try:
        # Pandas-like obrigatório; sem ele aborta silenciosamente.
        df50 = df_historico.tail(50).copy()
        if len(df50) < 10:
            return None
        # mplfinance espera colunas OHLCV com estes nomes exatos.
        cols = {c.lower(): c for c in list(getattr(df50, "columns", []))}
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(set(cols.keys())):
            return None
        df50 = df50.rename(
            columns={
                cols["open"]: "Open",
                cols["high"]: "High",
                cols["low"]: "Low",
                cols["close"]: "Close",
                cols["volume"]: "Volume",
            }
        )
        # Index datetime melhora render do eixo x; fallback mantém índice original.
        idx = getattr(df50, "index", None)
        if idx is not None and getattr(idx, "dtype", None) is not None:
            try:
                if str(idx.dtype).lower() not in ("datetime64[ns]", "datetime64[ns, utc]"):
                    ts_col = None
                    for cand in ("timestamp", "time", "date", "datetime"):
                        if cand in cols:
                            ts_col = cols[cand]
                            break
                    if ts_col is not None:
                        dt = df50[ts_col]
                        if hasattr(dt, "dtype"):
                            df50.index = __import__("pandas").to_datetime(dt, utc=True)
            except Exception:
                pass

        fig, _ax = mpf.plot(
            df50[["Open", "High", "Low", "Close", "Volume"]],
            type="candle",
            style="charles",
            volume=True,
            returnfig=True,
        )
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return None
    finally:
        try:
            if fig is not None:
                fig.clf()
        except Exception:
            pass
        try:
            if buf is not None:
                buf.close()
        except Exception:
            pass


def _confirmar_entrada_visao_sync(df_historico: Any, tipo_ordem: str) -> bool:
    """
    Executa confirmação visual multimodal. True = bloquear ordem.
    """
    try:
        token = (os.getenv("REPLICATE_API_TOKEN") or "").strip()
        if not token:
            return False
        ordem = str(tipo_ordem or "").strip().upper()
        if ordem not in ("LONG", "SHORT"):
            ordem = "LONG"
        img_uri = _dataframe_para_candle_png_data_uri(df_historico)
        if not img_uri:
            return False
        prompt = (
            f"Você é um trader institucional. O sistema quer executar uma ordem de {ordem} "
            "agora. Olhando este gráfico, há alguma barreira óbvia, divergência ou resistência "
            "forte contra essa operação? Responda APENAS com a palavra SIM (para bloquear a ordem) "
            "ou NAO (para permitir)."
        )
        out = replicate.run(
            REPLICATE_VISION_MODEL,
            input={
                "prompt": prompt,
                "image": img_uri,
                "temperature": 0.0,
                "max_tokens": 8,
            },
        )
        txt = out if isinstance(out, str) else "".join(out)
        ans = str(txt or "").strip().upper()
        return "SIM" in ans
    except Exception:
        return False


async def confirmar_entrada_visao(df_historico: Any, tipo_ordem: str) -> bool:
    """
    Juiz Final visual.
    Retorna True (bloquear) ou False (permitir). Falhas de API => False.
    """
    try:
        return bool(await asyncio.to_thread(_confirmar_entrada_visao_sync, df_historico, tipo_ordem))
    except Exception:
        return False


def buscar_tweets_nitter(
    username: str,
    limite: int = 12,
    *,
    nitter_bases: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Obtém entradas do RSS Nitter para um utilizador (handle sem @ na URL).

    Rotação: `{base}/{username}/rss` com `base` em https://nitter… — próxima em erro/timeout.
    Se `nitter_bases` for None, usa NITTER_INSTANCES (hostname) com prefixo https://.
    Devolve lista de dicts: text (limpo), link, published.
    Se todas falharem: lista vazia + log [INTELLIGENCE_HUB].
    """
    u = username.strip().lstrip("@")
    if not u:
        return []

    if nitter_bases:
        bases_iter: list[str] = [b.rstrip("/") for b in nitter_bases]
    else:
        bases_iter = [f"https://{h}" for h in NITTER_INSTANCES]

    ultimo_motivo = ""
    for base in bases_iter:
        url = f"{base}/{u}/rss"
        print(f"[NITTER_ROUTER] A testar instância={base} | feed=@{u}/rss | url={url}")
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT_NITTER, "Accept": "application/rss+xml,*/*"},
            )
            with urllib.request.urlopen(req, timeout=18) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            print(
                f"[NITTER_ROUTER] Falha HTTP {e.code} em {base} para @{u} — próxima instância."
            )
            ultimo_motivo = f"HTTP {e.code}"
            continue
        except TimeoutError as e:
            print(
                f"[NITTER_ROUTER] Timeout em {base} para @{u} ({e!s}) — próxima instância."
            )
            ultimo_motivo = "timeout"
            continue
        except (urllib.error.URLError, OSError) as e:
            print(
                f"[NITTER_ROUTER] Erro de rede em {base} para @{u}: {e!s} — próxima instância."
            )
            ultimo_motivo = str(e)
            continue

        parsed = feedparser.parse(body)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            print(
                f"[NITTER_ROUTER] RSS inválido ou vazio após parse em {base} para @{u} — "
                "próxima instância."
            )
            continue

        saida: list[dict[str, Any]] = []
        for ent in parsed.entries[:limite]:
            bruto = (ent.get("title") or "") + " " + (ent.get("summary") or "")
            texto_limpo = limpar_texto_feed_bruto(bruto.strip())
            if not texto_limpo:
                continue
            ts = _timestamp_da_entrada_feed(ent)
            item: dict[str, Any] = {
                "text": texto_limpo,
                "link": limpar_texto_feed_bruto((ent.get("link") or "")[:200]),
                "published": (ent.get("published") or ent.get("updated") or "").strip(),
            }
            if ts is not None:
                item["timestamp"] = ts
            saida.append(item)

        if saida:
            print(
                f"[NITTER_ROUTER] OK instância={base} | @{u} | entradas={len(saida)} "
                f"(primeira instância bem-sucedida)."
            )
            return saida

        print(f"[NITTER_ROUTER] Feed sem entradas úteis em {base} para @{u} — próxima.")

    print("[INTELLIGENCE_HUB] Twitter indisponível em todas as instâncias.")
    if ultimo_motivo:
        print(f"[NITTER_ROUTER] Último motivo para @{u}: {ultimo_motivo}")
    return []


# Compatibilidade com código antigo
buscar_tweets_via_nitter = buscar_tweets_nitter


class IntelligenceHub:
    """
    Coleta e formata uma «foto» do mercado: notícias (institucional) vs. redes (varejo).
    """

    def __init__(
        self,
        limite_rss: int = 5,
        limite_reddit: int = 3,
        limite_twitter: int = 3,
        contas_twitter_alpha: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        self.sources = {
            "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "reddit_eth": "https://www.reddit.com/r/ethereum/new/.rss",
            "reddit_ethtrader": "https://www.reddit.com/r/ethtrader/new/.rss",
        }

        # Lista de instâncias Nitter para rotação (URLs base)
        self.nitter_instances = [
            "https://nitter.net",
            "https://nitter.cz",
            "https://nitter.it",
            "https://nitter.privacydev.net",
        ]

        self.limite_rss = limite_rss
        self.limite_reddit = limite_reddit
        self.limite_twitter_por_conta = limite_twitter
        self._contas_alpha = contas_twitter_alpha or TWITTER_ALPHA_ACCOUNTS

        cd = os.getenv("COINDESK_RSS", "").strip()
        if cd:
            self.sources["coindesk"] = cd

    def buscar_noticias_rss(self, url: str, limite: int) -> list[dict[str, Any]]:
        """
        Extrai entradas de um feed RSS/Atom (CoinDesk, subreddits .rss, etc.).

        Devolve lista de dicts com: title, link, published (quando existir).
        Em falha, regista aviso e devolve lista vazia (não propaga exceção).
        """
        try:
            parsed = feedparser.parse(url)
            if getattr(parsed, "bozo", False) and not parsed.entries:
                print(
                    f"[INTELLIGENCE_HUB] Aviso: feed com aviso de parse ({url}). "
                    "A tentar usar entradas disponíveis."
                )
            saida: list[dict[str, Any]] = []
            for ent in parsed.entries[:limite]:
                title = (ent.get("title") or "").strip()
                if not title:
                    continue
                ts = _timestamp_da_entrada_feed(ent)
                row: dict[str, Any] = {
                    "title": limpar_texto_feed_bruto(title, max_chars=500),
                    "link": (ent.get("link") or "").strip(),
                    "published": (ent.get("published") or ent.get("updated") or "").strip(),
                }
                if ts is not None:
                    row["timestamp"] = ts
                saida.append(row)
            return saida
        except Exception as e:  # noqa: BLE001
            print(
                f"[INTELLIGENCE_HUB] RSS indisponível para {url!r}: {e}. "
                "Esta fonte será omitida; o pipeline continua."
            )
            return []

    def _formatar_camada_twitter_nitter(self) -> str:
        """Monta o bloco Twitter só com Nitter + texto limpo (Alpha: Vitalik, Whale, Watcher)."""
        blocos: list[str] = []
        for handle_slug, label_exibicao in self._contas_alpha:
            itens = buscar_tweets_nitter(
                handle_slug,
                self.limite_twitter_por_conta,
                nitter_bases=self.nitter_instances,
            )
            if not itens:
                blocos.append(
                    f"[{label_exibicao} — Twitter / X]\n"
                    "(sem entradas neste ciclo — ver logs [NITTER_ROUTER] / [INTELLIGENCE_HUB].)"
                )
                continue
            itens = filtrar_noticias_recentes(itens)
            if not itens:
                blocos.append(
                    f"[{label_exibicao} — Twitter / X]\n"
                    "(apenas entradas >2h ou sem data — descartadas por [SENTIMENT DECAY].)"
                )
                continue
            linhas = [f"- {x['text']}" for x in itens]
            blocos.append(f"[{label_exibicao} — Twitter / X — varejo]\n" + "\n".join(linhas))

        if not blocos:
            return (
                "[Twitter / X — varejo]\n"
                "(Nitter: nenhum bloco gerado.)"
            )
        return "\n\n".join(blocos)

    def coletar_twitter_alpha(self, tweets: list[str] | None = None) -> str:
        """
        Prioridade: lista manual → **Nitter real** (contas Alpha) → SOCIAL_TWITTER_SNIPPET.
        """
        if tweets:
            linhas = [f"- {limpar_texto_feed_bruto(t)}" for t in tweets if t and str(t).strip()]
            if linhas:
                return "[Twitter / X — varejo]\n" + "\n".join(linhas)

        nitter_txt = self._formatar_camada_twitter_nitter()
        tem_itens = any(ln.strip().startswith("- ") for ln in nitter_txt.splitlines())

        if tem_itens:
            return "[Twitter / X — varejo — Nitter RSS]\n\n" + nitter_txt

        snippet = (os.getenv("SOCIAL_TWITTER_SNIPPET") or "").strip()
        if snippet:
            print(
                "[INTELLIGENCE_HUB] Twitter: Nitter sem texto útil; "
                "a usar SOCIAL_TWITTER_SNIPPET do .env."
            )
            return (
                "[Twitter / X — varejo — fallback .env]\n"
                + limpar_texto_feed_bruto(snippet, max_chars=2500)
            )

        print("[INTELLIGENCE_HUB] Twitter: sem Nitter nem SOCIAL_TWITTER_SNIPPET.")
        return "[Twitter / X — varejo]\n" + nitter_txt

    def _formatar_bloco_institucional(self) -> str:
        itens = self.buscar_noticias_rss(self.sources["coindesk"], self.limite_rss)
        itens = filtrar_noticias_recentes(itens)
        if not itens:
            return (
                "[CoinDesk — institucional]\n"
                "(Sem manchetes neste ciclo — feed indisponível, vazio ou só notícias >2h / sem data.)"
            )
        linhas = [f"- {x['title']}" for x in itens]
        return "[CoinDesk — institucional / notícias]\n" + "\n".join(linhas)

    def _formatar_bloco_reddit_rss(self) -> str:
        blocos: list[str] = []
        feeds: list[tuple[str, str]] = [
            (self.sources["reddit_eth"], "r/ethereum"),
            (self.sources["reddit_ethtrader"], "r/ethtrader"),
        ]
        for feed_url, label in feeds:
            itens = self.buscar_noticias_rss(feed_url, self.limite_reddit)
            itens = filtrar_noticias_recentes(itens)
            if not itens:
                print(
                    f"[INTELLIGENCE_HUB] Reddit {label}: sem entradas (rate limit, bloqueio "
                    "ou feed vazio). A seguir com outras fontes."
                )
                continue
            linhas = [f"- {x['title']}" for x in itens]
            blocos.append(f"[{label} — varejo / Reddit]\n" + "\n".join(linhas))
        if not blocos:
            return (
                "[Reddit — varejo]\n"
                "(Subreddits indisponíveis neste ciclo; continuar só com notícias e Twitter.)"
            )
        return "\n\n".join(blocos)

    def obter_contexto_agregado(self) -> str:
        """
        Foto do mercado: institucional + Reddit + Twitter via Nitter (sem simulador).
        """
        print("[INTELLIGENCE_HUB] A montar contexto agregado (institucional + varejo)...")

        inst = self._formatar_bloco_institucional()
        reddit_txt = self._formatar_bloco_reddit_rss()
        # Camada Twitter: apenas pipeline Nitter + fallbacks em coletar_twitter_alpha
        twitter_txt = self.coletar_twitter_alpha()

        texto = f"""=== CAMADA INSTITUCIONAL (notícias / mídia) ===

{inst}

=== CAMADA DE VAREJO — Reddit (comunidade) ===

{reddit_txt}

=== CAMADA DE VAREJO — Twitter / X (fluxo social) ===

{twitter_txt}
"""
        texto_final = texto.strip()
        if len(texto_final) <= MAX_CONTEXTO_AGREGADO_CHARS:
            return texto_final

        # Truncation rígida apenas no texto livre do hub (notícias/social),
        # preservando o bloco técnico/ML que é enviado separadamente ao Claude.
        limite_base = max(0, MAX_CONTEXTO_AGREGADO_CHARS - len(TRUNCATION_SUFFIX))
        return texto_final[:limite_base].rstrip() + TRUNCATION_SUFFIX


def obter_hub_padrao() -> IntelligenceHub:
    """Fábrica simples para o orquestrador."""
    return IntelligenceHub()


if __name__ == "__main__":
    h = obter_hub_padrao()
    print(h.obter_contexto_agregado()[:4000])
