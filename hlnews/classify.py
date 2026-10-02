"""Two-lane classifier.

Rules lane: deterministic regex/keyword/number parsing, sub-millisecond, high precision on a narrow set of
event classes. LLM lane: optional, 0.5-3 s, broader coverage, used to confirm/size or to catch what the
rules missed. The fusion policy lives in engine.py.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from .models import Classification, Direction, EventClass, NewsItem

log = logging.getLogger("classify")

# --------------------------------------------------------------------------- helpers
_NUM = r"(-?\d+(?:\.\d+)?)\s*%"


def _find_pct(text: str, label_pat: str, max_gap: int = 40) -> float | None:
    """Find 'LABEL ... 3.1%' style numbers. Returns the first percentage after the label.

    The gap between label and number may not contain digits, '-' or ';' and may not contain the word 'None'
    (the BLS poller writes 'FORECAST None%' when ForexFactory had no consensus; that must parse as missing,
    not skip ahead to the next number in the sentence)."""
    m = re.search(label_pat + r"(?:(?!None)[^\d\-;]){0," + str(max_gap) + r"}" + _NUM, text, flags=re.I)
    return float(m.group(1)) if m else None


def _has(text: str, *pats: str) -> bool:
    return any(re.search(p, text, flags=re.I) for p in pats)


@dataclass
class RuleContext:
    aliases: dict[str, str]          # free text -> HL coin
    universe: set[str]               # HL coins we trade
    trusted_accounts: set[str]
    cpi_consensus: float | None = None    # headline CPI YoY consensus, set by scheduler before 08:30 ET
    core_cpi_consensus: float | None = None
    nfp_consensus: float | None = None    # thousands
    fomc_expected_bps: int | None = None  # expected change, e.g. -25
    sectors: dict = field(default_factory=dict)          # config.yaml `sectors`
    strong_publishers: set[str] = field(default_factory=set)  # publishers whose google_news/wallstreetcn items count as trusted


def is_trusted_source(item: NewsItem, ctx: RuleContext) -> bool:
    """One definition shared by the rules lane and the engine's corroboration step."""
    if (item.account or "").lower() in {a.lower() for a in ctx.trusted_accounts}:
        return True
    if item.channel in {"Binance EN", "Upbit", "Bithumb", "usGov"} or item.source in {"hl_listings", "bls_poll"}:
        return True
    # strong publishers count regardless of transport (Google News, 见闻, Tree of Alpha "Blogs", replay)
    pub = (item.account or item.channel or "").strip().lower()
    if pub and any(p.lower() == pub or (len(p) >= 6 and p.lower() in pub) for p in ctx.strong_publishers):
        return True
    return False


# --------------------------------------------------------------------------- sector supply rule
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
# capacity / output vocabulary. English needs a verb and an object; Chinese and Japanese compounds carry both.
_EXPAND_EN = re.compile(r"\b(?:double|doubles|doubling|triple|boost|boosts|boosting|expand|expands|expanding|increase|increases|increasing|ramp(?:s|ing)? up|raise|raises|raising|add|adds|adding|build|builds|building|invest(?:s|ing)?)\b[^.;]{0,60}\b(?:capacity|production|output|supply|plant|factory|fab|line)\b|\bnew (?:plant|factory|fab)\b|\bcapacity (?:expansion|increase)\b", re.I)
_CUT_EN = re.compile(r"\b(?:cut|cuts|cutting|reduce|reduces|reducing|slash|slashes|slashing|halt|halts|halting|suspend|suspends|idle|idles|idling|shut|shuts|shutting|lower|lowers|lowering)\b[^.;]{0,60}\b(?:capacity|production|output|supply|plant|factory|fab|line|wafer starts)\b|\b(?:plant|factory|fab) (?:fire|outage|shutdown|closure|explosion)\b|\bproduction (?:cut|halt|suspension)\b", re.I)
_EXPAND_ZH = re.compile(r"扩产|扩建|增产|(?:产能|产量|供应能力|供应|供给|生产能力|供給|生産能力).{0,6}(?:翻倍|倍增|翻番|提升|扩大|增加|倍増|拡大|増強|増)|(?:提高|提升|扩大|增加|翻倍).{0,8}(?:产能|产量|供应)|新建(?:工厂|产线|晶圆厂)|加大投资|投资.{0,12}(?:扩|建)|増産|新工場")
_CUT_ZH = re.compile(r"减产|停产|削减产能|产能(?:削减|下调|收缩)|关停|停工|工厂(?:火灾|爆炸|停电|事故)|減産|生産(?:停止|縮小)|工場(?:火災|停止|閉鎖)")
_SUPPLY_DENY = re.compile(r"\bdenies\b|\bdenied\b|\bnot planning\b|\bno plan\b|否认|否定|rumou?r|unconfirmed", re.I)


def _mention_pos(text_low: str, name: str) -> int:
    """Start index of the first mention of `name`, or -1."""
    if _CJK.search(name):
        return text_low.find(name.lower())
    m = re.search(rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])", text_low)
    return m.start() if m else -1


def _mentions(text_low: str, name: str) -> bool:
    return _mention_pos(text_low, name) >= 0


def classify_sector(item: NewsItem, ctx: RuleContext, trusted: bool) -> Classification | None:
    """Capacity expansion / cut by a maker -> direction for its tradeable peers. Returns None when nothing matches."""
    t0 = time.perf_counter()
    text = item.title
    low = text.lower()
    for sector, sc in (ctx.sectors or {}).items():
        mentions = {m: _mention_pos(low, m) for m in sc.get("makers", []) if _mentions(low, m)}
        if not mentions:
            continue
        has_product = any(_mentions(low, w) for w in sc.get("product_words", [])) if sc.get("product_words") else True
        em = _EXPAND_EN.search(text) or _EXPAND_ZH.search(text)
        cm = _CUT_EN.search(text) or _CUT_ZH.search(text)
        if (em is None and cm is None) or (em is not None and cm is not None):
            continue
        if _SUPPLY_DENY.search(text):
            continue
        expand = em is not None
        verb_pos = (em or cm).start()
        # Actors = makers named in the same clause as the capacity verb, before it ("SK Hynix and Samsung to expand ...").
        # A clause starts after sentence punctuation or a subordinating word ("Seagate falls as Toshiba plans to double").
        # A maker followed by "shares/stock/股/跌/涨" is a price-reaction mention, not an actor.
        clause_start = 0
        for m in re.finditer(r"[.;。；，,]|\b(?:as|after|while|amid|following|on|because)\b", low[:verb_pos]):
            clause_start = m.end()
        reaction = re.compile(r"^.{0,3}(?:shares?|stock|股价|股|跌|涨|急落|下落|上昇)")
        in_clause = {m: p for m, p in mentions.items() if clause_start <= p < verb_pos and not reaction.search(low[p + len(m):p + len(m) + 12])}
        if in_clause:
            actor_names = set(in_clause)
        else:
            before = {m: p for m, p in mentions.items() if p < verb_pos and not reaction.search(low[p + len(m):p + len(m) + 12])}
            pick = max(before, key=before.get) if before else min(mentions, key=mentions.get)
            actor_names = {pick}
        # names that alias the same company (e.g. "sk hynix" and "hynix") count as the same actor
        actor_names |= {m for m, p in mentions.items() for a in list(actor_names) if m != a and (m in a or a in m)}
        actor_name = "+".join(sorted(actor_names, key=lambda m: mentions[m]))
        actor_map = sc.get("actor_ticker", {}) or {}
        actors = {actor_map[m] for m in actor_names if m in actor_map}
        peers = [tk for tk in sc.get("tickers", []) if tk in ctx.universe and tk not in actors]
        if not peers:
            return Classification(lane="rules", event_class=EventClass.SUPPLY_EXPANSION if expand else EventClass.SUPPLY_CUT,
                                  direction=Direction.NONE, confidence=0.3, tickers=[], reason=f"{sector}: actor {actor_name} is the only tradeable name",
                                  latency_ms=int((time.perf_counter() - t0) * 1000))
        ec = EventClass.SUPPLY_EXPANSION if expand else EventClass.SUPPLY_CUT
        d = Direction.SHORT if expand else Direction.LONG
        # confidence: product word present and trusted publisher -> 0.8; product word only -> 0.7; maker-only -> 0.6
        conf = 0.8 if (has_product and trusted) else (0.7 if has_product else 0.6)
        reason = f"{sector}: {actor_name} {'expands capacity' if expand else 'cuts output'} -> peers {'short' if expand else 'long'}"
        return Classification(lane="rules", event_class=ec, direction=d, confidence=conf, tickers=peers, reason=reason,
                              latency_ms=int((time.perf_counter() - t0) * 1000))
    return None


def map_tickers(item: NewsItem, ctx: RuleContext) -> list[str]:
    text = item.title.lower()
    out: list[str] = []
    for c in item.suggestions:
        hl = ctx.aliases.get(c.lower())
        if hl and hl in ctx.universe and hl not in out:
            out.append(hl)
    for word, hl in ctx.aliases.items():
        if hl in ctx.universe and hl not in out and re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text):
            out.append(hl)
    return out


# --------------------------------------------------------------------------- rules lane
def classify_rules(item: NewsItem, ctx: RuleContext) -> Classification:
    t0 = time.perf_counter()
    text = item.title
    low = text.lower()
    tickers = map_tickers(item, ctx)
    trusted = is_trusted_source(item, ctx)
    # "reportedly" / "sources say" are normal wire attribution (digitimes re-reporting Nikkei) and are NOT rumor markers
    is_rumor = _has(text, r"\brumou?r", r"\bunconfirmed", r"\bunverified", r"\bspeculat", r"\bmay be\b", r"\?\s*$")

    def done(ec: EventClass, d: Direction, conf: float, reason: str, tk: list[str] | None = None, sched=False, mag=None):
        return Classification(
            lane="rules", event_class=ec, direction=d, confidence=conf, tickers=tk if tk is not None else tickers,
            reason=reason, is_rumor=is_rumor, is_scheduled=sched, magnitude_usd=mag,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    # ---- denials / corrections: reverse the original read, never trade on them unless trusted
    denial = _has(text, r"\bnot approved\b", r"\bhas not\b", r"\bcompromised\b", r"\bunauthori[sz]ed (?:tweet|post)", r"\bdenies\b", r"\bdenied\b",
                  r"\bfalse report", r"\bfake\b", r"\bretract", r"\bcorrection\b")

    # ---- scheduled macro: only act when we can parse the number and have a consensus to compare
    if _has(text, r"\bCPI\b", r"consumer price index"):
        actual = _find_pct(text, r"(?:headline\s+)?CPI(?:\s*\([A-Z]{3}\))?(?:\s+YoY|\s+Y/Y|\s+\(YoY\))?(?:\s+ACTUAL)?") or _find_pct(text, r"YoY")
        core = _find_pct(text, r"core\s+CPI(?:\s+YoY)?(?:\s+ACTUAL)?")
        # squawk format carries the consensus in the text: "(FORECAST 2.9%" / "EXP 2.9%" / "EST. 2.9%"
        cons = ctx.cpi_consensus
        if cons is None:
            cons = _find_pct(text, r"(?:FORECAST|EXP(?:ECTED)?|EST\.?|CONSENSUS)")
        core_cons = ctx.core_cpi_consensus
        if core_cons is None and core is not None:
            m_core = re.search(r"core\s+CPI.*?(?:FORECAST|EXP(?:ECTED)?|EST\.?|CONSENSUS)(?:(?!None)[^\d\-;]){0,10}" + _NUM, text, flags=re.I | re.S)
            core_cons = float(m_core.group(1)) if m_core else None
        if actual is not None and cons is not None:
            surprise = round(actual - cons, 2)
            core_s = round(core - core_cons, 2) if (core is not None and core_cons is not None) else 0.0
            score = round(surprise + core_s, 2)
            if abs(score) >= 0.1:
                d = Direction.SHORT if score > 0 else Direction.LONG   # hot CPI -> risk off
                return done(EventClass.MACRO_CPI, d, 0.85, f"CPI actual {actual} vs cons {cons} (core Δ {core_s:+.1f})",
                            ["BTC", "ETH"], sched=True)
            return done(EventClass.MACRO_CPI, Direction.NONE, 0.5, "CPI in line", ["BTC", "ETH"], sched=True)
        return done(EventClass.MACRO_CPI, Direction.NONE, 0.3, "CPI mention without parsable number/consensus", ["BTC", "ETH"], sched=True)

    if _has(text, r"non-?farm payrolls?", r"\bNFP\b", r"payrolls? (?:rose|fell|increased|decreased|added)"):
        m = re.search(r"(?:ACTUAL\s+)?(-?\d{2,3}(?:,\d{3})?|\-?\d+(?:\.\d+)?\s*[kK])", text)
        cons = ctx.nfp_consensus
        if cons is None:
            mc = re.search(r"(?:FORECAST|EXP(?:ECTED)?|EST\.?|CONSENSUS)[^\d\-]{0,10}(-?\d+(?:\.\d+)?)\s*[kK]?", text, flags=re.I)
            cons = float(mc.group(1)) if mc else None
        if m and cons is not None:
            s = m.group(1).replace(",", "").lower().replace("k", "")
            try:
                actual_k = float(s)
            except ValueError:
                actual_k = None
            if actual_k is not None:
                surprise = actual_k - cons
                if abs(surprise) >= 50:
                    d = Direction.SHORT if surprise > 0 else Direction.LONG   # strong jobs -> higher for longer -> risk off
                    return done(EventClass.MACRO_NFP, d, 0.80, f"NFP {actual_k:.0f}k vs cons {cons:.0f}k", ["BTC", "ETH"], sched=True)
                return done(EventClass.MACRO_NFP, Direction.NONE, 0.5, "NFP in line", ["BTC", "ETH"], sched=True)
        return done(EventClass.MACRO_NFP, Direction.NONE, 0.3, "NFP mention without number/consensus", ["BTC", "ETH"], sched=True)

    if _has(text, r"\bFOMC\b", r"fed (?:holds|cuts|raises|hikes|leaves) rates?", r"federal reserve .*(?:rate|basis points)"):
        m = re.search(r"(cuts?|lowers?|raises?|hikes?|holds?|leaves?|unchanged)", low)
        bps = re.search(r"(\d{2,3})\s*(?:bps|basis points)", low)
        if m and ctx.fomc_expected_bps is not None:
            verb = m.group(1)
            actual = 0
            if verb.startswith(("cut", "lower")):
                actual = -int(bps.group(1)) if bps else -25
            elif verb.startswith(("rais", "hike")):
                actual = int(bps.group(1)) if bps else 25
            surprise = actual - ctx.fomc_expected_bps
            if surprise != 0:
                d = Direction.LONG if surprise < 0 else Direction.SHORT   # more dovish than expected -> risk on
                return done(EventClass.MACRO_FOMC, d, 0.80, f"FOMC {actual:+d}bps vs expected {ctx.fomc_expected_bps:+d}", ["BTC", "ETH"], sched=True)
            return done(EventClass.MACRO_FOMC, Direction.NONE, 0.5, "FOMC as expected", ["BTC", "ETH"], sched=True)
        return done(EventClass.MACRO_FOMC, Direction.NONE, 0.3, "FOMC mention without decision/expectation", ["BTC", "ETH"], sched=True)

    # ---- Hyperliquid own listing (from meta diff) -> momentum template, direction decided by engine from first ticks
    if item.source == "hl_listings":
        coin = item.raw.get("name") or (tickers[0] if tickers else "")
        return done(EventClass.LISTING_HL, Direction.LONG, 0.9, "new HL perp", [coin] if coin else [])

    # ---- exchange listings. BTC/ETH cannot be "listed" anywhere that matters, so for this universe the only
    #      listing class that can trade is LISTING_HL (handled above). Everything else is logged, not traded.
    if item.channel in {"Binance EN", "Upbit", "Bithumb"} or _has(text, r"\bwill list\b", r"\bnew listing\b", r"\blists? \w+ \(", r"상장"):
        ec = EventClass.LISTING_UPBIT if item.channel == "Upbit" or "upbit" in low else EventClass.LISTING_BINANCE
        explicit = _has(text, r"\bwill list\b", r"\bnew listing\b", r"\blists? [A-Z0-9]{2,10} \(", r"\badds? [A-Z0-9]{2,10} (?:spot|perpetual|trading)", r"상장")
        listed = [t for t in tickers if t not in ("BTC", "ETH")]
        if explicit and listed:
            return done(ec, Direction.LONG, 0.8 if trusted else 0.5, "listing announcement", listed)
        return done(ec, Direction.NONE, 0.3, "exchange channel item / listing outside universe", [])

    # ---- follow-up detector for incident classes: reports *about* an earlier incident are not new information
    followup = _has(text, r"\blaunder", r"\btraces?\b", r"\btraced\b", r"\bmoves? (?:stolen )?funds", r"\btransferr", r"\breopens?\b", r"\bresum", r"\brecover",
                    r"\bpost-?mortem", r"\bafter (?:the )?(?:hack|exploit|breach)", r"\bfollowing (?:the )?(?:hack|exploit)", r"\bhacker\b", r"\battackers?\b.*\b(?:begin|start|move|swap|convert)",
                    r"\bQ[1-4]\b.*\blosses\b", r"\bsecurity losses\b", r"\bupdate:", r"\bcompensat", r"\breimburs", r"\bwithdrawals? (?:reopen|resume|restor)")

    # ---- hacks / exploits / depegs
    if _has(text, r"\bexploit(ed)?\b", r"\bhack(ed)?\b", r"\bdrained\b", r"unauthori[sz]ed (?:withdrawals?|access|activity|transactions?)", r"\bsecurity incident", r"suspicious outflows?") \
            and not _has(text, r"unauthori[sz]ed (?:tweet|post)"):
        amt = re.search(r"\$\s?(\d+(?:\.\d+)?)\s*(billion|bn|million|mn|m|b)\b", low)
        mag = None
        if amt:
            v = float(amt.group(1)); unit = amt.group(2)
            mag = v * (1e9 if unit.startswith("b") else 1e6)
        big_venue = _has(text, r"\bbinance\b", r"\bcoinbase\b", r"\bbybit\b", r"\bokx\b", r"\bkraken\b", r"\bhyperliquid\b", r"\bbitget\b", r"\bkucoin\b")
        if followup:
            return done(EventClass.HACK, Direction.NONE, 0.3, "follow-up report on an earlier incident", [], mag=mag)
        if (mag and mag >= 100e6) or big_venue:
            conf = 0.8 if trusted else 0.6
            return done(EventClass.HACK, Direction.SHORT, conf, f"exploit/hack mag={mag}", ["BTC", "ETH"] if not tickers else tickers, mag=mag)
        return done(EventClass.HACK, Direction.NONE, 0.4, "small/unknown hack", [], mag=mag)

    if _has(text, r"\bdepeg", r"loses? (?:its )?peg", r"\bbelow \$0\.9\d\b"):
        return done(EventClass.DEPEG, Direction.SHORT, 0.8 if trusted else 0.6, "stablecoin depeg", ["BTC", "ETH"])

    # ---- ETF / regulatory
    if _has(text, r"\bETF", r"exchange-traded (?:fund|product)", r"\bspot bitcoin\b.*\bexchange-traded"):
        if denial:
            # "account compromised / has not approved" -> reverse of the approval read; trusted only
            return done(EventClass.ETF, Direction.SHORT if trusted else Direction.NONE, 0.8 if trusted else 0.3, "ETF approval denied/retracted")
        if _has(text, r"\bapprov", r"\bgreen.?light", r"\bcleared\b", r"\bgrants?\b"):
            if _has(text, r"\bdenied|\breject|\bdelay"):
                return done(EventClass.ETF, Direction.SHORT, 0.8 if trusted else 0.6, "ETF denied/delayed")
            return done(EventClass.ETF, Direction.LONG, 0.8 if trusted else 0.6, "ETF approval")
        if _has(text, r"\bdenied|\breject|\bdelay"):
            return done(EventClass.ETF, Direction.SHORT, 0.8 if trusted else 0.6, "ETF denied/delayed")
        return done(EventClass.ETF, Direction.NONE, 0.3, "ETF chatter")

    if _has(text, r"\bSEC\b.*(?:sues?|charges?|lawsuit|wells notice|subpoena)", r"\bDOJ\b.*(?:charges?|indict)", r"\bban(?:s|ned)\b.*crypto", r"crypto.*\bban(?:s|ned)\b"):
        return done(EventClass.REGULATORY, Direction.SHORT, 0.8 if trusted else 0.6, "enforcement/ban")

    # ---- tariffs / Trump policy (direction only when wording is unambiguous)
    if _has(text, r"\btariffs?\b"):
        if _has(text, r"\bpause", r"\bsuspend", r"\bdelay", r"\bexempt", r"\breduc", r"\bcut\b", r"\bdeal\b", r"\bagreement\b"):
            return done(EventClass.TARIFF, Direction.LONG, 0.8 if trusted else 0.6, "tariff relief", ["BTC", "ETH"] if not tickers else tickers)
        if _has(text, r"\b\d{2,3}\s?%", r"\braise", r"\bhike", r"\bimpose", r"\badditional", r"\bretaliat"):
            return done(EventClass.TARIFF, Direction.SHORT, 0.8 if trusted else 0.6, "tariff escalation", ["BTC", "ETH"] if not tickers else tickers)
        return done(EventClass.TARIFF, Direction.NONE, 0.3, "tariff chatter")

    own_post = (item.account or "").lower() == "realdonaldtrump" or bool((item.raw.get("info") or {}).get("truthId"))
    relay = trusted and _has(text, r"\bTRUMP\b")
    if own_post or relay:
        if own_post and re.search(r"^(?:[^:]{0,60}\(@[A-Za-z0-9_]+\):\s*)?RT @", text):  # repost = body starts with "RT @"
            return done(EventClass.TRUMP, Direction.NONE, 0.2, "Trump repost")
        if _has(text, r"\bbitcoin\b", r"\bcrypto", r"\bdigital assets?\b", r"\bstablecoin") and _has(text, r"\breserve\b", r"\bstrategic\b", r"\bexecutive order\b", r"\bsign(?:ed|ing)?\b", r"\bban\b", r"\btax\b"):
            d = Direction.SHORT if _has(text, r"\bban\b", r"\btax\b", r"\bcrack ?down") else Direction.LONG
            # own account or a trusted relay both count as a source; the two-source rule in the engine still applies
            return done(EventClass.TRUMP, d, 0.8, "Trump crypto policy post" if own_post else "trusted relay of Trump crypto policy", ["BTC", "ETH"])
        if own_post:
            return done(EventClass.TRUMP, Direction.NONE, 0.3, "Trump post, no clear read")

    # ---- sector supply: a maker expands or cuts capacity -> peers in the same sector
    sec = classify_sector(item, ctx, trusted)
    if sec is not None:
        sec.is_rumor = is_rumor
        return sec

    # ---- equities: earnings / guidance / M&A (only for names in universe, and only from financial sources)
    wire = item.channel == "Blogs" and (item.account or "").upper() in {"BUSINESS WIRE", "PR NEWSWIRE", "GLOBENEWSWIRE", "REUTERS", "BLOOMBERG", "WSJ", "CNBC", "FT", "THE BLOCK", "COINDESK"}
    fin_source = trusted or wire or (item.account or "").lower() in {"unusual_whales", "deitaone", "firstsquawk", "cnbc", "reuters", "bloomberg", "wsj", "benzinga", "marketwatch"}
    strong = trusted or wire
    if tickers and any(t.startswith("xyz:") for t in tickers) and fin_source:
        stock_tk = [t for t in tickers if t.startswith("xyz:")]
        if _has(text, r"\bEPS\b", r"\bearnings\b", r"\brevenue\b", r"\bquarter(ly)? results", r"\bQ[1-4]\b.*\b(?:results|EPS|revenue)\b", r"\breports? (?:first|second|third|fourth)[- ]quarter"):
            beat = _has(text, r"\bbeats?\b", r"\babove (?:estimates|expectations|consensus)", r"\btops?\b", r"\bexceeds?\b", r"\bbetter.than.expected")
            miss = _has(text, r"\bmiss(?:es)?\b", r"\bbelow (?:estimates|expectations|consensus)", r"\bfalls? short", r"\bworse.than.expected", r"\bdisappoint")
            if beat and not miss:
                return done(EventClass.EARNINGS, Direction.LONG, 0.8 if strong else 0.6, "earnings beat", stock_tk)
            if miss and not beat:
                return done(EventClass.EARNINGS, Direction.SHORT, 0.8 if strong else 0.6, "earnings miss", stock_tk)
            return done(EventClass.EARNINGS, Direction.NONE, 0.4, "earnings, mixed/unparsed", stock_tk)
        if _has(text, r"\bguidance\b", r"\boutlook\b", r"\bforecast\b"):
            up = _has(text, r"\braises?\b", r"\bboosts?\b", r"\blifts?\b", r"\babove\b")
            dn = _has(text, r"\bcuts?\b", r"\blowers?\b", r"\btrims?\b", r"\bbelow\b", r"\bwithdraw")
            if up and not dn:
                return done(EventClass.GUIDANCE, Direction.LONG, 0.8 if strong else 0.6, "guidance raised", stock_tk)
            if dn and not up:
                return done(EventClass.GUIDANCE, Direction.SHORT, 0.8 if strong else 0.6, "guidance cut", stock_tk)
        if _has(text, r"\bto acquire\b", r"\bacquisition\b", r"\bmerger\b", r"\btakeover\b", r"\bbuyout\b"):
            return done(EventClass.MNA, Direction.NONE, 0.5, "M&A: target/acquirer ambiguous for rules", stock_tk)

    return done(EventClass.NOISE, Direction.NONE, 0.0, "no rule matched")


# --------------------------------------------------------------------------- LLM lane
LLM_SYSTEM = """You classify one financial headline for an automated trader on Hyperliquid perpetuals.
Tradeable instruments: BTC, ETH, xyz:TSLA, xyz:NVDA, xyz:AAPL, xyz:MSFT, xyz:META, xyz:GOOGL, xyz:AMZN, xyz:MU.
Return ONLY compact JSON with keys:
event_class: one of [macro_cpi, macro_nfp, macro_fomc, listing_binance, listing_upbit, listing_coinbase, hack_exploit, depeg, etf, regulatory, tariff, trump_policy, earnings, guidance, m_and_a, noise]
direction: long | short | none   (expected price direction of the listed tickers over the next 30-120 minutes)
confidence: 0..1  (probability the direction is right; use <=0.5 when the headline is a rumor, a denial, old news, satire, or ambiguous)
tickers: subset of the tradeable instruments that the headline is about (empty if none)
is_rumor: true/false
reason: <= 12 words
Rules: a denial or correction reverses the original direction. Headlines about assets not in the list -> tickers [] and direction none.
Pre-scheduled data (CPI/NFP/FOMC) -> direction none unless the actual number and the consensus are both in the text."""


async def classify_llm(item: NewsItem, cfg: dict) -> Classification | None:
    """Call a small model with a tiny structured prompt. Returns None on any failure."""
    t0 = time.perf_counter()
    provider = cfg.get("provider", "anthropic")
    model = cfg.get("model", "claude-haiku-4-5")
    timeout = float(cfg.get("timeout_s", 4.0))
    user = f"Source: {item.channel} {('@' + item.account) if item.account else ''}\nAge: {item.age_s:.1f}s\nHeadline: {item.title[:600]}"
    try:
        if provider == "anthropic":
            import anthropic  # type: ignore

            client = anthropic.AsyncAnthropic(timeout=timeout)
            resp = await client.messages.create(
                model=model, max_tokens=int(cfg.get("max_tokens", 200)), system=LLM_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            content = "".join(getattr(b, "text", "") for b in resp.content)
        else:
            import openai  # type: ignore

            client = openai.AsyncOpenAI(timeout=timeout)
            resp = await client.chat.completions.create(
                model=model, max_tokens=int(cfg.get("max_tokens", 200)), temperature=0,
                messages=[{"role": "system", "content": LLM_SYSTEM}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or ""
        j = json.loads(content[content.find("{") : content.rfind("}") + 1])
        ec = EventClass(j.get("event_class", "noise"))
        d = Direction(j.get("direction", "none"))
        return Classification(
            lane="llm", event_class=ec, direction=d, confidence=float(j.get("confidence", 0.0)),
            tickers=[t for t in j.get("tickers", []) if isinstance(t, str)], reason=str(j.get("reason", ""))[:120],
            is_rumor=bool(j.get("is_rumor", False)), latency_ms=int((time.perf_counter() - t0) * 1000),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("llm classify failed: %s", e)
        return None
