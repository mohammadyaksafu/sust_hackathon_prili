from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    active_provider: str = "gemini"
    model_name: str = ""
    port: int = 8000
    debug: bool = False

    model_config = {
        "env_file": ".env",
        "case_sensitive": False,
        "protected_namespaces": ("settings_",),
    }


settings = Settings()

PROVIDER_DEFAULTS = {
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}


def get_model() -> str:
    if settings.model_name:
        return settings.model_name
    return PROVIDER_DEFAULTS.get(settings.active_provider, "gemini-2.0-flash-lite")