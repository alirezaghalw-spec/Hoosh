import hmac
import json
import os
import time
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from pydantic import BaseModel

from sandbox import run_python

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
HOST_KEY = os.environ.get("HOST_API_KEY", "")
DEFAULT_MODEL = "openrouter/free"
MAX_TOOL_STEPS = 5

# سقف‌های ورودی
MAX_BODY = 400_000          # بایت
MAX_MSGS = 60               # تعداد پیام
MAX_MSG_CHARS = 20_000      # نویسه در هر پیام
MAX_TOTAL_CHARS = 120_000   # نویسه در کل
# محدودیت تلاش ناموفق: ۱۰ بار در ۱۰ دقیقه برای هر IP
FAIL_MAX = 10
FAIL_WINDOW = 600

# کلیدها را از دسترس پروسه‌های هم‌کاربر (مثل /proc/PID/environ) خارج می‌کند
try:
    import ctypes
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 0
except Exception:
    pass

if len(HOST_KEY) < 16:
    print("WARNING: HOST_API_KEY is empty or shorter than 16 characters. "
          "Use a long random value (empty means every request is rejected).")

# مستندات خودکار خاموش است تا ساختار API عمومی نباشد
app = FastAPI(title="AI Host", docs_url=None, redoc_url=None, openapi_url=None)

_cache = {"t": 0, "data": []}
_fails = defaultdict(deque)

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

EMPTY_AFTER_TOOL = "(مدل بعد از اجرای کد جوابی ننوشت؛ خروجی را در بخش ابزار ببین.)"


@app.middleware("http")
async def limit_body(request: Request, call_next):
    if request.method == "POST":
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = 0
        if size > MAX_BODY:
            return JSONResponse({"detail": "درخواست خیلی بزرگ است"}, status_code=413)
    return await call_next(request)


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


def client_ip(request: Request) -> str:
    # Render پشت پراکسی است؛ آخرین مقدار X-Forwarded-For را خود پراکسی اضافه می‌کند
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip() or "?"
    return request.client.host if request.client else "?"


def check_auth(request: Request, x_api_key: str = Header(default="")):
    ip, now = client_ip(request), time.time()
    if len(_fails) > 5000:
        _fails.clear()
    q = _fails[ip]
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    if len(q) >= FAIL_MAX:
        raise HTTPException(429, "تلاش‌های ناموفق زیاد بود؛ چند دقیقه بعد دوباره امتحان کن")
    ok = bool(HOST_KEY) and hmac.compare_digest(x_api_key.encode("utf-8"), HOST_KEY.encode("utf-8"))
    if not ok:
        q.append(now)
        raise HTTPException(401, "کلید نامعتبر")


def clean_messages(raw):
    """فقط نقش user/assistant با متن ساده؛ بقیه‌ی فیلدها دور ریخته می‌شود."""
    if not isinstance(raw, list) or not raw or len(raw) > MAX_MSGS:
        raise HTTPException(400, f"تعداد پیام‌ها باید بین ۱ و {MAX_MSGS} باشد")
    out, total = [], 0
    for m in raw:
        role = m.get("role") if isinstance(m, dict) else None
        content = m.get("content") if isinstance(m, dict) else None
        if role not in ("user", "assistant") or not isinstance(content, str):
            raise HTTPException(400, "پیام نامعتبر است")
        if len(content) > MAX_MSG_CHARS:
            raise HTTPException(400, "یک پیام بیش از حد بلند است")
        total += len(content)
        out.append({"role": role, "content": content})
    if total > MAX_TOTAL_CHARS:
        raise HTTPException(400, "مجموع پیام‌ها بیش از حد بلند است")
    if out[-1]["role"] != "user":
        raise HTTPException(400, "آخرین پیام باید از کاربر باشد")
    return out


def headers():
    if not API_KEY:
        raise HTTPException(500, "OPENROUTER_API_KEY تنظیم نشده")
    return {"Authorization": f"Bearer {API_KEY}"}


async def post_openrouter(payload):
    async with httpx.AsyncClient(timeout=120) as client:
        return await client.post(OPENROUTER_URL, json=payload, headers=headers())


def upstream_error(r):
    # ۴۰۱/۴۰۳ از OpenRouter نباید به شکل ۴۰۱ به مرورگر برسد، چون صفحه فکر می‌کند رمز میزبان غلط است
    status = 429 if r.status_code == 429 else 502
    return HTTPException(status, f"خطای OpenRouter ({r.status_code}): {r.text[:300]}")


# ---------------------------------------------------------------- استریم SSE

class UpstreamError(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status = status
        self.text = text


def ev(kind, **data):
    """یک رویداد SSE؛ هر رویداد یک خط JSON است."""
    return "data: " + json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n\n"


async def upstream_stream(payload):
    """تکه‌های JSON پاسخ جریانی OpenRouter را یکی‌یکی برمی‌گرداند."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=30)) as client:
        async with client.stream(
            "POST", OPENROUTER_URL, json=payload, headers=headers()
        ) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", errors="replace")
                raise UpstreamError(r.status_code, body[:300])
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue  # کامنت‌هایی مثل ": OPENROUTER PROCESSING"
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


def model_supports_tools(model, items):
    for m in items:
        if m.get("id") == model:
            return bool(m.get("tools"))
    return True  # ناشناخته (مثل openrouter/free)؛ اگر نپذیرد، بدون ابزار تکرار می‌شود


async def sse_events(model, messages, use_tools, opener=None, runner=None):
    """حلقهٔ گفتگو با ابزار؛ رویدادها را همزمان با رسیدن تولید می‌کند.
    رویدادها: delta, tool_start, tool_result, error, done.
    opener و runner فقط برای تست جایگزین می‌شوند."""
    opener = opener or upstream_stream
    runner = runner or run_python
    messages = list(messages)
    if use_tools:
        messages.insert(0, {"role": "system", "content": SYSTEM_HINT})
    tool_used = False
    total_text = False

    try:
        for step in range(MAX_TOOL_STEPS + 1):
            payload = {"model": model, "messages": messages, "stream": True}
            if use_tools and step < MAX_TOOL_STEPS:
                payload["tools"] = [RUN_PYTHON_TOOL]

            text_parts = []
            calls = {}
            for attempt in (0, 1):
                try:
                    async for ch in opener(payload):
                        if ch.get("error"):
                            raise UpstreamError(502, str(ch["error"])[:300])
                        choices = ch.get("choices") or [{}]
                        d = (choices[0] or {}).get("delta") or {}
                        t = d.get("content")
                        if isinstance(t, str) and t:
                            text_parts.append(t)
                            total_text = True
                            yield ev("delta", text=t)
                        for tc in d.get("tool_calls") or []:
                            i = tc.get("index", 0)
                            slot = calls.setdefault(i, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            f = tc.get("function") or {}
                            if f.get("name"):
                                slot["name"] = slot["name"] or f["name"]
                            if f.get("arguments"):
                                slot["args"] += f["arguments"]
                    break
                except UpstreamError as e:
                    nothing_yet = not text_parts and not calls
                    if attempt == 0 and "tools" in payload and not tool_used and nothing_yet:
                        # مدل از ابزار پشتیبانی نمی‌کند؛ بدون ابزار دوباره تلاش کن
                        use_tools = False
                        payload.pop("tools")
                        if messages and messages[0].get("role") == "system":
                            messages.pop(0)
                        continue
                    status = 429 if e.status == 429 else 502
                    yield ev("error", message=f"خطای OpenRouter ({e.status}): {e.text}", status=status)
                    return

            if calls and "tools" in payload:
                tool_used = True
                ordered = [calls[k] for k in sorted(calls)]
                tool_calls = []
                for n, c in enumerate(ordered):
                    tool_calls.append({
                        "id": c["id"] or f"call_{step}_{n}",
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["args"] or "{}"},
                    })
                messages.append({
                    "role": "assistant",
                    "content": "".join(text_parts),
                    "tool_calls": tool_calls,
                })
                for tcall in tool_calls:
                    fn = tcall["function"]
                    code = ""
                    if fn["name"] == "run_python":
                        try:
                            parsed = json.loads(fn["arguments"])
                            code = parsed.get("code", "") if isinstance(parsed, dict) else ""
                        except json.JSONDecodeError:
                            code = ""
                        if not isinstance(code, str):
                            code = ""
                        yield ev("tool_start", code=code)
                        output = await runner(code) if code else "کدی ارسال نشد"
                        yield ev("tool_result", code=code, output=output)
                    else:
                        output = "ابزار ناشناخته"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tcall["id"],
                        "content": output,
                    })
                continue

            if tool_used and not "".join(text_parts).strip():
                yield ev("delta", text=EMPTY_AFTER_TOOL)
            elif not total_text:
                yield ev("delta", text="(پاسخ خالی بود)")
            break
        yield ev("done")
    except Exception as e:  # خطای پیش‌بینی‌نشده نباید اتصال را بی‌صدا ببندد
        yield ev("error", message="خطای داخلی سرور: " + type(e).__name__, status=500)


@app.post("/chat", dependencies=[Depends(check_auth)])
async def chat(req: ChatRequest):
    if not req.model or len(req.model) > 100:
        raise HTTPException(400, "نام مدل نامعتبر است")
    messages = clean_messages(req.messages)

    # فقط مدل‌های رایگان مجازند، تا اعتبار خرج نشود
    items = []
    try:
        items = await free_models()
    except HTTPException:
        pass
    ids = {m["id"] for m in items}
    if not (req.model in ids or req.model.endswith(":free") or req.model == DEFAULT_MODEL):
        raise HTTPException(400, "فقط مدل‌های رایگان مجاز هستند")

    headers()  # اگر کلید OpenRouter تنظیم نشده، همین‌جا خطا بده

    if req.stream:
        use_tools = req.tools and model_supports_tools(req.model, items)
        return StreamingResponse(
            sse_events(req.model, messages, use_tools),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    use_tools = req.tools
    if use_tools:
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
            raise upstream_error(r)

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
                except (json.JSONDecodeError, AttributeError):
                    code = ""
                output = await run_python(code) if isinstance(code, str) and code else "کدی ارسال نشد"
                tool_log.append({"code": code if isinstance(code, str) else "", "output": output})
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
            m["content"] = EMPTY_AFTER_TOOL
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
  
