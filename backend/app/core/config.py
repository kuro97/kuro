from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "KURO_", "env_file": ".env"}

    # App
    app_name: str = "KuroTrack"
    debug: bool = False
    secret_key: str = "change-me-in-production"

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://kuro:kuro@localhost:5432/kurotrack"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Asterisk AMI
    ami_host: str = "45.136.56.159"
    ami_port: int = 5038
    ami_username: str = "kurotrack"
    ami_secret: str = "change-me"

    # Number pool
    default_freeze_time: int = 900  # 15 min in seconds
    heartbeat_interval: int = 30  # seconds
    heartbeat_timeout: int = 60  # seconds without heartbeat = release number


settings = Settings()
