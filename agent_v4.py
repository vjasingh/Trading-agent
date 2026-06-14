"""
Trading Agent v4 — Multi-ticker brief with TSLA dedicated section
"""
import os
import re
import json
import csv
import requests
import anthropic
import yfinance as yf
import smtplib
import markdown as md
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date
from pathlib import Path
import statistics

TRADIER_TOKEN = os.environ.get("TRADIER_TOKEN")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
FINNHUB_TOKEN = os.environ.get("FINNHUB_TOKEN")
RECIPIENT_EMAIL = "vja118@gmail.com"

HEADERS = {
    "Authorization": f"Bearer {TRADIER_TOKEN}",
    "Accept": "application/json"
}
ACCOUNT_BP = 1_000_000
TSLA_SHARES_HELD = 1001
TSLA_MAX_CC_CONTRACTS = TSLA_SHARES_HELD // 100  # 10

DEFAULT_RULES = {
    "dte_min": 7, "dte_max": 42,
    "delta_min": 0.16, "delta_max": 0.35,
    "max_contracts": None,
    "single_ticker_bp_pct": 0.15,
    "earnings_buffer_days": 7,
    "strategy": "Standard cash-secured put selling. Avoid trades through earnings (close/skip if earnings within 7 days).",
}

TICKER_RULES = {
    "TSLA": {
        "dte_min": 7, "dte_max": 30,
        "delta_min": 0.16, "delta_max": 0.40,
        "max_contracts": 10,
        "single_ticker_bp_pct": 0.25,
        "earnings_buffer_days": 0,
        "strategy": "Diagonal put structure — long puts 6-8 weeks out + weekly short puts ABOVE long put strike.",
    },
}

WATCHLIST = ["AAPL", "NVDA", "AMD", "AMZN", "MSFT", "AVGO",
             "GOOGL", "LLY", "OKLO", "TEM", "SHOP", "HOOD", "PLTR", "GILD"]

CONTEXT_TICKERS = ["SPY", "QQQ", "VIX"]

HIGH_RISK_TICKERS = {"OKLO", "TEM", "PLTR"}
MAX_PREMIUM_RISK_MULTIPLIER = 2.0
MAX_PREMIUM_RISK_DOLLAR = 50_000

LOG_PATH = Path(__file__).parent / "recommendations_log.csv"
LOG_FIELDS = ["date", "ticker", "type", "strike", "expiry", "credit",
              "stock_price", "delta", "iv", "dte", "contracts", "stars",
              "outcome", "close_at_expiry", "pnl", "evaluated_date"]


def get_rules(ticker):
    rules = DEFAULT_RULES.copy()
    rules.update(TICKER_RULES.get(ticker, {}))
    return rules


def fetch_quote(ticker):
    # VIX uses index symbol on Tradier
    sym = "VIX" if ticker == "VIX" else ticker
    r = requests.get("https://api.tradier.com/v1/markets/quotes",
                     headers=HEADERS, params={"symbols": sym, "greeks": "false"})
    return r.json().get("quotes", {}).get("quote")


def fetch_hv_data(ticker):
    end_date = datetime.today().date()
    start_date = end_date - timedelta(days=365)
    r = requests.get("https://api.tradier.com/v1/markets/history", headers=HEADERS,
                     params={"symbol": ticker, "interval": "daily",
                             "start": start_date.strftime("%Y-%m-%d"),
                             "end": end_date.strftime("%Y-%m-%d")})
    history = r.json().get("history", {})
    if not history or "day" not in history:
        return None
    days = history["day"]
    if not isinstance(days, list) or len(days) < 50:
        return None
    closes = [d["close"] for d in days]
    daily_returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
    rolling_hv = []
    for i in range(20, len(daily_returns)):
        window = daily_returns[i-20:i]
        rolling_hv.append(statistics.stdev(window) * (252 ** 0.5) * 100)
    current_hv = rolling_hv[-1]
    hv_min, hv_max = min(rolling_hv), max(rolling_hv)
    hv_rank = (current_hv - hv_min) / (hv_max - hv_min) * 100 if hv_max > hv_min else 50
    return {"current_hv": current_hv, "hv_min": hv_min, "hv_max": hv_max,
            "hv_rank": hv_rank,
            "ma_50": sum(closes[-50:]) / 50,
            "ma_200": sum(closes[-200:]) / 200 if len(closes) >= 200 else None}


def fetch_earnings(ticker):
    try:
        cal = yf.Ticker(ticker).calendar
        if cal and "Earnings Date" in cal and cal["Earnings Date"]:
            ed = cal["Earnings Date"][0]
            if isinstance(ed, datetime):
                ed = ed.date()
            days_out = (ed - date.today()).days
            if days_out < 0:
                return None
            return {"date": ed, "days_out": days_out}
    except Exception:
        pass
    return None


def fetch_news(ticker, hours=24):
    """Fetch recent news headlines via Finnhub (last 24h)."""
    try:
        to_date = datetime.utcnow().date()
        from_date = (datetime.utcnow() - timedelta(hours=hours)).date()
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": ticker,
                "from": from_date.strftime("%Y-%m-%d"),
                "to": to_date.strftime("%Y-%m-%d"),
                "token": FINNHUB_TOKEN,
            }
        )
        items = r.json() if r.status_code == 200 else []
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).timestamp()
        recent = []
        for item in items:
            if item.get("datetime", 0) >= cutoff:
                title = item.get("headline", "").strip()
                link = item.get("url", "").strip()
                publisher = item.get("source", "").strip()
                if title and link:
                    recent.append({"title": title, "link": link, "publisher": publisher})
        print(f"    {ticker} news: {len(recent)} items in last {hours}h")
        return recent[:5]
    except Exception as e:
        print(f"    {ticker} news error: {e}")
        return []


def fetch_options_in_range(ticker, dte_min, dte_max, delta_min, delta_max, option_type="put"):
    """Returns expirations list with eligible options of the given type."""
    r = requests.get("https://api.tradier.com/v1/markets/options/expirations",
                     headers=HEADERS,
                     params={"symbol": ticker, "includeAllRoots": "true", "strikes": "false"})
    exp_data = r.json().get("expirations", {})
    if not exp_data or "date" not in exp_data:
        return []
    expirations = exp_data["date"]
    if not isinstance(expirations, list):
        expirations = [expirations]
    today = datetime.today().date()
    targets = []
    for exp_str in expirations:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (exp_date - today).days
        if dte_min <= dte <= dte_max:
            targets.append((exp_str, dte))
    targets = targets[:5]  # Allow more expirations for TSLA's wide range
    result = []
    for exp_str, dte in targets:
        cr = requests.get("https://api.tradier.com/v1/markets/options/chains", headers=HEADERS,
                         params={"symbol": ticker, "expiration": exp_str, "greeks": "true"})
        opts = cr.json().get("options", {})
        if not opts or "option" not in opts:
            continue
        contracts = []
        for opt in opts["option"]:
            if opt["option_type"] != option_type:
                continue
            g = opt.get("greeks")
            if not g or g.get("delta") is None:
                continue
            d = g["delta"]
            # Puts: delta is negative; Calls: delta is positive
            if option_type == "put":
                if -delta_max <= d <= -delta_min:
                    contracts.append(_format_contract(opt, g, d))
            else:  # call
                if delta_min <= d <= delta_max:
                    contracts.append(_format_contract(opt, g, d))
        if contracts:
            result.append({"expiration": exp_str, "dte": dte, "contracts": contracts})
    return result


def _format_contract(opt, g, d):
    return {"strike": opt["strike"],
            "mid": round((opt["bid"] + opt["ask"]) / 2, 2),
            "delta": round(d, 3),
            "iv": round(g.get("mid_iv", 0) * 100, 1),
            "volume": opt["volume"],
            "open_interest": opt["open_interest"]}


def analyze_ticker(ticker, include_calls=False, call_dte_max=None, call_delta_min=None, call_delta_max=None):
    print(f"  Fetching {ticker}...")
    rules = get_rules(ticker)
    try:
        quote = fetch_quote(ticker)
        if not quote or quote.get("last") is None:
            return None
        data = {
            "ticker": ticker, "rules": rules, "quote": quote,
            "hv_data": fetch_hv_data(ticker),
            "earnings": fetch_earnings(ticker),
            "news": fetch_news(ticker),
            "puts": fetch_options_in_range(
                ticker, rules["dte_min"], rules["dte_max"],
                rules["delta_min"], rules["delta_max"], "put"),
            "is_high_risk": ticker in HIGH_RISK_TICKERS,
        }
        if include_calls:
            data["calls"] = fetch_options_in_range(
                ticker, rules["dte_min"], call_dte_max,
                call_delta_min, call_delta_max, "call")
        return data
    except Exception as e:
        print(f"    ⚠️ Error on {ticker}: {e}")
        return None


def fetch_context_ticker(ticker):
    """Lightweight fetch for SPY/QQQ/VIX — just quote and HV."""
    try:
        q = fetch_quote(ticker)
        hv = fetch_hv_data(ticker) if ticker != "VIX" else None
        return {"ticker": ticker, "quote": q, "hv_data": hv}
    except Exception as e:
        print(f"    ⚠️ Context fetch error on {ticker}: {e}")
        return None


def build_context_block(context_data):
    text = "MARKET CONTEXT:\n"
    for c in context_data:
        if not c or not c["quote"]:
            continue
        q = c["quote"]
        change = q.get("change_percentage", 0)
        text += f"  {c['ticker']}: ${q['last']} ({change}% today)"
        if c.get("hv_data"):
            text += f", HV Rank: {c['hv_data']['hv_rank']:.0f}"
        text += "\n"
    return text


def build_news_block(tsla_data, watchlist_data):
    """Build a news context block for all recommended tickers."""
    all_data = ([tsla_data] if tsla_data else []) + [d for d in watchlist_data if d]
    lines = ["RECENT NEWS (last 24 hours):"]
    any_news = False
    for d in all_data:
        ticker = d["ticker"]
        news = d.get("news", [])
        if news:
            any_news = True
            lines.append(f"\n{ticker}:")
            for item in news:
                lines.append(f'  - [{item["title"]}]({item["link"]}) — {item["publisher"]}')
    if not any_news:
        return "RECENT NEWS: None available in last 24 hours.\n"
    return "\n".join(lines) + "\n"


def build_ticker_context(data, include_calls=False):
    t = data["ticker"]
    q = data["quote"]
    hv = data["hv_data"]
    e = data["earnings"]
    rules = data["rules"]
    
    text = f"\n===== {t} =====\n"
    text += f"Price: ${q['last']} ({q.get('change_percentage', 0)}% today)\n"
    text += f"52w range: ${q.get('week_52_low')} - ${q.get('week_52_high')}\n"
    
    if hv:
        text += f"HV Rank: {hv['hv_rank']:.0f} (current {hv['current_hv']:.1f}%, range {hv['hv_min']:.1f}%-{hv['hv_max']:.1f}%)\n"
        if hv['ma_200']:
            pos50 = 'ABOVE' if q['last'] > hv['ma_50'] else 'BELOW'
            pos200 = 'ABOVE' if q['last'] > hv['ma_200'] else 'BELOW'
            text += f"Technicals: {pos50} 50d MA (${hv['ma_50']:.2f}), {pos200} 200d MA (${hv['ma_200']:.2f})\n"
    
    if e:
        text += f"Earnings: {e['date']} ({e['days_out']} days out)\n"
        buffer = rules['earnings_buffer_days']
        if buffer > 0 and e['days_out'] <= buffer:
            text += f"🚨 EARNINGS WITHIN {buffer}-DAY BUFFER — SKIP/AVOID per rules\n"
        elif e['days_out'] <= 14:
            text += f"⚠️ Earnings approaching (within 14 days)\n"
    else:
        text += "Earnings: not available\n"
    
    if data["is_high_risk"]:
        text += "⚠️ HIGH-RISK TICKER\n"
    
    bp_cap = ACCOUNT_BP * rules['single_ticker_bp_pct']
    text += f"\nRules: DTE {rules['dte_min']}-{rules['dte_max']}, delta {rules['delta_min']}-{rules['delta_max']}, BP cap ${bp_cap:,.0f}"
    if rules['max_contracts']:
        text += f", max {rules['max_contracts']} contracts"
    text += f"\nStrategy: {rules['strategy']}\n"
    
    if not data.get("puts"):
        text += "❌ No eligible puts in target range\n"
    else:
        text += "\nPUTS:\n"
        for ed in data["puts"]:
            text += f"--- {ed['expiration']} ({ed['dte']} DTE) ---\n"
            for p in ed['contracts']:
                text += f"  ${p['strike']}: credit ${p['mid']}, delta {p['delta']}, IV {p['iv']}%, vol {p['volume']}, OI {p['open_interest']}\n"
    
    if include_calls:
        if not data.get("calls"):
            text += "\nCALLS: No eligible calls in target range\n"
        else:
            text += "\nCALLS:\n"
            for ed in data["calls"]:
                text += f"--- {ed['expiration']} ({ed['dte']} DTE) ---\n"
                for c in ed['contracts']:
                    text += f"  ${c['strike']}: credit ${c['mid']}, delta {c['delta']}, IV {c['iv']}%, vol {c['volume']}, OI {c['open_interest']}\n"
    
    return text


def claude_analyze(tsla_data, watchlist_data, context_data):
    context_block = build_context_block(context_data)
    tsla_block = build_ticker_context(tsla_data, include_calls=True) if tsla_data else "TSLA data unavailable"
    watchlist_blocks = "\n".join([build_ticker_context(d) for d in watchlist_data if d])
    news_block = build_news_block(tsla_data, watchlist_data)
    
    prompt = f"""You are an options analyst producing a morning brief for an experienced premium seller with ${ACCOUNT_BP:,} buying power.

{context_block}

═══════════════════════════════════════════
TSLA (USER HOLDS {TSLA_SHARES_HELD} SHARES — UP TO {TSLA_MAX_CC_CONTRACTS} COVERED CALL CONTRACTS POSSIBLE)
═══════════════════════════════════════════
{tsla_block}

═══════════════════════════════════════════
OTHER WATCHLIST (for ranked setups)
═══════════════════════════════════════════
{watchlist_blocks}

═══════════════════════════════════════════
{news_block}
═══════════════════════════════════════════

CRITICAL SIZING RULES (apply to EVERY recommendation):
For each pick, you MUST compute and show ALL THREE constraints:
1. Premium-risk cap: max contracts = ${MAX_PREMIUM_RISK_DOLLAR:,} / ({MAX_PREMIUM_RISK_MULTIPLIER} × credit × 100)
2. Single-ticker BP cap (per-ticker, in the rules above): max contracts = (BP cap $) / (strike × 100)
3. Contract cap (where specified, e.g. TSLA = 10, TSLA CCs = 10)

THE BINDING CONSTRAINT IS THE LOWEST. Never recommend more than the lowest of these.
Show all three numbers explicitly for every pick. Example format:
  Sizing: premium-risk = 15.5 / BP cap = 6.0 / contract cap = 10 → BINDING: 6 contracts

PRODUCE A MORNING BRIEF WITH THESE SECTIONS IN THIS EXACT ORDER:

---

**1. MARKET CONTEXT**
2-3 sentences on the regime using SPY/QQQ/VIX data. State whether environment is favorable/neutral/unfavorable for premium selling.

---

**2. 🚗 TSLA DAILY**
ALWAYS include this section, regardless of whether TSLA ranks well vs other tickers.

A. **TSLA SHORT PUT RECOMMENDATION** (diagonal short put leg)
   - Header line format: "TSLA @ $[current price] | [expiration] $[strike]P @ $[credit] credit ([DTE] DTE)"
   - Delta, IV, cushion (do NOT repeat current price in the cushion line — it's already in the header)
   - Sizing: show all three constraints, name binding one
   - Reminder: "Verify short put strike is ABOVE your current long put floor"

B. **TSLA COVERED CALL RECOMMENDATION** (label as "CC", NOT "P")
   - Header line format: "TSLA @ $[current price] | [expiration] $[strike]C @ $[credit] credit ([DTE] DTE)"
   - User holds 1,001 shares → max 10 CC contracts
   - Delta, IV, % distance above current price (do NOT repeat current price in body — it's in the header)
   - Sizing: contracts = min(10, what user wants to risk losing shares on)
   - Note: if delta > 0.30, mention higher assignment risk

---

**3. TOP 3-5 WATCHLIST SETUPS** (excluding TSLA — that has its own section)
Rank only the strong setups across the OTHER 14 tickers. For each:
- Header line format: "**[Ticker] @ $[current price]** ⭐⭐⭐ | [expiration] $[strike]P @ $[credit] credit ([DTE] DTE)"
- Delta, IV, cushion (do NOT repeat current price in the cushion line — it's already in the header)
- Sizing: ALL THREE constraints with numbers (premium-risk / BP cap / contract cap), name binding
- 1-line rationale

---

**4. NEWS & CATALYSTS**
For each ticker you recommended (TSLA + top watchlist picks only), list up to 3 key headlines from the RECENT NEWS section above. Format each as a markdown link on its own line: [Headline text](url) — Publisher. After the link, add one phrase noting the impact on the trade thesis (e.g. "bullish catalyst", "watch for vol spike", "no trade impact"). Skip tickers with no news in the last 24h. Do not invent headlines — only use what is in the RECENT NEWS data.

---

**5. WATCH BUT DON'T TRADE**
One-line per skipped ticker explaining why.

---

**6. PORTFOLIO SANITY CHECK**
Sum the BP deployment across ALL your recommendations (TSLA puts + TSLA CCs + Top setups).
TSLA puts deploy: (contracts × strike × 100)
TSLA CCs deploy: $0 BP (covered by shares, no cash needed)
Each watchlist pick: (contracts × strike × 100)
Total: $X out of ${ACCOUNT_BP:,} BP (X%)
If total exceeds ${ACCOUNT_BP:,}, FLAG IT and reduce sizes.

---

**7. KEY RISKS / FLAGS**
2-3 specific bullets on broader risk themes (vol regime, earnings clusters, technical themes).

Be concise and decisive. Use real numbers. Math must be correct.

---
SYSTEM LOGGING — Append this block at the very end of your response (it is stripped before email delivery):

RECOMMENDATION_LOG_JSON
{{"date": "{datetime.now().strftime('%Y-%m-%d')}", "recommendations": [
  {{"ticker": "TICKER", "type": "put", "strike": 0.0, "expiry": "YYYY-MM-DD", "credit": 0.00, "stock_price": 0.00, "delta": 0.000, "iv": 0.0, "dte": 0, "contracts": 0, "stars": null}}
]}}
END_RECOMMENDATION_LOG_JSON

Replace the template with real values — one object per recommended trade (TSLA put, TSLA CC, each watchlist pick). For "type" use "put" or "call". For "stars" use integer 3/4/5 for watchlist picks, null for TSLA trades."""

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=5000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def parse_and_strip_log(brief_text):
    """Extract the JSON log block from Claude's response. Always safe — never blocks email."""
    try:
        pattern = r'RECOMMENDATION_LOG_JSON\s*(.*?)\s*END_RECOMMENDATION_LOG_JSON'
        match = re.search(pattern, brief_text, re.DOTALL)
        if not match:
            print("  ⚠️ No recommendation log block found in brief")
            return brief_text, []
        clean_brief = re.sub(pattern, '', brief_text, flags=re.DOTALL).strip()
        data = json.loads(match.group(1).strip())
        recs = data.get("recommendations", [])
        print(f"  ✅ Parsed {len(recs)} recommendations from log block")
        return clean_brief, recs
    except Exception as e:
        print(f"  ⚠️ Log parsing failed ({e}) — email unaffected")
        return brief_text, []


def save_latest_brief_json(brief_text, recommendations, run_date):
    """Save brief content + recommendations to data/latest_brief.json for the dashboard."""
    try:
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "run_date": run_date,
            "brief_text": brief_text,
            "recommendations": recommendations,
        }
        with open(data_dir / "latest_brief.json", "w") as f:
            json.dump(payload, f, indent=2, default=str)
        print(f"  ✅ Saved data/latest_brief.json")
    except Exception as e:
        print(f"  ⚠️ Failed to save latest_brief.json ({e}) — email unaffected")


def append_recommendations_to_csv(recommendations, run_date):
    """Append today's recommendations to the log CSV. Safe — never blocks email."""
    if not recommendations:
        return
    try:
        write_header = not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0
        with open(LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            for rec in recommendations:
                rec.setdefault("date", run_date)
                rec.setdefault("outcome", "")
                rec.setdefault("close_at_expiry", "")
                rec.setdefault("pnl", "")
                rec.setdefault("evaluated_date", "")
                writer.writerow(rec)
        print(f"  ✅ Logged {len(recommendations)} recommendations to {LOG_PATH.name}")
    except Exception as e:
        print(f"  ⚠️ CSV logging failed ({e}) — email unaffected")


def send_email(subject, body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_SENDER
    msg["To"] = RECIPIENT_EMAIL
    text_part = MIMEText(body, "plain")
    msg.attach(text_part)
    html_body = md.markdown(body, extensions=["tables"])
    html = f"""<html><body style="font-family: -apple-system, sans-serif; max-width: 900px; line-height: 1.6; padding: 16px; color: #1a1a1a;">
    {html_body}
    </body></html>"""
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


if __name__ == "__main__":
    run_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print("=" * 70)
    print(f"TRADING AGENT v4 — {run_time}")
    print("=" * 70)
    
    print("\nFetching market context...")
    context_data = [fetch_context_ticker(t) for t in CONTEXT_TICKERS]
    
    print("\nFetching TSLA (with covered calls)...")
    tsla_data = analyze_ticker("TSLA", include_calls=True,
                                call_dte_max=365,
                                call_delta_min=0.25, call_delta_max=0.40)
    
    print("\nFetching watchlist...")
    watchlist_data = []
    for t in WATCHLIST:
        d = analyze_ticker(t)
        watchlist_data.append(d)
    
    print("\nSending to Claude for analysis...")
    brief = claude_analyze(tsla_data, watchlist_data, context_data)

    print("\nParsing recommendation log...")
    run_date = datetime.now().strftime("%Y-%m-%d")
    clean_brief, recommendations = parse_and_strip_log(brief)
    append_recommendations_to_csv(recommendations, run_date)
    save_latest_brief_json(clean_brief, recommendations, run_date)

    print("\n" + "=" * 70)
    print("📊 MORNING BRIEF")
    print("=" * 70)
    print(clean_brief)

    print("\n" + "=" * 70)
    print("Sending email...")
    try:
        subject = f"📊 Daily Brief — {datetime.now().strftime('%a %b %d')}"
        send_email(subject, clean_brief)
        print(f"✅ Email sent to {RECIPIENT_EMAIL}")
    except Exception as e:
        print(f"❌ Email failed: {e}")
    
    print("=" * 70)
    print("✅ Done")