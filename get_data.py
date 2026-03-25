import yfinance as yf
import pandas as pd
import os
import time
from datetime import datetime, timedelta
import pytz

os.makedirs("data", exist_ok=True)

ET  = pytz.timezone("US/Eastern")
now = datetime.now(ET)

# ── yfinance hard limit ───────────────────────────────────────────────────────
# 1-minute data is only available for the last 30 days from Yahoo.
# We fetch in 7-day chunks going back as far as possible within that window.
# 4 chunks × 7 days = 28 days ≈ ~20 trading days ≈ ~7,800 bars.
# This is the maximum yfinance will return for 1-min interval — no workaround.

CHUNKS     = 4          # 4 × 7 days = 28 calendar days (yfinance max for 1-min)
CHUNK_DAYS = 7

print(f"Downloading SPY 1-minute data ({CHUNKS} chunks × {CHUNK_DAYS} days)...")
print(f"Note: yfinance caps 1-min history at 30 days — this is a Yahoo limitation.")
print()

all_chunks = []

for i in range(CHUNKS):
    end   = now - timedelta(days=i * CHUNK_DAYS)
    start = end - timedelta(days=CHUNK_DAYS)

    print(f"  Fetching chunk {i+1}/{CHUNKS}: "
          f"{start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")

    try:
        chunk = yf.download(
            "SPY",
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1m",
            progress=False,
            auto_adjust=True,
        )

        if chunk.empty:
            print(f"    No data returned — skipping (likely outside 30-day window)")
            continue

        # Flatten multi-level columns
        if isinstance(chunk.columns, pd.MultiIndex):
            chunk.columns = [col[0].lower() for col in chunk.columns]
        else:
            chunk.columns = [col.lower() for col in chunk.columns]

        # Reset index so datetime becomes a regular column
        chunk = chunk.reset_index()

        # Rename datetime column to timestamp regardless of what yfinance calls it
        for possible_name in ["datetime", "date", "index", "Datetime", "Date"]:
            if possible_name in chunk.columns:
                chunk = chunk.rename(columns={possible_name: "timestamp"})
                break

        chunk = chunk[["timestamp", "open", "high", "low", "close", "volume"]].dropna()
        print(f"    Got {len(chunk)} bars")
        all_chunks.append(chunk)

    except Exception as e:
        print(f"    Error: {e}")

    time.sleep(1.5)  # Be polite to Yahoo servers

# ── Combine ───────────────────────────────────────────────────────────────────

if not all_chunks:
    print("\nERROR: No data downloaded.")
    print("Check your internet connection or try again later.")
    exit(1)

df = pd.concat(all_chunks, ignore_index=True)
df = df.drop_duplicates(subset=["timestamp"])
df = df.sort_values("timestamp").reset_index(drop=True)

# ── Timezone → ET ─────────────────────────────────────────────────────────────

df["timestamp"] = pd.to_datetime(df["timestamp"])

if df["timestamp"].dt.tz is not None:
    df["timestamp"] = df["timestamp"].dt.tz_convert("US/Eastern")
else:
    df["timestamp"] = df["timestamp"].dt.tz_localize("UTC").dt.tz_convert("US/Eastern")

# ── RTH filter (9:30 AM – 4:00 PM ET, weekdays only) ─────────────────────────

rth_mask = (
    (df["timestamp"].dt.weekday < 5) &
    (
        (df["timestamp"].dt.hour > 9) |
        ((df["timestamp"].dt.hour == 9) & (df["timestamp"].dt.minute >= 30))
    ) &
    (df["timestamp"].dt.hour < 16)
)
df = df[rth_mask].reset_index(drop=True)

# Strip timezone for clean CSV
df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

# ── Save ──────────────────────────────────────────────────────────────────────

trading_days = df["timestamp"].apply(lambda x: x[:10]).nunique()

print()
print(f"Total bars:      {len(df)}")
print(f"Trading days:    {trading_days}")
print(f"Date range:      {df.timestamp.iloc[0]} → {df.timestamp.iloc[-1]}")
print(f"Price range:     ${df.close.min():.2f} → ${df.close.max():.2f}")

df.to_csv("data/spy_real.csv", index=False)

print()
print("Saved to data/spy_real.csv")
print()
print("─" * 50)
print("IMPORTANT: yfinance only gives ~20 trading days of")
print("1-min data. For longer history (6-12 months) you")
print("need one of these free alternatives:")
print()
print("  1. Polygon.io  — free tier, 2 years of 1-min data")
print("     https://polygon.io  (sign up, use their Python client)")
print()
print("  2. Alpha Vantage — free tier, limited calls/day")
print("     https://www.alphavantage.co")
print()
print("  3. IBKR itself — once TWS is running, the system")
print("     pulls historical data automatically on startup.")
print("     After 1 week of paper trading you will have")
print("     enough data to run a meaningful backtest.")
print("─" * 50)
print()
print("Next step: python backtest.py --csv data/spy_real.csv")