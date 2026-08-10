from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MongoDB
    mongo_uri: str = "mongodb://admin:changeme@localhost:27017/kubadilishana_vituo?authSource=admin"

    # MQTT — self-hosted Mosquitto kwenye docker-compose yako mwenyewe.
    # (Hakuna cloud broker — kila kitu kinakaa kwenye backend/EC2 yako.)
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

    # Donations — manual verification flow.
    # Donors pay this mobile-money number (any network), then paste the SMS
    # confirmation on the donate page; the admin verifies and approves.
    donation_phone: str = "0763795801"
    payment_currency: str = "TZS"

    # Admin login — admins authenticate with email (never phone). The email
    # must be verified by code before admin access is granted.
    admin_email: str = "admin@kubadilishana.go.tz"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
