from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://admin:changeme@localhost:27017/kubadilishana_vituo?authSource=admin"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    jwt_secret: str = "change-me"
    jwt_alg: str = "HS256"
    jwt_expire_hours: int = 168  # 7 days

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
