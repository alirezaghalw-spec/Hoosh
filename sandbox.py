"""اجرای محدود و ایزوله‌ی کد پایتون برای ابزار run_python.

لایه‌های محافظ:
  1. پروسه‌ی جدا با محیط (env) خالی، پس به کلیدهای OpenRouter دسترسی ندارد
  2. محدودیت CPU، حافظه (۲۵۶ مگابایت)، حجم فایل و تعداد فایل باز
  3. قلاب audit: شبکه، ساخت پروسه، ctypes و /proc و /sys ممنوع؛
     خواندن فقط از پوشه‌ی کار و کتابخانه‌های پایتون؛
     نوشتن، حذف، تغییر نام و ساخت پوشه فقط داخل پوشه‌ی کار
  4. سقف زمان ۱۰ ثانیه، سقف حجم خروجی و حداکثر ۲ اجرای هم‌زمان
  5. با workdir، کد در پوشه‌ی workspace اجرا می‌شود و import از همان پوشه ممکن است
توجه: این یک sandbox کامل (مثل کانتینر) نیست. قلاب audit داخل همان پروسه‌ی کد
کاربر اجرا می‌شود و فقط لایه‌ی دفاعی اضافه است، نه مرز امنیتی.
"""
import asyncio
import os
import shutil
import sys
import tempfile

TIMEOUT = 10          # ثانیه
MAX_OUT = 4000        # نویسه‌ی خروجی برگشتی به مدل
READ_LIMIT = 200_000  # بایت؛ بیشتر از این خوانده نمی‌شود
MAX_CODE = 100_000    # نویسه؛ بیشتر از این اجرا نمی‌شود
MEM_MB = 256          # سقف حافظه‌ی هر اجرا
MAX_PARALLEL = 2      # حداکثر اجرای هم‌زمان
QUEUE_WAIT = 20       # ثانیه؛ انتظار برای نوبت

WRAPPER = r'''
import sys, os, resource
sys.dont_write_bytecode = True
MEM = int(sys.argv[2]) * 1024 * 1024
resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_AS, (MEM, MEM))
resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

CWD = os.path.realpath(os.getcwd())
script = sys.argv[1]
src = open(script, encoding="utf-8").read()
sys.path.insert(0, CWD)

BLOCK_PREFIX = (
    "socket.", "subprocess.", "ctypes.", "pty.", "webbrowser.",
    "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.system",
    "os.kill", "os.chmod", "os.chown", "os.symlink", "os.link",
    "os.mkfifo", "os.mknod", "os.setxattr", "os.removexattr",
)
BLOCK_IMPORT = {"ctypes", "_ctypes", "pty", "_posixsubprocess"}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
SEP = os.sep

# رویدادهای تغییردهنده: (اندیس آرگومان‌های مسیر، اندیس dir_fd، سخت‌گیر؟)
# سخت‌گیر یعنی خود پوشه‌ی کار هم نباید هدف باشد.
MUTATE = {
    "os.remove": ((0,), (1,), True),
    "os.rmdir": ((0,), (1,), True),
    "os.rename": ((0, 1), (2, 3), True),
    "os.mkdir": ((0,), (2,), False),
    "os.truncate": ((0,), (), False),
    "os.utime": ((0,), (4,), False),
    "shutil.rmtree": ((0,), (1,), True),
    "shutil.move": ((0, 1), (), True),
    "shutil.copyfile": ((1,), (), False),
    "shutil.copymode": ((1,), (), False),
    "shutil.copystat": ((1,), (), False),
    "shutil.copytree": ((1,), (), False),
    "shutil.make_archive": ((0,), (), False),
    "shutil.unpack_archive": ((1,), (), False),
}


def _roots(paths):
    out = []
    for p in paths:
        try:
            r = os.path.realpath(p)
        except Exception:
            continue
        if r and r != SEP and r not in out:
            out.append(r)
    return tuple(out)


READ_ROOTS = _roots([
    CWD, os.path.dirname(os.path.realpath(script)),
    sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
    "/usr/lib", "/usr/local/lib", "/lib", "/usr/lib64", "/lib64",
    "/usr/share/zoneinfo",
])
READ_FILES = {"/dev/null", "/dev/urandom", "/dev/random", "/dev/zero", "/etc/localtime"}


def inside(p, roots):
    return any(p == r or p.startswith(r + SEP) for r in roots)


def as_path(p):
    if isinstance(p, bytes):
        p = os.fsdecode(p)
    return os.path.realpath(p) if isinstance(p, str) else None


def hook(event, args):
    if event.startswith(BLOCK_PREFIX):
        raise PermissionError("blocked: " + event)
    if event == "import" and args and args[0] in BLOCK_IMPORT:
        raise PermissionError("blocked import: " + str(args[0]))

    if event == "open":
        ap = as_path(args[0])
        if ap is None:
            return
        if ap.startswith(("/proc", "/sys")):
            raise PermissionError("blocked path: " + ap)
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        write = False
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            write = True
        if isinstance(flags, int) and flags & WRITE_FLAGS:
            write = True
        if write:
            if ap != "/dev/null" and not inside(ap, (CWD,)):
                raise PermissionError("write outside sandbox: " + ap)
        elif ap not in READ_FILES and not inside(ap, READ_ROOTS):
            raise PermissionError("read outside sandbox: " + ap)
        return

    rule = MUTATE.get(event)
    if rule:
        path_idx, fd_idx, strict = rule
        for i in fd_idx:
            if len(args) > i and isinstance(args[i], int) and args[i] not in (-1, -100):
                raise PermissionError("blocked: dir_fd in " + event)
        for i in path_idx:
            if len(args) <= i:
                continue
            ap = as_path(args[i])
            if ap is None:
                continue
            ok = ap.startswith(CWD + SEP) if strict else (ap == CWD or ap.startswith(CWD + SEP))
            if not ok:
                raise PermissionError("blocked outside sandbox: " + event)


sys.addaudithook(hook)
exec(compile(src, script, "exec"), {"__name__": "__main__", "__file__": script})
'''

_sem = None


def _get_sem():
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_PARALLEL)
    return _sem


async def run_python(code: str, workdir: str | None = None, timeout: int = TIMEOUT) -> str:
    """کد را اجرا می‌کند. اگر workdir داده شود، همان‌جا اجرا می‌شود و نوشتن فقط
    در همان پوشه مجاز است؛ وگرنه در یک پوشه‌ی موقت که بعد پاک می‌شود."""
    if len(code) > MAX_CODE:
        return "کد خیلی بلند است"
    sem = _get_sem()
    try:
        await asyncio.wait_for(sem.acquire(), QUEUE_WAIT)
    except asyncio.TimeoutError:
        return "سرور مشغول است؛ چند ثانیه بعد دوباره امتحان کن"
    try:
        return await _run(code, workdir, timeout)
    finally:
        sem.release()


async def _run(code, workdir, timeout):
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
            sys.executable, "-I", "-c", WRAPPER, script, str(MEM_MB),
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
