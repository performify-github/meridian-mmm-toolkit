# Example Co marketing mix model — readout

*Model run `example-main` · Google Meridian 2.1.0 · weekly, UK / DE / FR / NORDICS, 2023-01-02 → 2025-12-22 (156 weeks)*

## What the model says

- **Paid media explains about 21% of orders. The other ~79% is the baseline**: people who already wanted the brand, seasonality and promotions.
- Across the paid channels, each £1 brought back about **£1.28** in revenue (90% range £0.97–£1.69).
- At a 50% margin, a channel needs more than **£2.00** per £1 to pay for itself in profit (the dotted line in the chart).
- Highest return: **Google PMax / Shopping / non-brand search**. Its range overlaps at least one other channel's, so treat the order as a guide, not a ranking.

![Return on spend by channel](roi.png)

![Share explained by each driver](share.png)

## Channel table

| Channel | Spend | Return per £1 (median) | 90% range | Return on the next £1 | Share explained |
|---|---|---|---|---|---|
| Google PMax / Shopping / non-brand search | £476,711 | £2.29 | £1.40–£3.71 | £1.22 | 8.0% |
| Google YouTube / Demand Gen | £499,415 | £0.49 | £0.22–£0.95 | £0.23 | 1.8% |
| Meta conversion | £671,730 | £1.85 | £1.25–£2.85 | £0.96 | 9.1% |
| Meta reach / awareness | £615,735 | £0.38 | £0.17–£0.71 | £0.17 | 1.7% |

*Return on the next £1* is the budget number: move money from channels where it is low to channels where it is high.

![Revenue against spend, by channel](curves.png)

Each curve is what the channel brought in at different levels of spend over the window, with the same weekly pattern. The dashed line from zero to today's spend is the average return; the orange line is the slope at today's spend, the return on the next £1. Where the orange line is much flatter than the dashed one, the channel is deep into diminishing returns.

`brand_search_spend` (£23,892) is counted but not modelled as a channel. Brand search mostly reaches people who already want the brand, so its clicks are used to measure demand instead.

## What the data added to the priors

![Prior and posterior ranges](priors.png)

Each pale bar is the range the model started from (the prior: benchmarks); the blue line is where it ended up after the data. Where the blue range is barely narrower than the pale one, the data had little to say about that channel: its number is mostly the starting assumption, and it belongs on the test list. Where the blue range sits outside the pale one, the data pulled hard against the prior: either it's telling you something, or existing demand is leaking into the channel. Find out which before using it.

| Channel | Prior 90% range | Posterior 90% range | Narrower by |
|---|---|---|---|
| Google PMax / Shopping / non-brand search | £0.50–£4.00 | £1.40–£3.71 | 53% |
| Google YouTube / Demand Gen | £0.30–£3.00 | £0.22–£0.95 | 36% |
| Meta conversion | £0.50–£3.50 | £1.25–£2.85 | 58% |
| Meta reach / awareness | £0.30–£3.00 | £0.17–£0.71 | 37% |

*Narrower by* compares the ranges in proportions, so £1–£2 counts the same as £2–£4.

## Nov–Dec scenarios

Last year's Nov–Dec weekly pattern stands in for this year's. Each channel can move up to ±30% from its share.

![Current vs optimised spend](budget.png)

| Budget | Current mix: extra revenue | Optimised: extra revenue | Uplift | Uplift, middle 80% of draws | Chance the new split wins |
|---|---|---|---|---|---|
| £167,400 (×0.8) | £173,939 | £187,044 | +7.5% | +5.0% to +10.2% | 100% |
| £209,200 (×1.0) | £193,013 | £205,699 | +6.6% | +4.2% to +9.1% | 100% |
| £251,000 (×1.2) | £208,504 | £220,689 | +5.8% | +3.5% to +8.3% | 100% |

The uplift is the optimiser's best estimate. Scored on every draw from the model, the new split beats the current one in **100%** of them. The rule here: move budget when that is at least 80%; below that, test the change first. On that rule this re-split can go ahead. In nine draws out of ten it changes revenue by +4.2% or better.

**How much to spend, not just where.** Starting from the optimised split at today's budget:
- cutting 20%: each £1 cut loses about **£0.44** (middle 80% of draws: £0.35–£0.55)
- adding 20%: each extra £1 brings back about **£0.35** (middle 80% of draws: £0.27–£0.45)

At a 50% margin an extra £1 pays for itself in profit only if it brings back more than £2.00.

A small uplift means the current split is already close to efficient; at peak spend the bigger lever is often the budget level, not the split.

## How far to trust it

Each check hides some weeks from the model, then asks it to predict them.

| Check | Fit on hidden weeks (R², higher is better) | Error on hidden weeks (wMAPE, lower is better) | Converged (R-hat < 1.1) | Meridian review |
|---|---|---|---|---|
| Random 10% of weeks | 0.99 | 0.04 | 1.03 | PASS |
| Q4 2024 held out, UK only | 0.93 | 0.06 | 1.02 | PASS |
| Q4 2024 held out, all markets | 0.42 | 0.18 | 2.12 | FAIL |

## What moves the answer

Each run changes one setting. Channels whose return holds steady are robust; the ones that move lean on that setting.

| Change | Google PMax / Shopping / non-brand search | Google YouTube / Demand Gen | Meta conversion | Meta reach / awareness | Paid share |
|---|---|---|---|---|---|
| **Main fit** | £2.29 | £0.49 | £1.85 | £0.38 | 21% |
| Demand control: trends | £2.43 | £0.85 | £1.93 | £0.60 | 25% |
| No demand control | £3.30 | £0.59 | £2.62 | £0.78 | 31% |
| Lift tests: experiments.csv | £2.18 | £0.49 | £1.83 | £0.38 | 21% |

## Caveats

- **Online sales only.** Retail and marketplace sales aren't in the data, so brand activity that sends people to shops is under-counted.
- **Upper-funnel returns are uncertain.** Video and reach ran in a few flights, mostly at seasonal peaks, so their ranges lean on the priors.
- **Channels outside the model** (email, TV, influencers) may have their effect credited to the paid media that ran at the same time.
- Every judgement call and open question is in the assumptions register.
