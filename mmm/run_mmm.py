#!/usr/bin/env python3
"""Build, run and publish a Google Meridian MMM: locally, or on Colab Enterprise.

Every brand-specific value comes from the brand's config (see example/mmm.yaml). Pass it
with --config, or set $MMM_CONFIG.

  Check the config     run_mmm.py --config B/mmm.yaml check [--data]
  Fit on this machine  run_mmm.py --config B/mmm.yaml local --sampling smoke      → runs/<run_id>/
  One cloud fit        run_mmm.py --config B/mmm.yaml submit --sampling standard --holdout random
  Status               run_mmm.py --config B/mmm.yaml status <run_id>
  Notebook only        run_mmm.py --config B/mmm.yaml build --sampling smoke      → build/
  Plain Colab          run_mmm.py --config B/mmm.yaml build --colab --sampling quick  (CSV input)
  A batch              run_mmm.py --config B/mmm.yaml queue validation.txt [--max-parallel N]
  Planner tables       run_mmm.py --config B/mmm.yaml planner <fitted_run_id> [--wait | --local]
  Planner sheet        run_mmm.py --config B/mmm.yaml publish <planner_run_id> [--as-sa | --local]
  Cloud copy           run_mmm.py --config B/mmm.yaml sync
  Whole chain          run_mmm.py --config B/mmm.yaml orchestrate --grid refresh.txt
  Schedule             run_mmm.py --config B/mmm.yaml schedule

Grid files (B/grids/*.txt) hold one line of `submit` flags per run, each with --run-id;
{date} in a run id becomes today's date (UTC, YYYYMMDD).

Quotas: Colab Enterprise FAILS a run that would go over quota (it doesn't wait), so
`queue` keeps --max-parallel runs in flight and resubmits quota failures. README → Quotas.

Auth: gcloud. On a laptop whose CLI login has lapsed (e.g. a Workspace reauth policy) the
script bridges through Application Default Credentials via CLOUDSDK_AUTH_ACCESS_TOKEN.
Inside Colab the runtime's identity (the service account) is used as is.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import pprint
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as cfgmod  # noqa: E402

FIT_SRC = HERE / "fit.py"
PLANNER_SRC = HERE / "scenario_planner.py"
ORCH_SRC = HERE / "orchestrator.py"
TOOLKIT_FILES = [HERE / "run_mmm.py", HERE / "config.py", FIT_SRC, PLANNER_SRC, ORCH_SRC]
GCLOUD = os.environ.get("GCLOUD") or shutil.which("gcloud") or "gcloud"
IN_COLAB = os.path.exists("/content") or bool(os.environ.get("COLAB_RELEASE_TAG"))
TERMINAL = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

# Set by init() from the config.
CONF: cfgmod.Config = None  # type: ignore[assignment]
CFG: dict = {}
PROJECT = REGION = BUCKET = PREFIX = SA = CODE_URI = ""
TEMPLATES: dict = {}


def init(config_path: str | os.PathLike) -> cfgmod.Config:
    global CONF, CFG, PROJECT, REGION, BUCKET, PREFIX, SA, CODE_URI, TEMPLATES
    CONF = cfgmod.load(config_path)
    CFG = CONF.cfg
    g = CFG["gcp"]
    PROJECT, REGION, BUCKET, PREFIX, SA = g["project"], g["region"], g["bucket"], g["prefix"], g["service_account"]
    TEMPLATES = {k: v for k, v in g["runtime_templates"].items() if v}
    CODE_URI = f"gs://{BUCKET}/{PREFIX}/code"
    return CONF


def now() -> str:
    return time.strftime("%H:%M:%S")


def _env() -> dict:
    env = dict(os.environ)
    if not IN_COLAB and "CLOUDSDK_AUTH_ACCESS_TOKEN" not in env:
        tok = subprocess.run([GCLOUD, "auth", "application-default", "print-access-token"],
                             capture_output=True, text=True).stdout.strip()
        if tok:
            env["CLOUDSDK_AUTH_ACCESS_TOKEN"] = tok
    return env


def gcloud(*args: str, check: bool = True, stderr: bool = True) -> str:
    """Run gcloud. stderr=False returns stdout only — needed for JSON output, because
    gcloud's 'Using endpoint [...]' notice goes to stderr."""
    r = subprocess.run([GCLOUD, *args, f"--project={PROJECT}"], capture_output=True, text=True, env=_env())
    if check and r.returncode != 0:
        sys.exit(f"gcloud {' '.join(args[:3])} failed:\n{r.stderr[-2000:]}")
    return r.stdout + (r.stderr if stderr else "")


# ── Notebook building ─────────────────────────────────────────────────────────
def _cells(src: pathlib.Path) -> list[dict]:
    cells, kind, buf = [], None, []

    def flush():
        if kind is None:
            return
        text = "\n".join(buf).strip("\n")
        if kind == "markdown":
            text = "\n".join(line[2:] if line.startswith("# ") else line.lstrip("#") for line in text.splitlines())
        cells.append({"cell_type": kind, "metadata": {}, "source": text,
                      **({"outputs": [], "execution_count": None} if kind == "code" else {})})

    for line in src.read_text().splitlines():
        if line.startswith("# %%"):
            flush()
            kind, buf = ("markdown" if "[markdown]" in line else "code"), []
        else:
            buf.append(line)
    flush()
    return cells


def build(run: dict, src: pathlib.Path = FIT_SRC, cfg: dict | None = None) -> dict:
    """Percent-format .py → notebook dict, with the parameters cell injected as cell 2."""
    import yaml  # noqa: PLC0415

    cells = _cells(src)
    config_yaml = yaml.safe_dump(cfg or CFG, sort_keys=False, allow_unicode=True)
    priors = CONF.priors_path(run.get("priors_file")).read_text() if src == FIT_SRC else ""
    experiments = (CONF.experiments_path(run["experiments_file"]).read_text()
                   if src == FIT_SRC and run.get("experiments_file") else "")
    for text in (config_yaml, priors, experiments):
        assert "'''" not in text, "config, priors and experiments may not contain '''"
    params = (f"# Parameters — injected by run_mmm.py at {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC\n"
              f"CONFIG_YAML = r'''{config_yaml}'''\n\n"
              f"PRIORS_CSV = r'''{priors}'''\n\n"
              + (f"EXPERIMENTS_CSV = r'''{experiments}'''\n\n" if src == FIT_SRC else "")
              + f"RUN = {pprint.pformat(run, sort_dicts=False)}\n")
    cells.insert(1, {"cell_type": "code", "metadata": {"tags": ["parameters"]}, "source": params,
                     "outputs": [], "execution_count": None})
    for i, c in enumerate(cells):
        c["id"] = f"cell-{i}"
    return {"nbformat": 4, "nbformat_minor": 5, "cells": cells,
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}}}


def _knots_arg(v: str):
    return v if v in ("aks", "monthly", "quarterly") else int(v)


def run_settings(a: argparse.Namespace) -> dict:
    d = cfgmod.run_defaults(CONF)
    pick = lambda flag, key: d[key] if flag is None else flag  # noqa: E731
    kpi, sampling = pick(a.kpi, "kpi"), pick(a.sampling, "sampling")
    knots = _knots_arg(str(pick(a.knots, "knots")))
    fourier = int(pick(a.fourier, "fourier"))
    priors = pick(a.priors, "priors_file")
    priors_scale = float(pick(a.priors_scale, "priors_scale"))
    dc = "none" if a.no_demand_control else pick(a.demand_control, "demand_control")
    if a.holdout and a.holdout not in CFG["holdouts"]:
        sys.exit(f"--holdout {a.holdout}: not in the config's holdouts ({', '.join(CFG['holdouts']) or 'none'})")
    for part in ([] if dc == "none" else dc.split("+")):
        if part not in CFG["demand_controls"]["series"]:
            sys.exit(f"--demand-control {dc}: {part!r} is not in demand_controls.series")
    if not CONF.priors_path(priors).exists():
        sys.exit(f"--priors {priors}: {CONF.priors_path(priors)} not found")
    experiments = None if a.no_experiments else pick(a.experiments, "experiments_file")
    if experiments and not CONF.experiments_path(experiments).exists():
        sys.exit(f"--experiments {experiments}: {CONF.experiments_path(experiments)} not found")
    run_id = a.run_id or (f"{dt.datetime.now(dt.UTC):%Y%m%d-%H%M}-{kpi}-{sampling}-{a.holdout or 'noholdout'}"
                          f"-k{knots}-f{fourier}-dc{dc.replace('+', '')}"
                          + (f"-prior{priors_scale:g}" if priors_scale != 1.0 else "")
                          + ("-calibrated" if experiments else ""))
    return {"run_id": run_id, "kpi": kpi, "sampling": sampling, "holdout": a.holdout, "knots": knots,
            "fourier": fourier, "priors_file": priors, "priors_scale": priors_scale,
            "experiments_file": experiments, "demand_control": dc,
            "window_end": a.window_end or d["window_end"],
            "optimise": d["optimise"] and not a.no_optimise, "report": d["report"] and not a.no_report,
            "save_model": d["save_model"] and not a.no_save, "template": a.template}


def _write_nb(nb: dict, gcs_path: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "notebook.ipynb"
        path.write_text(json.dumps(nb, indent=1))
        gcloud("storage", "cp", str(path), gcs_path)


def execute(display_name: str, nb_uri: str, out_uri: str, template: str, timeout: str) -> str:
    out = gcloud("colab", "executions", "create", f"--region={REGION}", f"--display-name={display_name}",
                 f"--notebook-runtime-template={TEMPLATES[template]}", f"--gcs-notebook-uri={nb_uri}",
                 f"--gcs-output-uri={out_uri}", f"--service-account={SA}",
                 f"--execution-timeout={timeout}", "--async")
    job = re.search(r"notebookExecutionJobs/(\d+)", out)
    return job.group(1) if job else out.strip()[-300:]


def run_local(nb: dict, out_dir: pathlib.Path) -> None:
    """Execute a built notebook's code cells on this machine, as one Python script."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "notebook.ipynb").write_text(json.dumps(nb, indent=1))
    code = [c["source"] for c in nb["cells"] if c["cell_type"] == "code"]
    script = out_dir / "notebook_script.py"
    script.write_text("def display(x):\n    print(x)\n\n\n" + "\n\n".join(code) + "\n")
    env = {**os.environ, "JAX_PLATFORMS": os.environ.get("JAX_PLATFORMS", "cpu")}
    r = subprocess.run([sys.executable, str(script)], cwd=out_dir, env=env)
    if r.returncode:
        sys.exit(f"local run failed (exit {r.returncode}); the script is {script}")


def submit(a: argparse.Namespace, run: dict | None = None, src: pathlib.Path = FIT_SRC) -> str:
    run = run or run_settings(a)
    if a.cmd == "local" or getattr(a, "local", False):
        out = pathlib.Path(a.out).resolve() / run["run_id"]
        run["local_out"] = str(out)
        run_local(build(run, src), out)
        print(json.dumps({"run_id": run["run_id"], "outputs": str(out)}))
        return run["run_id"]
    if a.cmd == "build":
        cfg = None
        if getattr(a, "colab", False):
            # Plain Colab: read the CSV uploaded to /content, keep outputs in /content, no GCS.
            if CFG["data"]["source"] != "csv":
                sys.exit("--colab needs data.source: csv (upload the CSV to the Colab session)")
            cfg = json.loads(json.dumps(CFG))
            cfg["data"]["csv"] = f"/content/{pathlib.Path(CFG['data']['csv']).name}"
            run["local_out"] = f"/content/mmm_out/{run['run_id']}"
        out = pathlib.Path(a.out) / f"{run['run_id']}.ipynb"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(build(run, src, cfg), indent=1))
        print(out)
        if cfg:
            print(f"Plain Colab: upload this notebook and {CFG['data']['csv']} to the session, then Run all.")
        return run["run_id"]
    nb = build(run, src)
    cfgmod.require_cloud(CONF)
    base = f"gs://{BUCKET}/{PREFIX}/runs/{run['run_id']}"
    _write_nb(nb, f"{base}/notebook.ipynb")
    job = execute(run["run_id"], f"{base}/notebook.ipynb", f"{base}/executed", a.template,
                  a.timeout or CFG["gcp"]["execution_timeout"])
    print(json.dumps({"run_id": run["run_id"], "execution": job}))
    return run["run_id"]


# ── Job tracking ──────────────────────────────────────────────────────────────
def _latest_jobs() -> dict:
    out = gcloud("colab", "executions", "list", f"--region={REGION}", "--limit=300", "--format=json",
                 check=False, stderr=False)
    try:
        jobs = json.loads(out)
    except ValueError:
        print(f"{now()} could not read execution list; retrying", flush=True)
        return {}
    latest: dict = {}
    for j in sorted(jobs, key=lambda j: j.get("createTime", "")):
        latest[j.get("displayName")] = j
    return latest


def wait(run_ids: list[str], poll: int = 45) -> dict:
    while True:
        jobs = _latest_jobs()
        states = {r: jobs.get(r, {}).get("jobState") for r in run_ids}
        if jobs and all(s in TERMINAL for s in states.values()):
            return {r: jobs[r] for r in run_ids}
        time.sleep(poll)


def read_summary(run_id: str, local_dir: str | None = None) -> dict | None:
    if local_dir:
        p = pathlib.Path(local_dir) / run_id / "run_summary.json"
        return json.loads(p.read_text()) if p.exists() else None
    s = gcloud("storage", "cat", f"gs://{BUCKET}/{PREFIX}/runs/{run_id}/run_summary.json", check=False, stderr=False)
    try:
        return json.loads(s)
    except ValueError:
        return None


def digest(run_ids: list[str], local_dir: str | None = None) -> list[dict]:
    """One comparison row per run: fit quality, convergence, ROI and contribution per channel."""
    rows = []
    for r in run_ids:
        d = read_summary(r, local_dir)
        if not d:
            rows.append({"run_id": r, "status": "no summary"})
            continue
        # fits without a holdout report accuracy with no evaluation_set column (all data)
        acc = {(x["geo_granularity"], x.get("evaluation_set", "All Data"), x["metric"]): x["value"]
               for x in d.get("predictive_accuracy", [])}
        row = {"run_id": r, "holdout": (d.get("run") or {}).get("holdout"), "elapsed_min": d.get("elapsed_min"),
               "rhat_max": d.get("rhat_max"),
               "review": (d.get("review") or {}).get("status", "").split(".")[-1],
               "r2_test_national": acc.get(("national", "Test", "R_Squared")),
               "r2_train_national": acc.get(("national", "Train", "R_Squared")),
               "wmape_test_national": acc.get(("national", "Test", "wMAPE")),
               "r2_all_national": acc.get(("national", "All Data", "R_Squared"))}
        for ch, v in (d.get("channels") or {}).items():
            key = "all" if ch == "All Channels" else ch
            row[f"{key}_roi"] = v.get("roi_median")
            row[f"{key}_roi_lo"] = v.get("roi_ci_lo")
            row[f"{key}_roi_hi"] = v.get("roi_ci_hi")
            row[f"{key}_contrib_pct"] = v.get("pct_contribution_median")
        rows.append(row)
    return rows


def read_grid(path: str | os.PathLike) -> list[list[str]]:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    lines = [ln.replace("{date}", stamp) for ln in pathlib.Path(path).read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    runs = [shlex.split(ln) for ln in lines]
    for args in runs:
        if "--run-id" not in args:
            sys.exit(f"every grid line needs --run-id: {args}")
    return runs


def queue(path: str, max_parallel: int, report: str | None = None, max_attempts: int = 30) -> list[str]:
    cfgmod.require_cloud(CONF)
    pending = read_grid(CONF.grid_path(path))
    rid = lambda args: args[args.index("--run-id") + 1]  # noqa: E731
    order = [rid(a) for a in pending]
    attempts: dict = {}
    active: dict = {}
    done: dict = {}
    while pending or active:
        jobs = _latest_jobs()
        quota_hit = False
        for r in list(active):
            j = jobs.get(r, {})
            if j.get("jobState") not in TERMINAL:
                continue
            msg = (j.get("status") or {}).get("message", "")
            if j["jobState"] == "JOB_STATE_FAILED" and "Quota" in msg and attempts[r] < max_attempts:
                pending.insert(0, active.pop(r))
                quota_hit = True
                print(f"{now()} {r}: quota — will retry", flush=True)
            else:
                done[r] = f"{j['jobState']} {msg[:200]}".strip()
                active.pop(r)
                print(f"{now()} {r}: {done[r]}", flush=True)
        while pending and len(active) < max_parallel and not quota_hit:
            args = pending.pop(0)
            r = rid(args)
            attempts[r] = attempts.get(r, 0) + 1
            subprocess.run([sys.executable, __file__, "--config", str(CONF.path), "submit", *args],
                           check=True, capture_output=True)
            active[r] = args
            print(f"{now()} {r}: submitted (attempt {attempts[r]})", flush=True)
            time.sleep(5)
        if pending or active:
            time.sleep(60 if quota_hit else 45)
    rows = digest(order)
    print(json.dumps(rows, indent=1, default=str))
    if report:
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "comparison.json"
            p.write_text(json.dumps({"grid": str(path), "states": done, "runs": rows}, indent=1, default=str))
            gcloud("storage", "cp", str(p), f"{report.rstrip('/')}/comparison.json")
    return order


# ── Scenario Planner ──────────────────────────────────────────────────────────
def planner(a: argparse.Namespace) -> str:
    p = CFG["planner"]
    run = {"run_id": a.run_id or f"{dt.datetime.now(dt.UTC):%Y%m%d-%H%M}-planner-{a.source_run_id[:20]}",
           "source_run_id": a.source_run_id,
           "min_spend_shift_ratio": p["min_shift"] if a.min_shift is None else a.min_shift,
           "max_spend_shift_ratio": p["max_shift"] if a.max_shift is None else a.max_shift,
           "optimization_name": p["name"]}
    rid = submit(a, run, PLANNER_SRC)
    if getattr(a, "wait", False) and not a.local:
        j = wait([rid])[rid]
        print(f"{now()} {rid}: {j['jobState']} {(j.get('status') or {}).get('message', '')}", flush=True)
    return rid


def _sheets_credentials(as_sa: bool):
    import google.auth  # noqa: PLC0415
    from google.auth import impersonated_credentials  # noqa: PLC0415

    if not as_sa:
        creds, _ = google.auth.default(scopes=SHEETS_SCOPES)
        return creds
    # Runtime/ADC tokens carry cloud-platform only; mint a Sheets-scoped token for the service
    # account (it holds Token Creator on itself — README → Set up Google Cloud).
    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if hasattr(source, "with_quota_project") and not IN_COLAB:
        source = source.with_quota_project(PROJECT)
    return impersonated_credentials.Credentials(source_credentials=source, target_principal=SA,
                                                target_scopes=SHEETS_SCOPES, lifetime=3600)


def _write_tables(service, spreadsheet_id: str, tables: dict) -> dict:
    """Replace the sheet's tabs with `tables` in place (same spreadsheet id, so a saved
    Looker Studio report keeps working). Returns {tab: sheetId}."""
    import math  # noqa: PLC0415

    def cell(v):
        """JSON-safe cell: NaN/inf → empty (None), numpy scalars → Python."""
        if hasattr(v, "item"):
            v = v.item()
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    ss = service.spreadsheets()
    meta = ss.get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    adds = [{"addSheet": {"properties": {"title": t}}} for t in tables if t not in existing]
    if adds:
        ss.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": adds}).execute()
    ss.values().batchClear(spreadsheetId=spreadsheet_id, body={"ranges": [f"'{t}'" for t in tables]}).execute()
    data = []
    for t, df in tables.items():
        rows = [[cell(v) for v in row] for row in df.astype(object).itertuples(index=False, name=None)]
        data.append({"range": f"'{t}'!A1", "values": [df.columns.tolist()] + rows})
    # Sheets caps request size (~10 MB); send ≤ ~2 MB per call, splitting big tabs by rows.
    chunk, size = [], 0

    def send():
        if chunk:
            ss.values().batchUpdate(spreadsheetId=spreadsheet_id,
                                    body={"valueInputOption": "USER_ENTERED", "data": list(chunk)}).execute()
        chunk.clear()

    for d in data:
        tab, rows = d["range"].split("!")[0], d["values"]
        step = max(1, int(len(rows) * 2_000_000 / max(1, len(json.dumps(rows)))))
        for start in range(0, len(rows), step):
            part = {"range": f"{tab}!A{start + 1}", "values": rows[start:start + step]}
            n = len(json.dumps(part))
            if size + n > 2_000_000:
                send()
                size = 0
            chunk.append(part)
            size += n
    send()
    meta = ss.get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
    current = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    stale = [{"deleteSheet": {"sheetId": sid}} for t, sid in current.items() if t not in tables]
    if stale:
        ss.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": stale}).execute()
    return {t: sid for t, sid in current.items() if t in tables}


def publish(run_id: str, spreadsheet_id: str | None, as_sa: bool, title: str | None = None,
            local_dir: str | None = None) -> dict:
    """Planner tables (CSVs) → Google Sheet → Looker Studio report link.

    With a spreadsheet id (default: config planner.spreadsheet_id) the sheet is updated
    IN PLACE, so the saved report refreshes itself. With --as-sa (the cloud path) the sheet
    must be shared with the service account as Editor. Without an id, a new sheet is created
    in the caller's Drive (needs ADC with Sheets + Drive scopes; service accounts cannot own
    Drive files).
    """
    import pandas as pd  # noqa: PLC0415
    from googleapiclient import discovery  # noqa: PLC0415
    from scenarioplanner.converters import sheets  # noqa: PLC0415
    from scenarioplanner.linkingapi import url_generator  # noqa: PLC0415

    spreadsheet_id = spreadsheet_id or CFG["planner"].get("spreadsheet_id")
    if local_dir:
        base_dir = pathlib.Path(local_dir) / run_id / "scenario_planner"
        manifest = json.loads((base_dir / "manifest.json").read_text())
        tables = {tab: pd.read_csv(base_dir / f) for tab, f in manifest["tables"].items()}
    else:
        base = f"gs://{BUCKET}/{PREFIX}/runs/{run_id}/scenario_planner"
        manifest = json.loads(gcloud("storage", "cat", f"{base}/manifest.json", stderr=False))
        with tempfile.TemporaryDirectory() as tmp:
            gcloud("storage", "cp", f"{base}/*.csv", tmp)
            tables = {tab: pd.read_csv(pathlib.Path(tmp) / f) for tab, f in manifest["tables"].items()}
    creds = _sheets_credentials(as_sa)
    if spreadsheet_id:
        service = discovery.build("sheets", "v4", credentials=creds, cache_discovery=False)
        tabs = _write_tables(service, spreadsheet_id, tables)
        # Pass the API's own URL (…/edit), as upload_to_gsheet does: the planner's Looker Studio
        # connector rejects a bare /d/<id> URL ("No GoogleSheet found/accessible").
        url = service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="spreadsheetUrl").execute()
        sheet = sheets.Spreadsheet(url=url["spreadsheetUrl"].split("?")[0], id=spreadsheet_id,
                                   sheet_id_by_tab_name=tabs)
    else:
        sheet = sheets.upload_to_gsheet(tables, creds,
                                        spreadsheet_name=title or f"{CFG['planner']['name']} Scenario Planner ({run_id})")
    out = {"planner_run_id": run_id, "source_run_id": manifest.get("source_run_id"),
           "spreadsheet": sheet.url, "report_url": url_generator.create_report_url(sheet),
           "updated_in_place": bool(spreadsheet_id), "published_utc": dt.datetime.now(dt.UTC).isoformat()}
    if local_dir:
        (pathlib.Path(local_dir) / run_id / "scenario_planner" / "published.json").write_text(json.dumps(out, indent=1))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "published.json"
            p.write_text(json.dumps(out, indent=1))
            gcloud("storage", "cp", str(p), f"{base}/published.json")
            gcloud("storage", "cp", str(p), f"gs://{BUCKET}/{PREFIX}/planner_latest.json")
    print(json.dumps(out, indent=1))
    return out


# ── Cloud: code sync, orchestration, schedule ─────────────────────────────────
def _git(path: pathlib.Path) -> dict:
    sha = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(path), "status", "--porcelain", "--", "."],
                                capture_output=True, text=True).stdout.strip())
    return {"git_sha": sha or None, "uncommitted_changes": dirty}


def sync() -> None:
    """Copy the toolkit code and the brand's config, priors and grids to GCS (what the cloud
    orchestrator and the schedule run), plus ready-to-run notebooks for anyone opening them
    in the Colab Enterprise UI."""
    cfgmod.require_cloud(CONF)
    brand_files = [*sorted(CONF.dir.glob("*.csv")), *sorted((CONF.dir / "grids").glob("*.txt"))]
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        for f in TOOLKIT_FILES:
            shutil.copy2(f, root / f.name)
        (root / "brand" / "grids").mkdir(parents=True)
        shutil.copy2(CONF.path, root / "brand" / "mmm.yaml")
        for f in brand_files:
            shutil.copy2(f, root / "brand" / f.relative_to(CONF.dir))
        version = {"toolkit": _git(HERE), "brand_config": _git(CONF.dir), "config_file": CONF.path.name,
                   "synced_utc": dt.datetime.now(dt.UTC).isoformat()}
        (root / "VERSION.json").write_text(json.dumps(version, indent=1))
        main = {"run_id": "ui-main", **cfgmod.run_defaults(CONF)}
        (root / "main.ipynb").write_text(json.dumps(build(main), indent=1))
        sched = CFG["schedule"]
        (root / "orchestrator_scheduled.ipynb").write_text(json.dumps(build(
            {"orchestration_id": None, "grid": sched["grid"], "max_parallel": CFG["gcp"]["max_parallel"],
             "planner": True, "publish": True}, ORCH_SRC), indent=1))
        gcloud("storage", "rm", "-r", f"{CODE_URI}/**", check=False)
        gcloud("storage", "cp", "-r", f"{root}/*", f"{CODE_URI}/")
    print(json.dumps({"synced_to": CODE_URI, **version, "files": len(TOOLKIT_FILES) + len(brand_files) + 4}, indent=1))


def orchestrate(grid: str, max_parallel: int | None, planner_step: bool, publish_step: bool,
                publish_only: str | None = None) -> None:
    cfgmod.require_cloud(CONF)
    oid = f"{dt.datetime.now(dt.UTC):%Y%m%d-%H%M}-" + (f"publish-{publish_only[:24]}" if publish_only
                                                         else pathlib.Path(grid).stem)
    nb = build({"orchestration_id": oid, "grid": grid, "max_parallel": max_parallel or CFG["gcp"]["max_parallel"],
                "planner": planner_step, "publish": publish_step, "publish_only": publish_only}, ORCH_SRC)
    base = f"gs://{BUCKET}/{PREFIX}/orchestrations/{oid}"
    _write_nb(nb, f"{base}/notebook.ipynb")
    job = execute(f"orch-{oid}", f"{base}/notebook.ipynb", f"{base}/executed", "orchestrator",
                  CFG["gcp"]["orchestrator_timeout"])
    print(json.dumps({"orchestration_id": oid, "execution": job, "results": base}))


def schedule(cron: str | None, start: str | None) -> None:
    cfgmod.require_cloud(CONF)
    brand = CFG["brand"]["id"]
    args = ["colab", "schedules", "create", f"--region={REGION}", f"--display-name={brand}-mmm-refresh",
            f"--cron-schedule={cron or CFG['schedule']['cron']}", "--max-concurrent-runs=1",
            f"--execution-display-name=orch-scheduled-{brand}",
            f"--notebook-runtime-template={TEMPLATES['orchestrator']}",
            f"--gcs-notebook-uri={CODE_URI}/orchestrator_scheduled.ipynb",
            f"--gcs-output-uri=gs://{BUCKET}/{PREFIX}/orchestrations/scheduled", f"--service-account={SA}",
            f"--execution-timeout={CFG['gcp']['orchestrator_timeout']}"]
    if start:
        args.append(f"--start-time={start}")
    print(gcloud(*args).strip()[-500:])


def status(run_id: str) -> None:
    cfgmod.require_cloud(CONF)
    out = gcloud("colab", "executions", "list", f"--region={REGION}",
                 f"--filter=displayName={run_id}", "--format=json", stderr=False)
    jobs = json.loads(out) if out.strip() else []
    for j in jobs:
        print(json.dumps({k: j.get(k) for k in ("name", "jobState", "createTime", "updateTime", "status")}))
    d = read_summary(run_id)
    print(json.dumps(d, indent=1) if d else "(no run_summary.json yet)")


def check(with_data: bool) -> None:
    """Validate the config (already done by init) and, optionally, the input table's shape."""
    c = CFG
    print(f"config OK: {CONF.path}")
    print(f"  brand      {c['brand']['name']} ({c['brand']['id']})")
    print(f"  geos       {', '.join(c['geos'])}")
    print(f"  channels   {', '.join(c['channels'])}")
    print(f"  KPIs       {', '.join(c['kpis'])}")
    print(f"  demand     {', '.join(c['demand_controls']['series'])} (default {c['demand_controls']['default']})")
    print(f"  holdouts   {', '.join(c['holdouts']) or 'none'}")
    print(f"  main fit   {json.dumps(cfgmod.run_defaults(CONF))}")
    for g in sorted((CONF.dir / "grids").glob("*.txt")):
        p = _parser()
        for args in read_grid(g):
            p.parse_args(["submit", *args])
        print(f"  grid OK    {g.name}")
    if not with_data:
        return
    import pandas as pd  # noqa: PLC0415

    d, cols = c["data"], c["columns"]
    if d["source"] == "csv":
        df = pd.read_csv(d["csv"])
    else:
        from google.cloud import bigquery  # noqa: PLC0415
        df = bigquery.Client(project=PROJECT, location=c["gcp"]["bq_location"]).query(
            f"SELECT * FROM `{d['table']}`").to_dataframe(create_bqstorage_client=False)
    need = {cols["week"], cols["geo"], *(CONF.spend_col(ch) for ch in c["channels"]),
            *(CONF.media_col(ch) for ch in c["channels"]), *c["demand_controls"]["series"].values(),
            *c["controls"], *c["guardrails"]["other_spend_cols"],
            *(v for v in (c["guardrails"]["unmapped_spend_col"], c["guardrails"]["unmapped_geo_spend_col"]) if v),
            *(k["kpi_col"] for k in c["kpis"].values()), *(k["revenue_col"] for k in c["kpis"].values()
                                                             if k.get("revenue_col"))}
    missing = sorted(need - set(df.columns))
    weeks, geos = df[cols["week"]].nunique(), set(df[cols["geo"]])
    print(f"data: {len(df)} rows, {weeks} weeks, geos {sorted(geos)}")
    problems = []
    if missing:
        problems.append(f"missing columns: {missing}")
    if geos != set(c["geos"]):
        problems.append(f"geos in data {sorted(geos)} ≠ config {sorted(c['geos'])}")
    if len(df) != weeks * len(geos):
        problems.append("incomplete geo × week grid")
    if weeks < 78:
        problems.append(f"only {weeks} weeks: under 18 months the model can't separate the channels")
    sys.exit("data problems:\n  - " + "\n  - ".join(problems)) if problems else print("data OK")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.environ.get("MMM_CONFIG"), help="the brand's mmm.yaml (or $MMM_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)
    ch = sub.add_parser("check", help="validate the config and grids")
    ch.add_argument("--data", action="store_true", help="also load the input table and check its shape")
    for name in ("submit", "build", "local"):
        s = sub.add_parser(name)
        s.add_argument("--run-id")
        s.add_argument("--kpi", help="a key of the config's kpis (default: main_fit.kpi)")
        s.add_argument("--sampling", help="a sampling preset: smoke | quick | standard (default: main_fit)")
        s.add_argument("--holdout", help="a key of the config's holdouts (default: none)")
        s.add_argument("--knots", help="aks | monthly | quarterly | an integer (default: config)")
        s.add_argument("--fourier", type=int, help="yearly sine/cosine pairs as controls (default: config)")
        s.add_argument("--demand-control", help="demand series from demand_controls.series, '+'-joined, or none")
        s.add_argument("--no-demand-control", action="store_true")
        s.add_argument("--priors", help="priors CSV next to the config (default: priors.default)")
        s.add_argument("--priors-scale", type=float, help="multiply every prior range (sensitivity runs)")
        s.add_argument("--experiments", help="lift-test results CSV next to the config (default: experiments.default)")
        s.add_argument("--no-experiments", action="store_true", help="fit on the benchmark priors alone")
        s.add_argument("--window-end", help="last week start to include (YYYY-MM-DD)")
        s.add_argument("--no-optimise", action="store_true")
        s.add_argument("--no-report", action="store_true")
        s.add_argument("--no-save", action="store_true")
        s.add_argument("--template", default="cpu", help="a key of gcp.runtime_templates")
        s.add_argument("--timeout", help="cloud execution timeout (default: gcp.execution_timeout)")
        s.add_argument("--out", default={"build": "build", "local": "runs"}.get(name, "build"),
                       help="local folder for built notebooks or local runs")
        if name == "build":
            s.add_argument("--colab", action="store_true",
                           help="a self-contained notebook for plain Colab: reads the CSV from /content, no GCS")
    st = sub.add_parser("status")
    st.add_argument("run_id")
    pl = sub.add_parser("planner")
    pl.add_argument("source_run_id", help="a fitted run (has model.binpb)")
    pl.add_argument("--run-id")
    pl.add_argument("--min-shift", type=float, help="largest per-channel cut, 0–0.9 (default: planner.min_shift)")
    pl.add_argument("--max-shift", type=float, help="largest per-channel increase (default: planner.max_shift)")
    pl.add_argument("--template", default="cpu")
    pl.add_argument("--timeout")
    pl.add_argument("--wait", action="store_true")
    pl.add_argument("--local", action="store_true", help="run on this machine; the fit must be in --out")
    pl.add_argument("--out", default="runs")
    qu = sub.add_parser("queue")
    qu.add_argument("file", help="a grid file (in the config's grids/ folder, or a path)")
    qu.add_argument("--max-parallel", type=int, help="default: gcp.max_parallel")
    qu.add_argument("--report", help="gs:// folder for comparison.json")
    pb = sub.add_parser("publish")
    pb.add_argument("run_id", help="a planner run")
    pb.add_argument("--spreadsheet-id", help="default: planner.spreadsheet_id")
    pb.add_argument("--as-sa", action="store_true", help="write as the service account (the sheet must be shared with it)")
    pb.add_argument("--title")
    pb.add_argument("--local", action="store_true", help="read the planner tables from --out instead of GCS")
    pb.add_argument("--out", default="runs")
    sub.add_parser("sync")
    orc = sub.add_parser("orchestrate")
    orc.add_argument("--grid", default=None, help="a grid file name (default: schedule.grid)")
    orc.add_argument("--max-parallel", type=int)
    orc.add_argument("--no-planner", action="store_true")
    orc.add_argument("--no-publish", action="store_true")
    orc.add_argument("--publish-only", help="skip fits and planner; publish this planner run to the sheet")
    sc = sub.add_parser("schedule")
    sc.add_argument("--cron", default=None, help="default: schedule.cron")
    sc.add_argument("--start", default=None, help="RFC3339 start time")
    return p


def main() -> None:
    a = _parser().parse_args()
    if not a.config:
        sys.exit("pass --config <brand>/mmm.yaml (or set MMM_CONFIG)")
    try:
        init(a.config)
        dispatch(a)
    except cfgmod.ConfigError as e:
        sys.exit(str(e))


def dispatch(a: argparse.Namespace) -> None:
    if a.cmd == "check":
        check(a.data)
    elif a.cmd == "status":
        status(a.run_id)
    elif a.cmd == "planner":
        planner(a)
    elif a.cmd == "queue":
        queue(a.file, a.max_parallel or CFG["gcp"]["max_parallel"], a.report)
    elif a.cmd == "publish":
        publish(a.run_id, a.spreadsheet_id, a.as_sa, a.title, a.out if a.local else None)
    elif a.cmd == "sync":
        sync()
    elif a.cmd == "orchestrate":
        orchestrate(a.grid or CFG["schedule"]["grid"], a.max_parallel, not a.no_planner, not a.no_publish,
                    a.publish_only)
    elif a.cmd == "schedule":
        schedule(a.cron, a.start)
    else:
        submit(a)


if __name__ == "__main__":
    main()
