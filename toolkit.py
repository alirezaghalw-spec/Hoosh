"""ابزارها، فروشگاه و دستور روش کدنویسی میزبان.

ابزارهای داخلی: فایل‌های workspace، اجرای کد، ویرایش عکس.
افزونه‌ها (فروشگاه): «مهارت» = متن دستورالعمل؛ «ابزار» = کد پایتون که در sandbox اجرا می‌شود.
"""
import asyncio
import json
import os
import re

from sandbox import run_python

WORKSPACE = os.path.realpath(os.environ.get("WORKSPACE_DIR", "/tmp/hoosh_workspace"))
os.makedirs(WORKSPACE, exist_ok=True)

MAX_FILE = 200_000                 # بایت، برای write_file
MAX_WORKSPACE = 50 * 1024 * 1024   # کل workspace
MAX_IMG_PIXELS = 25_000_000
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# ---------------------------------------------------------------- دستور روش کار

CODING_METHOD = """تو «هوش» هستی، دستیار کدنویسی. به زبان کاربر (پیش‌فرض فارسی) جواب بده؛ کد و نام‌ها انگلیسی بمانند.

روش کار تو:
1. اول بفهم. خواسته را برای خودت در یک دو جمله روشن کن. اگر ابهامی هست که بدون جوابش کار اشتباه پیش می‌رود، فقط یک سؤال کوتاه بپرس. وگرنه یک فرض معقول بگیر و فرضت را در یک خط بگو.
2. قبل از تغییر، ببین چه هست. با list_files و read_file و search_code کد موجود را بخوان. کدی را که نخوانده‌ای ویرایش نکن.
3. نقشه‌ی کوتاه بکش (دو تا چهار قدم) و بعد انجام بده. تغییر کوچک و متمرکز؛ فقط آنچه خواسته شده. نه بازنویسی بی‌دلیل، نه ویژگی اضافه.
4. کد کامل و قابل‌اجرا بنویس، نه تکه‌های نیمه‌کاره یا «بقیه مثل قبل». برای فایل تازه write_file، برای تغییر کوچک edit_file. کد بلند را در فایل بنویس، نه در متن چت.
5. خودت امتحانش کن. بعد از نوشتن، با run_python اجرا کن و خروجی واقعی را ببین. اگر خطا داد، متن خطا را کامل بخوان، علت اصلی را پیدا کن (نه حدس)، رفع کن و دوباره اجرا کن.
6. راستگو باش. فقط وقتی بگو «کار می‌کند» که واقعاً اجرا و نتیجه را دیده‌ای. چیزی را که نتوانستی امتحان کنی صریح بگو. خروجی ابزار را هرگز از خودت نساز.
7. ساده و خوانا بنویس: نام‌های معنادار، تابع‌های کوچک، کامنت فقط برای «چرا». خطاهای محتمل را مدیریت کن.
8. امنیت: کلید و رمز را داخل کد نگذار، ورودی را اعتبارسنجی کن، eval روی ورودی کاربر و ساختن SQL با چسباندن رشته ممنوع.
9. جواب نهایی کوتاه باشد: چه کردی، چطور اجرا کنم، چه محدودیتی هست.

محدودیت ابزارها: run_python فقط کتابخانه‌ی استاندارد پایتون دارد، اینترنت ندارد، حداکثر ۱۰ ثانیه اجرا می‌شود و فایل‌ها فقط داخل workspace هستند. برای عکس‌ها از image_info و edit_image استفاده کن."""

# ---------------------------------------------------------------- مسیرها

def safe_path(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError("مسیر نامعتبر است")
    p = os.path.realpath(os.path.join(WORKSPACE, name.strip().lstrip("/")))
    if p == WORKSPACE or not p.startswith(WORKSPACE + os.sep):
        raise ValueError("مسیر باید یک فایل داخل workspace باشد")
    return p


def rel(p):
    return os.path.relpath(p, WORKSPACE)


def workspace_size():
    total = 0
    for root, _, files in os.walk(WORKSPACE):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def list_workspace(limit=200):
    rows = []
    for root, _, files in os.walk(WORKSPACE):
        for f in sorted(files):
            p = os.path.join(root, f)
            rows.append({"name": rel(p), "size": os.path.getsize(p)})
            if len(rows) >= limit:
                return rows
    return rows


def clean_filename(name):
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^\w.\-]", "_", name)[:80].lstrip(".") or "file"
    base, ext = os.path.splitext(name)
    final, i = name, 1
    while os.path.exists(os.path.join(WORKSPACE, final)):
        final = f"{base}_{i}{ext}"
        i += 1
    return final

# ---------------------------------------------------------------- ابزارهای فایل

def t_list_files(a):
    rows = list_workspace()
    return ("\n".join(f"{r['name']}  ({r['size']} bytes)" for r in rows) or "(workspace خالی است)"), []


def _is_binary(data):
    return b"\x00" in data[:2000]


def t_read_file(a):
    p = safe_path(a.get("path"))
    if not os.path.isfile(p):
        return "فایل پیدا نشد", []
    data = open(p, "rb").read(MAX_FILE * 2)
    if _is_binary(data):
        return "این فایل باینری است (برای عکس از image_info استفاده کن)", []
    lines = data.decode("utf-8", errors="replace").splitlines()
    start = max(1, int(a.get("start_line") or 1))
    end = int(a.get("end_line") or len(lines))
    out = "\n".join(f"{i}: {l}" for i, l in enumerate(lines[start - 1:end], start))
    return (out[:20000] or "(فایل خالی است)"), []


def t_write_file(a):
    p = safe_path(a.get("path"))
    content = a.get("content")
    if not isinstance(content, str):
        return "content باید متن باشد", []
    if len(content.encode()) > MAX_FILE:
        return f"فایل بزرگ‌تر از {MAX_FILE} بایت است", []
    if workspace_size() + len(content.encode()) > MAX_WORKSPACE:
        return "فضای workspace پر است", []
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return f"نوشته شد: {rel(p)} ({len(content.encode())} بایت)", [rel(p)]


def t_edit_file(a):
    p = safe_path(a.get("path"))
    if not os.path.isfile(p):
        return "فایل پیدا نشد", []
    old, new = a.get("old"), a.get("new")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        return "old و new باید متن باشند و old خالی نباشد", []
    text = open(p, encoding="utf-8").read()
    n = text.count(old)
    if n == 0:
        return "متن old در فایل پیدا نشد؛ دقیقاً همان‌طور که هست بنویس", []
    if n > 1:
        return f"متن old {n} بار پیدا شد؛ چند خط دور آن را هم بنویس تا یکتا شود", []
    with open(p, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new, 1))
    return f"ویرایش شد: {rel(p)}", [rel(p)]


def t_delete_file(a):
    p = safe_path(a.get("path"))
    if not os.path.isfile(p):
        return "فایل پیدا نشد", []
    os.remove(p)
    return f"حذف شد: {rel(p)}", []


def t_search_code(a):
    try:
        rx = re.compile(a.get("pattern", ""))
    except re.error as e:
        return f"الگو نامعتبر: {e}", []
    hits = []
    for root, _, files in os.walk(WORKSPACE):
        for f in sorted(files):
            p = os.path.join(root, f)
            if os.path.getsize(p) > 500_000:
                continue
            data = open(p, "rb").read()
            if _is_binary(data):
                continue
            for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel(p)}:{i}: {line.strip()[:160]}")
                    if len(hits) >= 50:
                        return "\n".join(hits), []
    return ("\n".join(hits) or "موردی پیدا نشد"), []


async def t_run_python(a):
    code = a.get("code")
    if not code and a.get("path"):
        p = safe_path(a["path"])
        if not os.path.isfile(p):
            return "فایل پیدا نشد", []
        code = open(p, encoding="utf-8").read()
    if not code:
        return "code یا path لازم است", []
    return await run_python(code, workdir=WORKSPACE), []

# ---------------------------------------------------------------- ویرایش عکس

def _num(v, lo, hi, name):
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} باید عدد باشد")
    if not lo <= v <= hi:
        raise ValueError(f"{name} باید بین {lo} و {hi} باشد")
    return v


def _open_image(path):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_IMG_PIXELS
    p = safe_path(path)
    if not os.path.isfile(p):
        raise ValueError("فایل پیدا نشد")
    im = Image.open(p)
    im.load()
    return im, p


def t_image_info(a):
    im, p = _open_image(a.get("path"))
    return f"{rel(p)}: {im.format} {im.size[0]}x{im.size[1]} حالت={im.mode}", []


def _apply(im, op):
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
    name = op.get("op")
    w, h = im.size
    rgb = lambda: im.convert("RGB")
    if name == "resize":
        if op.get("scale") is not None:
            s = _num(op["scale"], 0.01, 8, "scale")
            nw, nh = int(w * s), int(h * s)
        else:
            nw = int(_num(op["width"], 1, 6000, "width")) if op.get("width") else None
            nh = int(_num(op["height"], 1, 6000, "height")) if op.get("height") else None
            if not nw and not nh:
                raise ValueError("width یا height یا scale لازم است")
            nw = nw or max(1, round(w * nh / h))
            nh = nh or max(1, round(h * nw / w))
        if nw * nh > MAX_IMG_PIXELS:
            raise ValueError("اندازه‌ی نتیجه بیش از حد بزرگ است")
        return im.resize((max(1, nw), max(1, nh)), Image.LANCZOS)
    if name == "thumbnail":
        m = int(_num(op.get("max_size"), 1, 6000, "max_size"))
        out = im.copy()
        out.thumbnail((m, m))
        return out
    if name == "crop":
        l, t = int(_num(op.get("left", 0), 0, w, "left")), int(_num(op.get("top", 0), 0, h, "top"))
        r, b = int(_num(op.get("right", w), 1, w, "right")), int(_num(op.get("bottom", h), 1, h, "bottom"))
        if r <= l or b <= t:
            raise ValueError("ناحیه‌ی برش نامعتبر است")
        return im.crop((l, t, r, b))
    if name == "rotate":
        return im.rotate(_num(op.get("degrees"), -360, 360, "degrees"), expand=True)
    if name == "flip":
        return ImageOps.mirror(im) if op.get("direction", "horizontal") == "horizontal" else ImageOps.flip(im)
    if name == "grayscale":
        return ImageOps.grayscale(im)
    if name == "invert":
        return ImageOps.invert(rgb())
    if name == "sepia":
        return ImageOps.colorize(ImageOps.grayscale(im), "#2b1a0a", "#f1dfb8")
    if name in ("brightness", "contrast", "saturation"):
        cls = {"brightness": ImageEnhance.Brightness, "contrast": ImageEnhance.Contrast,
               "saturation": ImageEnhance.Color}[name]
        return cls(rgb()).enhance(_num(op.get("factor"), 0, 5, "factor"))
    if name == "blur":
        return im.filter(ImageFilter.GaussianBlur(_num(op.get("radius", 2), 0, 50, "radius")))
    if name == "sharpen":
        return im.filter(ImageFilter.SHARPEN)
    if name == "pixelate":
        k = int(_num(op.get("block", 8), 2, 128, "block"))
        small = im.resize((max(1, w // k), max(1, h // k)), Image.BOX)
        return small.resize((w, h), Image.NEAREST)
    if name == "palette":
        n = int(_num(op.get("colors", 16), 2, 64, "colors"))
        return rgb().quantize(colors=n, method=Image.MEDIANCUT).convert("RGB")
    if name == "text":
        txt = str(op.get("text", ""))[:200]
        size = int(_num(op.get("size", 24), 8, 200, "size"))
        out = im.convert("RGBA")
        d = ImageDraw.Draw(out)
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
        d.text((int(op.get("x", 10)), int(op.get("y", 10))), txt, fill=op.get("color", "white"), font=font)
        return out
    raise ValueError(f"عملیات ناشناخته: {name}")


def t_edit_image(a):
    im, src = _open_image(a.get("path"))
    ops = a.get("operations")
    if not isinstance(ops, list) or not ops or len(ops) > 12:
        return "operations باید لیستی از ۱ تا ۱۲ عملیات باشد", []
    for op in ops:
        if not isinstance(op, dict):
            return "هر عملیات باید یک شیء باشد", []
        im = _apply(im, op)
        if im.size[0] * im.size[1] > MAX_IMG_PIXELS:
            return "اندازه‌ی تصویر بیش از حد بزرگ شد", []
    stem = os.path.splitext(os.path.basename(src))[0]
    out_name = a.get("output") or f"{stem}_edited.png"
    ext = os.path.splitext(out_name)[1].lower()
    if ext not in IMG_EXT:
        return "پسوند خروجی باید png، jpg، webp، gif یا bmp باشد", []
    dst = safe_path(out_name)
    if ext in (".jpg", ".jpeg") and im.mode != "RGB":
        im = im.convert("RGB")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    im.save(dst)
    return f"ذخیره شد: {rel(dst)} ({im.size[0]}x{im.size[1]})", [rel(dst)]

# ---------------------------------------------------------------- تعریف ابزارهای داخلی

def _tool(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


S = {"type": "string"}
BUILTIN_TOOLS = [
    _tool("list_files", "فهرست فایل‌های workspace را نشان می‌دهد.", {}),
    _tool("read_file", "محتوای یک فایل متنی را با شماره‌ی خط می‌خواند.",
          {"path": S, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    _tool("write_file", "فایل جدید می‌سازد یا فایل موجود را کامل بازنویسی می‌کند.",
          {"path": S, "content": S}, ["path", "content"]),
    _tool("edit_file", "یک تکه‌ی دقیق و یکتا از فایل را با متن جدید عوض می‌کند.",
          {"path": S, "old": S, "new": S}, ["path", "old", "new"]),
    _tool("delete_file", "یک فایل را پاک می‌کند.", {"path": S}, ["path"]),
    _tool("search_code", "با عبارت باقاعده (regex) در همه‌ی فایل‌های متنی workspace جستجو می‌کند.",
          {"pattern": S}, ["pattern"]),
    _tool("run_python",
          "کد پایتون را در workspace اجرا می‌کند (یا فایل path را). فقط کتابخانه‌ی استاندارد، بدون اینترنت، "
          "حداکثر ۱۰ ثانیه. برای دیدن نتیجه print کن.",
          {"code": S, "path": S}),
    _tool("image_info", "ابعاد و نوع یک عکس در workspace را می‌گوید.", {"path": S}, ["path"]),
    _tool("edit_image",
          "عکس را با لیستی از عملیات ویرایش می‌کند و نسخه‌ی جدید می‌سازد. عملیات: "
          "resize{width,height|scale}, thumbnail{max_size}, crop{left,top,right,bottom}, rotate{degrees}, "
          "flip{direction:horizontal|vertical}, grayscale, invert, sepia, brightness/contrast/saturation{factor}, "
          "blur{radius}, sharpen, pixelate{block}, palette{colors}, text{text,x,y,size,color}.",
          {"path": S, "operations": {"type": "array", "items": {"type": "object"}}, "output": S},
          ["path", "operations"]),
]

_SYNC = {
    "list_files": t_list_files, "read_file": t_read_file, "write_file": t_write_file,
    "edit_file": t_edit_file, "delete_file": t_delete_file, "search_code": t_search_code,
    "image_info": t_image_info, "edit_image": t_edit_image,
}

# ---------------------------------------------------------------- افزونه‌ها

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
MAX_PLUGINS = 20


def validate_plugin(p):
    if not isinstance(p, dict):
        raise ValueError("افزونه باید یک شیء JSON باشد")
    pid, typ = p.get("id"), p.get("type")
    if not isinstance(pid, str) or not ID_RE.match(pid):
        raise ValueError("id افزونه نامعتبر است (حروف کوچک انگلیسی، عدد، - و _)")
    if typ not in ("skill", "tool"):
        raise ValueError("type باید skill یا tool باشد")
    out = {"id": pid, "type": typ,
           "name": str(p.get("name", pid))[:80],
           "description": str(p.get("description", ""))[:400],
           "version": str(p.get("version", "1.0"))[:20]}
    if typ == "skill":
        ins = p.get("instructions")
        if not isinstance(ins, str) or not ins.strip() or len(ins) > 8000:
            raise ValueError("instructions باید متنی تا ۸۰۰۰ نویسه باشد")
        out["instructions"] = ins
    else:
        code, params = p.get("code"), p.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(code, str) or not code.strip() or len(code) > 20000:
            raise ValueError("code باید کد پایتون تا ۲۰۰۰۰ نویسه باشد")
        if not isinstance(params, dict) or params.get("type") != "object" or len(json.dumps(params)) > 3000:
            raise ValueError("parameters باید یک JSON Schema از نوع object باشد")
        out["code"], out["parameters"] = code, params
    return out


def prepare_plugins(plugins):
    """خروجی: (تعریف ابزارها، متن مهارت‌ها، نگاشت نام ابزار به افزونه)"""
    if len(plugins or []) > MAX_PLUGINS:
        raise ValueError(f"حداکثر {MAX_PLUGINS} افزونه")
    defs, skills, pmap = [], [], {}
    for raw in plugins or []:
        p = validate_plugin(raw)
        if p["type"] == "skill":
            skills.append(f"### {p['name']}\n{p['instructions']}")
        else:
            fname = "p_" + p["id"].replace("-", "_")
            pmap[fname] = p
            defs.append({"type": "function", "function": {
                "name": fname, "description": p["description"] or p["name"], "parameters": p["parameters"]}})
    skills_text = ("## مهارت‌های فعال\n" + "\n\n".join(skills)) if skills else ""
    return defs, skills_text, pmap


async def call_tool(name, args, pmap):
    """اجرای یک ابزار؛ خروجی: (متن، فهرست فایل‌های ساخته‌شده)"""
    try:
        if name in pmap:
            pre = "import json\nARGS = json.loads(%s)\n" % json.dumps(json.dumps(args))
            return await run_python(pre + pmap[name]["code"], workdir=WORKSPACE), []
        if name == "run_python":
            return await t_run_python(args)
        if name in _SYNC:
            return await asyncio.to_thread(_SYNC[name], args)
        return f"ابزار ناشناخته: {name}", []
    except ValueError as e:
        return f"خطا: {e}", []
    except ImportError:
        return "خطا: کتابخانه‌ی Pillow نصب نیست (pillow را به requirements.txt اضافه کن)", []
    except Exception as e:  # نباید کل درخواست را خراب کند
        return f"خطای غیرمنتظره: {type(e).__name__}: {e}", []

# ---------------------------------------------------------------- فروشگاه

def _skill(pid, name, desc, text):
    return {"id": pid, "type": "skill", "name": name, "description": desc, "version": "1.0", "instructions": text}


def _tool_plugin(pid, name, desc, props, required, code):
    return {"id": pid, "type": "tool", "name": name, "description": desc, "version": "1.0",
            "parameters": {"type": "object", "properties": props, "required": required},
            "code": code.strip() + "\n"}


CATALOG = [
    _skill("clean-python", "پایتون تمیز", "قواعد نوشتن پایتون خوانا و استاندارد",
           "هر وقت پایتون می‌نویسی: از type hint برای ورودی و خروجی تابع‌ها استفاده کن، تابع‌ها را کوتاه و تک‌کاره نگه دار، "
           "از f-string و pathlib استفاده کن، خطاهای مشخص را بگیر (نه except خالی)، و بالای هر فایل فقط importهای لازم را بگذار."),
    _skill("code-review", "بازبینی کد", "بازبینی کد با چک‌لیست باگ، امنیت و خوانایی",
           "وقتی از تو بازبینی کد خواستند: اول کد را با read_file بخوان. بعد فهرست کن: ۱) باگ‌های قطعی، ۲) مشکل امنیتی، "
           "۳) موارد لبه‌ای که کد را می‌شکنند، ۴) پیشنهاد خوانایی. هر مورد را با شماره‌ی خط و راه‌حل کوتاه بنویس. "
           "اگر مشکل مهمی ندیدی، صریح بگو و چیز ساختگی پیدا نکن."),
    _skill("debug-detective", "کارآگاه باگ", "روش گام‌به‌گام پیدا کردن و رفع باگ",
           "برای رفع باگ: ۱) خطا را با run_python بازتولید کن. ۲) متن خطا و خط مشکل را بخوان. ۳) یک فرضیه بساز و با print یا "
           "تست کوچک بیازمایش، نه حدس. ۴) فقط علت اصلی را رفع کن. ۵) دوباره اجرا کن و ثابت کن درست شد. ۶) یک جمله بگو علت چه بود."),
    _skill("test-writer", "نویسنده تست", "نوشتن و اجرای تست با unittest",
           "وقتی تست خواستند: با ماژول unittest (کتابخانه‌ی استاندارد) فایل test_*.py بساز. حالت عادی، حالت‌های لبه‌ای "
           "(ورودی خالی، صفر، منفی) و خطاها را پوشش بده. بعد با run_python اجرا کن "
           "(مثلاً import unittest; unittest.main(module='test_x', exit=False)) و نتیجه‌ی واقعی را گزارش کن."),
    _skill("simple-explainer", "توضیح‌دهنده ساده", "توضیح کد به فارسی ساده برای مبتدی",
           "وقتی از تو توضیح کد خواستند: اول در یک جمله بگو کد چه کاری می‌کند. بعد بخش‌به‌بخش و با زبان ساده و مثال روزمره "
           "توضیح بده. اصطلاح فنی را هر جا آوردی، یک بار معنی کن. در آخر یک تمرین کوچک پیشنهاد بده."),
    _skill("pixel-artist", "پیکسل‌آرتیست", "ساخت تصویر پیکسلی با ابزار edit_image",
           "برای تبدیل عکس به پیکسل‌آرت: ابتدا image_info بزن. بعد edit_image با این ترتیب: resize برای کوچک کردن (مثلاً عرض ۶۴)، "
           "palette با ۱۶ رنگ، و در آخر resize با scale بزرگ (مثلاً ۸) تا پیکسل‌ها درشت دیده شوند. خروجی را png ذخیره کن."),

    _
