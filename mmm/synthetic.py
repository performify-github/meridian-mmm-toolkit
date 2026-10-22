#!/usr/bin/env python3
"""Make a synthetic input table that matches a config, with known true channel returns.

    python3 mmm/synthetic.py --config example/mmm.yaml            # → data.csv from the config
    python3 mmm/synthetic.py --config example/mmm.yaml --weeks 156 --out /tmp/demo.csv

For trying the toolkit end to end with no client data, and for checking that a model
recovers returns you know. Next to the CSV it writes `<name>.truth.json`: the true ROI and
share of orders for each channel.

How the fake brand behaves (deliberately the traps a real model has to handle):
- **Demand** rises into a Christmas peak, grows a little each year and wanders week to week.
  Google Trends and brand-search clicks both follow it.
- **Lower-funnel channels** (any channel id without "upper") spend more when demand is high,
  the way PMax and retargeting do. Without a demand control they look better than they are.
  Their budgets also move quarter to quarter for their own reasons, which is what lets a
  model separate spend from demand.
- **Upper-funnel channels** run in a few flights a year, one always in Q4.
- **Every channel** carries over into later weeks and saturates at higher spend.
- **Promotions** lift orders in a handful of weeks, including Black Friday.

Sales are built the way the model sees them (baseline + promotions + each channel's effect,
added together), so a well-specified model should land near the true returns. The traps are
in what moves together, not in the shape of the maths.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import config as cfgmod  # noqa: E402

TRUE_ROI = {"convert": [1.8, 1.3, 2.2], "upper": [1.1, 0.7, 1.4]}   # true revenue per unit of spend, by channel type
DECAY = {"convert": 0.3, "upper": 0.6}      # weekly carry-over


def _adstock(x, decay):
    import numpy as np

    out, carry = np.zeros_like(x), 0.0
    for i, v in enumerate(x):
        carry = v + decay * carry
        out[i] = carry
    return out


def generate(conf: cfgmod.Config, weeks: int, seed: int):
    import numpy as np
    import pandas as pd

    c = conf.cfg
    rng = np.random.default_rng(seed)
    start = pd.Timestamp(c["data"]["window"]["start"] or "2023-01-02")
    week = pd.date_range(start, periods=weeks, freq="7D")
    t = np.arange(weeks)
    doy = (week + pd.Timedelta(days=3)).dayofyear.to_numpy()
    month, day = week.month.to_numpy(), week.day.to_numpy()
    q4_peak = ((month == 11) & (day >= 20)) | ((month == 12) & (day <= 24))
    bfcm = (month == 11) & (day >= 22) | ((month == 12) & (day <= 3))
    season = 1 + 0.25 * np.cos(2 * np.pi * (doy - 350) / 365.25) + 0.6 * q4_peak
    trend = 1 + 0.08 * t / 52

    pops = c["geos"]
    top = max(pops.values())
    channels = list(c["channels"])
    kind = {ch: "upper" if "upper" in ch else "convert" for ch in channels}
    nth = {ch: [k for k in channels if kind[k] == kind[ch]].index(ch) for ch in channels}
    roi = {ch: TRUE_ROI[kind[ch]][nth[ch] % 3] for ch in channels}
    rows, effects = [], {ch: 0.0 for ch in channels}
    spend_tot = {ch: 0.0 for ch in channels}
    orders_tot = 0.0

    # Upper-funnel flights: three a year, six weeks each, one always starting in early November.
    flights = {}
    for ch in channels:
        on = np.zeros(weeks, bool)
        for y0 in range(0, weeks, 52):
            for s in [*rng.integers(y0 + 6, y0 + 36, 2), y0 + 44]:
                on[s:s + 6] = True
        flights[ch] = on[:weeks]

    for g, pop in pops.items():
        size = (pop / top) ** 0.9
        noise = np.zeros(weeks)
        for i in range(1, weeks):
            noise[i] = 0.7 * noise[i - 1] + rng.normal(0, 0.05)
        demand = 400 * size * season * trend * np.exp(noise)      # baseline orders a week
        promo = bfcm | (rng.random(weeks) < 0.08)
        depth = np.where(promo, rng.uniform(0.12, 0.3, weeks), rng.uniform(0, 0.03, weeks))
        aov = 48 * (1 + rng.normal(0, 0.03, weeks)) * (1 - 0.5 * depth)
        # Additive, like the model: baseline demand + a promotion bump + each channel's effect.
        base_orders = demand + 1.5 * depth * promo * demand.mean()
        quarter = (t // 13)

        frame = {"week_start": week.strftime("%Y-%m-%d"), "geo": g}
        incr = np.zeros(weeks)
        for j, ch in enumerate(channels):
            if kind[ch] == "convert":
                # follows demand (the trap), but budgets also change quarter to quarter for their own
                # reasons — the independent movement a model needs to tell spend from demand
                budget = rng.uniform(0.5, 1.5, quarter.max() + 1)[quarter]
                spend = (900 + 250 * j) * size * (demand / demand.mean()) * budget * rng.lognormal(0, 0.25, weeks)
            else:
                spend = np.where(flights[ch], (2500 + 500 * j) * size * rng.lognormal(0, 0.15, weeks), 0.0)
            cpm = (6 + 3 * j) * rng.lognormal(0, 0.08, weeks)
            frame[conf.spend_col(ch)] = spend.round(2)
            frame[conf.media_col(ch)] = (spend / cpm * 1000).round()
            x = _adstock(spend, DECAY[kind[ch]])
            half = np.median(x[x > 0]) if (x > 0).any() else 1.0
            hill = x / (x + half)                                # diminishing returns
            effects_raw = hill * aov
            # scale so this geo's incremental revenue ÷ spend equals the channel's true ROI
            beta = roi[ch] * spend.sum() / max(effects_raw.sum(), 1e-9)
            eff = beta * hill
            incr += eff
            effects[ch] += float((eff * aov).sum())
            spend_tot[ch] += float(spend.sum())

        upper_lift = 1 + 0.15 * sum(_adstock(flights[ch].astype(float), 0.5) for ch in channels if kind[ch] == "upper")
        orders = rng.poisson(base_orders + incr)
        orders_tot += float(orders.sum())
        new = rng.binomial(orders, 0.82)
        revenue = orders * aov
        clicks = 0.35 * demand * upper_lift * rng.lognormal(0, 0.05, weeks)
        impr = clicks / 0.3
        frame |= {
            "orders": orders, "new_customers": new, "revenue": revenue.round(2),
            "new_customer_revenue": (new * aov * 0.97).round(2),
            "brand_search_spend": (clicks * 0.25).round(2), "brand_search_clicks": clicks.round(),
            "brand_search_impressions": impr.round(),
            "brand_search_eligible_impressions": (impr / rng.uniform(0.8, 0.95, weeks)).round(),
            "brand_trends_index": np.clip(100 * demand / demand.max() + rng.normal(0, 3, weeks), 0, 100).round(),
            "discount_depth": depth.round(4), "sale_campaign_live": promo.astype(int),
            "unmapped_spend": 0.0, "unmapped_geo_spend_all_geos": 0.0,
        }
        rows.append(pd.DataFrame(frame))

    df = pd.concat(rows).sort_values(["week_start", "geo"]).reset_index(drop=True)
    for ch in c["controls"]:
        if ch not in df:
            df[ch] = 0.0
    truth = {"seed": seed, "weeks": weeks,
             "channels": {ch: {"true_roi": round(effects[ch] / spend_tot[ch], 3),
                               "kind": kind[ch], "weekly_decay": DECAY[kind[ch]]} for ch in channels},
             "note": "true_roi = incremental revenue ÷ spend over the whole window, all geos"}
    return df, truth


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    p.add_argument("--weeks", type=int, default=156)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", help="default: the config's data.csv")
    a = p.parse_args()
    conf = cfgmod.load(a.config)
    out = pathlib.Path(a.out or conf.cfg["data"]["csv"] or "synthetic.csv")
    df, truth = generate(conf, a.weeks, a.seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    out.with_suffix(".truth.json").write_text(json.dumps(truth, indent=1))
    print(f"{out}: {len(df)} rows, {df['week_start'].nunique()} weeks × {df['geo'].nunique()} geos "
          f"({dt.date.fromisoformat(df['week_start'].min())} → {df['week_start'].max()})")
    print(json.dumps(truth["channels"], indent=1))


if __name__ == "__main__":
    main()
