import os


class Settings:
    """Runtime settings read from environment variables."""

    database_url: str = os.environ.get(
        "CERP_DATABASE_URL",
        "postgresql+psycopg://cerp:cerp@localhost:5432/cerp",
    )
    secret_key: str = os.environ.get("CERP_SECRET_KEY", "dev-only-change-me")
    default_currency: str = os.environ.get("CERP_CURRENCY", "SAR")
    session_cookie: str = "cerp_session"


settings = Settings()
