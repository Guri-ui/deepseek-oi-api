import os
import json
import time
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Header, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from app.models import (
    MODEL_MAP,
    resolve_model_config,
    ChatCompletionRequest,
    ModelListResponse,
    ModelItem
)
from app.client import DeepSeekClient

app = FastAPI(
    title="DeepSeek OpenAI-Compatible API Wrapper",
    description="High-performance, pure HTTP/SSE OpenAI API bridge for DeepSeek Web",
    version="1.0.0"
)

# Enable CORS for browser-based clients and tooling
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ds_client = DeepSeekClient()

def get_configured_api_key() -> Optional[str]:
    """Retrieve configured API_KEY from environment or .env file."""
    key = os.environ.get("API_KEY")
    if not key:
        for env_path in [Path(__file__).parent.parent / ".env", Path(".env")]:
            if env_path.exists():
                try:
                    with open(env_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("API_KEY="):
                                val = line.split("=", 1)[1].strip().strip("'\"")
                                if val:
                                    return val
                except Exception:
                    pass
    return key if key else None

def verify_authorization(authorization: Optional[str] = Header(None)):
    """Enforce API_KEY if configured in environment or .env."""
    configured_key = get_configured_api_key()
    if not configured_key:
        return  # Open mode: No API key required for local development
    
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "message": "You didn't provide an API key. You need to provide your API key in an Authorization header using Bearer format (e.g. 'Authorization: Bearer YOUR_KEY').",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "missing_api_key"
                }
            }
        )
    
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token.strip() != configured_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "message": "Incorrect API key provided.",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key"
                }
            }
        )

# Global JSON error response helper matching oi-com.md
def openai_error_response(message: str, error_type: str = "invalid_request_error", code: Optional[str] = None, param: Optional[str] = None, status_code: int = 400):
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code
            }
        }
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return openai_error_response(
        message=str(exc.detail),
        error_type="api_error" if exc.status_code >= 500 else "invalid_request_error",
        status_code=exc.status_code
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    return openai_error_response(
        message=f"Internal server error: {str(exc)}",
        error_type="api_error",
        code="internal_error",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
    )

@app.get("/")
@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "DeepSeek OpenAI-Compatible API Wrapper",
        "timestamp": int(time.time()),
        "authenticated": bool(ds_client._user_token),
        "api_key_required": bool(get_configured_api_key())
    }

@app.get("/v1/models", response_model=ModelListResponse)
@app.get("/models", response_model=ModelListResponse)
def list_models(authorization: Optional[str] = Header(None)):
    """List the 6 supported DeepSeek models."""
    verify_authorization(authorization)
    models_data = [
        ModelItem(
            id=model_id,
            object="model",
            created=1755216000,
            owned_by="deepseek"
        )
        for model_id in MODEL_MAP.keys()
    ]
    return ModelListResponse(object="list", data=models_data)

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    authorization: Optional[str] = Header(None)
):
    """
    OpenAI-compatible chat completion endpoint supporting streaming and non-streaming responses.
    """
    # 1. Verify authorization if API_KEY is configured
    verify_authorization(authorization)
    # 1. Resolve requested model
    model_cfg = resolve_model_config(request.model)
    if not model_cfg:
        return openai_error_response(
            message=f"Model '{request.model}' does not exist. Available models: {list(MODEL_MAP.keys())}",
            error_type="invalid_request_error",
            param="model",
            code="model_not_found",
            status_code=status.HTTP_404_NOT_FOUND
        )

    # 2. Validate messages
    if not request.messages:
        return openai_error_response(
            message="Field 'messages' must be a non-empty array.",
            error_type="invalid_request_error",
            param="messages",
            code="missing_required_parameter",
            status_code=status.HTTP_400_BAD_REQUEST
        )

    # 3. Validate attachments against model capabilities
    has_image = False
    has_file = False
    for msg in request.messages:
        if isinstance(msg.content, list):
            for part in msg.content:
                pdict = part.model_dump() if hasattr(part, "model_dump") else (part if isinstance(part, dict) else {})
                ptype = pdict.get("type", "text")
                if ptype == "image_url":
                    has_image = True
                elif ptype in ("file", "file_data", "document"):
                    has_file = True

    if has_image and not model_cfg.get("supports_images"):
        return openai_error_response(
            message=f"Model '{request.model}' does not support image inputs. Please use 'deepseek-v4-vision' or 'deepseek-v4-vision-thinking'.",
            error_type="invalid_request_error",
            param="messages",
            code="unsupported_modality",
            status_code=status.HTTP_400_BAD_REQUEST
        )

    if has_file and not model_cfg.get("supports_files"):
        return openai_error_response(
            message=f"Model '{request.model}' does not support file attachments. Please use 'deepseek-v4-flash' or 'deepseek-v4-vision'.",
            error_type="invalid_request_error",
            param="messages",
            code="unsupported_attachment",
            status_code=status.HTTP_400_BAD_REQUEST
        )

    # 3. Ensure credentials loaded
    if not ds_client._user_token:
        ds_client.load_credentials()
        if not ds_client._user_token:
            return openai_error_response(
                message="DeepSeek user token not found. Please run login.py or provide DEEPSEEK_TOKEN.",
                error_type="authentication_error",
                code="missing_credentials",
                status_code=status.HTTP_401_UNAUTHORIZED
            )

    model_name = model_cfg["canonical_name"]
    model_type = model_cfg["model_type"]
    thinking_enabled = model_cfg["thinking_enabled"]
    search_enabled = False  # Hardcoded false per project requirements

    # 4. Handle streaming vs non-streaming
    if request.stream:
        stream_gen = await ds_client.execute_completion(
            model_name=model_name,
            messages=request.messages,
            model_type=model_type,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
            stream=True,
            tools=request.tools,
            tool_choice=request.tool_choice
        )
        return StreamingResponse(
            stream_gen,
            media_type="text/event-stream",
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    else:
        response_data = await ds_client.execute_completion(
            model_name=model_name,
            messages=request.messages,
            model_type=model_type,
            thinking_enabled=thinking_enabled,
            search_enabled=search_enabled,
            stream=False,
            tools=request.tools,
            tool_choice=request.tool_choice
        )
        return response_data
