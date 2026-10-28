"""Ratings -> Classification. Nothing in this module reads headline text to decide anything.

Three lanes, one per kind of input:
  newsliquid  NewsLiquid's AI rating. Direction = signal (long / short / neutral), confidence = score / 100,
              tickers = the coins NewsLiquid tagged that map to the bot's universe. When NewsLiquid rates each
              coin separately (coins[].score / signal), every coin keeps its own direction and confidence.
  macro       macro.py polls the BLS release page and puts the parsed numbers and the ForexFactory consensus in
              item.raw. The surprise is arithmetic: hot CPI / strong payrolls -> risk off.
  hl_listing  Hyperliquid meta diff: a new perp. The engine decides the direction from the first ticks.

The previous rules lane (regex keyword parsing, sector supply rule) and the optional LLM lane were removed in
favour of NewsLiquid; see git history before commit "Replace self-built news feeds and classifier with NewsLiquid".
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .models import Classification, Direction, EventClass, NewsItem

ENGINE_CLASS = {
    "news": EventClass.NL_NEWS,
    "listing": EventClass.NL_LISTING,
    "onchain": EventClass.NL_ONCHAIN,
    "market": EventClass.NL_MARKET,
    "meme": EventClass.NL_MEME,
    "prediction": EventClass.NL_PREDICTION,
}
_DIR = {"long": Direction.LONG, "short": Direction.SHORT}
MACRO_COINS = ("BTC", "ETH")


@dataclass
class ClassifyContext:
    symbol_map: dict[str, str]              # explicit NewsLiquid tag -> HL coin overrides, upper-case keys (universe.symbol_map)
    universe: set[str]                      # HL coins the bot trades: BTC, ETH, xyz:TSLA ...
    min_score: float = 0.0                  # NewsLiquid ratings below this never become directional
    use_coin_rating: bool = True            # prefer coins[].score / signal over the item's top-level rating
    # A coin without a rating of its own gets the item's rating only when the item tags one underlying asset
    # ("single", default); "all" applies it to every tagged coin; "none" never. The top-level score is NewsLiquid's
    # maximum over coins, so on a multi-asset item it may describe another asset (oil, not BTC).
    item_rating_fallback: str = "single"
    cpi_consensus: float | None = None      # armed by macro.py at T-15 min (fallback when item.raw lacks it)
    core_cpi_consensus: float | None = None
    nfp_consensus: float | None = None      # thousands
    fomc_expected_bps: int | None = None    # kept for the UI state file; no FOMC lane any more


def map_symbol(symbol: str, ctx: ClassifyContext) -> str | None:
    """NewsLiquid coin tag -> HL coin in the universe, or None. Exact matches only, no fuzzy names.

    "BTC" -> BTC; "XYZ-NVDA" (NewsLiquid's tag for the trade.xyz stock perp) -> xyz:NVDA; anything else only through
    universe.symbol_map. A plain "NVDA" tag is ignored on purpose: NewsLiquid always adds the XYZ- tag when it means
    the Hyperliquid stock perp (60 of 60 items in the 2026-10-28 sample), and plain tickers can collide with crypto
    tokens of the same name. xyz:SKHY and xyz:SKHX are two different perps, so tags are never aliased to each other."""
    up = (symbol or "").strip().upper()
    if not up:
        return None
    if up in ctx.symbol_map:
        hl = ctx.symbol_map[up]
        return hl if hl in ctx.universe else None
    if up.startswith(("XYZ-", "XYZ:")):
        cand = "xyz:" + up[4:]
        return cand if cand in ctx.universe else None
    return up if up in ctx.universe else None


def underlying(symbol: str) -> str:
    """"XYZ-NVDA" and "NVDA" name the same asset; used to count how many assets an item is about."""
    up = (symbol or "").strip().upper()
    return up[4:] if up.startswith(("XYZ-", "XYZ:")) else up


def event_class_for(ec: EventClass, coin: str) -> EventClass:
    """Per-coin event class: NewsLiquid news about a trade.xyz stock perp uses its own template."""
    if ec is EventClass.NL_NEWS and coin.startswith("xyz:"):
        return EventClass.NL_NEWS_STOCK
    return ec


# --------------------------------------------------------------------------- NewsLiquid lane
def classify_newsliquid(item: NewsItem, ctx: ClassifyContext) -> Classification:
    t0 = time.perf_counter()
    nl = item.raw.get("nl") or {}
    rating = nl.get("rating") or {}
    ec = ENGINE_CLASS.get(str(nl.get("engine_type") or "news"), EventClass.NOISE)

    def done(d: Direction, conf: float, tickers: list[str], reason: str, per: dict | None = None) -> Classification:
        return Classification(lane="newsliquid", event_class=ec, direction=d, confidence=round(conf, 4), tickers=tickers,
                              reason=reason[:240], per_ticker=per or {}, latency_ms=int((time.perf_counter() - t0) * 1000))

    if rating.get("score") is None:
        return done(Direction.NONE, 0.0, [], "NewsLiquid: no AI rating")
    score = float(rating["score"])
    head = f"NL {nl.get('news_type') or item.channel} {score:.0f}{('/' + rating['grade']) if rating.get('grade') else ''} {rating.get('signal', 'neutral')}"
    summary = (rating.get("summary_en") or rating.get("summary") or "").strip()

    per: dict[str, dict] = {}
    unmapped: list[str] = []
    cut_by_min = 0
    withheld = 0
    coins = nl.get("coins") or []
    assets = {underlying(c.get("symbol", "")) for c in coins} - {""}
    item_applies = ctx.item_rating_fallback == "all" or (ctx.item_rating_fallback == "single" and len(assets) == 1)
    for c in coins:
        hl = map_symbol(c.get("symbol", ""), ctx)
        if hl is None:
            unmapped.append(c.get("symbol", ""))
            continue
        if hl in per:
            continue
        own = ctx.use_coin_rating and c.get("signal") is not None and c.get("score") is not None
        if own:
            sig, sc, src = c["signal"], float(c["score"]), "coin"
        elif item_applies:
            sig, sc, src = rating.get("signal", "neutral"), score, "item"
        else:
            sig, sc, src = "neutral", score, "item-withheld"
            withheld += 1
        d = _DIR.get(sig, Direction.NONE)
        if d is not Direction.NONE and sc < ctx.min_score:
            d = Direction.NONE
            cut_by_min += 1
        per[hl] = {"direction": d.value, "confidence": round(sc / 100.0, 4), "source": src}

    if not per:
        why = f"no tagged coin in universe ({', '.join(unmapped[:6])})" if unmapped else "no coin tagged"
        return done(Direction.NONE, score / 100.0, [], f"{head} | {why}")
    directional = {k: v for k, v in per.items() if v["direction"] != "none"}
    detail = " · ".join(f"{k} {v['direction']} {v['confidence'] * 100:.0f}" for k, v in per.items())
    reason = f"{head} | {detail}" + (f" | {summary[:90]}" if summary else "")
    if withheld:
        reason += f" | item rating not applied: {len(assets)} assets tagged ({', '.join(sorted(assets)[:6])}), no per-coin rating"
    if not directional:
        best = max(v["confidence"] for v in per.values())
        note = f" (below min_score {ctx.min_score:.0f})" if cut_by_min else ("" if withheld else " (neutral)")
        return done(Direction.NONE, best, list(per), reason + note, per)
    # headline verdict = the strongest directional coin; each coin keeps its own direction / confidence in per_ticker
    top = max(directional, key=lambda k: directional[k]["confidence"])
    return done(Direction(directional[top]["direction"]), directional[top]["confidence"], list(directional), reason, per)


# --------------------------------------------------------------------------- macro lane (BLS numbers, arithmetic)
def classify_macro(item: NewsItem, ctx: ClassifyContext) -> Classification:
    t0 = time.perf_counter()
    raw = item.raw or {}
    kind = raw.get("kind")
    parsed = raw.get("parsed") or {}
    cons = raw.get("consensus") or {}
    coins = [c for c in MACRO_COINS if c in ctx.universe]

    def done(ec: EventClass, d: Direction, conf: float, reason: str) -> Classification:
        return Classification(lane="macro", event_class=ec, direction=d, confidence=conf, tickers=coins, reason=reason,
                              is_scheduled=True, latency_ms=int((time.perf_counter() - t0) * 1000))

    if kind == "cpi":
        actual, c = parsed.get("yoy"), cons.get("yoy", ctx.cpi_consensus)
        core, cc = parsed.get("core_yoy"), cons.get("core_yoy", ctx.core_cpi_consensus)
        if actual is None or c is None:
            return done(EventClass.MACRO_CPI, Direction.NONE, 0.3, "CPI release without number or consensus")
        surprise = round(float(actual) - float(c), 2)
        core_s = round(float(core) - float(cc), 2) if (core is not None and cc is not None) else 0.0
        total = round(surprise + core_s, 2)
        if abs(total) >= 0.1:
            d = Direction.SHORT if total > 0 else Direction.LONG   # hot CPI -> risk off
            return done(EventClass.MACRO_CPI, d, 0.85, f"CPI actual {actual} vs cons {c} (core Δ {core_s:+.1f})")
        return done(EventClass.MACRO_CPI, Direction.NONE, 0.5, f"CPI in line ({actual} vs {c})")
    if kind == "nfp":
        actual_k, c = parsed.get("k"), cons.get("k", ctx.nfp_consensus)
        if actual_k is None or c is None:
            return done(EventClass.MACRO_NFP, Direction.NONE, 0.3, "NFP release without number or consensus")
        surprise = float(actual_k) - float(c)
        if abs(surprise) >= 50:
            d = Direction.SHORT if surprise > 0 else Direction.LONG   # strong jobs -> higher for longer -> risk off
            return done(EventClass.MACRO_NFP, d, 0.80, f"NFP {float(actual_k):.0f}k vs cons {float(c):.0f}k")
        return done(EventClass.MACRO_NFP, Direction.NONE, 0.5, f"NFP in line ({float(actual_k):.0f}k vs {float(c):.0f}k)")
    return Classification(lane="macro", event_class=EventClass.NOISE, direction=Direction.NONE, confidence=0.0, tickers=[],
                          reason=f"unknown macro kind {kind!r}")


# --------------------------------------------------------------------------- Hyperliquid listing lane
def classify_hl_listing(item: NewsItem) -> Classification:
    coin = (item.raw or {}).get("name") or (item.suggestions[0] if item.suggestions else "")
    return Classification(lane="hl_listing", event_class=EventClass.LISTING_HL, direction=Direction.LONG, confidence=0.9,
                          tickers=[coin] if coin else [], reason="new HL perp (direction from first ticks)")


def classify(item: NewsItem, ctx: ClassifyContext) -> Classification:
    raw = item.raw or {}
    if isinstance(raw.get("nl"), dict):
        return classify_newsliquid(item, ctx)
    if item.source == "bls_poll" or raw.get("kind") in ("cpi", "nfp"):
        return classify_macro(item, ctx)
    if item.source == "hl_listings":
        return classify_hl_listing(item)
    return Classification(lane="none", event_class=EventClass.NOISE, direction=Direction.NONE, confidence=0.0, tickers=[],
                          reason="no rating: not a NewsLiquid item")
