"""
Tool Calling Parser and Formatter for DeepSeek OpenAI Compatibility.
Translates between OpenAI native tool_calls schemas and DeepSeek XML tool conventions.
"""

import re
import json
import uuid
from typing import List, Tuple, Dict, Any, Optional
from app.models import ToolCall, FunctionCall, DeltaToolCall, DeltaFunctionCall

DEEPSEEK_TOOL_SYSTEM_PROMPT = """
# Tools

You have access to the following tools:
```json
{tools_json}
```

To call a tool, respond ONLY with a tool call in the following XML format:
<tool_calls>
<invoke name="$TOOL_NAME">
<parameter name="$PARAM_NAME">$PARAM_VALUE</parameter>
...
</invoke>
</tool_calls>
"""

def format_tools_system_prompt(tools: List[Dict[str, Any]]) -> str:
    """Format OpenAI tools array into DeepSeek tool instruction block."""
    if not tools:
        return ""
    tools_json = json.dumps(tools, indent=2, ensure_ascii=False)
    return DEEPSEEK_TOOL_SYSTEM_PROMPT.format(tools_json=tools_json).strip()

def parse_deepseek_tool_calls(text: str) -> Tuple[List[ToolCall], Optional[str]]:
    """
    Extracts DeepSeek XML / JSON tool calls from model output text.
    Returns (list_of_tool_calls, cleaned_text_content).
    """
    if not text:
        return [], None

    tool_calls: List[ToolCall] = []
    clean_text = text

    # Pattern 1: <tool_calls>...</tool_calls> or standalone <invoke name="...">...</invoke>
    tool_blocks = re.findall(r'<tool_calls>(.*?)</tool_calls>', text, re.DOTALL)
    if not tool_blocks:
        if '<invoke' in text and '</invoke>' in text:
            tool_blocks = [text]

    if tool_blocks:
        for block in tool_blocks:
            invokes = re.findall(r'<invoke\s+name=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</invoke>', block, re.DOTALL)
            for name, body in invokes:
                params = {}
                param_matches = re.findall(r'<parameter\s+name=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</parameter>', body, re.DOTALL)
                for p_name, p_val in param_matches:
                    p_val_clean = p_val.strip()
                    try:
                        # Parse numbers, booleans, arrays, nested dicts
                        parsed_val = json.loads(p_val_clean)
                        params[p_name] = parsed_val
                    except Exception:
                        params[p_name] = p_val_clean

                call_id = f"call_{uuid.uuid4().hex[:8]}"
                tool_calls.append(
                    ToolCall(
                        id=call_id,
                        type="function",
                        function=FunctionCall(
                            name=name.strip(),
                            arguments=json.dumps(params, ensure_ascii=False)
                        )
                    )
                )

        clean_text = re.sub(r'<tool_calls>.*?</tool_calls>', '', clean_text, flags=re.DOTALL).strip()
        clean_text = re.sub(r'<invoke\s+name=[\"\'][^\"\']+[\"\'][^>]*>.*?</invoke>', '', clean_text, flags=re.DOTALL).strip()

    # Pattern 2: Markdown ```tool_call or ```json tool call block
    if not tool_calls:
        json_blocks = re.findall(r'```(?:tool_call|json)?\s*(\{\s*\"name\"\s*:.*?)\s*```', clean_text, re.DOTALL)
        for jb in json_blocks:
            try:
                data = json.loads(jb)
                if isinstance(data, dict) and "name" in data and "arguments" in data:
                    args = data["arguments"]
                    args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                    call_id = f"call_{uuid.uuid4().hex[:8]}"
                    tool_calls.append(
                        ToolCall(
                            id=call_id,
                            type="function",
                            function=FunctionCall(
                                name=data["name"].strip(),
                                arguments=args_str
                            )
                        )
                    )
                    clean_text = clean_text.replace(jb, "").strip()
            except Exception:
                pass

    clean_result = clean_text.strip() if clean_text.strip() else None
    return tool_calls, clean_result

def format_assistant_tool_calls(tool_calls: List[Any]) -> str:
    """Format OpenAI assistant tool_calls back into DeepSeek XML for multi-turn history."""
    if not tool_calls:
        return ""
    invokes = []
    for tc in tool_calls:
        tcdict = tc.model_dump() if hasattr(tc, "model_dump") else (tc if isinstance(tc, dict) else {})
        func = tcdict.get("function", {})
        name = func.get("name", "tool")
        args_raw = func.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except Exception:
            args = {}
        
        param_lines = []
        if isinstance(args, dict):
            for k, v in args.items():
                v_str = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool, int, float)) else str(v)
                param_lines.append(f'<parameter name="{k}">{v_str}</parameter>')
        
        body = "\n".join(param_lines)
        invokes.append(f'<invoke name="{name}">\n{body}\n</invoke>')

    return "<tool_calls>\n" + "\n".join(invokes) + "\n</tool_calls>"

def get_safe_streamable_text(accumulated: str, streamed_len: int) -> Tuple[str, int]:
    """
    Determines how much text can be safely streamed to delta.content
    without leaking partial or full tool call XML tags.
    """
    tag_starts = ["<tool_calls", "<invoke", "<function_calls", "```tool_call"]
    tag_idx = -1
    for ts in tag_starts:
        idx = accumulated.find(ts)
        if idx != -1:
            if tag_idx == -1 or idx < tag_idx:
                tag_idx = idx

    if tag_idx != -1:
        if tag_idx > streamed_len:
            safe_text = accumulated[streamed_len:tag_idx]
            return safe_text, tag_idx
        else:
            return "", streamed_len

    # Check if trailing characters are the start of a potential tag (e.g. "<", "<tool")
    last_lt = accumulated.rfind("<")
    if last_lt != -1 and last_lt >= streamed_len:
        potential_tag = accumulated[last_lt:]
        if any(ts.startswith(potential_tag) for ts in tag_starts):
            safe_text = accumulated[streamed_len:last_lt]
            return safe_text, last_lt

    safe_text = accumulated[streamed_len:]
    return safe_text, len(accumulated)
