import pytest
import yaml

import config
import sql_views


def test_example_renders(brand):
    files = sql_views.render(config.load(brand / "mmm.yaml"))
    weekly = files["example_mmm.vw_mmm_weekly_geo.sql"]
    for ch in ("google_convert", "google_upper", "meta_convert", "meta_upper"):
        assert f"AS spend_{ch}" in weekly and f"AS impressions_{ch}" in weekly
    assert "brand_search_clicks" in weekly and "UNNEST(['UK', 'DE', 'FR', 'NORDICS'])" in weekly
    campaign = files["example_mmm.vw_mmm_campaign_channel.sql"]
    # rules keep their order: brand search is claimed before the Google catch-all
    assert campaign.index("THEN 'brand_search'") < campaign.index("THEN 'google_convert'")
    assert "ELSE 'unmapped'" in campaign


def test_quotes_in_patterns_are_refused(brand):
    p = brand / "mmm.yaml"
    c = yaml.safe_load(p.read_text())
    c["sql"]["sale_campaign_regex"] = "SALE'S"
    p.write_text(yaml.safe_dump(c, sort_keys=False))
    with pytest.raises(config.ConfigError, match="quotes"):
        sql_views.render(config.load(p))
