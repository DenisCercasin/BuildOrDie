FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY bookpool_bot ./bookpool_bot
RUN pip install --no-cache-dir .
ENV BACKEND_MODE=mock
CMD ["bookpool-bot"]
