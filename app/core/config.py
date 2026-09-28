from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyUrl, EmailStr


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    DATABASE_URL: str = "mysql+aiomysql://root:root@localhost:3306/robot_db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Robot authentication
    ROBOT_API_KEY: str = "change-me-robot-key"

    # First admin seeding
    ADMIN_EMAIL: EmailStr = "admin@example.com"
    ADMIN_PASSWORD: str = "change-me-admin-password"

    # Simulation mode
    SIMULATION_MODE: bool = False
    SIM_NAV_DELAY_S: float = 5.0
    SIM_NAV_SUCCESS_RATE: float = 0.9
    SIM_SLAM_HZ: float = 10.0
    SIM_PATH: str = "random_walk"  # "scripted" | "random_walk"

    # Application
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DEBUG: bool = False

    # Safety
    DELIVERY_STATE_TIMEOUT_S: int = 120
    HEARTBEAT_TIMEOUT_S: int = 5


settings = Settings()
