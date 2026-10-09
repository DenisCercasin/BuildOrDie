import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/bookpool.db"
    service_api_key: str = ""
    payment_mode: str = "mock"
    reap_api_key: str = ""
    reap_base_url: str = "https://sg.sandbox.api.reap.global"
    reap_version: str = "2025-02-14"
    public_base_url: str = "http://localhost:8000"
    allowed_merchants: tuple[str, ...] = ("kinokuniya.com.sg", "popular.com.sg", "mossery.co")

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            service_api_key=os.getenv("BOOKPOOL_SERVICE_API_KEY", ""),
            payment_mode=os.getenv("PAYMENT_MODE", "mock"),
            reap_api_key=os.getenv("REAP_API_KEY", ""),
            reap_base_url=os.getenv("REAP_BASE_URL", cls.reap_base_url),
            reap_version=os.getenv("REAP_VERSION", cls.reap_version),
            public_base_url=os.getenv("PUBLIC_BASE_URL", cls.public_base_url).rstrip("/"),
        )

    def validate(self):
        if len(self.service_api_key) < 24:
            raise ValueError("Set BOOKPOOL_SERVICE_API_KEY to a random secret of at least 24 characters")
        if self.payment_mode not in {"mock", "reap_sandbox"}:
            raise ValueError("PAYMENT_MODE must be mock or reap_sandbox")
        if self.reap_base_url not in {
            "https://sg.sandbox.api.reap.global", "https://sandbox.api.reap.global"
        }:
            raise ValueError("Only official Reap sandbox hosts are allowed")
        if self.payment_mode == "reap_sandbox" and not self.reap_api_key:
            raise ValueError("Set REAP_API_KEY before enabling reap_sandbox")
