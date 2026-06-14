"""
Trading Agent Dashboard Generator
===================================
Generates docs/index.html — a static, mobile-first dashboard.
Runs every 30 minutes during market hours via GitHub Actions.

Reads:
  data/latest_brief.json    — written by agent_v4.py each morning
  recommendations_log.csv   — written by agent_v4.py each morning

Fetches live:
  Quotes for SPY, QQQ, VIX + 15 watchlist tickers (Tradier, 1 API call)

Writes:
  docs/index.html
  docs/favicon.png  (generated once on first run, then kept)

Security: zero secrets in output HTML. All API calls happen here, server-side.
"""

import sys
import json
import csv
import struct
import zlib
import requests
import markdown as md
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Import shared constants/functions from agent_v4 ───────────────────────────
# We import only what we need; agent_v4's __main__ block does NOT run on import.
sys.path.insert(0, str(Path(__file__).parent))
import agent_v4 as agent

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent
DOCS_DIR  = ROOT / "docs"
DATA_DIR  = ROOT / "data"
INDEX_HTML = DOCS_DIR / "index.html"
FAVICON   = DOCS_DIR / "favicon.png"
BRIEF_JSON = DATA_DIR / "latest_brief.json"
RECS_CSV  = ROOT / "recommendations_log.csv"

# ── Ticker list for the quotes strip ──────────────────────────────────────────
# SPY / QQQ / VIX first, then TSLA, then the full watchlist
STRIP_TICKERS = ["SPY", "QQQ", "VIX", "TSLA"] + agent.WATCHLIST  # 18 total


# ══════════════════════════════════════════════════════════════════════════════
#  Data fetching
# ══════════════════════════════════════════════════════════════════════════════

def fetch_quotes_batch(tickers):
    """Fetch quotes for all tickers in a single Tradier API call."""
    syms = ",".join(tickers)
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/quotes",
            headers=agent.HEADERS,
            params={"symbols": syms, "greeks": "false"},
            timeout=15,
        )
        data = r.json().get("quotes", {}).get("quote", [])
        if isinstance(data, dict):  # single ticker returns dict, not list
            data = [data]
        return {q["symbol"]: q for q in (data or []) if q and q.get("symbol")}
    except Exception as e:
        print(f"  ⚠️ Batch quote fetch failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
#  Data loading
# ══════════════════════════════════════════════════════════════════════════════

def load_brief():
    """Load latest_brief.json. Returns dict or None."""
    if not BRIEF_JSON.exists():
        return None
    try:
        with open(BRIEF_JSON) as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠️ Could not load brief JSON: {e}")
        return None


def load_recommendations():
    """Load recommendations_log.csv. Returns list of row dicts."""
    if not RECS_CSV.exists():
        return []
    try:
        with open(RECS_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
        return [r for r in rows if r.get("ticker")]  # skip blank rows
    except Exception as e:
        print(f"  ⚠️ Could not load CSV: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  Favicon (generated once, pure Python stdlib — no Pillow needed)
# ══════════════════════════════════════════════════════════════════════════════

def make_favicon():
    """Generate a 180×180 solid green PNG for apple-touch-icon (stdlib only)."""
    if FAVICON.exists():
        return
    w, h = 180, 180
    r, g, b = 22, 163, 74   # Tailwind green-600
    raw = b"".join(b"\x00" + bytes([r, g, b] * w) for _ in range(h))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    FAVICON.write_bytes(png)
    print("  ✅ favicon.png created")


# ══════════════════════════════════════════════════════════════════════════════
#  Timestamp helper
# ══════════════════════════════════════════════════════════════════════════════

def et_now_str():
    """Current time as a human-readable ET string (approximated as UTC-4 / EDT)."""
    et = datetime.now(timezone(timedelta(hours=-4)))
    return et.strftime("%-I:%M %p ET, %b %-d %Y")


# ══════════════════════════════════════════════════════════════════════════════
#  HTML building blocks
# ══════════════════════════════════════════════════════════════════════════════

def _quote_tile(ticker, q):
    """Single quote tile for the sticky strip."""
    if not q:
        return f"""
        <div class="flex-shrink-0 bg-gray-800/60 rounded-lg px-3 py-2 min-w-[74px] text-center">
          <div class="text-xs text-gray-500 font-medium tracking-wide">{ticker}</div>
          <div class="text-sm font-bold text-gray-600 mt-0.5">—</div>
          <div class="text-xs text-gray-700 mt-0.5">N/A</div>
        </div>"""

    price  = float(q.get("last") or q.get("close") or 0)
    chg    = float(q.get("change_percentage") or 0)

    # VIX: invert color logic (rising VIX = bad for premium sellers)
    if ticker == "VIX":
        color = "text-red-400" if chg > 0 else ("text-green-400" if chg < 0 else "text-gray-400")
    else:
        color = "text-green-400" if chg > 0 else ("text-red-400" if chg < 0 else "text-gray-400")

    arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "●")
    border = 'ring-1 ring-amber-500/60' if ticker == 'TSLA' else ''

    return f"""
        <div class="flex-shrink-0 bg-gray-800/60 rounded-lg px-3 py-2 min-w-[74px] text-center {border}">
          <div class="text-xs text-gray-400 font-medium tracking-wide">{ticker}</div>
          <div class="text-sm font-bold text-gray-100 mt-0.5">${price:,.2f}</div>
          <div class="text-xs {color} font-medium mt-0.5">{arrow} {abs(chg):.2f}%</div>
        </div>"""


def _quotes_strip(quotes, timestamp):
    tiles = "\n".join(_quote_tile(t, quotes.get(t)) for t in STRIP_TICKERS)
    return f"""
  <!-- ══ Sticky Quotes Strip ══════════════════════════════════════ -->
  <div id="quotes-strip" class="sticky top-0 z-50 bg-gray-900/95 backdrop-blur border-b border-gray-700/50 shadow-xl">
    <div class="px-4 pt-2">
      <span class="text-xs text-gray-600">Quotes as of {timestamp} &nbsp;·&nbsp; refreshes every 30 min during market hours</span>
    </div>
    <div class="flex gap-2 px-4 pt-1.5 pb-3 overflow-x-auto scrollbar-hide">
      {tiles}
    </div>
  </div>"""


def _brief_section(brief_data):
    """Render the morning brief section from latest_brief.json."""
    if not brief_data:
        return """
  <!-- ══ Morning Brief ════════════════════════════════════════════ -->
  <section class="mb-10">
    <h2 class="text-xl font-bold text-gray-100 mb-4 flex items-center gap-2">
      <span>📊</span> Today's Morning Brief
    </h2>
    <div class="bg-gray-800/50 rounded-2xl p-8 text-center border border-dashed border-gray-700">
      <div class="text-5xl mb-3">⏳</div>
      <p class="font-semibold text-gray-300 text-lg">Brief not yet generated</p>
      <p class="text-sm text-gray-500 mt-2">Runs automatically at 6:17 AM ET on weekdays.</p>
    </div>
  </section>"""

    generated_at = brief_data.get("generated_at", "")
    brief_text   = brief_data.get("brief_text", "")
    html_content = md.markdown(brief_text, extensions=["tables"])

    return f"""
  <!-- ══ Morning Brief ════════════════════════════════════════════ -->
  <section class="mb-10">
    <div class="flex flex-wrap items-baseline justify-between gap-2 mb-4">
      <h2 class="text-xl font-bold text-gray-100 flex items-center gap-2">
        <span>📊</span> Today's Morning Brief
      </h2>
      <span class="text-xs text-gray-600">Generated {generated_at} ET</span>
    </div>
    <div class="brief-content bg-gray-800/50 rounded-2xl p-6 lg:p-8 border border-gray-700/40">
      {html_content}
    </div>
  </section>"""


def _performance_section(rows):
    """Render performance section with graceful sparse-data handling."""
    dates     = set(r.get("date", "") for r in rows if r.get("date"))
    days      = len(dates)
    total     = len(rows)
    evaluated = [r for r in rows if r.get("outcome") in ("win", "loss")]
    wins      = sum(1 for r in evaluated if r["outcome"] == "win")
    losses    = len(evaluated) - wins

    header = """
  <!-- ══ Historical Performance ══════════════════════════════════ -->
  <section class="mb-10">
    <h2 class="text-xl font-bold text-gray-100 mb-4 flex items-center gap-2">
      <span>📈</span> Historical Performance
    </h2>"""

    # ── < 7 days: collecting ─────────────────────────────────────
    if days < 7:
        body = f"""
    <div class="bg-gray-800/50 rounded-2xl p-8 border border-dashed border-gray-700">
      <div class="text-center mb-6">
        <div class="text-5xl mb-3">🌱</div>
        <p class="font-semibold text-gray-300 text-lg">Collecting data</p>
        <p class="text-sm text-gray-500 mt-1 max-w-md mx-auto">
          Full analytics appear after 2+ weeks of history. Once positions start
          expiring you'll see win rates by star rating, P&amp;L tracking, and
          calibration stats.
        </p>
      </div>
      <div class="grid grid-cols-3 gap-3 max-w-xs mx-auto text-center">
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-amber-400">{total}</div>
          <div class="text-xs text-gray-500 mt-1">Picks logged</div>
        </div>
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-gray-300">{len(evaluated)}</div>
          <div class="text-xs text-gray-500 mt-1">Expired</div>
        </div>
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-gray-300">{days}</div>
          <div class="text-xs text-gray-500 mt-1">Days tracked</div>
        </div>
      </div>
    </div>"""

    # ── 7–14 days: counts only ────────────────────────────────────
    elif days < 15:
        body = f"""
    <div class="bg-gray-800/50 rounded-2xl p-6 border border-gray-700/40">
      <p class="text-xs text-amber-400/80 mb-5">
        ⚠️ Early data — win-rate stats shown after 15+ days of history
      </p>
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-center">
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-amber-400">{total}</div>
          <div class="text-xs text-gray-500 mt-1">Total picks</div>
        </div>
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-gray-300">{len(evaluated)}</div>
          <div class="text-xs text-gray-500 mt-1">Expired</div>
        </div>
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-green-400">{wins}</div>
          <div class="text-xs text-gray-500 mt-1">Winners</div>
        </div>
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-red-400">{losses}</div>
          <div class="text-xs text-gray-500 mt-1">Losers</div>
        </div>
      </div>
    </div>"""

    # ── 15+ days: full analytics ──────────────────────────────────
    else:
        win_rate  = wins / len(evaluated) * 100 if evaluated else 0
        total_pnl = sum(float(r["pnl"]) for r in evaluated if r.get("pnl"))
        pnl_color = "text-green-400" if total_pnl >= 0 else "text-red-400"

        # Stars breakdown table
        by_stars = defaultdict(list)
        for r in evaluated:
            by_stars[str(r.get("stars") or "—")].append(r)

        star_rows_html = ""
        for stars in sorted(by_stars, key=lambda x: int(x) if x.isdigit() else -1, reverse=True):
            g = by_stars[stars]
            g_wins = sum(1 for r in g if r["outcome"] == "win")
            g_pnl  = sum(float(r["pnl"]) for r in g if r.get("pnl"))
            g_rate = g_wins / len(g) * 100
            label  = "⭐" * int(stars) if stars.isdigit() else stars
            pc     = "text-green-400" if g_pnl >= 0 else "text-red-400"
            bar    = int(g_rate)
            star_rows_html += f"""
              <tr class="border-t border-gray-700/50">
                <td class="py-2.5 pr-4 text-gray-200">{label}</td>
                <td class="py-2.5 pr-4 text-gray-400">{len(g)}</td>
                <td class="py-2.5 pr-4">
                  <div class="flex items-center gap-2">
                    <div class="w-16 bg-gray-700 rounded-full h-1.5">
                      <div class="bg-green-500 h-1.5 rounded-full" style="width:{bar}%"></div>
                    </div>
                    <span class="text-gray-300 text-sm">{g_rate:.0f}%</span>
                  </div>
                </td>
                <td class="py-2.5 {pc} font-medium">${g_pnl:,.0f}</td>
              </tr>"""

        body = f"""
    <div class="bg-gray-800/50 rounded-2xl p-6 border border-gray-700/40">
      <!-- KPI strip -->
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-7 text-center">
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-amber-400">{win_rate:.0f}%</div>
          <div class="text-xs text-gray-500 mt-1">Win rate</div>
        </div>
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold {pnl_color}">${total_pnl:,.0f}</div>
          <div class="text-xs text-gray-500 mt-1">Total P&amp;L</div>
        </div>
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-gray-100">{len(evaluated)}</div>
          <div class="text-xs text-gray-500 mt-1">Evaluated</div>
        </div>
        <div class="bg-gray-700/60 rounded-xl p-4">
          <div class="text-2xl font-bold text-gray-100">{days}</div>
          <div class="text-xs text-gray-500 mt-1">Days tracked</div>
        </div>
      </div>
      <!-- Stars table -->
      <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3">Win Rate by Star Rating</h3>
      <table class="w-full text-sm">
        <thead>
          <tr class="text-xs text-gray-600 uppercase tracking-wide">
            <th class="pb-2 pr-4 text-left font-medium">Rating</th>
            <th class="pb-2 pr-4 text-left font-medium">Trades</th>
            <th class="pb-2 pr-4 text-left font-medium">Win Rate</th>
            <th class="pb-2 text-left font-medium">P&amp;L</th>
          </tr>
        </thead>
        <tbody>{star_rows_html}
        </tbody>
      </table>
    </div>"""

    return header + body + "\n  </section>"


# ══════════════════════════════════════════════════════════════════════════════
#  Full HTML assembly
# ══════════════════════════════════════════════════════════════════════════════

def generate_html(quotes, brief_data, rows, timestamp):
    quotes_strip = _quotes_strip(quotes, timestamp)
    brief_sec    = _brief_section(brief_data)
    perf_sec     = _performance_section(rows)
    build_time   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="description" content="Personal trading dashboard — options premium strategy">
  <link rel="apple-touch-icon" href="favicon.png">
  <link rel="icon" href="favicon.png">
  <title>Trading Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    /* ── Dark base ──────────────────────────────────────────────── */
    body {{ background-color: #030712; }}  /* gray-950 */

    /* ── Hide scrollbar on quotes strip (keep scroll functionality) */
    .scrollbar-hide {{ -ms-overflow-style: none; scrollbar-width: none; }}
    .scrollbar-hide::-webkit-scrollbar {{ display: none; }}

    /* ── Morning brief markdown rendering ───────────────────────── */
    .brief-content h2 {{
      font-size: 1.05rem;
      font-weight: 700;
      color: #f3f4f6;
      border-bottom: 1px solid #374151;
      padding-bottom: 5px;
      margin-top: 1.8rem;
      margin-bottom: 0.75rem;
    }}
    .brief-content h2:first-child {{ margin-top: 0; }}
    .brief-content h3 {{
      font-size: 0.9rem;
      font-weight: 600;
      color: #e5e7eb;
      margin-top: 1.1rem;
      margin-bottom: 0.4rem;
    }}
    .brief-content p {{
      color: #d1d5db;
      font-size: 0.875rem;
      line-height: 1.65;
      margin-bottom: 0.55rem;
    }}
    .brief-content ul {{
      list-style: disc;
      padding-left: 1.25rem;
      margin-bottom: 0.7rem;
    }}
    .brief-content li {{
      color: #d1d5db;
      font-size: 0.875rem;
      line-height: 1.65;
      margin-bottom: 0.2rem;
    }}
    .brief-content a {{
      color: #60a5fa;
      text-decoration: underline;
      text-underline-offset: 2px;
    }}
    .brief-content a:hover {{ color: #93c5fd; }}
    .brief-content hr {{
      border: none;
      border-top: 1px solid #374151;
      margin: 1.25rem 0;
    }}
    .brief-content strong {{ color: #f9fafb; font-weight: 600; }}
    .brief-content table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
      margin: 0.75rem 0 1rem;
    }}
    .brief-content th {{
      background: #1f2937;
      color: #9ca3af;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 8px 12px;
      text-align: left;
      font-weight: 600;
    }}
    .brief-content td {{
      padding: 7px 12px;
      color: #d1d5db;
      border-top: 1px solid #1f2937;
    }}
    .brief-content tr:hover td {{ background: #1f2937; }}

    /* ── Mobile touch targets ───────────────────────────────────── */
    @media (max-width: 640px) {{
      .brief-content p, .brief-content li {{ font-size: 1rem; }}
    }}
  </style>
</head>
<body class="text-gray-100 min-h-screen font-sans antialiased">

  {quotes_strip}

  <!-- ══ Page header ═════════════════════════════════════════════ -->
  <header class="max-w-5xl mx-auto px-4 pt-5 pb-3">
    <div class="flex items-start justify-between">
      <div>
        <h1 class="text-2xl font-bold tracking-tight text-gray-100">Trading Dashboard</h1>
        <p class="text-xs text-gray-600 mt-0.5">Premium Seller · Options Strategy</p>
      </div>
      <div class="text-right shrink-0">
        <div class="text-xs text-gray-700">Built</div>
        <div class="text-xs text-gray-600">{build_time}</div>
      </div>
    </div>
  </header>

  <!-- ══ Main content ════════════════════════════════════════════ -->
  <main class="max-w-5xl mx-auto px-4 pb-6">
    {brief_sec}
    {perf_sec}
  </main>

  <!-- ══ Footer ══════════════════════════════════════════════════ -->
  <footer class="max-w-5xl mx-auto px-4 pb-10 text-center">
    <p class="text-xs text-gray-800">
      For informational purposes only &nbsp;·&nbsp; Not financial advice
      &nbsp;·&nbsp; Data: Tradier &amp; Finnhub &nbsp;·&nbsp; Built with Claude
    </p>
  </footer>

</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print(f"DASHBOARD GENERATOR — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    # Ensure output dirs exist
    DOCS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    # Favicon (generated once)
    make_favicon()

    # Fetch live quotes (1 API call for all 18 tickers)
    print(f"\nFetching {len(STRIP_TICKERS)} quotes...")
    quotes = fetch_quotes_batch(STRIP_TICKERS)
    print(f"  Received: {', '.join(quotes.keys()) or 'none'}")

    # Load brief
    print("\nLoading brief data...")
    brief_data = load_brief()
    if brief_data:
        print(f"  Generated: {brief_data.get('generated_at', '?')}")
    else:
        print("  Not found — will show placeholder")

    # Load recommendations
    print("Loading recommendations CSV...")
    rows = load_recommendations()
    print(f"  Rows: {len(rows)}")

    # Timestamp for quotes strip
    timestamp = et_now_str()

    # Generate and write HTML
    print("\nGenerating HTML...")
    html = generate_html(quotes, brief_data, rows, timestamp)
    INDEX_HTML.write_text(html, encoding="utf-8")

    print(f"\n✅ Written: {INDEX_HTML} ({len(html):,} bytes)")
    print("=" * 60)
    print("Done")
