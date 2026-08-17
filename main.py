#!/usr/bin/env python3
import os
from pathlib import Path
import uvicorn

def load_dotenv():
    """Load environment variables from .env file into os.environ."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

if __name__ == "__main__":
    load_dotenv()
    host = os.environ.get("HOST") or "127.0.0.1"
    port_str = os.environ.get("PORT") or "8000"
    port = int(port_str)
    print(f"[*] Starting DeepSeek OpenAI-Compatible API Wrapper on http://{host}:{port}")
    print(f"[*] Base URL: http://{host}:{port}/v1")
    uvicorn.run("app.server:app", host=host, port=port, log_level="info")
