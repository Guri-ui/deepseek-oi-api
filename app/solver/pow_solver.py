import json
import subprocess
import asyncio
from pathlib import Path

POW_JS_PATH = Path(__file__).parent / "pow_solver.js"

def solve_pow_sync(algorithm: str, challenge: str, salt: str, difficulty: int, expire_at: int, signature: str, target_path: str = "/api/v0/chat/completion") -> dict:
    """
    Synchronously solve DeepSeek PoW challenge using the local WebAssembly engine.
    Returns dict with 'answer' and base64 'header'.
    """
    cmd = [
        "node",
        str(POW_JS_PATH),
        algorithm,
        challenge,
        salt,
        str(difficulty),
        str(expire_at),
        signature,
        target_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout.strip())

async def solve_pow_async(algorithm: str, challenge: str, salt: str, difficulty: int, expire_at: int, signature: str, target_path: str = "/api/v0/chat/completion") -> dict:
    """
    Asynchronously solve DeepSeek PoW challenge.
    """
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(POW_JS_PATH),
        algorithm,
        challenge,
        salt,
        str(difficulty),
        str(expire_at),
        signature,
        target_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"PoW solve failed: {stderr.decode('utf-8')}")
    return json.loads(stdout.decode('utf-8').strip())
