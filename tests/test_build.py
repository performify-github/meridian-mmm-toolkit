import argparse

import yaml

import run_mmm


def args(**kw):
    base = dict(cmd="build", run_id=None, kpi=None, sampling=None, holdout=None, knots=None, fourier=None,
                demand_control=None, no_demand_control=False, priors=None, priors_scale=None, window_end=None,
                no_optimise=False, no_report=False, no_save=False, template="cpu", timeout=None, out="build")
    base.update(kw)
    return argparse.Namespace(**base)


def test_run_settings_default_to_main_fit(brand):
    run_mmm.init(brand / "mmm.yaml")
    run = run_mmm.run_settings(args())
    assert run["kpi"] == "orders" and run["demand_control"] == "clicks+trends" and run["knots"] == "monthly"
    run = run_mmm.run_settings(args(no_demand_control=True, knots="24", holdout="random"))
    assert run["demand_control"] == "none" and run["knots"] == 24 and run["holdout"] == "random"


def test_notebook_carries_config_priors_and_run(brand):
    run_mmm.init(brand / "mmm.yaml")
    nb = run_mmm.build(run_mmm.run_settings(args(run_id="t1")))
    params = nb["cells"][1]
    assert params["metadata"]["tags"] == ["parameters"]
    ns = {}
    exec(params["source"], ns)
    assert yaml.safe_load(ns["CONFIG_YAML"])["brand"]["id"] == "example"
    assert ns["PRIORS_CSV"].startswith("channel,roi_low")
    assert ns["RUN"]["run_id"] == "t1"
    for src in (run_mmm.PLANNER_SRC, run_mmm.ORCH_SRC):
        assert run_mmm.build({"run_id": "x"}, src)["cells"][1]["metadata"]["tags"] == ["parameters"]


def test_every_grid_line_parses(brand):
    run_mmm.init(brand / "mmm.yaml")
    parser = run_mmm._parser()
    for grid in (brand / "grids").glob("*.txt"):
        for line in run_mmm.read_grid(grid):
            a = parser.parse_args(["--config", str(brand / "mmm.yaml"), "submit", *line])
            run_mmm.run_settings(a)
