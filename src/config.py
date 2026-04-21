"""
Skills service configuration via environment variables.
"""
import os
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    skills_base_dir: Path = Path(os.getenv("SKILLS_BASE_DIR", "/app/skills"))
    skills_index_cache_dir: Path = Path(
        os.getenv("SKILLS_INDEX_CACHE_DIR", str(Path.home() / ".crew-ai" / "skill_index_cache"))
    )
    port: int = int(os.getenv("PORT", "8090"))

    class Config:
        env_prefix = ""


settings = Settings()
