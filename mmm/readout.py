#!/usr/bin/env python3
"""Readout: a main fit (+ validation and sensitivity runs) → a plain-English markdown + charts.

    python3 mmm/readout.py --config B/mmm.yaml --main <run_id> \\
        [--validation <run_id> ...] [--sensitivity <run_id> ...] --out DIR [--local-dir runs]

Reads each run's run_summary.json (and the main run's optimisation_scenarios.csv and
response_curves.csv) from the brand's GCS folder, or from --local-dir for local runs. Writes
DIR/readout.md and its PNG charts. Every number in the markdown comes from the run files; the
words around them are fixed, and the caveats come from the config (`readout.caveats`). Runs made
before a section's numbers existed simply skip that section.

Charts follow a neutral, colour-blind-checked palette (series blue and orange on a near-white
surface, text in ink, never in the series colour). Override it per brand with
`readout.palette` in the config, e.g. to match a client's PDF theme.
"""
from __future__ import annotations

import argparse
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_mmm  # noqa: E402

PALETTE = {"surface": "#fcfcfb", "ink": "#0b0b0b", "secondary": "#52514e", "muted": "#898781",
           "grid": "#e1e0d9", "baseline": "#c3c2b7", "series1": "#2a78d6", "series2": "#eb6834"}
PRIOR_LED = 0.3   # a posterior range less than 30% narrower than its prior's: the number is mostly the prior


def money(x: float, sym: str) -> str:
    return f"{sym}{x:,.0f}"


def use_exact_priors(d: dict, ch: dict) -> None:
    """Swap the prior ranges estimated from prior draws for the exact ones: recorded by the fit, or
    for older runs rebuilt from the priors file's range or the calibrated prior's mean and spread."""
    import math

    for c, p in (d.get("priors") or {}).items():
        if c not in ch:
            continue
        if p.get("range_90"):
            lo, hi = p["range_90"]
        elif p.get("calibrated"):
            sd = math.sqrt(math.log1p((p["calibrated_sd"] / p["calibrated_mean"]) ** 2))
            mu = math.log(p["calibrated_mean"]) - sd ** 2 / 2
            lo, hi = math.exp(mu - 1.6449 * sd), math.exp(mu + 1.6449 * sd)
        elif "low" in p:
            lo, hi = p["low"], p["high"]
        else:
            continue
        ch[c]["prior_roi_ci_lo"], ch[c]["prior_roi_ci_hi"] = lo, hi


def prior_flag(v: dict) -> str:
    """A warning when the data pulled a channel outside its prior's range, or barely moved it."""
    n = narrowing(v)
    if v["roi_median"] > v["prior_roi_ci_hi"]:
        return " **: above the prior's range, check for demand leaking in**"
    if v["roi_median"] < v["prior_roi_ci_lo"]:
        return " **: below the prior's range, check why**"
    return " **: mostly the prior, test it**" if n is not None and n < PRIOR_LED else ""


def narrowing(v: dict) -> float | None:
    """How much narrower the posterior's 90% range is than the prior's, in proportions (so 1–2
    counts the same as 2–4), which suits returns that can only be positive."""
    import math

    lo, hi, plo, phi = (v.get(k) for k in ("roi_ci_lo", "roi_ci_hi", "prior_roi_ci_lo", "prior_roi_ci_hi"))
    if not all(x and x > 0 for x in (lo, hi, plo, phi)) or phi <= plo:
        return None
    return 1 - math.log(hi / lo) / math.log(phi / plo)


def _style(ax, fig, p):
    fig.patch.set_facecolor(p["surface"])
    ax.set_facecolor(p["surface"])
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(p["baseline"])
    ax.tick_params(colors=p["secondary"], labelsize=9, length=0)
    ax.xaxis.grid(True, color=p["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def chart_roi(ch: dict, names: dict, sym: str, p: dict, path: pathlib.Path, margin: float | None = None) -> None:
    import matplotlib.pyplot as plt

    order = sorted(names, key=lambda c: ch[c]["roi_median"])
    fig, ax = plt.subplots(figsize=(7, 0.6 * len(order) + 0.8), dpi=200)
    _style(ax, fig, p)
    xmax = max(max(ch[c]["roi_ci_hi"] for c in order) * 1.4, 2.0, (1 / margin) * 1.15 if margin else 0)
    for i, c in enumerate(order):
        v = ch[c]
        ax.plot([v["roi_ci_lo"], v["roi_ci_hi"]], [i, i], color=p["series1"], linewidth=2, solid_capstyle="round")
        ax.scatter([v["roi_median"]], [i], s=60, color=p["series1"], zorder=3, edgecolors=p["surface"], linewidths=2)
        ax.text(v["roi_ci_hi"] + 0.03 * xmax, i, f"{v['roi_median']:.1f}  ({v['roi_ci_lo']:.1f}–{v['roi_ci_hi']:.1f})",
                va="center", fontsize=8.5, color=p["ink"], bbox={"facecolor": p["surface"], "edgecolor": "none", "pad": 1})
    # 1.0 = revenue equals spend. Profit break-even is 1 ÷ margin, drawn only when the margin is known.
    ax.axvline(1.0, color=p["secondary"], linewidth=1, linestyle=(0, (4, 3)), zorder=1)
    ax.text(1.0 + 0.01 * xmax, -0.55, "revenue = spend", color=p["secondary"], fontsize=8, va="center", zorder=2,
            bbox={"facecolor": p["surface"], "edgecolor": "none", "pad": 1})
    if margin:
        be = 1 / margin
        ax.axvline(be, color=p["ink"], linewidth=1, linestyle=(0, (1, 2)), zorder=1)
        ax.text(be + 0.01 * xmax, len(order) - 0.45, f"profit break-even ({margin:.0%} margin)", color=p["ink"],
                fontsize=8, va="center", bbox={"facecolor": p["surface"], "edgecolor": "none", "pad": 1})
    ax.set_yticks(range(len(order)), [names[c] for c in order], color=p["ink"])
    ax.set_ylim(-0.8, len(order) - (0.2 if margin else 0.5))
    ax.set_xlim(0, xmax)
    ax.set_xlabel(f"{sym} revenue per {sym}1 spent (median and 90% range)", color=p["secondary"], fontsize=9)
    fig.tight_layout()
    fig.savefig(path, facecolor=p["surface"])
    plt.close(fig)


def chart_share(ch: dict, names: dict, kpi: str, p: dict, path: pathlib.Path) -> None:
    import matplotlib.pyplot as plt

    paid = {c: ch[c]["pct_contribution_median"] for c in names}
    # The baseline is 100 − the model's all-channel median (a median of the sum, not a sum of medians).
    total_paid = (ch.get("All Channels") or {}).get("pct_contribution_median") or sum(paid.values())
    rows = [("Baseline (existing demand, seasonality, promotions)", 100 - total_paid)] + \
        sorted(((names[c], v) for c, v in paid.items()), key=lambda r: -r[1])
    fig, ax = plt.subplots(figsize=(7, 0.5 * len(rows) + 0.8), dpi=200)
    _style(ax, fig, p)
    ys = range(len(rows))[::-1]
    for y, (label, v) in zip(ys, rows):
        ax.barh(y, v, height=0.55, color=p["baseline"] if label.startswith("Baseline") else p["series1"])
        ax.text(v + 0.8, y, f"{v:.0f}%", va="center", fontsize=8.5, color=p["ink"])
    ax.set_yticks(list(ys), [r[0] for r in rows], color=p["ink"])
    ax.set_xlim(0, max(r[1] for r in rows) * 1.15)
    ax.set_xlabel(f"Share of {kpi.replace('_', ' ')} explained (median)", color=p["secondary"], fontsize=9)
    fig.tight_layout()
    fig.savefig(path, facecolor=p["surface"])
    plt.close(fig)


def chart_budget(scen, names: dict, sym: str, p: dict, path: pathlib.Path) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from matplotlib.ticker import FuncFormatter

    s = scen[scen.budget_multiplier == 1.0].pivot_table(index="channel", columns="allocation", values="spend")
    order = [c for c in sorted(names, key=lambda c: s.loc[c, "current_mix"]) if c in s.index]
    fig, ax = plt.subplots(figsize=(7, 0.7 * len(order) + 0.9), dpi=200)
    _style(ax, fig, p)
    h = 0.34
    for i, c in enumerate(order):
        ax.barh(i + h / 2 + 0.02, s.loc[c, "current_mix"], height=h, color=p["series2"])
        ax.barh(i - h / 2 - 0.02, s.loc[c, "optimised"], height=h, color=p["series1"])
        ax.text(max(s.loc[c, "current_mix"], s.loc[c, "optimised"]) * 1.02, i,
                f"{money(s.loc[c, 'current_mix'], sym)} → {money(s.loc[c, 'optimised'], sym)}",
                va="center", fontsize=8.5, color=p["ink"], bbox={"facecolor": p["surface"], "edgecolor": "none", "pad": 1})
    ax.set_yticks(range(len(order)), [names[c] for c in order], color=p["ink"])
    ax.set_xlim(0, s.values.max() * 1.45)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{sym}{x / 1000:,.0f}k"))
    ax.set_xlabel("Spend over the period", color=p["secondary"], fontsize=9)
    ax.legend(handles=[Patch(color=p["series2"], label="Current mix"),
                       Patch(color=p["series1"], label="Optimised (same budget)")],
              loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=8.5, labelcolor=p["ink"])
    fig.tight_layout()
    fig.savefig(path, facecolor=p["surface"])
    plt.close(fig)


def chart_priors(ch: dict, names: dict, sym: str, p: dict, path: pathlib.Path, calibrated: set) -> None:
    """Each channel's prior 90% range (wide, pale) under its posterior range (the data's answer)."""
    import matplotlib.pyplot as plt

    order = sorted(names, key=lambda c: ch[c]["roi_median"])
    fig, ax = plt.subplots(figsize=(7, 0.7 * len(order) + 0.9), dpi=200)
    _style(ax, fig, p)
    xmax = max(max(ch[c]["prior_roi_ci_hi"] for c in order), max(ch[c]["roi_ci_hi"] for c in order)) * 1.55
    for i, c in enumerate(order):
        v = ch[c]
        ax.plot([v["prior_roi_ci_lo"], v["prior_roi_ci_hi"]], [i, i], color=p["baseline"], linewidth=9,
                solid_capstyle="butt", zorder=1)
        ax.plot([v["roi_ci_lo"], v["roi_ci_hi"]], [i, i], color=p["series1"], linewidth=2.2,
                solid_capstyle="round", zorder=2)
        ax.scatter([v["roi_median"]], [i], s=46, color=p["series1"], zorder=3, edgecolors=p["surface"], linewidths=2)
        n = narrowing(v)
        if n is not None:
            ax.text(max(v["prior_roi_ci_hi"], v["roi_ci_hi"]) + 0.02 * xmax, i,
                    f"{n:.0%} narrower" + (" (lift test)" if c in calibrated else ""),
                    va="center", fontsize=8.5, color=p["ink"])
    ax.set_yticks(range(len(order)), [names[c] for c in order], color=p["ink"])
    ax.set_ylim(-0.7, len(order) - 0.3)
    ax.set_xlim(0, xmax)
    ax.set_xlabel(f"{sym} revenue per {sym}1, 90% range: before the data (pale), after it (blue)",
                  color=p["secondary"], fontsize=9)
    fig.tight_layout()
    fig.savefig(path, facecolor=p["surface"])
    plt.close(fig)


def chart_curves(rc, names: dict, sym: str, p: dict, path: pathlib.Path) -> None:
    """Revenue against spend per channel, with the average return and the return on the next unit."""
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    chans = [c for c in names if c in set(rc.channel)]
    cols = 2
    rows = (len(chans) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(7, 2.5 * rows + 0.5), dpi=200, squeeze=False)
    k = FuncFormatter(lambda x, _: f"{sym}{x / 1e6:,.1f}m" if x >= 1e6 else f"{sym}{x / 1000:,.0f}k" if x else f"{sym}0")
    for ax, c in zip(axes.flat, chans):
        _style(ax, fig, p)
        ax.yaxis.grid(True, color=p["grid"], linewidth=0.8)
        d = rc[rc.channel == c].sort_values("spend_multiplier")
        x, y = d.spend.to_numpy(), d.incremental_outcome_mean.to_numpy()
        ax.fill_between(x, d.incremental_outcome_ci_lo, d.incremental_outcome_ci_hi, color=p["series1"],
                        alpha=0.12, linewidth=0)
        ax.plot(x, y, color=p["series1"], linewidth=1.8)
        i = int((d.spend_multiplier - 1.0).abs().argmin())
        x1, y1 = x[i], y[i]
        ax.plot([0, x1], [0, y1], color=p["secondary"], linewidth=1, linestyle=(0, (4, 3)))
        j0, j1 = max(i - 1, 0), min(i + 1, len(x) - 1)
        slope = (y[j1] - y[j0]) / (x[j1] - x[j0])
        dx = 0.35 * x1
        ax.plot([x1 - dx, x1 + dx], [y1 - slope * dx, y1 + slope * dx], color=p["series2"], linewidth=1.8)
        ax.scatter([x1], [y1], s=36, color=p["ink"], zorder=3, edgecolors=p["surface"], linewidths=1.5)
        ax.set_title(names[c], fontsize=9, color=p["ink"], loc="left")
        ax.xaxis.set_major_locator(MaxNLocator(4))
        ax.yaxis.set_major_locator(MaxNLocator(4))
        ax.xaxis.set_major_formatter(k)
        ax.yaxis.set_major_formatter(k)
        ax.set_xlim(0, x.max())
        ax.set_ylim(0, d.incremental_outcome_ci_hi.max() * 1.05)
    for ax in list(axes.flat)[len(chans):]:
        ax.set_visible(False)
    fig.text(0.01, 0.005, "x: spend over the window   y: revenue it brought in   ● today's spend   "
             "dashed: average return   orange: return on the next unit", fontsize=7.5, color=p["secondary"])
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, facecolor=p["surface"])
    plt.close(fig)


def describe(run: dict, main: dict) -> str:
    """What a sensitivity run changed from the main fit, in words."""
    words = {"kpi": "KPI: {}", "demand_control": "demand control: {}", "priors_file": "priors: {}",
             "priors_scale": "priors ×{:g}", "experiments_file": "lift tests: {}", "knots": "knots: {}",
             "fourier": "{} seasonal wave pairs", "window_end": "data to {}"}
    diffs = []
    for k, w in words.items():
        v = run.get(k)
        if v != main.get(k):
            diffs.append("no demand control" if k == "demand_control" and v == "none" else
                         "no lift tests" if k == "experiments_file" and not v else w.format(v))
    return (", ".join(diffs) or "same settings").capitalize()


def read_csv(run_id: str, name: str, local_dir: str | None):
    """One of a run's CSV outputs, or None if the run didn't write it."""
    import pandas as pd

    if local_dir:
        f = pathlib.Path(local_dir) / run_id / name
        return pd.read_csv(f) if f.exists() else None
    csv = run_mmm.gcloud("storage", "cat", f"gs://{run_mmm.BUCKET}/{run_mmm.PREFIX}/runs/{run_id}/{name}",
                         check=False, stderr=False)
    return pd.read_csv(io.StringIO(csv)) if csv[:200].count(",") >= 2 else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    p.add_argument("--main", required=True)
    p.add_argument("--validation", nargs="*", default=[])
    p.add_argument("--sensitivity", nargs="*", default=[])
    p.add_argument("--out", required=True)
    p.add_argument("--local-dir", help="read local runs from this folder instead of GCS")
    a = p.parse_args()
    conf = run_mmm.init(a.config)
    cfg = conf.cfg
    names, sym, brand = cfg["channels"], cfg["currency"]["symbol"], cfg["brand"]["name"]
    pal = {**PALETTE, **(cfg["readout"].get("palette") or {})}
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    d = run_mmm.read_summary(a.main, a.local_dir)
    if not d:
        sys.exit(f"no run_summary.json for {a.main}")
    ch, data, run = d["channels"], d["data"], d["run"]
    kpi = run["kpi"]
    all_ch = ch.get("All Channels", {})
    scen = read_csv(a.main, "optimisation_scenarios.csv", a.local_dir)
    rc = read_csv(a.main, "response_curves.csv", a.local_dir)
    use_exact_priors(d, ch)
    has_priors = all(ch[c].get("prior_roi_ci_lo") for c in names)
    calibrated = {c for c, v in (d.get("priors") or {}).items() if v.get("calibrated")}

    margin = cfg["readout"].get("margin")
    chart_roi(ch, names, sym, pal, out / "roi.png", margin)
    chart_share(ch, names, kpi, pal, out / "share.png")
    if scen is not None:
        chart_budget(scen, names, sym, pal, out / "budget.png")
    if rc is not None:
        chart_curves(rc, names, sym, pal, out / "curves.png")
    if has_priors:
        chart_priors(ch, names, sym, pal, out / "priors.png", calibrated)

    paid = all_ch.get("pct_contribution_median", 0)
    best = max(names, key=lambda c: ch[c]["roi_median"])
    clear = all(ch[best]["roi_ci_lo"] > ch[c]["roi_ci_hi"] for c in names if c != best)
    lead = ("Its range sits clear of every other channel's, so the lead is solid." if clear else
            "Its range overlaps at least one other channel's, so treat the order as a guide, not a ranking.")
    L = [f"# {brand} marketing mix model — readout\n",
         f"*Model run `{a.main}` · Google Meridian {d.get('meridian')} · weekly, {' / '.join(cfg['geos'])}, "
         f"{data['first_week']} → {data['last_week']} ({data['weeks']} weeks)*\n",
         "## What the model says\n",
         f"- **Paid media explains about {paid:.0f}% of {kpi.replace('_', ' ')}. The other ~{100 - paid:.0f}% is "
         "the baseline**: people who already wanted the brand, seasonality and promotions.",
         f"- Across the paid channels, each {sym}1 brought back about **{sym}{all_ch.get('roi_median', 0):.2f}** in "
         f"revenue (90% range {sym}{all_ch.get('roi_ci_lo', 0):.2f}–{sym}{all_ch.get('roi_ci_hi', 0):.2f}).",
         *([f"- At a {margin:.0%} margin, a channel needs more than **{sym}{1 / margin:.2f}** per {sym}1 to pay for "
            "itself in profit (the dotted line in the chart)."] if margin else []),
         f"- Highest return: **{names[best]}**. {lead}\n",
         "![Return on spend by channel](roi.png)\n",
         "![Share explained by each driver](share.png)\n",
         "## Channel table\n",
         f"| Channel | Spend | Return per {sym}1 (median) | 90% range | Return on the next {sym}1 | Share explained |",
         "|---|---|---|---|---|---|"]
    for c in names:
        v = ch[c]
        L.append(f"| {names[c]} | {money(data['media_spend'][c], sym)} | {sym}{v['roi_median']:.2f} | "
                 f"{sym}{v['roi_ci_lo']:.2f}–{sym}{v['roi_ci_hi']:.2f} | {sym}{(v['mroi_median'] or 0):.2f} | "
                 f"{v['pct_contribution_median']:.1f}% |")
    L.append("\n*Return on the next " + sym + "1* is the budget number: move money from channels where it is low "
             "to channels where it is high.\n")
    if rc is not None:
        L += ["![Revenue against spend, by channel](curves.png)\n",
              "Each curve is what the channel brought in at different levels of spend over the window, with the "
              "same weekly pattern. The dashed line from zero to today's spend is the average return; the orange "
              "line is the slope at today's spend, the return on the next " + sym + "1. Where the orange line is "
              "much flatter than the dashed one, the channel is deep into diminishing returns.\n"]
    notes = cfg["readout"].get("notes") or {}
    for col, amount in (data.get("other_spend") or {}).items():
        L.append(f"`{col}` ({money(amount, sym)}) is counted but not modelled as a channel. "
                 + (notes.get(col) or "") + "\n")

    if has_priors:
        L += ["## What the data added to the priors\n",
              "![Prior and posterior ranges](priors.png)\n",
              "Each pale bar is the range the model started from (the prior: benchmarks"
              + (", or a lift test" if calibrated else "") + "); the blue line is where it ended up after the data. "
              "Where the blue range is barely narrower than the pale one, the data had little to say about that "
              "channel: its number is mostly the starting assumption, and it belongs on the test list. Where the "
              "blue range sits outside the pale one, the data pulled hard against the prior: either it's telling "
              "you something, or existing demand is leaking into the channel. Find out which before using it.\n",
              "| Channel | Prior 90% range | Posterior 90% range | Narrower by |", "|---|---|---|---|"]
        for c in names:
            v, n = ch[c], narrowing(ch[c])
            L.append(f"| {names[c]} | {sym}{v['prior_roi_ci_lo']:.2f}–{sym}{v['prior_roi_ci_hi']:.2f}"
                     + (" (lift test)" if c in calibrated else "")
                     + f" | {sym}{v['roi_ci_lo']:.2f}–{sym}{v['roi_ci_hi']:.2f} | "
                     + ("–" if n is None else f"{n:.0%}") + prior_flag(v) + " |")
        L.append("\n*Narrower by* compares the ranges in proportions, so £1–£2 counts the same as £2–£4.\n")
    if d.get("calibration"):
        L += ["## Lift tests in the model\n",
              "Each test replaced that channel's benchmark prior. Meridian widens a test's uncertainty when its "
              "spend level, length or age differs from the model's, so a short or old test counts for less.\n",
              "| Channel | Test result per " + sym + "1 | As used by the model |", "|---|---|---|"]
        for t in d["calibration"]:
            L.append(f"| {names.get(t['channel'], t['channel'])} | {sym}{t['estimate']:.2f} ± {t['standard_error']:.2f} | "
                     f"{sym}{t['adjusted_estimate']:.2f} ± {t['adjusted_standard_error']:.2f} |")
        L.append("")

    if scen is not None:
        oc = cfg["optimisation"] or {}
        draws = {s["budget_multiplier"]: s for s in (d.get("optimisation") or {}).get("draws") or []}
        mc = cfg["readout"]["min_chance"]
        L += [f"## {oc.get('label') or 'Budget'} scenarios\n",
              f"Last year's {oc.get('label') or 'period'} weekly pattern stands in for this year's. Each channel can "
              f"move up to ±{oc.get('spend_bound', 0):.0%} from its share.\n",
              "![Current vs optimised spend](budget.png)\n"]
        if draws:
            L += ["| Budget | Current mix: extra revenue | Optimised: extra revenue | Uplift | Uplift, middle 80% "
                  "of draws | Chance the new split wins |", "|---|---|---|---|---|---|"]
        else:
            L += ["| Budget | Current mix: extra revenue | Optimised: extra revenue | Uplift |", "|---|---|---|---|"]
        for m, g in scen.groupby("budget_multiplier"):
            cur = g[g.allocation == "current_mix"].incremental_outcome_mean.sum()
            opt = g[g.allocation == "optimised"].incremental_outcome_mean.sum()
            spend = g[g.allocation == "current_mix"].spend.sum()
            row = f"| {money(spend, sym)} (×{m:.1f}) | {money(cur, sym)} | {money(opt, sym)} | {opt / cur - 1:+.1%} |"
            s = draws.get(m)
            if s:
                lo, _, hi = s["uplift_pct_p10_p50_p90"]
                row += f" {lo:+.1%} to {hi:+.1%} | {s['chance_optimised_beats_current']:.0%} |"
            L.append(row)
        if 1.0 in draws:
            s = draws[1.0]
            ok = s["chance_optimised_beats_current"] >= mc
            L.append(f"\nThe uplift is the optimiser's best estimate. Scored on every draw from the model, the new split "
                     f"beats the current one in **{s['chance_optimised_beats_current']:.0%}** of them. The rule here: move "
                     f"budget when that is at least {mc:.0%}; below that, test the change first. "
                     + ("On that rule this re-split can go ahead. " if ok else "On that rule this re-split needs a test first. ")
                     + f"In nine draws out of ten it changes revenue by {s['uplift_pct_p10_p50_p90'][0]:+.1%} or better.")
        steps = [(m, s["return_on_budget_change_p10_p50_p90"]) for m, s in sorted(draws.items())
                 if s.get("return_on_budget_change_p10_p50_p90")]
        if steps:
            L.append("\n**How much to spend, not just where.** Starting from the optimised split at today's budget:")
            for m, (lo, mid, hi) in steps:
                verb = "each extra " + sym + "1 brings back" if m > 1 else "each " + sym + "1 cut loses"
                L.append(f"- {'adding' if m > 1 else 'cutting'} {abs(m - 1):.0%}: {verb} about **{sym}{mid:.2f}** "
                         f"(middle 80% of draws: {sym}{lo:.2f}–{sym}{hi:.2f})")
            if margin:
                L.append(f"\nAt a {margin:.0%} margin an extra {sym}1 pays for itself in profit only if it brings back "
                         f"more than {sym}{1 / margin:.2f}.")
        L.append("\nA small uplift means the current split is already close to efficient; at peak spend the "
                 "bigger lever is often the budget level, not the split.\n")

    fmt = lambda x: "–" if x is None else f"{x:.2f}"  # noqa: E731
    if a.validation:
        L += ["## How far to trust it\n",
              "Each check hides some weeks from the model, then asks it to predict them.\n",
              "| Check | Fit on hidden weeks (R², higher is better) | Error on hidden weeks (wMAPE, lower is better) "
              "| Converged (R-hat < 1.1) | Meridian review |",
              "|---|---|---|---|---|"]
        for r in run_mmm.digest(a.validation, a.local_dir):
            h = cfg["holdouts"].get(r.get("holdout") or "", {})
            L.append(f"| {h.get('label') or r.get('holdout') or r['run_id']} | {fmt(r.get('r2_test_national'))} | "
                     f"{fmt(r.get('wmape_test_national'))} | {fmt(r.get('rhat_max'))} | {r.get('review')} |")
        L.append("")
    if a.sensitivity:
        L += ["## What moves the answer\n",
              "Each run changes one setting. Channels whose return holds steady are robust; the ones that move "
              "lean on that setting.\n",
              "| Change | " + " | ".join(names.values()) + " | Paid share |",
              "|---|" + "---|" * (len(names) + 1)]
        for r in run_mmm.digest([a.main, *a.sensitivity], a.local_dir):
            s = run_mmm.read_summary(r["run_id"], a.local_dir) or {}
            label = "**Main fit**" if r["run_id"] == a.main else describe(s.get("run") or {}, run)
            L.append(f"| {label} | " + " | ".join(f"{sym}{r.get(c + '_roi') or 0:.2f}" for c in names)
                     + f" | {r.get('all_contrib_pct') or 0:.0f}% |")
        L.append("")
    L.append("## Caveats\n")
    L += [f"- {c}" for c in cfg["readout"].get("caveats") or []]
    L.append("- Every judgement call and open question is in the assumptions register.")
    (out / "readout.md").write_text("\n".join(L) + "\n")
    print(out / "readout.md")


if __name__ == "__main__":
    main()
