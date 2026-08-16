from typing import List, Optional, Union, Dict, Any, Literal
from pydantic import BaseModel, Field
import time
import uuid

# Official Model Definitions mapped to DeepSeek backend
MODEL_MAP = {
    "deepseek-v4-flash": {
        "model_type": "default",
        "thinking_enabled": False,
        "search_enabled": False,
        "supports_files": True,
        "supports_images": True,
        "modalities": ["text", "image"],
        "capabilities": {
            "vision": False,
            "ocr_images": True,
            "file_upload": True,
            "thinking": False
        },
        "description": "DeepSeek V4 Flash (Fast text chat; supports OCR images, documents: PDF, TXT, code)"
    },
    "deepseek-v4-flash-thinking": {
        "model_type": "default",
        "thinking_enabled": True,
        "search_enabled": False,
        "supports_files": True,
        "supports_images": True,
        "modalities": ["text", "image"],
        "capabilities": {
            "vision": False,
            "ocr_images": True,
            "file_upload": True,
            "thinking": True
        },
        "description": "DeepSeek V4 Flash with DeepThink reasoning (Supports OCR images, documents: PDF, TXT, code)"
    },
    "deepseek-v4-pro": {
        "model_type": "expert",
        "thinking_enabled": False,
        "search_enabled": False,
        "supports_files": False,
        "supports_images": False,
        "modalities": ["text"],
        "capabilities": {
            "vision": False,
            "file_upload": False,
            "thinking": False
        },
        "description": "DeepSeek V4 Pro (Expert model for complex multi-domain queries; text-only, no file uploads)"
    },
    "deepseek-v4-pro-thinking": {
        "model_type": "expert",
        "thinking_enabled": True,
        "search_enabled": False,
        "supports_files": False,
        "supports_images": False,
        "modalities": ["text"],
        "capabilities": {
            "vision": False,
            "file_upload": False,
            "thinking": True
        },
        "description": "DeepSeek V4 Pro with DeepThink reasoning (Expert reasoning; text-only, no file uploads)"
    },
    "deepseek-v4-vision": {
        "model_type": "vision",
        "thinking_enabled": False,
        "search_enabled": False,
        "supports_files": True,
        "supports_images": True,
        "modalities": ["text", "image"],
        "capabilities": {
            "vision": True,
            "file_upload": True,
            "thinking": False
        },
        "description": "DeepSeek V4 Vision (Multimodal vision model; supports image and document uploads)"
    },
    "deepseek-v4-vision-thinking": {
        "model_type": "vision",
        "thinking_enabled": True,
        "search_enabled": False,
        "supports_files": True,
        "supports_images": True,
        "modalities": ["text", "image"],
        "capabilities": {
            "vision": True,
            "file_upload": True,
            "thinking": True
        },
        "description": "DeepSeek V4 Vision with DeepThink reasoning (Multimodal reasoning; supports image and document uploads)"
    }
}

def resolve_model_config(model_name: str) -> dict:
    """Resolve model name to DeepSeek configuration."""
    if model_name in MODEL_MAP:
        cfg = MODEL_MAP[model_name].copy()
        cfg["canonical_name"] = model_name
        return cfg
    
    clean_name = model_name.strip().lower()
    if clean_name in MODEL_MAP:
        cfg = MODEL_MAP[clean_name].copy()
        cfg["canonical_name"] = clean_name
        return cfg
            
    return None

class ContentPart(BaseModel):
    type: str
    text: Optional[str] = None
    image_url: Optional[Dict[str, Any]] = None
    file_data: Optional[str] = None
    filename: Optional[str] = None
    file_id: Optional[str] = None

class ChatMessage(BaseModel):
    role: str
    content: Optional[Union[str, List[Union[ContentPart, Dict[str, Any]]]]] = ""
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None

class StreamOptions(BaseModel):
    include_usage: Optional[bool] = True

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    stream_options: Optional[StreamOptions] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    response_format: Optional[Dict[str, Any]] = None
    user: Optional[str] = None

    class Config:
        extra = "ignore"

class ModelItem(BaseModel):
    id: str
    object: str = "model"
    created: int = 1755216000
    owned_by: str = "deepseek"

class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelItem]

class UsagePromptTokensDetails(BaseModel):
    cached_tokens: int = 0

class UsageCompletionTokensDetails(BaseModel):
    reasoning_tokens: int = 0

class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: Optional[UsagePromptTokensDetails] = None
    completion_tokens_details: Optional[UsageCompletionTokensDetails] = None

class FunctionCall(BaseModel):
    name: str
    arguments: str

class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: FunctionCall

class ChatChoiceMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None

class ChatChoice(BaseModel):
    index: int = 0
    message: ChatChoiceMessage
    finish_reason: Optional[str] = "stop"

class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl_{uuid.uuid4().hex}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatChoice]
    usage: UsageInfo

class DeltaFunctionCall(BaseModel):
    name: Optional[str] = None
    arguments: Optional[str] = None

class DeltaToolCall(BaseModel):
    index: int = 0
    id: Optional[str] = None
    type: Optional[str] = "function"
    function: Optional[DeltaFunctionCall] = None

class DeltaMessage(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[DeltaToolCall]] = None

class ChunkChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[str] = None

class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChunkChoice]
    usage: Optional[UsageInfo] = None
