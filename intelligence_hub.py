"""
Intelligence Hub — agregação de contexto de mercado (institucional vs. varejo).

CoinDesk + subreddits via RSS (feedparser). Twitter: Nitter (RSS) com rotação de instâncias,
texto limpo para o Claude. Fallback: SOCIAL_TWITTER_SNIPPET no .env.
"""

from __future__ import annotations

import html as html_stdlib
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
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
        return texto.strip()


def obter_hub_padrao() -> IntelligenceHub:
    """Fábrica simples para o orquestrador."""
    return IntelligenceHub()


if __name__ == "__main__":
    h = obter_hub_padrao()
    print(h.obter_contexto_agregado()[:4000])
