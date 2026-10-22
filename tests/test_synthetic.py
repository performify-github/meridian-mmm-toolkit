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
