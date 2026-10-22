"""Load, default and validate a brand's MMM config (`mmm.yaml`).

The runner, the SQL renderer, the Trends loader and the readout all read the config through
here. The runner embeds the *resolved* config (defaults filled in) in every notebook it
builds, so the notebooks never need to know a default, and every executed notebook records
exactly what it ran with.

    python3 mmm/config.py example/mmm.yaml      # validate and print the resolved config
"""
from __future__ import annotations

import copy
import csv
import dataclasses
import datetime as dt
import pathlib
import re
import sys

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
CALENDAR_FLAGS = ("bfcm", "xmas_runin", "xmas_week", "newyear_week", "easter_week")
HOLDOUT_TYPES = ("random", "dates", "geo_dates")
SLUG = re.compile(r"^[a-z][a-z0-9_-]*$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DEFAULTS: dict = {
    "brand": {"id": None, "name": None},
    "currency": {"code": "GBP", "symbol": "£"},
    "gcp": {
        "project": None, "region": None, "bq_location": None, "service_account": None,
        "bucket": None, "prefix": None,
        "runtime_templates": {"cpu": None, "orchestrator": None},
        "max_parallel": 4, "execution_timeout": "6h", "orchestrator_timeout": "12h",
    },
    "data": {"source": "bigquery", "table": None, "csv": None,
             "window": {"start": None, "end": None}, "week_start_day": "monday"},
    "columns": {"week": "week_start", "geo": "geo", "spend": "spend_{channel}", "media": "impressions_{channel}"},
    "geos": {},
    "kpis": {},
    "channels": {},
    "demand_controls": {"series": {}, "default": "none"},
    "controls": [],
    "calendar": [],
    "events": {},
    "fill_zero": [],
    "model": {"max_lag": 8, "knots": "monthly", "fourier": 2},
    "holdouts": {},
    "priors": {"default": "priors.csv"},
    "guardrails": {"max_unmapped_spend_share": 0.01, "unmapped_spend_col": None,
                   "unmapped_geo_spend_col": None, "other_spend_cols": []},
    "optimisation": None,
    "sampling": {
        "smoke": {"n_chains": 2, "n_adapt": 100, "n_burnin": 100, "n_keep": 50},
        "quick": {"n_chains": 4, "n_adapt": 500, "n_burnin": 500, "n_keep": 250},
        "standard": {"n_chains": 4, "n_adapt": 2000, "n_burnin": 1000, "n_keep": 1000},
        "prior_draws": 500,
        "seed": 1,
    },
    "main_fit": {},
    "main_run_marker": "main",
    "planner": {"spreadsheet_id": None, "name": None, "min_shift": 0.5, "max_shift": 1.0},
    "schedule": {"cron": "TZ=Europe/London 0 6 7 1,4,7,10 *", "grid": "refresh.txt"},
    "readout": {"caveats": [], "palette": {}, "margin": None, "notes": {}},
    "trends": None,
    "sql": None,
}

# What a run can set (CLI flags / main_fit / grid lines); the rest of the config is per brand.
RUN_KEYS = ("kpi", "sampling", "holdout", "knots", "fourier", "priors_file", "priors_scale",
            "demand_control", "window_end", "optimise", "report", "save_model")


class ConfigError(ValueError):
    pass


@dataclasses.dataclass
class Config:
    path: pathlib.Path
    cfg: dict

    @property
    def dir(self) -> pathlib.Path:
        return self.path.parent

    def priors_path(self, name: str | None = None) -> pathlib.Path:
        return self.dir / (name or self.cfg["priors"]["default"])

    def grid_path(self, name: str) -> pathlib.Path:
        p = pathlib.Path(name)
        return p if p.exists() or p.is_absolute() else self.dir / "grids" / name

    def spend_col(self, channel: str) -> str:
        return self.cfg["columns"]["spend"].format(channel=channel)

    def media_col(self, channel: str) -> str:
        return self.cfg["columns"]["media"].format(channel=channel)


def _merge(base, over):
    """Defaults ← user values. Dicts merge key by key; anything else replaces."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if k in base else v
        return out
    return copy.deepcopy(over) if over is not None else copy.deepcopy(base)


def _date(v, what: str, errors: list, weekday: str | None = None) -> dt.date | None:
    if v is None:
        return None
    try:
        d = v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))
    except ValueError:
        errors.append(f"{what}: {v!r} is not a YYYY-MM-DD date")
        return None
    if weekday and WEEKDAYS[d.weekday()] != weekday:
        errors.append(f"{what}: {d} is a {WEEKDAYS[d.weekday()].title()}, not a week start "
                      f"({weekday.title()}). Meridian time coordinates are week starts")
    return d


def read_priors(path: pathlib.Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def validate(conf: Config) -> list[str]:
    c, e = conf.cfg, []
    wd = c["data"]["week_start_day"]
    if wd not in WEEKDAYS:
        e.append(f"data.week_start_day: {wd!r} is not a weekday name")
        wd = None

    b = c["brand"]
    if not b.get("id") or not SLUG.match(str(b["id"])):
        e.append("brand.id: a lower-case slug is required (letters, digits, - or _)")
    if not b.get("name"):
        e.append("brand.name is required")

    if not c["geos"] or not isinstance(c["geos"], dict):
        e.append("geos: map each geo to its population")
    else:
        for g, pop in c["geos"].items():
            if not isinstance(pop, (int, float)) or pop <= 0:
                e.append(f"geos.{g}: population must be a positive number")

    if not c["kpis"]:
        e.append("kpis: at least one KPI is required")
    for k, v in c["kpis"].items():
        if not v.get("kpi_col"):
            e.append(f"kpis.{k}.kpi_col is required")
        if v.get("type", "non_revenue") not in ("non_revenue", "revenue"):
            e.append(f"kpis.{k}.type must be non_revenue or revenue")
        if v.get("type", "non_revenue") == "non_revenue" and not v.get("revenue_col"):
            e.append(f"kpis.{k}: a non-revenue KPI needs revenue_col (revenue ÷ KPI gives revenue per KPI, "
                     "so ROI reads in currency)")

    if not c["channels"]:
        e.append("channels: at least one paid media channel is required")
    for ch in c["channels"]:
        if not IDENT.match(ch):
            e.append(f"channels.{ch}: ids become column names, so use letters, digits and _ only")

    d = c["data"]
    if d["source"] not in ("bigquery", "csv"):
        e.append("data.source must be bigquery or csv")
    elif d["source"] == "bigquery" and not d.get("table"):
        e.append("data.table is required when data.source is bigquery")
    elif d["source"] == "csv" and not d.get("csv"):
        e.append("data.csv is required when data.source is csv")
    _date(d["window"].get("start"), "data.window.start", e, wd)
    _date(d["window"].get("end"), "data.window.end", e, wd)

    dc = c["demand_controls"]
    series = dc.get("series") or {}

    def check_dc(value, what):
        if value in (None, "none"):
            return
        for part in str(value).split("+"):
            if part not in series:
                e.append(f"{what}: {part!r} is not in demand_controls.series ({', '.join(series) or 'empty'})")

    check_dc(dc.get("default"), "demand_controls.default")

    for flag in c["calendar"]:
        if flag not in CALENDAR_FLAGS:
            e.append(f"calendar: {flag!r} is not one of {', '.join(CALENDAR_FLAGS)}")
    for name, weeks in (c["events"] or {}).items():
        if not IDENT.match(name):
            e.append(f"events.{name}: names become column names, so use letters, digits and _ only")
        for w in weeks or []:
            _date(w, f"events.{name}", e, wd)

    def check_knots(v, what):
        if v in ("aks", "monthly", "quarterly"):
            return
        try:
            if int(v) > 0:
                return
        except (TypeError, ValueError):
            pass
        e.append(f"{what}: {v!r} must be aks, monthly, quarterly or a positive integer")

    check_knots(c["model"]["knots"], "model.knots")

    for name, h in c["holdouts"].items():
        t = h.get("type")
        if t not in HOLDOUT_TYPES:
            e.append(f"holdouts.{name}.type must be one of {', '.join(HOLDOUT_TYPES)}")
            continue
        if t == "random":
            r = h.get("ratio")
            if not isinstance(r, (int, float)) or not 0 < r < 1:
                e.append(f"holdouts.{name}.ratio must be strictly between 0 and 1")
            continue
        ranges = h.get("ranges") or [[h.get("start"), h.get("end")]]
        for i, (s, en) in enumerate(ranges):
            ds = _date(s, f"holdouts.{name} start", e, wd)
            de = _date(en, f"holdouts.{name} end", e, wd)
            if ds is None or de is None:
                if s is None or en is None:
                    e.append(f"holdouts.{name}: range {i + 1} needs both a start and an end week")
            elif ds > de:
                e.append(f"holdouts.{name}: start {ds} is after end {de}")
        if t == "geo_dates":
            gs = h.get("geos") or []
            if not gs:
                e.append(f"holdouts.{name}.geos: list the geos to hold out")
            for g in gs:
                if g not in c["geos"]:
                    e.append(f"holdouts.{name}.geos: {g!r} is not in geos")

    sp = c["sampling"]
    presets = [k for k, v in sp.items() if isinstance(v, dict)]
    for k in presets:
        missing = {"n_chains", "n_adapt", "n_burnin", "n_keep"} - set(sp[k])
        if missing:
            e.append(f"sampling.{k}: missing {', '.join(sorted(missing))}")

    o = c["optimisation"]
    if o:
        _date(o.get("start"), "optimisation.start", e, wd)
        _date(o.get("end"), "optimisation.end", e, wd)
        if not o.get("start") or not o.get("end"):
            e.append("optimisation: start and end (week starts) are required")

    pf = c["priors"]["default"]
    priors_files = {pf, *(v for v in [c["main_fit"].get("priors_file")] if v)}
    for name in priors_files:
        if "/" in str(name):
            e.append(f"priors: {name!r} must be a file name next to the config, not a path")
            continue
        p = conf.priors_path(name)
        if not p.exists():
            e.append(f"priors: {p} not found")
            continue
        rows = read_priors(p)
        by = {r.get("channel"): r for r in rows}
        for ch in c["channels"]:
            if ch not in by:
                e.append(f"{name}: no prior for channel {ch!r}")
                continue
            try:
                lo, hi, mass = float(by[ch]["roi_low"]), float(by[ch]["roi_high"]), float(by[ch]["mass"])
                if not 0 < lo < hi:
                    e.append(f"{name}: {ch} needs 0 < roi_low < roi_high")
                if not 0 < mass < 1:
                    e.append(f"{name}: {ch} mass must be between 0 and 1 (e.g. 0.9 for a 90% range)")
            except (KeyError, TypeError, ValueError):
                e.append(f"{name}: {ch} needs numeric roi_low, roi_high and mass")
        masses = {by[ch].get("mass") for ch in c["channels"] if ch in by}
        if len(masses) > 1:
            e.append(f"{name}: every row must use the same mass (Meridian takes one mass for all ranges)")

    mf = c["main_fit"]
    for k in mf:
        if k not in RUN_KEYS:
            e.append(f"main_fit.{k}: not a run setting ({', '.join(RUN_KEYS)})")
    if mf.get("kpi") and mf["kpi"] not in c["kpis"]:
        e.append(f"main_fit.kpi: {mf['kpi']!r} is not in kpis")
    if mf.get("sampling") and mf["sampling"] not in presets:
        e.append(f"main_fit.sampling: {mf['sampling']!r} is not a sampling preset")
    if mf.get("holdout") and mf["holdout"] not in c["holdouts"]:
        e.append(f"main_fit.holdout: {mf['holdout']!r} is not in holdouts")
    check_dc(mf.get("demand_control"), "main_fit.demand_control")
    if "knots" in mf:
        check_knots(mf["knots"], "main_fit.knots")

    s = c["sql"]
    if s:
        rules = s.get("channel_rules") or []
        groups = set(c["channels"]) | set(s.get("control_groups") or [])
        for i, r in enumerate(rules):
            if r.get("group") not in groups:
                e.append(f"sql.channel_rules[{i}].group: {r.get('group')!r} is not a channel or control group")
            if not r.get("platform"):
                e.append(f"sql.channel_rules[{i}]: platform is required")
        for ch in c["channels"]:
            if not any(r.get("group") == ch for r in rules):
                e.append(f"sql.channel_rules: no rule maps campaigns to channel {ch!r}")
        for region, geo in (s.get("region_to_geo") or {}).items():
            if geo not in c["geos"]:
                e.append(f"sql.region_to_geo.{region}: {geo!r} is not in geos")
        for i, o in enumerate(s.get("ads_region_overrides") or []):
            if o.get("geo") not in c["geos"]:
                e.append(f"sql.ads_region_overrides[{i}].geo: {o.get('geo')!r} is not in geos")

    m = c["readout"].get("margin")
    if m is not None and not (isinstance(m, (int, float)) and 0 < m <= 1):
        e.append("readout.margin: a contribution margin between 0 and 1 (e.g. 0.5), or null")

    t = c["trends"]
    if t:
        if not t.get("topic"):
            e.append("trends.topic: the brand's Google Trends topic id (e.g. /m/0abc12) is required")
        for g, weights in (t.get("geos") or {}).items():
            if g not in c["geos"]:
                e.append(f"trends.geos.{g}: not in geos")
            if not weights or any(not isinstance(w, (int, float)) or w <= 0 for w in weights.values()):
                e.append(f"trends.geos.{g}: map each country code to a positive weight")
    return e


def load(path: str | pathlib.Path, check: bool = True) -> Config:
    import yaml  # noqa: PLC0415

    path = pathlib.Path(path).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    conf = Config(path=path, cfg=_merge(DEFAULTS, raw))
    c = conf.cfg
    # Derived defaults
    c["gcp"]["prefix"] = c["gcp"]["prefix"] or c["brand"]["id"]
    c["gcp"]["bq_location"] = c["gcp"]["bq_location"] or c["gcp"]["region"]
    c["planner"]["name"] = c["planner"]["name"] or f"{c['brand']['name']} MMM"
    csv_path = c["data"].get("csv")
    if csv_path and not str(csv_path).startswith("gs://") and not pathlib.Path(csv_path).is_absolute():
        c["data"]["csv"] = str(conf.dir / csv_path)   # relative to the config file, not the shell
    if c["sql"] and not c["sql"].get("start_week"):
        c["sql"]["start_week"] = c["data"]["window"]["start"]
    for k in ("start", "end"):
        v = c["data"]["window"].get(k)
        c["data"]["window"][k] = str(v) if v is not None else None
    if check:
        errors = validate(conf)
        if errors:
            raise ConfigError(f"{path}:\n  - " + "\n  - ".join(errors))
    return conf


def require_cloud(conf: Config) -> None:
    """Cloud commands need the GCP block filled in; local runs don't."""
    g = conf.cfg["gcp"]
    missing = [k for k in ("project", "region", "service_account", "bucket") if not g.get(k)]
    missing += [f"runtime_templates.{k}" for k in ("cpu", "orchestrator") if not g["runtime_templates"].get(k)]
    if missing:
        raise ConfigError(f"{conf.path}: cloud commands need gcp.{', gcp.'.join(missing)}")
    placeholders = [k for k in ("project", "service_account", "bucket") if "your-" in str(g[k])]
    if placeholders:
        raise ConfigError(f"{conf.path}: gcp.{', gcp.'.join(placeholders)} still hold the example's placeholder "
                          "values. README → Set up Google Cloud")


def run_defaults(conf: Config) -> dict:
    """Run settings when a flag isn't given: main_fit, then the model block, then fixed defaults."""
    c = conf.cfg
    mf = c["main_fit"]
    return {
        "kpi": mf.get("kpi") or next(iter(c["kpis"])),
        "sampling": mf.get("sampling", "smoke"),
        "holdout": None,
        "knots": mf.get("knots", c["model"]["knots"]),
        "fourier": int(mf.get("fourier", c["model"]["fourier"])),
        "priors_file": mf.get("priors_file", c["priors"]["default"]),
        "priors_scale": float(mf.get("priors_scale", 1.0)),
        "demand_control": mf.get("demand_control", c["demand_controls"]["default"]) or "none",
        "window_end": mf.get("window_end"),
        "optimise": bool(mf.get("optimise", c["optimisation"] is not None)),
        "report": bool(mf.get("report", True)),
        "save_model": bool(mf.get("save_model", True)),
    }


if __name__ == "__main__":
    import yaml  # noqa: PLC0415

    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        cf = load(sys.argv[1])
    except ConfigError as err:
        sys.exit(str(err))
    print(yaml.safe_dump(cf.cfg, sort_keys=False, allow_unicode=True))
    print("# OK:", cf.path)
