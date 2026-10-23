# %% [markdown]
# # Marketing mix model: one Google Meridian 2.1 fit
#
# Built by `mmm/run_mmm.py` from this file (percent-cell format: edit this, not the built
# `.ipynb`). The runner puts the brand's config, its priors and this run's settings in the
# parameters cell straight after this one, so the executed notebook records exactly what ran.
#
# Cloud runs write everything to `gs://<bucket>/<prefix>/runs/<run_id>/`; local and plain-Colab
# runs write to a folder on disk:
# - `run_summary.json`: the headline numbers;
# - `summary_metrics.csv`, `predictive_accuracy.csv`, `response_curves.csv`;
# - `optimisation_scenarios.csv` and `scenario_draws.csv` (each split scored on every posterior draw);
# - `model_summary.html` and `optimisation_x*.html`: Meridian's own reports;
# - `model.binpb`: the fitted model.

# %%
# Install. GPU runtimes need the CUDA build of JAX. CPU runtimes must not try to start
# the CUDA plugin that ships in the Colab image.
import importlib.metadata
import os
import shutil
import subprocess
import sys

MERIDIAN = "2.1.0"
LOCAL = bool(RUN.get("local_out"))  # noqa: F821 — injected by run_mmm.py
HAS_GPU = shutil.which("nvidia-smi") is not None and subprocess.run(
    ["nvidia-smi"], capture_output=True).returncode == 0
if not HAS_GPU:
    os.environ["JAX_PLATFORMS"] = "cpu"
_pkg = f"google-meridian[and-cuda,schema]=={MERIDIAN}" if HAS_GPU else f"google-meridian[schema]=={MERIDIAN}"
try:
    _have = importlib.metadata.version("google-meridian")
except importlib.metadata.PackageNotFoundError:
    _have = None
IN_COLAB = os.path.exists("/content") or bool(os.environ.get("COLAB_RELEASE_TAG"))
if _have != MERIDIAN:
    if LOCAL and not IN_COLAB:
        sys.exit(f"google-meridian {MERIDIAN} is needed (found {_have}): pip install -r requirements.txt")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", _pkg, "db-dtypes", "pyyaml"], check=True)
print("GPU" if HAS_GPU else "CPU", "runtime;", _pkg, "| was:", _have)

# %%
import datetime as dt
import io
import json
import time
import warnings

import jax
import meridian
import numpy as np
import pandas as pd
import yaml
from dateutil.easter import easter
from meridian import backend, constants
from meridian.analysis import analyzer, optimizer, summarizer, tensors
from meridian.analysis.review import reviewer
from meridian.data import data_frame_input_data_builder as dfb
from meridian.model import model, prior_distribution, spec
from meridian.schema.serde import meridian_serde

warnings.filterwarnings("ignore")
CFG = yaml.safe_load(CONFIG_YAML)  # noqa: F821 — injected by run_mmm.py
PRIORS = pd.read_csv(io.StringIO(PRIORS_CSV))  # noqa: F821
EXPERIMENTS = pd.read_csv(io.StringIO(EXPERIMENTS_CSV)) if EXPERIMENTS_CSV.strip() else None  # noqa: F821
RUN = dict(RUN)  # noqa: F821
RUN_ID = RUN["run_id"]
G, D, COLS = CFG["gcp"], CFG["data"], CFG["columns"]
OUT = RUN.get("local_out") or f"/tmp/mmm_out/{RUN_ID}"
os.makedirs(OUT, exist_ok=True)
SUMMARY = {"run_id": RUN_ID, "brand": CFG["brand"]["id"], "run": RUN, "meridian": meridian.__version__,
           "jax": jax.__version__, "devices": [str(d) for d in jax.devices()],
           "started_utc": dt.datetime.utcnow().isoformat(timespec="seconds")}
T0 = time.time()
print(json.dumps(SUMMARY, indent=1))

if LOCAL:
    gcs = None
else:
    from google.cloud import storage
    gcs = storage.Client(project=G["project"]).bucket(G["bucket"])
RUN_PREFIX = f"{G['prefix']}/runs/{RUN_ID}"


def upload(local_name, content_type=None):
    """Copy an output to the run's GCS folder. Local runs keep it on disk only."""
    if gcs is None:
        return
    blob = gcs.blob(f"{RUN_PREFIX}/{local_name}")
    blob.upload_from_filename(os.path.join(OUT, local_name), content_type=content_type)


def write_summary():
    SUMMARY["elapsed_min"] = round((time.time() - T0) / 60, 1)
    with open(os.path.join(OUT, "run_summary.json"), "w") as f:
        json.dump(SUMMARY, f, indent=1, default=str)
    upload("run_summary.json", "application/json")

# %% [markdown]
# ## 1. Data, with guardrails
#
# One row per geo per week. The run stops here, before any sampling, if the data is
# incomplete or if spend is going missing.

# %%
WEEK, GEO = COLS["week"], COLS["geo"]
end = RUN.get("window_end") or D["window"]["end"]
start = D["window"]["start"]
if D["source"] == "bigquery":
    from google.cloud import bigquery
    bq = bigquery.Client(project=G["project"], location=G["bq_location"])
    sql = f"""SELECT * FROM `{D['table']}`
              WHERE 1 = 1
              {f"AND {WEEK} >= '{start}'" if start else ''}
              {f"AND {WEEK} <= '{end}'" if end else ''}
              ORDER BY {WEEK}, {GEO}"""
    # Plain REST download: the frame is small, and the Storage Read API needs an extra
    # role (bigquery.readSessionUser) that a runner identity rarely has.
    df = bq.query(sql).to_dataframe(create_bqstorage_client=False)
else:
    src = D["csv"]
    if src.startswith("gs://"):
        from google.cloud import storage as _st
        _bucket, _path = src[5:].split("/", 1)
        src = io.BytesIO(_st.Client(project=G["project"]).bucket(_bucket).blob(_path).download_as_bytes())
    df = pd.read_csv(src)
df = df.rename(columns={WEEK: "week_start", GEO: "geo"})
df["week_start"] = pd.to_datetime(df["week_start"])
if start:
    df = df[df["week_start"] >= pd.Timestamp(start)]
if end:
    df = df[df["week_start"] <= pd.Timestamp(end)]
df = df.sort_values(["week_start", "geo"]).reset_index(drop=True)

channels = list(CFG["channels"])
spend_cols = [COLS["spend"].format(channel=c) for c in channels]
media_cols = [COLS["media"].format(channel=c) for c in channels]
GR = CFG["guardrails"]
needed = [*spend_cols, *media_cols, *GR["other_spend_cols"],
          *{v["kpi_col"] for v in CFG["kpis"].values()},
          *{v["revenue_col"] for v in CFG["kpis"].values() if v.get("revenue_col")}]
missing = [c for c in needed if c not in df.columns]
assert not missing, f"input is missing columns: {missing}"

n_weeks = df["week_start"].nunique()
geos = list(CFG["geos"])
assert set(df["geo"]) == set(geos), f"geo drift: data has {sorted(set(df['geo']))}, config has {geos}"
assert len(df) == n_weeks * len(geos), "incomplete geo × week grid"
assert (df[spend_cols] >= 0).all().all(), "negative spend"

all_spend = df[[*spend_cols, *GR["other_spend_cols"]]].sum().sum()
unmapped_share = None
if GR["unmapped_spend_col"]:
    assert GR["unmapped_spend_col"] in df, f"guardrail column {GR['unmapped_spend_col']} missing"
    all_spend += df[GR["unmapped_spend_col"]].sum()
    unmapped_share = float(df[GR["unmapped_spend_col"]].sum() / all_spend)
    assert unmapped_share <= GR["max_unmapped_spend_share"], f"unmapped spend {unmapped_share:.2%}"
if GR["unmapped_geo_spend_col"]:
    assert GR["unmapped_geo_spend_col"] in df, f"guardrail column {GR['unmapped_geo_spend_col']} missing"
    unmapped_geo = df.groupby("week_start")[GR["unmapped_geo_spend_col"]].max().sum()
    assert unmapped_geo == 0, f"{unmapped_geo:,.0f} of spend has no geo"
for c, col in zip(channels, spend_cols):
    assert df[col].sum() > 0, f"channel {c} has no spend in the window"

SUMMARY["data"] = {
    "weeks": int(n_weeks), "first_week": str(df["week_start"].min().date()),
    "last_week": str(df["week_start"].max().date()),
    "kpi_totals": {k: round(float(df[v["kpi_col"]].sum())) for k, v in CFG["kpis"].items()},
    "revenue_totals": {k: round(float(df[v["revenue_col"]].sum())) for k, v in CFG["kpis"].items()
                       if v.get("revenue_col")},
    "media_spend": {c: round(float(df[col].sum())) for c, col in zip(channels, spend_cols)},
    "other_spend": {col: round(float(df[col].sum())) for col in GR["other_spend_cols"]},
    "unmapped_spend_share": unmapped_share,
    "currency": CFG["currency"]["code"],
}
print(json.dumps(SUMMARY["data"], indent=1))
df.groupby("geo")[[*spend_cols, *GR["other_spend_cols"]]].sum().round()

# %% [markdown]
# ## 2. Controls and Meridian columns
#
# Controls are things that move sales but aren't caused by the ads: how much people already
# want the brand, promotions, and the calendar.

# %%
def calendar_flags(week_start: pd.Timestamp) -> dict:
    """Weekly calendar flags, identical across geos."""
    wk_end = week_start + pd.Timedelta(days=6)
    y = week_start.year if week_start.month > 1 or week_start.day > 7 else week_start.year - 1
    flags = {}
    # Black Friday = the day after the fourth Thursday of November
    nov1 = pd.Timestamp(y, 11, 1)
    bf = nov1 + pd.Timedelta(days=(3 - nov1.weekday()) % 7 + 21 + 1)
    bf_week = bf - pd.Timedelta(days=(bf - week_start).days % 7)
    flags["bfcm"] = int(week_start in (bf_week, bf_week + pd.Timedelta(days=7)))
    xmas = pd.Timestamp(y, 12, 25)
    xmas_week = xmas - pd.Timedelta(days=(xmas - week_start).days % 7)
    flags["xmas_week"] = int(week_start == xmas_week)
    flags["xmas_runin"] = int(xmas_week - pd.Timedelta(days=21) <= week_start < xmas_week)
    ny = pd.Timestamp(y + 1, 1, 1)
    flags["newyear_week"] = int(week_start <= ny <= wk_end and week_start != xmas_week)
    e = pd.Timestamp(easter(week_start.year))
    flags["easter_week"] = int(week_start <= e <= wk_end)
    return flags


cal = pd.DataFrame([{"week_start": w, **{k: v for k, v in calendar_flags(w).items() if k in CFG["calendar"]}}
                    for w in sorted(df["week_start"].unique())])
for name, weeks in (CFG["events"] or {}).items():
    cal[name] = cal["week_start"].isin(pd.to_datetime(weeks)).astype(int)
df = df.merge(cal, on="week_start", how="left")
for col in CFG["fill_zero"]:
    if col in df:
        df[col] = df[col].fillna(0.0)

kpi = CFG["kpis"][RUN["kpi"]]
KPI_TYPE = kpi.get("type", "non_revenue")
df["time"] = df["week_start"].dt.strftime("%Y-%m-%d")
df["kpi"] = df[kpi["kpi_col"]].astype(float)
if KPI_TYPE == "non_revenue":
    # Value of one KPI unit (e.g. order value) per geo-week; weeks with none take the geo's average.
    aov = df[kpi["revenue_col"]] / df[kpi["kpi_col"]].replace(0, np.nan)
    df["revenue_per_kpi"] = aov.fillna(df.groupby("geo")[kpi["revenue_col"]].transform("sum")
                                       / df.groupby("geo")[kpi["kpi_col"]].transform("sum"))
df["population"] = df["geo"].map(CFG["geos"]).astype(float)

# Demand control: which series stand in for how much people already want the brand.
DC = CFG["demand_controls"]
dc = RUN.get("demand_control") or "none"
demand_cols = [] if dc == "none" else [DC["series"][s] for s in dc.split("+")]
controls = [*demand_cols, *CFG["controls"], *CFG["calendar"], *(CFG["events"] or {})]
# Optional smooth yearly seasonality: K sine/cosine pairs on the day of year, so the
# seasonal peak sits in the baseline rather than in the media that peaks with it.
K = int(RUN.get("fourier", 0))
doy = (df["week_start"] + pd.Timedelta(days=3)).dt.dayofyear / 365.25
for k in range(1, K + 1):
    df[f"season_sin{k}"] = np.sin(2 * np.pi * k * doy)
    df[f"season_cos{k}"] = np.cos(2 * np.pi * k * doy)
    controls += [f"season_sin{k}", f"season_cos{k}"]
missing = [c for c in controls if c not in df]
assert not missing, f"control columns missing from the input: {missing}"
gaps = [c for c in controls if df[c].isna().any()]
assert not gaps, f"controls with gaps (add them to fill_zero if a gap means zero): {gaps}"
SUMMARY["controls"] = controls
print("KPI:", RUN["kpi"], "| demand control:", dc, "| controls:", controls)
cal.sum(numeric_only=True)

# %% [markdown]
# ## 3. Input data, priors, model spec

# %%
builder = (dfb.DataFrameInputDataBuilder(kpi_type=KPI_TYPE)
           .with_kpi(df)
           .with_population(df)
           .with_controls(df, control_cols=controls)
           .with_media(df, media_cols=media_cols, media_spend_cols=spend_cols, media_channels=channels))
if KPI_TYPE == "non_revenue":
    builder = builder.with_revenue_per_kpi(df)
data = builder.build()

# ROI priors: a LogNormal per channel, fitted to the 90% (or `mass`) range in the priors file.
pri = PRIORS.set_index("channel").loc[channels]
scale = float(RUN.get("priors_scale", 1.0))   # sensitivity: shifts the whole range
_ln = prior_distribution.lognormal_dist_from_range(
    (pri["roi_low"] * scale).tolist(), (pri["roi_high"] * scale).tolist(),
    mass_percent=float(pri["mass"].iloc[0]))
roi_prior = backend.tfd.LogNormal(_ln.loc, _ln.scale, name=constants.ROI_M)
SUMMARY["priors"] = {c: {"low": float(r.roi_low * scale), "high": float(r.roi_high * scale),
                         "status": r.status} for c, r in pri.iterrows()}

# Lift tests (config `experiments`): each result replaces that channel's benchmark prior with
# one built from the test. Meridian adjusts the test for its spend level, length and age, so an
# eight-week test can inform the return over the whole window. The other channels keep their
# benchmark priors.
#
# Meridian 2.1's `CalibrationBuilder.build()` returns a per-channel mixed-family prior, which fails
# inside the JAX sampler and has no save support. So the builder's registry turns each test into a
# prior (with Meridian's spend, recency and duration adjustments), and that prior is matched to a
# LogNormal with the same mean and standard deviation, the same shape as the benchmark priors.
if EXPERIMENTS is not None and len(EXPERIMENTS):
    from meridian.model.calibration import base as calibration_base
    from meridian.model.calibration import prior_builder
    _reg = calibration_base.CalibrationRegistry()
    _cb = prior_builder.CalibrationBuilder(data, max_lag=CFG["model"]["max_lag"], registry=_reg)
    for c, g in EXPERIMENTS.groupby("channel"):
        _cb.with_incrementality_experiment_result(
            c, point_estimates=g["estimate"].tolist(), standard_errors=g["standard_error"].tolist(),
            experiment_kpi_types=g["kpi_type"].tolist(), experiment_total_spends=g["spend"].tolist(),
            experiment_start_dates=g["start"].astype(str).tolist(), experiment_end_dates=g["end"].astype(str).tolist())
    _tc = data.time_coordinates
    _cal = _reg.get_roi_distributions_by_channel(
        _tc.all_dates[-1], max_lag=CFG["model"]["max_lag"], interval_days=_tc.interval_days,
        model_duration_days=(_tc.all_dates[-1] - _tc.all_dates[0]).days + _tc.interval_days)
    _loc = np.array(np.broadcast_to(np.asarray(_ln.loc), (len(channels),)))     # keeps the backend's dtype
    _sd = np.array(np.broadcast_to(np.asarray(_ln.scale), (len(channels),)))
    SUMMARY["calibration"] = []
    for c, o in _cal.outputs.items():
        d = _cal.distributions[c]
        mean, sd = float(d.mean()), float(d.stddev())
        i = channels.index(c)
        _sd[i] = np.sqrt(np.log1p((sd / mean) ** 2))
        _loc[i] = np.log(mean) - _sd[i] ** 2 / 2
        SUMMARY["priors"][c].update(calibrated=True, calibrated_family=type(d).__name__,
                                    calibrated_mean=mean, calibrated_sd=sd)
        for x in o.experiments:
            SUMMARY["calibration"].append({
                "channel": c, "estimate": x.raw_experiment_result.point_estimate,
                "standard_error": x.raw_experiment_result.standard_error,
                "adjusted_estimate": float(x.adjusted_experiment_result.point_estimate),
                "adjusted_standard_error": float(x.adjusted_experiment_result.standard_error)})
    roi_prior = backend.tfd.LogNormal(_loc, _sd, name=constants.ROI_M)
    print("calibrated from lift tests:", json.dumps(SUMMARY["calibration"], indent=1))

# Each prior's exact 90% range on ROI, for the readout to set beside the posterior's.
_pl = np.broadcast_to(np.asarray(roi_prior.loc, float), (len(channels),))
_ps = np.broadcast_to(np.asarray(roi_prior.scale, float), (len(channels),))
for i, c in enumerate(channels):
    SUMMARY["priors"][c]["range_90"] = [float(np.exp(_pl[i] - 1.6449 * _ps[i])), float(np.exp(_pl[i] + 1.6449 * _ps[i]))]

# Holdouts (config `holdouts`): weeks hidden from the fit, to test prediction.
holdout, hname = None, RUN.get("holdout")
if hname:
    h = CFG["holdouts"][hname]
    ranges = ([] if h["type"] == "random" else
              [spec.DateRange(str(s), str(e)) for s, e in (h.get("ranges") or [[h.get("start"), h.get("end")]])])
    if h["type"] == "random":
        holdout = spec.HoldoutSpec(spec=spec.RandomHoldoutSpec(
            ratio=h["ratio"], seed=h.get("seed", CFG["sampling"]["seed"])))
    elif h["type"] == "dates":
        holdout = spec.HoldoutSpec(spec=ranges)
    elif h["type"] == "geo_dates":
        # One market only: the other geos keep anchoring the shared baseline, so the held-out
        # weeks test media + controls rather than an unanchored baseline.
        holdout = spec.HoldoutSpec(spec=[spec.GeoHoldoutSpec(geos=h["geos"], date_ranges=ranges)])

knots = RUN.get("knots", CFG["model"]["knots"])
if knots == "monthly":
    knots = int(round(n_weeks / (52 / 12)))
elif knots == "quarterly":
    knots = int(round(n_weeks / 13))
spec_kwargs = dict(prior=prior_distribution.PriorDistribution(roi_m=roi_prior),
                   max_lag=CFG["model"]["max_lag"], holdout=holdout)
if knots == "aks":
    spec_kwargs["enable_aks"] = True
else:
    spec_kwargs["knots"] = int(knots)
mmm = model.Meridian(input_data=data, model_spec=spec.ModelSpec(**spec_kwargs))
SUMMARY["model"] = {"knots": knots, "holdout": hname, "n_geos": mmm.n_geos,
                    "n_times": mmm.n_times, "n_media_channels": mmm.n_media_channels}
print(SUMMARY["model"])

# %% [markdown]
# ## 4. Sampling

# %%
sp = CFG["sampling"][RUN["sampling"]]
t = time.time()
mmm.sample_prior(CFG["sampling"]["prior_draws"], seed=CFG["sampling"]["seed"])
SUMMARY["prior_sampling_min"] = round((time.time() - t) / 60, 2)
t = time.time()
mmm.sample_posterior(n_chains=sp["n_chains"], n_adapt=sp["n_adapt"], n_burnin=sp["n_burnin"],
                     n_keep=sp["n_keep"], seed=CFG["sampling"]["seed"])
SUMMARY["posterior_sampling_min"] = round((time.time() - t) / 60, 2)
print(f"prior {SUMMARY['prior_sampling_min']} min · posterior {SUMMARY['posterior_sampling_min']} min")
write_summary()

# %% [markdown]
# ## 5. Model review: convergence, fit, plausibility

# %%
an = analyzer.Analyzer(mmm)
rhat = an.rhat_summary()
SUMMARY["rhat_max"] = float(rhat["max_r_hat"].max()) if "max_r_hat" in rhat else None
SUMMARY["rhat_table"] = rhat.round(3).to_dict(orient="records")

review = reviewer.ModelReviewer(mmm).run()
SUMMARY["review"] = {"status": str(review.overall_status), "message": review.summary_message,
                     "health_score": review.health_score,
                     "checks": [str(r) for r in review.results]}
print(SUMMARY["review"]["status"], "|", review.summary_message, "| health", review.health_score)

acc = an.predictive_accuracy().to_dataframe().reset_index()
acc.to_csv(os.path.join(OUT, "predictive_accuracy.csv"), index=False)
upload("predictive_accuracy.csv")
SUMMARY["predictive_accuracy"] = acc.round(4).to_dict(orient="records")
write_summary()
acc

# %% [markdown]
# ## 6. Channel results: ROI, marginal ROI, contribution (90% credible intervals)

# %%
sm = an.summary_metrics(confidence_level=0.9)
smdf = sm.to_dataframe().reset_index()
smdf.to_csv(os.path.join(OUT, "summary_metrics.csv"), index=False)
upload("summary_metrics.csv")


def _pick(var, ch, metric, dist="posterior"):
    try:
        return float(sm[var].sel(channel=ch, metric=metric, distribution=dist).values)
    except Exception:  # noqa: BLE001 — metric absent for this channel/version
        return None


SUMMARY["channels"] = {
    ch: {
        "spend": float(sm["spend"].sel(channel=ch).values) if "spend" in sm else None,
        "roi_median": _pick("roi", ch, "median"), "roi_ci_lo": _pick("roi", ch, "ci_lo"),
        "roi_ci_hi": _pick("roi", ch, "ci_hi"),
        "prior_roi_median": _pick("roi", ch, "median", "prior"),
        "prior_roi_ci_lo": _pick("roi", ch, "ci_lo", "prior"),
        "prior_roi_ci_hi": _pick("roi", ch, "ci_hi", "prior"),
        "mroi_median": _pick("mroi", ch, "median"),
        "pct_contribution_median": _pick("pct_of_contribution", ch, "median"),
        "incremental_outcome_median": _pick("incremental_outcome", ch, "median"),
    }
    for ch in [*channels, constants.ALL_CHANNELS]
}
write_summary()
pd.DataFrame(SUMMARY["channels"]).T.round(3)

# %% [markdown]
# ## 7. Response curves
#
# Revenue against spend for each channel over the whole window, with the historical weekly
# pattern scaled from zero to twice its spend. The line from zero to today's point is the
# average return (ROI); the slope at today's point is the return on the next unit (mROI).

# %%
if RUN.get("report", True):
    rc = an.response_curves(spend_multipliers=[round(float(m), 2) for m in np.arange(0, 2.001, 0.05)],
                            confidence_level=0.9)
    rcdf = (rc["incremental_outcome"].to_dataframe().reset_index()
            .pivot_table(index=["channel", "spend_multiplier"], columns="metric", values="incremental_outcome")
            .add_prefix("incremental_outcome_").reset_index()
            .merge(rc["spend"].to_dataframe().reset_index(), on=["channel", "spend_multiplier"]))
    rcdf.to_csv(os.path.join(OUT, "response_curves.csv"), index=False)
    upload("response_curves.csv")
    display(rcdf[rcdf.spend_multiplier.isin([0.5, 1.0, 1.5])].set_index(["channel", "spend_multiplier"]).round())  # noqa: F821

# %% [markdown]
# ## 8. Meridian's standard report

# %%
if RUN.get("report", True):
    summarizer.Summarizer(mmm).output_model_results_summary(filename="model_summary.html", filepath=OUT)
    upload("model_summary.html", "text/html")

# %% [markdown]
# ## 9. Budget scenarios
#
# Meridian re-plans weeks it has seen, so last year's peak flighting stands in for this
# year's (config `optimisation`).
#
# The optimiser reports each split's mean. To say how likely the new split is to beat the
# current one, both are scored again on every posterior draw, the way the optimiser scores
# them: each channel's media scaled by its spend ratio, the outcome counted over the period.

# %%
def per_draw(ds, hist_spend, period):
    """Total incremental revenue of one allocation, on every posterior draw."""
    ratio = np.divide(ds["spend"].sel(channel=channels).values, hist_spend,
                      out=np.zeros(len(channels)), where=hist_spend > 0)
    media = mmm.media_tensors.media * backend.to_tensor(ratio, dtype=backend.float_dtype)
    inc = an.incremental_outcome(new_data=tensors.DataTensors(media=media), selected_times=period,
                                 include_non_paid_channels=False)
    return np.asarray(inc).sum(axis=-1).ravel()


def _q(x, qs=(0.1, 0.5, 0.9)):
    return [float(v) for v in np.quantile(x, qs)]


oc = CFG.get("optimisation")
if RUN.get("optimise", True) and oc:
    bo = optimizer.BudgetOptimizer(mmm)
    hist = bo.optimize(start_date=oc["start"], end_date=oc["end"], fixed_budget=True,
                       spend_constraint_lower=oc["spend_bound"], spend_constraint_upper=oc["spend_bound"])
    base_budget = float(hist.nonoptimized_data["spend"].sum().values)
    hist_spend = hist.nonoptimized_data["spend"].sel(channel=channels).values
    period = [t for t in mmm.input_data.time.values if oc["start"] <= str(t) <= oc["end"]]
    scen, draws, stats = [], [], []
    for mult in oc["budget_multipliers"]:
        res = hist if mult == 1.0 else bo.optimize(
            start_date=oc["start"], end_date=oc["end"], fixed_budget=True,
            budget=base_budget * mult,
            spend_constraint_lower=oc["spend_bound"], spend_constraint_upper=oc["spend_bound"])
        name = f"optimisation_x{mult:.1f}"
        res.output_optimization_summary(filename=f"{name}.html", filepath=OUT)
        upload(f"{name}.html", "text/html")
        for label, ds in (("current_mix", res.nonoptimized_data), ("optimised", res.optimized_data)):
            for ch in channels:
                scen.append({"budget_multiplier": mult, "allocation": label, "channel": ch,
                             "spend": float(ds["spend"].sel(channel=ch).values),
                             "incremental_outcome_mean": float(
                                 ds["incremental_outcome"].sel(channel=ch, metric="mean").values),
                             "roi_mean": float(ds["roi"].sel(channel=ch, metric="mean").values)})
        cur, opt = per_draw(res.nonoptimized_data, hist_spend, period), per_draw(res.optimized_data, hist_spend, period)
        draws.append(pd.DataFrame({"budget_multiplier": mult, "draw": range(len(cur)),
                                   "current_mix": cur, "optimised": opt}))
        reported = float(res.nonoptimized_data["incremental_outcome"].sel(metric="mean").sum().values)
        stats.append({"budget_multiplier": mult, "budget": round(base_budget * mult),
                      "chance_optimised_beats_current": float((opt > cur).mean()),
                      "uplift_p10_p50_p90": _q(opt - cur), "uplift_pct_p10_p50_p90": _q(opt / cur - 1),
                      "current_p10_p50_p90": _q(cur), "optimised_p10_p50_p90": _q(opt),
                      # the draws re-score what the optimiser scored; a gap here means the two disagree
                      "current_mean_vs_optimiser": float(cur.mean() / reported - 1)})
    scen = pd.DataFrame(scen)
    scen.to_csv(os.path.join(OUT, "optimisation_scenarios.csv"), index=False)
    upload("optimisation_scenarios.csv")
    draws = pd.concat(draws)
    draws.to_csv(os.path.join(OUT, "scenario_draws.csv"), index=False)
    upload("scenario_draws.csv")
    # How much to spend, not where: what each step in budget brings back per unit, on every draw.
    opt1 = draws[draws.budget_multiplier == 1.0].optimised.to_numpy()
    for s in stats:
        m = s["budget_multiplier"]
        if m != 1.0:
            optm = draws[draws.budget_multiplier == m].optimised.to_numpy()
            s["return_on_budget_change_p10_p50_p90"] = _q((optm - opt1) / ((m - 1) * base_budget))
    SUMMARY["optimisation"] = {"label": oc.get("label"), "period": [oc["start"], oc["end"]],
                               "historical_media_budget": round(base_budget),
                               "scenarios": scen.round(2).to_dict(orient="records"), "draws": stats}
    write_summary()
    display(pd.DataFrame(stats).set_index("budget_multiplier").round(3))  # noqa: F821

# %% [markdown]
# ## 10. Save the fitted model

# %%
if RUN.get("save_model", True):
    meridian_serde.save_meridian(mmm, os.path.join(OUT, "model.binpb"))
    upload("model.binpb", "application/octet-stream")
SUMMARY["finished_utc"] = dt.datetime.utcnow().isoformat(timespec="seconds")
write_summary()
print(f"done in {SUMMARY['elapsed_min']} min →", OUT if LOCAL else f"gs://{G['bucket']}/{RUN_PREFIX}/")
