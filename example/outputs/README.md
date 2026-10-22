# Example outputs (synthetic brand)

Real outputs from the toolkit, run on the fictional **Example Co**: `example/data/synthetic.csv`,
three years of weekly data in four markets. Because the data is made up, we know the true return
of every channel, so you can see how close the model gets, and where it goes wrong.

| File | What it is |
|---|---|
| [`model_summary.html`](model_summary.html) | Meridian's own report for the main fit: model fit, contribution, ROI, response curves |
| [`optimisation_x1.0.html`](optimisation_x1.0.html) | Meridian's budget report: the same Nov–Dec budget, re-split within ±30% per channel |
| [`readout/readout.md`](readout/readout.md) | The toolkit's plain-English readout, with its charts |

GitHub shows the HTML files as code. Download them and open them in a browser to see the reports.

All runs used `--sampling standard` on a laptop (about 5 minutes each). Reproduce them with:

```bash
python3 mmm/synthetic.py --config example/mmm.yaml
python3 mmm/run_mmm.py --config example/mmm.yaml local --sampling standard --run-id example-main
```

## What the example teaches

The fake brand is built with the traps a real one has: its conversion channels spend more when
demand is already high, and its brand-search clicks are partly *caused* by its video and reach ads.

| Channel | True return per £1 | No demand control | Brand clicks + Google Trends (main fit) | Google Trends only |
|---|---|---|---|---|
| Google PMax / Shopping | 1.80 | 3.30 (2.20–4.78) ✗ | 2.29 (1.40–3.71) ✓ | 2.43 (1.50–3.77) ✓ |
| Meta conversion | 1.30 | 2.62 (1.90–3.62) ✗ | 1.85 (1.25–2.85) ✓ | 1.93 (1.29–2.97) ✓ |
| Google YouTube / Demand Gen | 1.10 | 0.59 (0.27–1.12) ✓ | 0.49 (0.22–0.95) ✗ | 0.85 (0.38–1.43) ✓ |
| Meta reach / awareness | 0.70 | 0.78 (0.34–1.38) ✓ | 0.38 (0.17–0.71) ✓ | 0.60 (0.27–1.05) ✓ |

*Median and 90% range. ✓ = the true value is inside the range.*

1. **Without a demand control, the conversion channels take credit for existing demand.** Both
   come out at about double their true return, with ranges that miss the truth. Nothing in the
   model's own health checks flags it: every run here converged and passed review.
2. **With a demand control, they land close to the truth.**
3. **Brand-search clicks understate the upper funnel**, because video and reach ads cause some of
   those clicks. Controlling for them takes some of the ads' credit away. A Google Trends-only run
   shows how much, and here it recovers every channel. On real data you don't know the truth,
   so run both and report the range.

**How far to trust it** (the three holdouts, from the readout):

| Check | Fit on hidden weeks (R²) | Converged |
|---|---|---|
| A random 10% of weeks | 0.99 | ✓ |
| Q4 2024 hidden, one market | 0.93 | ✓ |
| Q4 2024 hidden, all markets | 0.42 | ✗ (R-hat 2.1) |

The last one is the hardest test: every point where the baseline can bend in those weeks has no
data in any market. It often fails, which is why the one-market version exists. Don't present a
run that hasn't converged; read it as "the peak can't be predicted from the other weeks alone".
