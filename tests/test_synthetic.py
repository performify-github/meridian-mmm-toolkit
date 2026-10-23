import config
import synthetic


def test_synthetic_matches_the_input_contract(brand):
    conf = config.load(brand / "mmm.yaml")
    df, truth = synthetic.generate(conf, weeks=60, seed=1)
    assert len(df) == 60 * len(conf.cfg["geos"])
    need = {conf.spend_col(c) for c in conf.cfg["channels"]} | {conf.media_col(c) for c in conf.cfg["channels"]}
    need |= set(conf.cfg["demand_controls"]["series"].values()) | set(conf.cfg["controls"])
    assert need <= set(df.columns)
    assert set(truth["channels"]) == set(conf.cfg["channels"])


def test_truth_has_the_answer_key_and_a_lift_test(brand):
    conf = config.load(brand / "mmm.yaml")
    _, truth = synthetic.generate(conf, weeks=104, seed=7)
    ch = truth["channels"]["google_convert"]
    assert 0 < ch["true_mroi"] < ch["true_roi"]          # diminishing returns
    assert 0 < truth["paid_share_of_orders_pct"] < 100
    assert abs(sum(v["true_share_of_orders_pct"] for v in truth["channels"].values())
               - truth["paid_share_of_orders_pct"]) < 0.1
    rows = synthetic.experiments_csv(truth).splitlines()
    assert rows[0].startswith("channel,start,end,estimate,standard_error,kpi_type,spend")
    assert rows[1].startswith("google_convert,") and ",SIMULATED," in rows[1]
