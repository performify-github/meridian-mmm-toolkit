# %% [markdown]
# # Scenario Planner tables (Meridian Scenario Planner, open beta)
#
# Loads a fitted model (`model.binpb` from a fit run) and builds the tables the Scenario
# Planner's Looker Studio report reads:
# - model fit;
# - channel analysis, for the whole period and by quarter;
# - budget optimisation grids.
#
# The tables are written as CSVs to `<run>/scenario_planner/`. Putting them in a Google Sheet
# and making the Looker Studio link is a separate step (`run_mmm.py publish`), because it
# needs Sheets/Drive credentials.
#
# Adapted from Google's `demo/Meridian_Scenario_Planner_Beta.ipynb` (google-meridian v2.1.0,
# Apache License 2.0).

# %%
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
_pkg = (f"google-meridian[and-cuda,schema,scenarioplanner]=={MERIDIAN}" if HAS_GPU
        else f"google-meridian[schema,scenarioplanner]=={MERIDIAN}")
try:
    _have = importlib.metadata.version("google-meridian")
except importlib.metadata.PackageNotFoundError:
    _have = None
IN_COLAB = os.path.exists("/content") or bool(os.environ.get("COLAB_RELEASE_TAG"))
if _have != MERIDIAN:
    if LOCAL and not IN_COLAB:
        sys.exit(f"google-meridian {MERIDIAN} is needed (found {_have}): pip install -r requirements.txt")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", _pkg, "pyyaml"], check=True)
print("GPU" if HAS_GPU else "CPU", "runtime;", _pkg, "| was:", _have)

# %%
import datetime as dt
import json
import time
import uuid

import yaml
from meridian.analysis import analyzer, weekly_optimization_grid
from meridian.schema import mmm_proto_generator
from meridian.schema.processors import (
    budget_optimization_processor,
    marketing_processor,
    model_fit_processor,
)
from meridian.schema.serde import meridian_serde
from meridian.schema.utils import date_range_bucketing
from scenarioplanner import mmm_ui_proto_generator as mmm_ui_gen
from scenarioplanner.converters.dataframe import dataframe_model_converter

CFG = yaml.safe_load(CONFIG_YAML)  # noqa: F821 — injected by run_mmm.py
RUN = dict(RUN)  # noqa: F821
G = CFG["gcp"]
T0 = time.time()
if LOCAL:
    # Local: the fitted run's folder sits next to this run's folder.
    model_path = os.path.join(os.path.dirname(RUN["local_out"]), RUN["source_run_id"], "model.binpb")
    OUT_DIR = os.path.join(RUN["local_out"], "scenario_planner")
    gcs = None
else:
    from google.cloud import storage
    gcs = storage.Client(project=G["project"]).bucket(G["bucket"])
    SRC = f"{G['prefix']}/runs/{RUN['source_run_id']}"
    DST = f"{G['prefix']}/runs/{RUN['run_id']}/scenario_planner"
    OUT_DIR = "/tmp/sp"
    model_path = "/tmp/sp/model.binpb"
os.makedirs(OUT_DIR, exist_ok=True)
if gcs is not None:
    gcs.blob(f"{SRC}/model.binpb").download_to_filename(model_path)
mmm = meridian_serde.load_meridian(model_path)
print("loaded", model_path, "| times", mmm.n_times, "| channels", list(mmm.input_data.get_all_paid_channels()))

# %% [markdown]
# ## Specs: whole period + each quarter, and a spend grid per channel

# %%
min_shift, max_shift = RUN.get("min_spend_shift_ratio", 0.5), RUN.get("max_spend_shift_ratio", 1.0)
name = RUN.get("optimization_name") or CFG["planner"]["name"]
use_kpi = mmm.input_data.revenue_per_kpi is None and mmm.input_data.kpi_type != "revenue"
kpi_type = (budget_optimization_processor.common.KpiType.NON_REVENUE if use_kpi
            else budget_optimization_processor.common.KpiType.REVENUE)

constraints = [budget_optimization_processor.ChannelConstraintRel(
    channel_name=ch, spend_constraint_lower=min_shift, spend_constraint_upper=max_shift)
    for ch in mmm.input_data.get_all_paid_channels()]

t = time.time()
weekly_grid = weekly_optimization_grid.WeeklyOptimizationGrid.create(
    analyzer=analyzer.Analyzer(mmm), use_kpi=use_kpi,
    max_budget_percent_decrease=min(min_shift, 0.9), max_budget_percent_increase=max_shift,
    use_optimal_frequency=False, max_frequency=None, multiplier_step=0.01)
print(f"weekly grid {time.time() - t:.0f}s")

tc = mmm.input_data.time_coordinates
dates = list(tc.all_dates)
dates_range = dates + [dates[-1] + dt.timedelta(days=tc.interval_days)]
group_id = str(uuid.uuid4())
prefix = "-".join(name.lower().split())
summary_spec = marketing_processor.MediaSummarySpec(include_non_paid_channels=False)

marketing_specs = [marketing_processor.MarketingAnalysisSpec(
    start_date=None, end_date=None, date_interval_tag="ALL", media_summary_spec=summary_spec)]
budget_specs = [budget_optimization_processor.BudgetOptimizationSpec(
    start_date=None, end_date=None, date_interval_tag="ALL", optimization_name=f"{name} for ALL",
    grid_name=f"{prefix}_ALL", group_id=f"{group_id}:ALL", constraints=constraints,
    use_optimal_frequency=False, max_frequency=None,
    grid=weekly_grid.to_optimization_grid(spend_constraint_lower=min_shift, spend_constraint_upper=max_shift),
    kpi_type=kpi_type)]

gen = date_range_bucketing.QuarterlyDateRangeGenerator(dates_range)
for b_start, b_end in gen.generate_date_intervals():
    tag = mmm_ui_gen.create_tag(date_range_bucketing.QuarterlyDateRangeGenerator, b_start)
    marketing_specs.append(marketing_processor.MarketingAnalysisSpec(
        start_date=b_start, end_date=b_end, date_interval_tag=tag, media_summary_spec=summary_spec))
    s, e = budget_optimization_processor.model_processor.DatedSpec(
        start_date=b_start, end_date=b_end).resolver(tc).to_closed_date_interval_tuple()
    budget_specs.append(budget_optimization_processor.BudgetOptimizationSpec(
        start_date=b_start, end_date=b_end, date_interval_tag=tag,
        optimization_name=f"{name} for {tag}", grid_name=f"{prefix}_{tag.replace(' ', '_')}",
        group_id=f"{group_id}:{tag}", constraints=constraints, use_optimal_frequency=False,
        max_frequency=None, kpi_type=kpi_type,
        grid=weekly_grid.to_optimization_grid(start_date=s, end_date=e, spend_constraint_lower=min_shift,
                                              spend_constraint_upper=max_shift)))
print(len(marketing_specs), "analysis specs,", len(budget_specs), "optimisation specs")

# %% [markdown]
# ## Proto → tables → CSVs

# %%
t = time.time()
proto = mmm_proto_generator.create_mmm_proto(
    mmm=mmm, specs=[model_fit_processor.ModelFitSpec(), *marketing_specs, *budget_specs])
tables = dataframe_model_converter.DataFrameModelConverter(proto)()
print(f"proto + tables {time.time() - t:.0f}s →", {k: v.shape for k, v in tables.items()})
manifest = {"source_run_id": RUN["source_run_id"], "run_id": RUN["run_id"], "tables": {},
            "min_spend_shift_ratio": min_shift, "max_spend_shift_ratio": max_shift,
            "created_utc": dt.datetime.utcnow().isoformat(timespec="seconds")}
for tab, frame in tables.items():
    fname = f"{tab}.csv"
    frame.to_csv(os.path.join(OUT_DIR, fname), index=False)
    if gcs is not None:
        gcs.blob(f"{DST}/{fname}").upload_from_filename(os.path.join(OUT_DIR, fname))
    manifest["tables"][tab] = fname
manifest["elapsed_min"] = round((time.time() - T0) / 60, 1)
with open(os.path.join(OUT_DIR, "manifest.json"), "w") as f:
    json.dump(manifest, f, indent=1)
if gcs is not None:
    gcs.blob(f"{DST}/manifest.json").upload_from_string(json.dumps(manifest, indent=1), "application/json")
print(json.dumps(manifest, indent=1))
