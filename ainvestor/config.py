from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    app_timezone: str = "Europe/Madrid"
    database_url: str = f"sqlite:///{DATA_DIR / 'ainvestor.db'}"
    trading_mode: Literal["paper", "testnet", "live"] = "paper"
    paper_initial_balance: float = 100.0
    paper_quote_currency: str = "USDT"

    binance_api_key: str = ""
    binance_api_secret: str = ""
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    default_exchange: str = "binance"

    cursor_api_key: str = ""
    ai_model: str = "composer-2.5"
    ai_use_fast: bool = False
    ai_fallback_enabled: bool = True

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    cryptopanic_api_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "ainvestor/0.1"

    risk_monitor_interval: int = 5
    market_collect_interval: int = 15
    ai_cycle_interval: int = 60
    decision_eval_hours: int = 24

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    live_max_capital_eur: float = 100.0
    live_max_crypto_eur: float = 50.0
    live_max_stocks_eur: float = 50.0
    live_max_derivatives_eur: float = 25.0
    stock_trading_mode: Literal["paper", "ibkr_paper", "ibkr_live"] = "paper"

    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1

    risk_config_path: Path = Field(default=CONFIG_DIR / "risk.yaml")

    @property
    def data_dir(self) -> Path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _read_risk_yaml(path: Path | None = None) -> dict:
    config_path = path or get_settings().risk_config_path
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_risk_config(path: Path | None = None, profile: str | None = None) -> dict:
    """Load risk config, optionally merged for a portfolio profile."""
    from ainvestor.portfolio.profiles import DEFAULT_PROFILE

    raw = _read_risk_yaml(path)
    if "profiles" not in raw:
        return raw

    prof = profile or DEFAULT_PROFILE
    if prof not in raw["profiles"]:
        prof = DEFAULT_PROFILE

    merged: dict = {}
    for key in ("fees", "stops", "allocation", "modes", "ibkr", "version"):
        if key in raw:
            merged[key] = copy.deepcopy(raw[key])

    profile_cfg = copy.deepcopy(raw["profiles"][prof])
    for key, value in profile_cfg.items():
        if key in ("initial_balance_usdt", "prompt_style"):
            merged[key] = value
        else:
            merged[key] = copy.deepcopy(value)

    merged["_profile"] = prof
    return merged


def get_all_market_pairs(path: Path | None = None) -> list[str]:
    """Union of crypto whitelists across all profiles (for market data collection)."""
    from ainvestor.portfolio.profiles import PROFILES

    raw = _read_risk_yaml(path)
    if "profiles" not in raw:
        return raw.get("whitelist", {}).get("pairs", [])

    pairs: list[str] = []
    seen: set[str] = set()
    for prof in PROFILES:
        for pair in raw["profiles"].get(prof, {}).get("whitelist", {}).get("pairs", []):
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


def get_profile_initial_balance(profile: str, path: Path | None = None) -> float:
    cfg = load_risk_config(path, profile=profile)
    return float(cfg.get("initial_balance_usdt", get_settings().paper_initial_balance))
