"""اجرای محدود و ایزوله‌ی کد پایتون برای ابزار run_python.

لایه‌های محافظ:
  1. پروسه‌ی جدا با محیط (env) خالی، پس به کلیدهای OpenRouter دسترسی ندارد
  2. محدودیت CPU، حافظه، حجم فایل و تعداد فایل باز
  3. قلاب audit: شبکه، ساخت پروسه، ctypes و خواندن /proc و /sys ممنوع؛
     نوشتن فقط داخل پوشه‌ی موقت
  4. سقف زمان ۱۰ ثانیه و سقف حجم خروجی
  5. با workdir، کد در پوشه‌ی workspace اجرا می‌شود و import از همان پوشه ممکن است
توجه: این یک sandbox کامل (مثل کانتینر) نیست؛ لایه‌ی دفاعی اضافه است.
"""
import asyncio
import os
import shutil
import sys
import tempfile

TIMEOUT = 10          # ثانیه
MAX_OUT = 4000        # نویسه‌ی خروجی برگشتی به مدل
READ_LIMIT = 200_000  # بایت؛ بیشتر از این خوانده نمی‌شود

WRAPPER = r'''
import sys, os, resource
sys.dont_write_bytecode = True
resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

CWD = os.path.realpath(os.getcwd())
script = sys.argv[1]
src = open(script, encoding="utf-8").read()
sys.path.insert(0, CWD)

BLOCK_PREFIX = (
    "socket.", "subprocess.", "ctypes.", "pty.", "webbrowser.",
    "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.system",
    "os.kill", "os.chmod", "os.chown", "os.symlink", "os.link",
)
BLOCK_IMPORT = {"ctypes", "_ctypes", "pty", "_posixsubprocess"}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def hook(event, args):
    if event.startswith(BLOCK_PREFIX):
        raise PermissionError("blocked: " + event)
    if event == "import" and args and args[0] in BLOCK_IMPORT:
        raise PermissionError("blocked import: " + str(args[0]))
    if event == "open":
        p = args[0]
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if isinstance(p, str):
            ap = os.path.realpath(p)
            if ap.startswith(("/proc", "/sys")):
                raise PermissionError("blocked path: " + p)
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            write = False
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                write = True
            if isinstance(flags, int) and flags & WRITE_FLAGS:
                write = True
            if write and ap != "/dev/null" and not (ap == CWD or ap.startswith(CWD + os.sep)):
                raise PermissionError("write outside sandbox: " + p)


sys.addaudithook(hook)
exec(compile(src, script, "exec"), {"__name__": "__main__", "__file__": script})
'''


async def run_python(code: str, workdir: str | None = None, timeout: int = TIMEOUT) -> str:
    """کد را اجرا می‌کند. اگر workdir داده شود، همان‌جا اجرا می‌شود و نوشتن فقط
    در همان پوشه مجاز است؛ وگرنه در یک پوشه‌ی موقت که بعد پاک می‌شود."""
    tmp = tempfile.mkdtemp(prefix="sbx_")
    cwd = workdir or tmp
    buf = bytearray()
    timed_out = False
    try:
        script = os.path.join(tmp, "main.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)

        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": cwd,
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", WRAPPER, script,
            cwd=cwd, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def reader():
            while len(buf) < READ_LIMIT:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)

        try:
            await asyncio.wait_for(reader(), timeout)
        except asyncio.TimeoutError:
            timed_out = True

        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()

        out = buf.decode("utf-8", errors="replace")
        if len(out) > MAX_OUT:
            out = out[:MAX_OUT] + "\n...[خروجی بریده شد]"
        if timed_out:
            out += f"\n[متوقف شد: زمان اجرا بیش از {timeout} ثانیه]"
        elif proc.returncode in (-9, -24):
            out += "\n[متوقف شد: محدودیت زمان یا منابع]"
        elif proc.returncode not in (0, None):
            out += f"\n[کد خروج: {proc.returncode}]"
        return out.strip() or "(بدون خروجی؛ برای دیدن نتیجه از print استفاده کن)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
