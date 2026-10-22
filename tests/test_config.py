import pytest
import yaml

import config


def edit(brand, fn):
    p = brand / "mmm.yaml"
    c = yaml.safe_load(p.read_text())
    fn(c)
    p.write_text(yaml.safe_dump(c, sort_keys=False))
    return p


def test_example_is_valid(brand):
    conf = config.load(brand / "mmm.yaml")
    assert conf.cfg["gcp"]["prefix"] == "example"
    assert conf.spend_col("meta_upper") == "spend_meta_upper"
    assert config.run_defaults(conf)["demand_control"] == "clicks+trends"


def test_dates_must_be_week_starts(brand):
    p = edit(brand, lambda c: c["holdouts"]["peak_all_geos"].update(end="2024-12-28"))   # a Saturday
    with pytest.raises(config.ConfigError, match="not a week start"):
        config.load(p)


def test_holdout_geo_must_exist(brand):
    p = edit(brand, lambda c: c["holdouts"]["peak_one_geo"].update(geos=["US"]))
    with pytest.raises(config.ConfigError, match="'US' is not in geos"):
        config.load(p)


def test_every_channel_needs_a_prior(brand):
    rows = (brand / "priors.csv").read_text().splitlines()
    (brand / "priors.csv").write_text("\n".join(r for r in rows if not r.startswith("meta_upper")) + "\n")
    with pytest.raises(config.ConfigError, match="no prior for channel 'meta_upper'"):
        config.load(brand / "mmm.yaml")


def test_every_channel_needs_a_sql_rule(brand):
    p = edit(brand, lambda c: c["sql"].update(channel_rules=[r for r in c["sql"]["channel_rules"]
                                                             if r["group"] != "meta_convert"]))
    with pytest.raises(config.ConfigError, match="no rule maps campaigns to channel 'meta_convert'"):
        config.load(p)


def test_unknown_demand_series(brand):
    p = edit(brand, lambda c: c["demand_controls"].update(default="clicks+weather"))
    with pytest.raises(config.ConfigError, match="'weather' is not in demand_controls.series"):
        config.load(p)


def test_cloud_block_only_needed_for_cloud(brand):
    p = edit(brand, lambda c: c["gcp"].update(project=None))
    conf = config.load(p)                      # local runs don't need it…
    with pytest.raises(config.ConfigError, match="gcp.project"):
        config.require_cloud(conf)             # …cloud commands do


def test_readout_margin_must_be_a_share(brand):
    p = edit(brand, lambda c: c["readout"].update(margin=50))
    with pytest.raises(config.ConfigError, match="readout.margin"):
        config.load(p)
