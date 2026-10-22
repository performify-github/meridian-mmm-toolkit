# <Brand> MMM: assumptions register

*Copy this file into the brand's repo and fill it in as you build. It lists every judgement
call and stand-in value in the model, so the client can go through them in one sitting (or
by email). Then you re-run once with the answers.*

**Why bother?** An MMM's numbers are only as good as its assumptions, and most assumptions
are invisible in the output. This register makes them visible, says which ones matter, and
turns the client's knowledge into better inputs.

**How to fill it in**
- **Current value**: what the model uses today.
- **Why**: one line, in plain words.
- **Where it lives**: the config key or file to change.
- **Sensitivity**: how much the headline answer moves when this changes. Fill it in from the
  sensitivity runs (`grids/sensitivity.txt`), so the conversation spends its time on the items
  that matter.

**Status key**
- **PLACEHOLDER**: a stand-in until someone confirms it.
- **CALL**: a modelling decision made on the evidence available; open to challenge.
- **CONFIRMED**: agreed with the person named.

After any change, re-run: `python3 mmm/run_mmm.py --config <brand>/mmm.yaml queue <grid>.txt`.

*Last updated <date> (<who>, <what changed>).*

---

## Questions to put to the brand

Start here: the answers that change the model most. Keep each question answerable without a
data team.

| # | Question | Why it matters | Item |
|---|---|---|---|
| B1 | What is your contribution margin on online sales (after product cost, fulfilment and payment fees)? | Published ROI benchmarks are usually *profit* ROI. They're converted to revenue at an assumed margin until you tell us | P5 |
| B2 | Roughly what share of sales in each market goes through shops or marketplaces rather than your own site? | The model sees online sales only. Ads that send people to shops count as zero | A5 |
| B3 | Do you run sales as price markdowns as well as discount codes? Is there a promotions calendar? | Markdowns are invisible to the discount measure | K1 |
| B4 | Any launches, collaborations, stock-outs, site outages or replatforms we should know about? With dates | Unexplained jumps in sales get credited to whatever ads ran that week | K5 |

## Questions to put to the agency / media team

| # | Question | Why it matters | Item |
|---|---|---|---|
| M1 | Is there spend outside the modelled channels (TV, out-of-home, influencers, TikTok, PR, retail co-op)? Can we get it weekly by market? | Missing spend gets credited to the channels that are in the model | A6 |
| M2 | Did any ad account change or start during the window? | A "£0" before an account existed is false, and it decides how far back the window can go | A2 |
| M3 | What does the budget for the planning period include (brand search? other channels?) | The scenarios are scaled to it | O2 |
| M4 | Does the channel grouping match how you move budgets? | The planner can only move money between these groups | C1 |
| M5 | Would you run a test on the channel the model is least sure about (e.g. PMax without brand terms in one market)? | A test result is the best prior there is | C2 |

---

## Register

Rows marked *(example)* show the kind of entry; replace them with the brand's.

### Data and KPI

| ID | Assumption | Current value | Why | Status | Where it lives | Sensitivity |
|---|---|---|---|---|---|---|
| A1 | What counts as an outcome | *(example)* Online orders, valued at revenue: merchandise, ex-VAT, after discounts, before refunds | Refunds lag the ads by weeks, so they aren't a media outcome | CALL | `kpis`; `sql.orders` | — |
| A1b | Second KPI | *(example)* New customers = a customer's first-ever order | Brand spend should show up here more | CALL | `--kpi new_customers` | |
| A2 | Model window | w/c <start> → last complete week (<n> weeks) | Starts when every channel has data | PLACEHOLDER (M2) | `data.window.start` | — |
| A3 | Markets | <geos, and which countries are in each> | Matches how ads and orders are both tagged | CALL | `geos`; `sql.region_to_geo` | — |
| A4 | Populations | <latest official estimates, rounded> | Only scales the markets against each other | CALL | `geos` | low |
| A5 | Sales scope | Online only; shops and marketplaces excluded | Not in the data | PLACEHOLDER (B2) | — | understates upper funnel |
| A6 | Media scope | <channels in the model; what's left out and why> | | PLACEHOLDER (M1) | `channels` | |
| A7 | Channels not modelled (e.g. email) | | | CALL | — | |
| A8 | Data fixes | <e.g. campaigns tagged to the wrong market, fixed by name> | | CALL | `sql.ads_region_overrides` | |

### Channels

| ID | Assumption | Current value | Why | Status | Where it lives | Sensitivity |
|---|---|---|---|---|---|---|
| C1 | Channel groups | <each group, what's in it, and its spend in the window> | Matches how budgets are moved. Three to six groups is about what the data supports | CALL (M4) | `channels`; `sql.channel_rules`; review list `qa_campaign_channel_map.sql` | — |
| C1a | Edge cases | <campaigns classified by hand or by name, with spend> | | CALL | as above | |
| C2 | Demand control | <series used, e.g. brand-search clicks + Google Trends brand topic; why; what the alternatives did> | Stops demand-harvesting channels taking credit for existing demand | CALL | `demand_controls`; `main_fit.demand_control` | **usually high.** Trade-off: clicks are partly caused by upper-funnel ads, so they understate it; a Trends-only run shows by how much |
| C3 | Excluded channels | <small or new channels left out, with spend> | | CALL | — | |

### Controls

| ID | Assumption | Current value | Why | Status | Where it lives | Sensitivity |
|---|---|---|---|---|---|---|
| K1 | Promotion intensity | Weekly discount depth = discounts ÷ (subtotal + discounts), per market | Markdowns aren't captured | CALL (B3) | `controls`; `sql` | |
| K2 | Sale flag | 1 if any spend that week on a campaign named <patterns> | Read from campaign names | CALL | `sql.sale_campaign_regex` | |
| K3 | Calendar | <flags used> | Peaks the ads didn't cause | CALL | `calendar` | |
| K4 | Seasonal baseline | <knots> knots + <n> yearly Fourier pairs. <why, from the holdout runs> | | CALL | `model.knots`, `model.fourier` | other knot settings in the sensitivity runs |
| K5 | One-off events | <launches, stock-outs, replatforms, with weeks> | | PLACEHOLDER (B4) | `events` | |

### Priors (`priors.csv`)

| ID | Channel | 90% ROI range (median) | Source | Status | Sensitivity |
|---|---|---|---|---|---|
| P1 | <channel> | <low–high (≈median)> | <published benchmark / platform cap / past test> | PROPOSED | priors ×0.5 / ×2 runs |
| P2 | | | | | |
| P5 | Margin used to turn profit ROI into revenue ROI | <assumed %> | Assumption | PLACEHOLDER (B1) | moves every prior proportionally |
| P6 | Carry-over and saturation | Max carry-over 8 weeks; Meridian's default shapes | Standard | CALL | — |

### Budget scenarios and Scenario Planner

| ID | Assumption | Current value | Why | Status | Where it lives |
|---|---|---|---|---|---|
| O1 | Planning-period stand-in | The weekly pattern of <last year's period> | Meridian re-plans weeks it has seen | CALL | `optimisation` |
| O2 | Budget | Historical media spend in that period × 0.8 / 1.0 / 1.2. <how this relates to the client's target> | | PLACEHOLDER (M3) | `optimisation.budget_multipliers` |
| O3 | Channel bounds | ±30% per channel | Avoids answers the data can't support | CALL | `optimisation.spend_bound` |
| S1 | Planner owner | <person who owns the sheet and the report> | Service accounts can't own Drive files | CONFIRMED (<who>) | `planner.spreadsheet_id` |
| S2 | Planner spend range | Each channel −50% to +100% | | CALL | `planner.min_shift`, `planner.max_shift` |
