# %% [markdown]
# # MMM cloud orchestrator
#
# Runs the whole refresh inside Colab Enterprise, as the brand's service account, with
# nothing local:
# 1. Pulls the code and the brand's config from `gs://<bucket>/<prefix>/code/`
#    (`run_mmm.py sync` puts them there; `VERSION.json` records the git commits).
# 2. Runs a grid of fits, `max_parallel` at a time, and writes `comparison.json`.
# 3. Builds the Scenario Planner tables from the grid's main run.
# 4. Updates the planner Google Sheet in place (config `planner.spreadsheet_id`), so a Looker
#    Studio report saved on that sheet refreshes by itself.
#
# Started by `run_mmm.py orchestrate` or by the Colab schedule (`run_mmm.py schedule`).
# Results: `gs://<bucket>/<prefix>/orchestrations/<orchestration_id>/`.

# %%
import datetime as dt
import json
import os
import subprocess
import sys

import yaml

CFG = yaml.safe_load(CONFIG_YAML)  # noqa: F821 — injected by run_mmm.py
RUN = dict(RUN)  # noqa: F821
G = CFG["gcp"]
ROOT = f"gs://{G['bucket']}/{G['prefix']}"
OID = RUN.get("orchestration_id") or f"{dt.datetime.now(dt.UTC):%Y%m%d-%H%M}-{RUN['grid'].rsplit('.', 1)[0]}"
CODE = "/tmp/mmm_code"
BASE = f"{ROOT}/orchestrations/{OID}"
os.makedirs(CODE, exist_ok=True)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyyaml",
                "google-meridian[scenarioplanner]==2.1.0"], check=True)
subprocess.run(["gcloud", "storage", "cp", "-r", f"{ROOT}/code/*", CODE + "/"], check=True)
with open(f"{CODE}/VERSION.json") as f:
    VERSION = json.load(f)
print(OID, VERSION)


def mmm(*args, check=True):
    """Call run_mmm.py from the synced code, with the synced brand config."""
    r = subprocess.run([sys.executable, "-u", f"{CODE}/run_mmm.py", "--config", f"{CODE}/brand/mmm.yaml", *args],
                       text=True, capture_output=True)
    print(r.stdout[-6000:])
    if r.returncode != 0:
        print(r.stderr[-4000:])
        if check:
            raise RuntimeError(f"run_mmm.py {args[0]} failed")
    return r.stdout

# %% [markdown]
# ## 1. Fits

# %%
if RUN.get("publish_only"):
    SUMMARY = {"orchestration_id": OID, "code_version": VERSION, "grid": None, "states": {}, "main_run": None}
else:
    grid = f"{CODE}/brand/grids/{RUN['grid']}"
    with open(grid) as f:
        print(f.read())
    mmm("queue", grid, "--max-parallel", str(RUN.get("max_parallel") or G["max_parallel"]), "--report", BASE)
    comparison = json.loads(subprocess.run(["gcloud", "storage", "cat", f"{BASE}/comparison.json"],
                                           capture_output=True, text=True).stdout)
    marker = CFG["main_run_marker"]
    main_runs = [r["run_id"] for r in comparison["runs"] if marker in r["run_id"]]
    SUMMARY = {"orchestration_id": OID, "code_version": VERSION, "grid": RUN["grid"],
               "states": comparison["states"], "main_run": main_runs[0] if main_runs else None}
print(json.dumps(SUMMARY, indent=1))

# %% [markdown]
# ## 2. Scenario Planner tables (from the main fit)

# %%
if RUN.get("publish_only"):
    SUMMARY["planner_run"] = RUN["publish_only"]
elif RUN.get("planner") and SUMMARY["main_run"] and "SUCCEEDED" in SUMMARY["states"].get(SUMMARY["main_run"], ""):
    SUMMARY["planner_run"] = f"{SUMMARY['main_run']}-planner"
    mmm("planner", SUMMARY["main_run"], "--run-id", SUMMARY["planner_run"], "--wait")
else:
    SUMMARY["planner_run"] = None
    print("planner skipped")

# %% [markdown]
# ## 3. Update the planner sheet in place

# %%
if RUN.get("publish") and SUMMARY["planner_run"] and CFG["planner"].get("spreadsheet_id"):
    out = mmm("publish", SUMMARY["planner_run"], "--as-sa", check=False)
    SUMMARY["published"] = out[out.find("{"):] if "{" in out else out[-500:]
else:
    SUMMARY["published"] = None
    print("publish skipped (no planner run, or no planner.spreadsheet_id in the config)")
SUMMARY["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
with open("/tmp/orchestration.json", "w") as f:
    json.dump(SUMMARY, f, indent=1, default=str)
subprocess.run(["gcloud", "storage", "cp", "/tmp/orchestration.json", f"{BASE}/orchestration.json"], check=True)
print(json.dumps(SUMMARY, indent=1, default=str))
