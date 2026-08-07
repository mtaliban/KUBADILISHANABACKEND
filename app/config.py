from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MongoDB
    mongo_uri: str = "mongodb://admin:changeme@localhost:27017/kubadilishana_vituo?authSource=admin"

    # MQTT — swap to HiveMQ Cloud / EMQX Cloud by changing these env vars
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_use_tls: bool = False
    mqtt_client_prefix: str = "kv-backend"

    # Redis (cache)
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 3600

    # JWT
    jwt_secret: str = "change-me-in-production"
    jwt_alg: str = "HS256"
    jwt_expire_hours: int = 168

    # CORS
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # Files
    csv_output_dir: str = "/app/csv_output"
    log_file: str = "/app/csv_output/backend.log"

    # Selcom payments — set to 'live' after receiving Selcom credentials
    selcom_mode: str = "mock"           # 'mock' | 'live'
    selcom_vendor_code: str = ""        # TILLxxxxxx from Selcom
    selcom_api_key: str = ""
    selcom_api_secret: str = ""
    selcom_base_url: str = "https://apigw.selcommobile.com/v1"
    selcom_webhook_secret: str = "webhook-shared-secret"
    payment_currency: str = "TZS"
    payment_default_amount: int = 1000
    public_base_url: str = "http://localhost:8080"  # for callback_url

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
