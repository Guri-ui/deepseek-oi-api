import os
import json
import base64
import time
import uuid
import asyncio
import mimetypes
import httpx
from pathlib import Path
from typing import AsyncGenerator, List, Tuple, Dict, Any, Optional
from fastapi import HTTPException

from app.solver.pow_solver import solve_pow_async
from app.tool_parser import (
    format_tools_system_prompt,
    parse_deepseek_tool_calls,
    format_assistant_tool_calls,
    get_safe_streamable_text
)
from app.models import (
    ChatMessage,
    ChatCompletionResponse,
    ChatChoice,
    ChatChoiceMessage,
    ToolCall,
    FunctionCall,
    UsageInfo,
    UsageCompletionTokensDetails,
    ChatCompletionChunk,
    ChunkChoice,
    DeltaMessage,
    DeltaToolCall,
    DeltaFunctionCall
)

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TOKEN_FILE = PROJECT_ROOT / "deepseek_token.json"
BASE_URL = "https://chat.deepseek.com"

class DeepSeekClient:
    def __init__(self, token_file: Path = TOKEN_FILE):
        self.token_file = token_file
        self._user_token: Optional[str] = None
        self._cookies: Dict[str, str] = {}
        self.load_credentials()

    def load_credentials(self):
        """Load user token and cookies from environment, .env file, or deepseek_token.json."""
        # 1. Check OS environment variable
        token = os.environ.get("DEEPSEEK_TOKEN")

        # 2. Check .env file if not in environment
        if not token:
            for env_path in [PROJECT_ROOT / ".env", Path(".env")]:
                if env_path.exists():
                    try:
                        with open(env_path, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith("DEEPSEEK_TOKEN="):
                                    val = line.split("=", 1)[1].strip().strip("'\"")
                                    if val:
                                        token = val
                                        break
                    except Exception as e:
                        print(f"[!] Warning: Failed to read .env file: {e}")
                if token:
                    break

        if token:
            self._user_token = token
            return

        # 3. Check deepseek_token.json file
        if self.token_file.exists():
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._user_token = data.get("userToken") or data.get("value") or data.get("token")
                        cookies_list = data.get("cookies", [])
                        for c in cookies_list:
                            if isinstance(c, dict) and "name" in c and "value" in c:
                                self._cookies[c["name"]] = c["value"]
                    elif isinstance(data, str):
                        self._user_token = data.strip()
            except Exception as e:
                print(f"[!] Warning: Failed to parse credentials file: {e}")

        if not self._user_token:
            print("[!] Warning: No DeepSeek userToken found. Run login.py, set DEEPSEEK_TOKEN, or add .env.")

    def get_headers(self, additional_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Host": "chat.deepseek.com",
            "Authorization": f"Bearer {self._user_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "x-client-platform": "web",
            "x-client-version": "2.3.0",
            "x-client-locale": "en_US",
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-timezone-offset": "19800",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"
        }
        if additional_headers:
            headers.update(additional_headers)
        return headers

    async def create_chat_session(self, http_client: httpx.AsyncClient) -> str:
        """Create a new chat session thread."""
        url = f"{BASE_URL}/api/v0/chat_session/create"
        res = await http_client.post(url, headers=self.get_headers(), json={}, timeout=15.0)
        res.raise_for_status()
        data = res.json()
        if data.get("code") != 0 or data.get("data", {}).get("biz_code") != 0:
            raise RuntimeError(f"Failed to create chat session: {data}")
        return data["data"]["biz_data"]["chat_session"]["id"]

    async def get_pow_challenge(self, http_client: httpx.AsyncClient, target_path: str = "/api/v0/chat/completion") -> dict:
        """Fetch PoW challenge for target endpoint."""
        url = f"{BASE_URL}/api/v0/chat/create_pow_challenge"
        res = await http_client.post(url, headers=self.get_headers(), json={"target_path": target_path}, timeout=15.0)
        res.raise_for_status()
        data = res.json()
        if data.get("code") != 0 or data.get("data", {}).get("biz_code") != 0:
            raise RuntimeError(f"Failed to get PoW challenge: {data}")
        return data["data"]["biz_data"]["challenge"]

    async def solve_pow(self, http_client: httpx.AsyncClient, target_path: str = "/api/v0/chat/completion") -> str:
        """Acquire and solve PoW challenge, returning base64 header string."""
        chal = await self.get_pow_challenge(http_client, target_path)
        sol = await solve_pow_async(
            algorithm=chal["algorithm"],
            challenge=chal["challenge"],
            salt=chal["salt"],
            difficulty=chal["difficulty"],
            expire_at=chal["expire_at"],
            signature=chal["signature"],
            target_path=chal.get("target_path", target_path)
        )
        return sol["header"]

    async def upload_file(
        self,
        http_client: httpx.AsyncClient,
        file_bytes: bytes,
        filename: str,
        content_type: str = "text/plain",
        to_model_type: str = "default"
    ) -> str:
        """Upload attachment (document or image) and fork to vision model if needed."""
        pow_header = await self.solve_pow(http_client, target_path="/api/v0/file/upload_file")
        url = f"{BASE_URL}/api/v0/file/upload_file"
        headers = self.get_headers({"x-ds-pow-response": pow_header})
        del headers["Content-Type"]  # Let httpx set multipart boundary

        files = {
            "file": (filename, file_bytes, content_type)
        }
        res = await http_client.post(url, headers=headers, files=files, timeout=60.0)
        res.raise_for_status()
        data = res.json()
        if data.get("code") != 0 or data.get("data", {}).get("biz_code") != 0:
            biz_msg = data.get("data", {}).get("biz_msg", "") or data.get("msg", "")
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"File upload failed: {biz_msg}", "type": "invalid_request_error", "code": "upload_failed"}}
            )
        
        file_id = data["data"]["biz_data"]["id"]

        # If target model is vision, fork to vision task for visual token encoding
        if to_model_type == "vision":
            try:
                fork_res = await http_client.post(
                    f"{BASE_URL}/api/v0/file/fork_file_task",
                    headers=self.get_headers(),
                    json={"file_id": file_id, "to_model_type": "vision"},
                    timeout=15.0
                )
                if fork_res.status_code == 200:
                    fork_data = fork_res.json()
                    forked_obj = fork_data.get("data", {}).get("biz_data", {})
                    if forked_obj and "id" in forked_obj:
                        file_id = forked_obj["id"]
            except Exception as e:
                print(f"[!] Warning: fork_file_task error: {e}")

        # Poll status until status is SUCCESS (up to 30s)
        last_status = "PENDING"
        for _ in range(30):
            try:
                poll_res = await http_client.get(
                    f"{BASE_URL}/api/v0/file/fetch_files?file_ids={file_id}",
                    headers=self.get_headers(),
                    timeout=10.0
                )
                if poll_res.status_code == 200:
                    poll_data = poll_res.json()
                    flist = poll_data.get("data", {}).get("biz_data", {}).get("files", [])
                    if flist:
                        last_status = flist[0].get("status", "UNKNOWN")
                        if last_status == "SUCCESS":
                            return file_id
                        elif last_status == "CONTENT_EMPTY":
                            raise HTTPException(
                                status_code=400,
                                detail={
                                    "error": {
                                        "message": "DeepSeek OCR could not extract any text from this image (the image appears to contain no readable text). For photos, graphics, or visual object analysis, please use 'deepseek-v4-vision' or 'deepseek-v4-vision-thinking'.",
                                        "type": "invalid_request_error",
                                        "code": "file_parse_empty"
                                    }
                                }
                            )
                        elif last_status in ("FAILED", "AUDIT_FAILED"):
                            err_c = flist[0].get("error_code") or last_status
                            raise HTTPException(
                                status_code=400,
                                detail={"error": {"message": f"DeepSeek file processing failed: {err_c}", "type": "invalid_request_error", "code": "file_parse_failed"}}
                            )
            except HTTPException:
                raise
            except Exception:
                pass
            await asyncio.sleep(1.0)

        if last_status != "SUCCESS":
            raise HTTPException(
                status_code=504,
                detail={"error": {"message": f"File parsing timed out while status is '{last_status}'. Please try again.", "type": "api_error", "code": "file_parse_timeout"}}
            )

        return file_id

    async def flatten_messages(
        self,
        http_client: httpx.AsyncClient,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        to_model_type: str = "default"
    ) -> Tuple[str, List[str]]:
        """
        Convert OpenAI multi-turn messages array into DeepSeek's prompt string.
        Extracts and uploads any inline image/file attachments.
        Injects tool definitions if provided.
        """
        file_ids: List[str] = []
        formatted_turns: List[str] = []
        system_prompts: List[str] = []

        if tools:
            tools_prompt = format_tools_system_prompt(tools)
            if tools_prompt:
                system_prompts.append(tools_prompt)

        for msg in messages:
            role = msg.role.lower()
            text_content = ""

            if isinstance(msg.content, str):
                text_content = msg.content
            elif isinstance(msg.content, list):
                parts = []
                for part in msg.content:
                    pdict = part.model_dump() if hasattr(part, "model_dump") else (part if isinstance(part, dict) else {})
                    ptype = pdict.get("type", "text")
                    if ptype == "text":
                        parts.append(pdict.get("text", ""))
                    elif ptype in ("image_url", "image", "input_image"):
                        img_obj = pdict.get("image_url") or pdict.get("image") or pdict.get("url") or {}
                        url_str = img_obj.get("url", "") if isinstance(img_obj, dict) else str(img_obj)
                        if url_str.startswith("data:"):
                            try:
                                header, base64_data = url_str.split(",", 1)
                                mime = header.split(";")[0].split(":")[1].lower().strip()
                                img_bytes = base64.b64decode(base64_data.strip())
                                ext = mime.split("/")[-1] if "/" in mime else "png"
                                if ext == "jpg":
                                    ext = "jpeg"
                                fid = await self.upload_file(http_client, img_bytes, f"image.{ext}", mime, to_model_type=to_model_type)
                                file_ids.append(fid)
                            except HTTPException:
                                raise
                            except Exception as e:
                                print(f"[!] Warning: Image upload error: {e}")
                                raise HTTPException(
                                    status_code=400,
                                    detail={"error": {"message": f"Failed to parse base64 image: {str(e)}", "type": "invalid_request_error", "code": "invalid_image_data"}}
                                )
                        elif url_str.startswith("http://") or url_str.startswith("https://"):
                            try:
                                img_resp = await http_client.get(url_str, timeout=30.0)
                                mime = img_resp.headers.get("content-type", "image/png").split(";")[0].lower().strip()
                                ext = mime.split("/")[-1] if "/" in mime else "png"
                                if ext == "jpg":
                                    ext = "jpeg"
                                fid = await self.upload_file(http_client, img_resp.content, f"image.{ext}", mime, to_model_type=to_model_type)
                                file_ids.append(fid)
                            except HTTPException:
                                raise
                            except Exception as e:
                                print(f"[!] Warning: Remote image download error: {e}")
                                raise HTTPException(
                                    status_code=400,
                                    detail={"error": {"message": f"Failed to download remote image from URL: {str(e)}", "type": "invalid_request_error", "code": "image_download_failed"}}
                                )
                    elif ptype in ("file", "file_data", "document"):
                        fid = pdict.get("file_id")
                        if fid:
                            file_ids.append(fid)
                        else:
                            raw_data = pdict.get("file_data") or pdict.get("content") or ""
                            filename = pdict.get("filename", "document.txt")
                            try:
                                guessed_mime, _ = mimetypes.guess_type(filename)
                                mime = guessed_mime or "text/plain"
                                if raw_data.startswith("data:"):
                                    header, b64 = raw_data.split(",", 1)
                                    if ";" in header and ":" in header:
                                        mime = header.split(";")[0].split(":")[1].strip()
                                    doc_bytes = base64.b64decode(b64.strip())
                                else:
                                    try:
                                        doc_bytes = base64.b64decode(raw_data.strip())
                                    except Exception:
                                        doc_bytes = raw_data.encode("utf-8")
                                fid = await self.upload_file(http_client, doc_bytes, filename, mime, to_model_type=to_model_type)
                                file_ids.append(fid)
                            except HTTPException:
                                raise
                            except Exception as e:
                                try:
                                    doc_text = doc_bytes.decode("utf-8", errors="ignore")
                                    parts.append(f"\n[Attached Document: {filename}]\n{doc_text}\n")
                                except Exception:
                                    pass
                    elif hasattr(part, "text") and part.text:
                        parts.append(part.text)
                text_content = "\n".join(parts)

            if role in ("system", "developer"):
                system_prompts.append(text_content)
            elif role == "user":
                formatted_turns.append(f"User: {text_content}")
            elif role == "assistant":
                if msg.tool_calls:
                    tc_xml = format_assistant_tool_calls(msg.tool_calls)
                    combined = f"{text_content}\n{tc_xml}".strip() if text_content else tc_xml
                    formatted_turns.append(f"Assistant: {combined}")
                else:
                    formatted_turns.append(f"Assistant: {text_content}")
            elif role == "tool":
                formatted_turns.append(f"Tool Response:\n<tool_response>\n{text_content}\n</tool_response>")

        # If single user message with no system instructions, no tools, and no file IDs
        if len(messages) == 1 and messages[0].role == "user" and isinstance(messages[0].content, str) and not file_ids and not tools:
            return messages[0].content, file_ids

        # Construct unified prompt
        prompt_parts = []
        if system_prompts:
            prompt_parts.append(f"[System Instructions]\n" + "\n\n".join(system_prompts))
        
        if formatted_turns:
            prompt_parts.append("\n\n".join(formatted_turns))

        final_prompt = "\n\n".join(prompt_parts).strip()
        return final_prompt, file_ids

    async def execute_completion(
        self,
        model_name: str,
        messages: List[ChatMessage],
        model_type: str,
        thinking_enabled: bool,
        search_enabled: bool = False,
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = None
    ) -> Any:
        """Main execution gateway supporting streaming and non-streaming responses with tool calling."""
        request_id = f"chatcmpl_{uuid.uuid4().hex}"
        created_ts = int(time.time())

        async with httpx.AsyncClient(timeout=180.0, cookies=self._cookies) as http_client:
            # 1. Create chat session
            chat_session_id = await self.create_chat_session(http_client)

            # 2. Process messages, tools & attachments
            prompt, ref_file_ids = await self.flatten_messages(http_client, messages, tools=tools, to_model_type=model_type)

            # 3. Solve PoW challenge
            pow_header = await self.solve_pow(http_client, target_path="/api/v0/chat/completion")

            # 4. Construct payload
            payload = {
                "chat_session_id": chat_session_id,
                "parent_message_id": None,
                "model_type": model_type,
                "prompt": prompt,
                "ref_file_ids": ref_file_ids,
                "thinking_enabled": thinking_enabled,
                "search_enabled": False  # Hardcoded to False per spec
            }

            headers = self.get_headers({"x-ds-pow-response": pow_header})
            completion_url = f"{BASE_URL}/api/v0/chat/completion"

            if stream:
                return self._stream_generator(
                    completion_url, headers, payload, model_name, request_id, created_ts
                )
            else:
                return await self._collect_response(
                    http_client, completion_url, headers, payload, model_name, request_id, created_ts, prompt
                )

    async def _stream_generator(
        self,
        url: str,
        headers: dict,
        payload: dict,
        model_name: str,
        request_id: str,
        created_ts: int
    ) -> AsyncGenerator[str, None]:
        """Translates DeepSeek JSON-patch SSE events into OpenAI chat.completion.chunk stream with tool calls."""
        first_chunk = ChatCompletionChunk(
            id=request_id,
            created=created_ts,
            model=model_name,
            choices=[
                ChunkChoice(
                    index=0,
                    delta=DeltaMessage(role="assistant", content=""),
                    finish_reason=None
                )
            ]
        )
        sent_first_chunk = False
        active_fragment_type = "RESPONSE"
        accumulated_reasoning = ""
        accumulated_content = ""
        streamed_content_len = 0
        accumulated_tokens = 0

        async with httpx.AsyncClient(timeout=180.0, cookies=self._cookies) as http_client:
            async with http_client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    err_text = await response.aread()
                    err_obj = {
                        "error": {
                            "message": f"DeepSeek upstream error: {err_text.decode('utf-8')}",
                            "type": "api_error",
                            "code": "upstream_error"
                        }
                    }
                    yield f"data: {json.dumps(err_obj)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                buffer = ""
                async for chunk_bytes in response.aiter_bytes():
                    buffer += chunk_bytes.decode("utf-8", errors="replace")
                    lines = buffer.split("\n")
                    buffer = lines.pop()

                    for line in lines:
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        
                        data_str = line[5:].strip()
                        if not data_str:
                            continue

                        try:
                            event_data = json.loads(data_str)
                        except Exception:
                            continue

                        # Check for upstream errors in stream
                        if event_data.get("type") == "error":
                            err_content = event_data.get("content", "DeepSeek upstream error")
                            finish_r = event_data.get("finish_reason", "")
                            err_type = "rate_limit_error" if (finish_r == "rate_limit_reached" or "frequent" in err_content.lower()) else "invalid_request_error"
                            err_code = "rate_limit_exceeded" if err_type == "rate_limit_error" else (finish_r or "upstream_error")
                            err_obj = {
                                "error": {
                                    "message": err_content,
                                    "type": err_type,
                                    "param": None,
                                    "code": err_code
                                }
                            }
                            yield f"data: {json.dumps(err_obj)}\n\n"
                            yield "data: [DONE]\n\n"
                            return

                        if not sent_first_chunk:
                            yield f"data: {first_chunk.model_dump_json(exclude_none=True)}\n\n"
                            sent_first_chunk = True

                        # 1. Check for initial fragment declaration
                        if "v" in event_data and isinstance(event_data["v"], dict):
                            resp_obj = event_data["v"].get("response", {})
                            frags = resp_obj.get("fragments", [])
                            if frags:
                                frag = frags[-1]
                                active_fragment_type = frag.get("type", "RESPONSE")
                                initial_txt = frag.get("content") or ""
                                if initial_txt:
                                    if active_fragment_type == "THINK":
                                        accumulated_reasoning += initial_txt
                                        chunk = ChatCompletionChunk(
                                            id=request_id,
                                            created=created_ts,
                                            model=model_name,
                                            choices=[ChunkChoice(index=0, delta=DeltaMessage(reasoning_content=initial_txt))]
                                        )
                                        yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
                                    else:
                                        accumulated_content += initial_txt
                                        safe_txt, streamed_content_len = get_safe_streamable_text(accumulated_content, streamed_content_len)
                                        if safe_txt:
                                            chunk = ChatCompletionChunk(
                                                id=request_id,
                                                created=created_ts,
                                                model=model_name,
                                                choices=[ChunkChoice(index=0, delta=DeltaMessage(content=safe_txt))]
                                            )
                                            yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
                            continue

                        # 2. Check for new fragment appended
                        p = event_data.get("p")
                        o = event_data.get("o")
                        v = event_data.get("v")

                        if p == "response/fragments" and o == "APPEND" and isinstance(v, list) and v:
                            frag = v[0]
                            active_fragment_type = frag.get("type", "RESPONSE")
                            initial_txt = frag.get("content") or ""
                            if initial_txt:
                                if active_fragment_type == "THINK":
                                    accumulated_reasoning += initial_txt
                                    chunk = ChatCompletionChunk(
                                        id=request_id,
                                        created=created_ts,
                                        model=model_name,
                                        choices=[ChunkChoice(index=0, delta=DeltaMessage(reasoning_content=initial_txt))]
                                    )
                                    yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
                                else:
                                    accumulated_content += initial_txt
                                    safe_txt, streamed_content_len = get_safe_streamable_text(accumulated_content, streamed_content_len)
                                    if safe_txt:
                                        chunk = ChatCompletionChunk(
                                            id=request_id,
                                            created=created_ts,
                                            model=model_name,
                                            choices=[ChunkChoice(index=0, delta=DeltaMessage(content=safe_txt))]
                                        )
                                        yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
                            continue

                        # 3. Check for text token updates
                        if (p == "response/fragments/-1/content" or p is None) and isinstance(v, str) and v:
                            if active_fragment_type == "THINK":
                                accumulated_reasoning += v
                                chunk = ChatCompletionChunk(
                                    id=request_id,
                                    created=created_ts,
                                    model=model_name,
                                    choices=[ChunkChoice(index=0, delta=DeltaMessage(reasoning_content=v))]
                                )
                                yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
                            elif active_fragment_type == "RESPONSE":
                                accumulated_content += v
                                safe_txt, streamed_content_len = get_safe_streamable_text(accumulated_content, streamed_content_len)
                                if safe_txt:
                                    chunk = ChatCompletionChunk(
                                        id=request_id,
                                        created=created_ts,
                                        model=model_name,
                                        choices=[ChunkChoice(index=0, delta=DeltaMessage(content=safe_txt))]
                                    )
                                    yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
                            continue

                        # 4. Check for token usage batch
                        if p == "response" and o == "BATCH" and isinstance(v, list):
                            for item in v:
                                if item.get("p") == "accumulated_token_usage":
                                    accumulated_tokens = int(item.get("v", 0))

        # Check for tool calls in accumulated_content
        tool_calls, cleaned_content = parse_deepseek_tool_calls(accumulated_content)

        if tool_calls:
            for i, tc in enumerate(tool_calls):
                # 1. Emit tool call start declaration chunk
                chunk_tc_start = ChatCompletionChunk(
                    id=request_id,
                    created=created_ts,
                    model=model_name,
                    choices=[
                        ChunkChoice(
                            index=0,
                            delta=DeltaMessage(
                                tool_calls=[
                                    DeltaToolCall(
                                        index=i,
                                        id=tc.id,
                                        type="function",
                                        function=DeltaFunctionCall(
                                            name=tc.function.name,
                                            arguments=""
                                        )
                                    )
                                ]
                            )
                        )
                    ]
                )
                yield f"data: {chunk_tc_start.model_dump_json(exclude_none=True)}\n\n"

                # 2. Emit tool call arguments chunk
                chunk_tc_args = ChatCompletionChunk(
                    id=request_id,
                    created=created_ts,
                    model=model_name,
                    choices=[
                        ChunkChoice(
                            index=0,
                            delta=DeltaMessage(
                                tool_calls=[
                                    DeltaToolCall(
                                        index=i,
                                        function=DeltaFunctionCall(
                                            arguments=tc.function.arguments
                                        )
                                    )
                                ]
                            )
                        )
                    ]
                )
                yield f"data: {chunk_tc_args.model_dump_json(exclude_none=True)}\n\n"

        reasoning_tokens = len(accumulated_reasoning) // 4
        completion_tokens = (len(accumulated_content) // 4) + reasoning_tokens
        prompt_tokens = len(payload.get("prompt", "")) // 4

        final_usage = UsageInfo(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            completion_tokens_details=UsageCompletionTokensDetails(
                reasoning_tokens=reasoning_tokens
            )
        )

        finish_reason = "tool_calls" if tool_calls else "stop"

        final_chunk = ChatCompletionChunk(
            id=request_id,
            created=created_ts,
            model=model_name,
            choices=[
                ChunkChoice(
                    index=0,
                    delta=DeltaMessage(),
                    finish_reason=finish_reason
                )
            ],
            usage=final_usage
        )
        yield f"data: {final_chunk.model_dump_json(exclude_none=True)}\n\n"
        yield "data: [DONE]\n\n"

    async def _collect_response(
        self,
        http_client: httpx.AsyncClient,
        url: str,
        headers: dict,
        payload: dict,
        model_name: str,
        request_id: str,
        created_ts: int,
        prompt: str
    ) -> ChatCompletionResponse:
        """Collect non-streaming response by aggregating DeepSeek SSE stream with tool calls."""
        accumulated_reasoning = ""
        accumulated_content = ""
        accumulated_tokens = 0
        active_fragment_type = "RESPONSE"

        async with http_client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code != 200:
                err_text = await response.aread()
                raise RuntimeError(f"DeepSeek upstream error {response.status_code}: {err_text.decode('utf-8')}")

            buffer = ""
            async for chunk_bytes in response.aiter_bytes():
                buffer += chunk_bytes.decode("utf-8", errors="replace")
                lines = buffer.split("\n")
                buffer = lines.pop()

                for line in lines:
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        event_data = json.loads(data_str)
                    except Exception:
                        continue

                    if event_data.get("type") == "error":
                        err_content = event_data.get("content", "DeepSeek upstream error")
                        finish_r = event_data.get("finish_reason", "")
                        if finish_r == "rate_limit_reached" or "frequent" in err_content.lower() or "rate limit" in err_content.lower():
                            raise HTTPException(
                                status_code=429,
                                detail={"error": {"message": err_content, "type": "rate_limit_error", "param": None, "code": "rate_limit_exceeded"}}
                            )
                        raise HTTPException(
                            status_code=400,
                            detail={"error": {"message": err_content, "type": "invalid_request_error", "param": None, "code": finish_r or "upstream_error"}}
                        )

                    if "v" in event_data and isinstance(event_data["v"], dict):
                        resp_obj = event_data["v"].get("response", {})
                        frags = resp_obj.get("fragments", [])
                        if frags:
                            frag = frags[-1]
                            active_fragment_type = frag.get("type", "RESPONSE")
                            txt = frag.get("content") or ""
                            if active_fragment_type == "THINK":
                                accumulated_reasoning += txt
                            else:
                                accumulated_content += txt
                        continue

                    p = event_data.get("p")
                    o = event_data.get("o")
                    v = event_data.get("v")

                    if p == "response/fragments" and o == "APPEND" and isinstance(v, list) and v:
                        frag = v[0]
                        active_fragment_type = frag.get("type", "RESPONSE")
                        txt = frag.get("content") or ""
                        if active_fragment_type == "THINK":
                            accumulated_reasoning += txt
                        else:
                            accumulated_content += txt
                        continue

                    if (p == "response/fragments/-1/content" or p is None) and isinstance(v, str) and v:
                        if active_fragment_type == "THINK":
                            accumulated_reasoning += v
                        elif active_fragment_type == "RESPONSE":
                            accumulated_content += v
                        continue

                    if p == "response" and o == "BATCH" and isinstance(v, list):
                        for item in v:
                            if item.get("p") == "accumulated_token_usage":
                                accumulated_tokens = int(item.get("v", 0))

        tool_calls, cleaned_content = parse_deepseek_tool_calls(accumulated_content)
        finish_reason = "tool_calls" if tool_calls else "stop"

        reasoning_tokens = len(accumulated_reasoning) // 4
        content_tokens = len(accumulated_content) // 4
        completion_tokens = content_tokens + reasoning_tokens
        prompt_tokens = len(prompt) // 4

        return ChatCompletionResponse(
            id=request_id,
            created=created_ts,
            model=model_name,
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatChoiceMessage(
                        role="assistant",
                        content=cleaned_content,
                        reasoning_content=accumulated_reasoning if accumulated_reasoning else None,
                        tool_calls=tool_calls if tool_calls else None
                    ),
                    finish_reason=finish_reason
                )
            ],
            usage=UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                completion_tokens_details=UsageCompletionTokensDetails(
                    reasoning_tokens=reasoning_tokens
                )
            )
        )
