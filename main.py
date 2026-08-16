#!/usr/bin/env python3
import os
import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"[*] Starting DeepSeek OpenAI-Compatible API Wrapper on http://{host}:{port}")
    print(f"[*] Base URL: http://{host}:{port}/v1")
    uvicorn.run("app.server:app", host=host, port=port, log_level="info")
