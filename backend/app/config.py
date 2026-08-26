import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]

# pydantic-settings의 env_file은 자기 Settings 객체에만 값을 채워줄 뿐 os.environ에는
# 넣어주지 않는다. 그런데 openai/anthropic SDK나 llm/client.py의 require_api_key()는
# os.environ을 직접 읽으므로, 여기서 명시적으로 .env를 프로세스 환경변수로 로드해준다.
# Docker에서는 이 경로에 .env가 없고(docker-compose의 env_file:이 이미 컨테이너 환경변수를
# 채워준 상태) 대신 조용히 무시된다.
load_dotenv(dotenv_path=_REPO_ROOT / ".env")

# 로컬 실행: backend/app/config.py 기준 두 단계 위(repo root)의 config/.
# Docker: CONFIG_DIR 환경변수(=/app/config, docker-compose.yml에서 설정)로 덮어씀.
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", _REPO_ROOT / "config"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT / ".env", extra="ignore")

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
