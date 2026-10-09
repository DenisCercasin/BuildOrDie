"""Local launcher. Generates a private service token, never Reap credentials."""
import argparse
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_env():
    envfile = ROOT / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.chdir(ROOT)
    load_env()
    if not os.getenv("BOOKPOOL_SERVICE_API_KEY"):
        keyfile = ROOT / "data" / "service.key"
        keyfile.parent.mkdir(exist_ok=True)
        if not keyfile.exists():
            keyfile.write_text(secrets.token_urlsafe(32))
        keyfile.chmod(0o600)
        os.environ["BOOKPOOL_SERVICE_API_KEY"] = keyfile.read_text().strip()
    os.environ.setdefault("PUBLIC_BASE_URL", f"http://localhost:{args.port}")
    import uvicorn
    uvicorn.run("app.main:app_factory", factory=True, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
