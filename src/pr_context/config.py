from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    github_token: str
    db_path: Path = Path("./data/pr_context.db")
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
