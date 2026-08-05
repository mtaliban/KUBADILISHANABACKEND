from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://admin:changeme@localhost:27017/kubadilishana_vituo?authSource=admin"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    csv_output_dir: str = "/app/csv_output"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
