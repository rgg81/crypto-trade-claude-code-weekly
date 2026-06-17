import os

from futures_fund.config import Settings, load_env_file, load_settings


def test_load_env_file_sets_and_preserves(tmp_path, monkeypatch):
    monkeypatch.delenv("FF_TEST_A", raising=False)
    monkeypatch.setenv("FF_TEST_B", "existing")
    (tmp_path / ".env").write_text('FF_TEST_A=fromfile\nFF_TEST_B=should_not_win\n# comment\n\n')
    loaded = load_env_file(tmp_path / ".env")
    assert loaded["FF_TEST_A"] == "fromfile"
    assert os.environ["FF_TEST_A"] == "fromfile"   # set from file
    assert os.environ["FF_TEST_B"] == "existing"   # real env not overridden


def test_load_env_file_absent_is_noop(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == {}


def test_load_settings_loads_env_beside_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FF_DEMO_KEY", raising=False)
    (tmp_path / "config.yaml").write_text("account_size_usdt: 5000\n")
    (tmp_path / ".env").write_text("FF_DEMO_KEY=xyz\n")
    s = load_settings(tmp_path / "config.yaml")
    assert s.account_size_usdt == 5000.0
    assert os.environ["FF_DEMO_KEY"] == "xyz"


def test_defaults_when_no_file(tmp_path):
    s = load_settings(tmp_path / "missing.yaml")
    assert s.account_size_usdt == 10_000.0
    assert s.timeframe == "4h"
    assert s.symbol_count == 12
    assert s.exchange.testnet is True
    assert s.data.fred_series == ["DTWEXBGS", "DGS10", "FEDFUNDS", "CPIAUCSL"]


def test_monthly_and_loop_defaults(tmp_path):
    # CONSERVATIVE DOLLAR-NEUTRAL desk: ~3%/month target, -15% drawdown limit. The 'fast' loop entry
    # is kept INERT (never scheduled) so the protected run_exit_sweep + its tests don't break.
    s = load_settings(tmp_path / "missing.yaml")
    assert s.target_monthly == 0.03
    assert s.max_drawdown_tolerance == 0.15
    assert s.live is False
    assert set(s.loops) == {"fast", "strategic"}
    assert s.loops["strategic"].timeframe == "4h"
    assert s.loops["strategic"].regime_timeframe == "4h"


def test_yaml_loop_override(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("loops:\n  fast:\n    timeframe: \"5m\"\n    deep_model: \"haiku\"\n")
    s = load_settings(p)
    assert s.loops["fast"].timeframe == "5m"
    assert s.loops["fast"].deep_model == "haiku"


def test_deciding_agents_are_opus(tmp_path):
    # RULE: every agent that DECIDES money runs on Opus; only operational ones run cheaper.
    s = load_settings(tmp_path / "missing.yaml")
    for role in ("cio", "trader", "momentum", "carry", "news", "sentiment", "reflector"):
        assert s.model_for(role) == "opus", role
    assert s.model_for("pace_officer") == "sonnet"  # operational: narrates deterministic pacing


def test_model_for_falls_back_to_loop_then_global(tmp_path):
    s = load_settings(tmp_path / "missing.yaml")
    # an unmapped role falls back to the loop deep_model, then the global deep_model
    assert s.model_for("some_helper", loop="fast") == "sonnet"   # fast loop deep_model
    assert s.model_for("some_helper") == "opus"                  # global deep_model


def test_yaml_overrides(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("account_size_usdt: 25000\nsymbol_count: 5\nexchange:\n  testnet: false\n")
    s = load_settings(p)
    assert s.account_size_usdt == 25000.0
    assert s.symbol_count == 5
    assert s.exchange.testnet is False


def test_secrets_read_from_env(monkeypatch):
    monkeypatch.setenv("BINANCE_KEY", "abc")
    monkeypatch.setenv("BINANCE_SECRET", "xyz")
    s = Settings()
    assert s.exchange.api_key == "abc"
    assert s.exchange.api_secret == "xyz"


def test_missing_secret_is_none(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    s = Settings()
    assert s.data.fred_api_key is None


def test_news_sources_default_present():
    s = Settings()
    assert any("coindesk" in u for u in s.data.news_rss_sources)
    assert any("cointelegraph" in u for u in s.data.news_rss_sources)
