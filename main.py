import json
import os
import time
import httpx
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from sandbox import run_python

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
HOST_KEY = os.environ.get("HOST_API_KEY", "")
DEFAULT_MODEL = "openrouter/free"
MAX_TOOL_STEPS = 5

# کلیدها را از دسترس پروسه‌های هم‌کاربر (مثل /proc/PID/environ) خارج می‌کند
try:
    import ctypes
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 0
except Exception:
    pass

app = FastAPI(title="AI Host")

_cache = {"t": 0, "data": []}

RUN_PYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": (
            "کد پایتون را اجرا می‌کند و خروجی print را برمی‌گرداند. "
            "فقط کتابخانه‌ی استاندارد پایتون؛ بدون اینترنت و بدون دسترسی به فایل‌های بیرون. "
            "حداکثر ۱۰ ثانیه. برای دیدن نتیجه حتماً print کن."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "کد کامل پایتون"}},
            "required": ["code"],
        },
    },
}

SYSTEM_HINT = (
    "تو یک دستیار هستی. اگر محاسبه، آزمایش کد یا پردازش داده لازم بود، "
    "از ابزار run_python استفاده کن و نتیجه‌ی واقعی را به کاربر بگو. "
    "اگر نیازی نبود، مستقیم جواب بده."
)


async def free_models():
    """لیست مدل‌های رایگان OpenRouter (یک ساعت کش می‌شود)."""
    if not _cache["data"] or time.time() - _cache["t"] > 3600:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(MODELS_URL)
        if r.status_code != 200:
            raise HTTPException(502, "دریافت لیست مدل‌ها ناموفق بود")
        items = []
        for m in r.json().get("data", []):
            p = m.get("pricing") or {}
            if str(p.get("prompt")) == "0" and str(p.get("completion")) == "0":
                items.append({
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                    "tools": "tools" in (m.get("supported_parameters") or []),
                })
        items.sort(key=lambda x: x["id"])
        _cache["data"] = items
        _cache["t"] = time.time()
    return _cache["data"]


class ChatRequest(BaseModel):
    messages: list[dict]
    model: str = DEFAULT_MODEL
    stream: bool = False
    tools: bool = True  # ابزار اجرای کد


def check_auth(x_api_key: str = Header(default="")):
    if not HOST_KEY or x_api_key != HOST_KEY:
        raise HTTPException(401, "کلید نامعتبر")


def headers():
    if not API_KEY:
        raise HTTPException(500, "OPENROUTER_API_KEY تنظیم نشده")
    return {"Authorization": f"Bearer {API_KEY}"}


async def post_openrouter(payload):
    async with httpx.AsyncClient(timeout=120) as client:
        return await client.post(OPENROUTER_URL, json=payload, headers=headers())


@app.post("/chat", dependencies=[Depends(check_auth)])
async def chat(req: ChatRequest):
    # فقط مدل‌های رایگان مجازند، تا اعتبار خرج نشود
    ids = set()
    try:
        ids = {m["id"] for m in await free_models()}
    except HTTPException:
        pass
    if not (req.model in ids or req.model.endswith(":free") or req.model == DEFAULT_MODEL):
        raise HTTPException(400, "فقط مدل‌های رایگان مجاز هستند")

    if req.stream:  # حالت جریانی بدون ابزار
        payload = {"model": req.model, "messages": req.messages, "stream": True}

        async def gen():
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", OPENROUTER_URL, json=payload, headers=headers()
                ) as r:
                    async for chunk in r.aiter_raw():
                        yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    messages = list(req.messages)
    use_tools = req.tools
    if use_tools and not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": SYSTEM_HINT})

    tool_log = []
    data = None
    for step in range(MAX_TOOL_STEPS + 1):
        payload = {"model": req.model, "messages": messages}
        if use_tools and step < MAX_TOOL_STEPS:
            payload["tools"] = [RUN_PYTHON_TOOL]

        r = await post_openrouter(payload)
        if r.status_code != 200 and "tools" in payload and not tool_log:
            # مدل از ابزار پشتیبانی نمی‌کند؛ بدون ابزار دوباره تلاش کن
            use_tools = False
            payload.pop("tools")
            r = await post_openrouter(payload)
        if r.status_code != 200:
            raise HTTPException(r.status_code, r.text)

        data = r.json()
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls")
        if not calls or "tools" not in payload:
            break

        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": calls,
        })
        for c in calls:
            fn = c.get("function", {})
            if fn.get("name") == "run_python":
                try:
                    code = json.loads(fn.get("arguments") or "{}").get("code", "")
                except json.JSONDecodeError:
                    code = ""
                output = await run_python(code) if code else "کدی ارسال نشد"
                tool_log.append({"code": code, "output": output})
            else:
                output = "ابزار ناشناخته"
            messages.append({
                "role": "tool",
                "tool_call_id": c.get("id", ""),
                "content": output,
            })

    if tool_log:
        data["tool_log"] = tool_log
        m = data["choices"][0]["message"]
        if not (m.get("content") or "").strip():
            m["content"] = "(مدل بعد از اجرای کد جوابی ننوشت؛ خروجی را در بخش ابزار ببین.)"
    return data


@app.get("/models")
async def models():
    return {"default": DEFAULT_MODEL, "models": await free_models()}


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
