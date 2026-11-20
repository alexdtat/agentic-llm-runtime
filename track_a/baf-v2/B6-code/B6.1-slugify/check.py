"""Compare function outputs locally after executing the submission in an isolated sandbox."""
import base64
import json
import stat
from pathlib import Path


CASES = (
    ("Mixed CASE_42", "mixed-case-42"),
    (" one---two !! three ", "one-two-three"),
    ("école 東京 2026-ITMO", "cole-2026-itmo"),
    ("---_?!", ""),
    ("", ""),
)
MAX_SOURCE_BYTES = 16 * 1024


def verdict(ok, reason, *, outcome=None):
    return {"pass": ok, "reason": reason, "outcome": outcome or ("pass" if ok else "fail")}


def isolated_program(source, text):
    encoded = base64.b64encode(source).decode("ascii")
    return f"""
import base64
import contextlib
import io
import json

result = None
try:
    submission = {{'__name__': 'slug', '__file__': 'slug.py'}}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(base64.b64decode({encoded!r}), 'slug.py', 'exec'), submission)
        output = submission['slugify']({text!r})
        if type(output) is str:
            result = output
except Exception:
    pass
print(json.dumps(result))
"""


def check_isolated(workdir, run_python):
    """Use a trusted callback that runs Python in a fresh networkless sandbox."""
    path = Path(workdir) / "slug.py"
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return verdict(False, "slug.py должен быть обычным файлом")
        with path.open("rb") as stream:
            source = stream.read(MAX_SOURCE_BYTES + 1)
    except FileNotFoundError:
        return verdict(False, "slug.py не создан")
    except OSError:
        return verdict(False, "Не удалось прочитать slug.py", outcome="infra_error")
    if len(source) > MAX_SOURCE_BYTES:
        return verdict(False, "Размер slug.py превышает 16 KiB")
    for text, expected in CASES:
        try:
            stdout = run_python(isolated_program(source, text))
        except (OSError, RuntimeError):
            return verdict(False, "Изолированная проверка не завершилась", outcome="infra_error")
        try:
            output = json.loads(stdout)
        except (TypeError, ValueError):
            return verdict(False, "Изолированная проверка вернула неверный формат")
        if output != expected:
            return verdict(False, "slugify не прошла скрытые тесты")
    return verdict(True, "ok")


if __name__ == "__main__":
    result = verdict(False, "Требуется изолированный исполнитель для скрытых тестов", outcome="infra_error")
    print(json.dumps(result, ensure_ascii=False))
