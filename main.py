"""ASGI entrypoint: ``uvicorn main:app --reload``."""

from merchant_intelligence.api import create_app


app = create_app()
