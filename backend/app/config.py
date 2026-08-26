import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

# 로컬 실행: backend/app/config.py 기준 두 단계 위(repo root)의 config/.
# Docker: CONFIG_DIR 환경변수(=/app/config, docker-compose.yml에서 설정)로 덮어씀.
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", _DEFAULT_CONFIG_DIR))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    openai_api_key: str | None = None
    database_url: str = "postgresql+psycopg://techradar:techradar@localhost:5432/techradar"
    basic_auth_user: str | None = None
    basic_auth_password: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def get_interests() -> dict:
    return _load_yaml("interests.yaml")


@lru_cache
def get_sources_config() -> dict:
    return _load_yaml("sources.yaml")


@lru_cache
def get_pipeline_settings() -> dict:
    return _load_yaml("settings.yaml")
