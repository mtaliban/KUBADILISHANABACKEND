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

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
