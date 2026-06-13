"""
Evaluate past trading agent recommendations against actual outcomes.
Runs weekly (Friday after close). Sends a performance report email.
"""
import os
import csv
import requests
import smtplib
import markdown as md
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict

TRADIER_TOKEN = os.environ.get("TRADIER_TOKEN")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = "vja118@gmail.com"

LOG_PATH = Path(__file__).parent / "recommendations_log.csv"
LOG_FIELDS = ["date", "ticker", "type", "strike", "expiry", "credit",
              "stock_price", "delta", "iv", "dte", "contracts", "stars",
              "outcome", "close_at_expiry", "pnl", "evaluated_date"]

HEADERS = {
    "Authorization": f"Bearer {TRADIER_TOKEN}",
    "Accept": "application/json"
}


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_closing_price(ticker, target_date):
    """Fetch the closing price on or just before target_date (handles weekends/holidays)."""
    for offset in range(5):
        check = target_date - timedelta(days=offset)
        r = requests.get(
            "https://api.tradier.com/v1/markets/history",
            headers=HEADERS,
            params={
                "symbol": ticker,
                "interval": "daily",
                "start": check.strftime("%Y-%m-%d"),
                "end": check.strftime("%Y-%m-%d"),
            }
        )
        history = r.json().get("history")
        if history and "day" in history:
            day = history["day"]
            if isinstance(day, list):
                day = day[-1]
            return float(day["close"]), check
    return None, None


# ── Evaluation logic ───────────────────────────────────────────────────────────

def evaluate_row(row):
    """Evaluate a single expired recommendation. Returns updated row dict."""
    expiry = datetime.strptime(row["expiry"], "%Y-%m-%d").date()
    if expiry >= date.today():
        return row  # Not expired yet — skip
    if row.get("outcome"):
        return row  # Already evaluated — skip

    ticker = row["ticker"]
    strike = float(row["strike"])
    credit = float(row["credit"])
    contracts = int(row["contracts"])
    option_type = row["type"]  # "put" or "call"

    print(f"  Evaluating {ticker} {option_type} ${strike} exp {expiry}...")
    close_price, actual_date = fetch_closing_price(ticker, expiry)
    if close_price is None:
        print(f"    ⚠️ Could not fetch close price — skipping")
        return row

    # Win/loss determination
    if option_type == "put":
        win = close_price >= strike
    else:  # call (covered call or naked)
        win = close_price <= strike

    # P&L calculation (option leg only)
    if win:
        pnl = credit * contracts * 100
        outcome = "win"
    else:
        if option_type == "put":
            intrinsic_loss = (strike - close_price) * contracts * 100
        else:
            intrinsic_loss = (close_price - strike) * contracts * 100
        pnl = (credit * contracts * 100) - intrinsic_loss
        outcome = "loss"

    print(f"    → {outcome.upper()} | close ${close_price:.2f} | P&L ${pnl:,.0f}")

    row["outcome"] = outcome
    row["close_at_expiry"] = round(close_price, 2)
    row["pnl"] = round(pnl, 2)
    row["evaluated_date"] = date.today().strftime("%Y-%m-%d")
    return row


# ── Report generation ──────────────────────────────────────────────────────────

def generate_report(rows):
    evaluated = [r for r in rows if r.get("outcome") in ("win", "loss")]

    if not evaluated:
        return (
            "## 📊 Weekly Performance Report\n\n"
            "No expired recommendations to evaluate yet — check back next week once "
            "positions start expiring.\n"
        )

    total = len(evaluated)
    wins = sum(1 for r in evaluated if r["outcome"] == "win")
    total_pnl = sum(float(r["pnl"]) for r in evaluated)
    win_rate = wins / total * 100

    # By star rating
    by_stars = defaultdict(list)
    for r in evaluated:
        key = str(r.get("stars") or "unrated")
        by_stars[key].append(r)

    # Last 30 days
    cutoff = date.today() - timedelta(days=30)
    recent = [r for r in evaluated
              if datetime.strptime(r["expiry"], "%Y-%m-%d").date() >= cutoff]

    # All-time best and worst
    sorted_by_pnl = sorted(evaluated, key=lambda r: float(r["pnl"]))
    worst = sorted_by_pnl[:3]
    best = sorted_by_pnl[-3:][::-1]

    report = f"""## 📊 Weekly Performance Report — {date.today().strftime('%b %d, %Y')}

### Overall (all-time)

| Metric | Value |
|--------|-------|
| Total evaluated | {total} |
| Win rate | {win_rate:.1f}% |
| Total P&L | ${total_pnl:,.0f} |
| Last 30 days | {len(recent)} trades |

---

### Win Rate by Star Rating

| Stars | Trades | Wins | Win Rate | P&L |
|-------|--------|------|----------|-----|
"""
    star_order = sorted(by_stars.keys(),
                        key=lambda x: int(x) if x.isdigit() else -1,
                        reverse=True)
    for stars in star_order:
        group = by_stars[stars]
        g_wins = sum(1 for r in group if r["outcome"] == "win")
        g_pnl = sum(float(r["pnl"]) for r in group)
        g_rate = g_wins / len(group) * 100
        label = f"{'⭐' * int(stars)}" if stars.isdigit() else stars
        report += f"| {label} | {len(group)} | {g_wins} | {g_rate:.0f}% | ${g_pnl:,.0f} |\n"

    if recent:
        report += "\n---\n\n### Recent Expirations (Last 30 Days)\n\n"
        report += "| Rec Date | Ticker | Type | Strike | Credit | Contracts | Stars | Close | Outcome | P&L |\n"
        report += "|----------|--------|------|--------|--------|-----------|-------|-------|---------|-----|\n"
        for r in sorted(recent, key=lambda x: x["expiry"], reverse=True):
            emoji = "✅" if r["outcome"] == "win" else "❌"
            stars_str = ("⭐" * int(r["stars"])) if str(r.get("stars", "")).isdigit() else "—"
            report += (
                f"| {r['date']} | {r['ticker']} | {r['type'].upper()} "
                f"| ${r['strike']} | ${r['credit']} | {r['contracts']} "
                f"| {stars_str} | ${r['close_at_expiry']} "
                f"| {emoji} {r['outcome']} | ${float(r['pnl']):,.0f} |\n"
            )

    report += "\n---\n\n### Best Trades (All-Time)\n\n"
    report += "| Rec Date | Ticker | Type | Strike | P&L |\n|----------|--------|------|--------|-----|\n"
    for r in best:
        report += f"| {r['date']} | {r['ticker']} | {r['type'].upper()} | ${r['strike']} | ${float(r['pnl']):,.0f} |\n"

    report += "\n---\n\n### Worst Trades (All-Time)\n\n"
    report += "| Rec Date | Ticker | Type | Strike | P&L |\n|----------|--------|------|--------|-----|\n"
    for r in worst:
        report += f"| {r['date']} | {r['ticker']} | {r['type'].upper()} | ${r['strike']} | ${float(r['pnl']):,.0f} |\n"

    return report


# ── Email ──────────────────────────────────────────────────────────────────────

def send_report_email(subject, body_md):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_SENDER
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(body_md, "plain"))
    html_body = md.markdown(body_md, extensions=["tables"])
    html = f"""<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      max-width: 900px; margin: 0 auto; line-height: 1.6; padding: 16px; color: #1a1a1a;">
    {html_body}
    </body></html>"""
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


# ── CSV read/write ─────────────────────────────────────────────────────────────

def load_csv():
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH, newline="") as f:
        return list(csv.DictReader(f))


def save_csv(rows):
    tmp = LOG_PATH.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(LOG_PATH)


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 70)
    print(f"RECOMMENDATION EVALUATOR — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    rows = load_csv()
    print(f"\nLoaded {len(rows)} rows from {LOG_PATH.name}")

    pending = [r for r in rows if not r.get("outcome") and r.get("expiry")]
    expired_pending = [
        r for r in pending
        if datetime.strptime(r["expiry"], "%Y-%m-%d").date() < date.today()
    ]
    print(f"Rows to evaluate: {len(expired_pending)}")

    updated_rows = []
    for row in rows:
        updated_rows.append(evaluate_row(row))

    save_csv(updated_rows)
    print(f"\n✅ CSV updated")

    print("\nGenerating performance report...")
    report = generate_report(updated_rows)
    print(report)

    print("\nSending weekly report email...")
    try:
        subject = f"📈 Weekly Trade Performance — {date.today().strftime('%b %d, %Y')}"
        send_report_email(subject, report)
        print(f"✅ Report sent to {RECIPIENT_EMAIL}")
    except Exception as e:
        print(f"❌ Email failed: {e}")

    print("=" * 70)
    print("✅ Done")
