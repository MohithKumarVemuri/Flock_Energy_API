from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""
    
    PORTAL_BASE_URL: str = "https://urja-ops.flockenergy.tech"
    PORTAL_USERNAME: str = "operator@urja.local"
    PORTAL_PASSWORD: str = "urja-ops-2026"
    
    # Concurrency and timeout limits
    PORTAL_MAX_CONCURRENCY: int = 3
    PORTAL_TIMEOUT_SECONDS: float = 10.0
    PORTAL_MAX_RETRIES: int = 3
    
    # Cache settings
    CACHE_TTL_METERS: int = 3600
    CACHE_TTL_CONSUMPTION: int = 60
    
    # Path to fallback snapshot export data
    DATA_PATH: str = str(Path(__file__).parent.parent / "data" / "meter-export.json")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
