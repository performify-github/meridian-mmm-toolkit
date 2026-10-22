#!/usr/bin/env python3
"""Readout: a main fit (+ validation and sensitivity runs) → a plain-English markdown + charts.

    python3 mmm/readout.py --config B/mmm.yaml --main <run_id> \\
        [--validation <run_id> ...] [--sensitivity <run_id> ...] --out DIR [--local-dir runs]

Reads each run's run_summary.json (and the main run's optimisation_scenarios.csv) from the
brand's GCS folder, or from --local-dir for local runs. Writes DIR/readout.md and three PNGs.
Every number in the markdown comes from the run files; the words around them are fixed, and
the caveats come from the config (`readout.caveats`).

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


def money(x: float, sym: str) -> str:
    return f"{sym}{x:,.0f}"


def _style(ax, fig, p):
    fig.patch.set_facecolor(p["surface"])
    ax.set_facecolor(p["surface"])
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(p["baseline"])
    ax.tick_params(colors=p["secondary"], labelsize=9, length=0)
    ax.xaxis.grid(True, color=p["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def chart_roi(ch: dict, names: dict, sym: str, p: dict, path: pathlib.Path) -> None:
    import matplotlib.pyplot as plt

    order = sorted(names, key=lambda c: ch[c]["roi_median"])
    fig, ax = plt.subplots(figsize=(7, 0.6 * len(order) + 0.8), dpi=200)
    _style(ax, fig, p)
    xmax = max(max(ch[c]["roi_ci_hi"] for c in order) * 1.4, 2.0)
    for i, c in enumerate(order):
        v = ch[c]
        ax.plot([v["roi_ci_lo"], v["roi_ci_hi"]], [i, i], color=p["series1"], linewidth=2, solid_capstyle="round")
        ax.scatter([v["roi_median"]], [i], s=60, color=p["series1"], zorder=3, edgecolors=p["surface"], linewidths=2)
        ax.text(v["roi_ci_hi"] + 0.03 * xmax, i, f"{v['roi_median']:.1f}  ({v['roi_ci_lo']:.1f}–{v['roi_ci_hi']:.1f})",
                va="center", fontsize=8.5, color=p["ink"], bbox={"facecolor": p["surface"], "edgecolor": "none", "pad": 1})
    ax.axvline(1.0, color=p["secondary"], linewidth=1, linestyle=(0, (4, 3)), zorder=1)
    ax.text(1.0 + 0.01 * xmax, -0.55, "break-even", color=p["secondary"], fontsize=8, va="center")
    ax.set_yticks(range(len(order)), [names[c] for c in order], color=p["ink"])
    ax.set_ylim(-0.8, len(order) - 0.5)
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


def describe(run: dict, main: dict) -> str:
    """What a sensitivity run changed from the main fit, in words."""
    words = {"kpi": "KPI: {}", "demand_control": "demand control: {}", "priors_file": "priors: {}",
             "priors_scale": "priors ×{:g}", "knots": "knots: {}", "fourier": "{} seasonal wave pairs",
             "window_end": "data to {}"}
    diffs = []
    for k, w in words.items():
        v = run.get(k)
        if v != main.get(k):
            diffs.append("no demand control" if k == "demand_control" and v == "none" else w.format(v))
    return (", ".join(diffs) or "same settings").capitalize()


def main() -> None:
    import pandas as pd

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
    if a.local_dir:
        f = pathlib.Path(a.local_dir) / a.main / "optimisation_scenarios.csv"
        scen = pd.read_csv(f) if f.exists() else None
    else:
        csv = run_mmm.gcloud("storage", "cat", f"gs://{run_mmm.BUCKET}/{run_mmm.PREFIX}/runs/{a.main}/"
                             "optimisation_scenarios.csv", check=False, stderr=False)
        scen = pd.read_csv(io.StringIO(csv)) if csv.startswith("budget_multiplier") else None

    chart_roi(ch, names, sym, pal, out / "roi.png")
    chart_share(ch, names, kpi, pal, out / "share.png")
    if scen is not None:
        chart_budget(scen, names, sym, pal, out / "budget.png")

    paid = all_ch.get("pct_contribution_median", 0)
    L = [f"# {brand} marketing mix model — readout\n",
         f"*Model run `{a.main}` · Google Meridian {d.get('meridian')} · weekly, {' / '.join(cfg['geos'])}, "
         f"{data['first_week']} → {data['last_week']} ({data['weeks']} weeks)*\n",
         "## What the model says\n",
         f"- **Paid media explains about {paid:.0f}% of {kpi.replace('_', ' ')}. The other ~{100 - paid:.0f}% is "
         "the baseline**: people who already wanted the brand, seasonality and promotions.",
         f"- Across the paid channels, each {sym}1 brought back about **{sym}{all_ch.get('roi_median', 0):.2f}** in "
         f"revenue (90% range {sym}{all_ch.get('roi_ci_lo', 0):.2f}–{sym}{all_ch.get('roi_ci_hi', 0):.2f}).",
         f"- Highest return: **{names[max(names, key=lambda c: ch[c]['roi_median'])]}**. The ranges overlap, so "
         "treat the order as a guide, not a ranking.\n",
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
    for col, amount in (data.get("other_spend") or {}).items():
        L.append(f"`{col}` ({money(amount, sym)}) isn't modelled as a channel: it mostly reaches people who "
                 "already want the brand, so it is used to measure demand instead.\n")

    if scen is not None:
        oc = cfg["optimisation"] or {}
        L += [f"## {oc.get('label') or 'Budget'} scenarios\n",
              f"Last year's {oc.get('label') or 'period'} weekly pattern stands in for this year's. Each channel can "
              f"move up to ±{oc.get('spend_bound', 0):.0%} from its share.\n",
              "![Current vs optimised spend](budget.png)\n",
              "| Budget | Current mix: extra revenue | Optimised: extra revenue | Uplift |",
              "|---|---|---|---|"]
        for m, g in scen.groupby("budget_multiplier"):
            cur = g[g.allocation == "current_mix"].incremental_outcome_mean.sum()
            opt = g[g.allocation == "optimised"].incremental_outcome_mean.sum()
            spend = g[g.allocation == "current_mix"].spend.sum()
            L.append(f"| {money(spend, sym)} (×{m:.1f}) | {money(cur, sym)} | {money(opt, sym)} | {opt / cur - 1:+.1%} |")
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
