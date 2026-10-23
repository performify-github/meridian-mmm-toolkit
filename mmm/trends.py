#!/usr/bin/env python3
"""Google Trends for the brand → a weekly demand series per geo (a demand control).

    python3 mmm/trends.py --config B/mmm.yaml fetch --out trends.csv   # pull weekly per country (rate-limited)
    python3 mmm/trends.py --config B/mmm.yaml load --csv trends.csv    # build geo series → BigQuery
    python3 mmm/trends.py --config B/mmm.yaml load --csv trends.csv --geo-csv geo.csv   # or → a CSV

Why Trends: it measures how many people search for the brand, and it is less tied to ad delivery
than brand-search clicks (upper-funnel ads can still raise it, but can't switch it on and off). Use the brand's language-independent
TOPIC id (config `trends.topic`), so searches in every language count.

How the series is built (config `trends`):
- Each country's series is scaled 0–100 to its own peak, by Google.
- Trends weeks run Sunday–Saturday; each is moved to the model's week start that follows.
- A geo made of several countries is the weighted average of them, weighted by each
  country's share of sales (`trends.geos`).
- Zero means search volume below Google's reporting threshold, which is common in small
  markets.

Tables written by `load` (both replaced each time):
  trends.country_table   week_start, country, trends_index, fetched_utc
  trends.geo_table       week_start, geo, trends_index    (joined by the weekly view)

pytrends is an unofficial client: Google may answer 429 (rate limit). The script backs off
and retries a few times, then gives up on that country; it never tries to get round a block.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import random
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import config as cfgmod  # noqa: E402

WEEKDAY = {d: i for i, d in enumerate(cfgmod.WEEKDAYS)}


def fetch(t: dict, out: str) -> None:
    import pandas as pd
    from pytrends.request import TrendReq

    py = TrendReq(hl=t.get("hl", "en-GB"), tz=0)   # no retries= (pytrends' Retry args break on urllib3 2.x)
    end = dt.date.today().isoformat()
    countries = sorted({cc for w in t["geos"].values() for cc in w})
    frames = []
    for cc in countries:
        df = None
        for attempt in range(4):
            try:
                py.build_payload([t["topic"]], timeframe=f"{t['start']} {end}", geo=cc)
                df = py.interest_over_time()
                break
            except Exception as e:  # noqa: BLE001 — 429s and transient errors
                print(f"{cc}: attempt {attempt + 1} {type(e).__name__}; backing off", flush=True)
                time.sleep(60 * (attempt + 1))
        if df is None or df.empty:
            print(f"{cc}: no data")
            continue
        df = df.reset_index().rename(columns={t["topic"]: "trends_index"})
        df["country"] = cc
        frames.append(df[["date", "country", "trends_index", "isPartial"]])
        print(f"{cc}: {len(df)} weeks", flush=True)
        time.sleep(8 + random.random() * 4)
    pd.concat(frames).to_csv(out, index=False)
    print("saved", out)


def geo_series(conf: cfgmod.Config, csv: str):
    import pandas as pd

    t = conf.cfg["trends"]
    raw = pd.read_csv(csv, parse_dates=["date"])
    raw = raw[~raw["isPartial"].astype(str).str.lower().eq("true")]
    countries = {cc for w in t["geos"].values() for cc in w}
    missing = countries - set(raw["country"])
    if missing:
        raise SystemExit(f"countries missing from {csv}: {sorted(missing)} — re-fetch before loading")
    # Trends dates are the Sunday that starts each Trends week; move to the model's week start on or after it.
    shift = (WEEKDAY[conf.cfg["data"]["week_start_day"]] - WEEKDAY["sunday"]) % 7
    raw["week_start"] = (raw["date"] + pd.Timedelta(days=shift)).dt.date
    raw["fetched_utc"] = pd.Timestamp.now(tz="UTC")
    country = raw[["week_start", "country", "trends_index", "fetched_utc"]]
    parts = []
    for geo, weights in t["geos"].items():
        g = country[country["country"].isin(weights)].assign(w=lambda d: d["country"].map(weights))
        g = (g.assign(wx=g["w"] * g["trends_index"]).groupby("week_start", as_index=False)[["wx", "w"]].sum()
             .assign(trends_index=lambda d: d["wx"] / d["w"], geo=geo))
        parts.append(g[["week_start", "geo", "trends_index"]])
    return country, pd.concat(parts)


def load(conf: cfgmod.Config, csv: str, geo_csv: str | None) -> None:
    country, geo = geo_series(conf, csv)
    if geo_csv:
        geo.to_csv(geo_csv, index=False)
        print(f"{geo_csv}: {len(geo)} rows")
        return
    from google.cloud import bigquery

    t, g = conf.cfg["trends"], conf.cfg["gcp"]
    bq = bigquery.Client(project=g["project"], location=g["bq_location"])
    job = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    for df, table in ((country, t["country_table"]), (geo, t["geo_table"])):
        bq.load_table_from_dataframe(df, table, job_config=job).result()
        print(f"{table}: {len(df)} rows")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", default="trends.csv")
    ld = sub.add_parser("load")
    ld.add_argument("--csv", required=True)
    ld.add_argument("--geo-csv", help="write the geo series to this CSV instead of BigQuery")
    a = p.parse_args()
    conf = cfgmod.load(a.config)
    if not conf.cfg["trends"]:
        sys.exit("the config has no `trends` block")
    fetch(conf.cfg["trends"], a.out) if a.cmd == "fetch" else load(conf, a.csv, a.geo_csv)


if __name__ == "__main__":
    main()
