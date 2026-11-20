"""Run submitted code via the isolated executor and compare observations locally."""
import base64
import json
import stat
from pathlib import Path

CASES = (
    ([], {"value": []}),
    ([(3, 8), (0, 1), (1, 3), (2, 4)], {"value": [[0, 8]]}),
    ([(99, 99), (-2, 0), (-9, -4), (3, 5), (-4, -3)], {"value": [[-9, -3], [-2, 0], [3, 5]]}),
    ([(3, 3), (0, 0)], {"value": []}),
    ([(0, 2), (11, 7)], {"error": "ValueError"}),
)
MAX_SOURCE_BYTES = 16 * 1024


def verdict(passed, reason, *, outcome=None):
    return {"pass": passed, "reason": reason, "outcome": outcome or ("pass" if passed else "fail")}


def isolated_program(source, ranges):
    encoded = base64.b64encode(source).decode("ascii")
    return f"""
import base64
import contextlib
import io
import json

ranges = {ranges!r}
original = ranges.copy()
result = None
try:
    submission = {{'__name__': 'join_ranges', '__file__': 'join_ranges.py'}}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(base64.b64decode({encoded!r}), 'join_ranges.py', 'exec'), submission)
        function = submission['join_ranges']
        try:
            value = function(ranges)
        except ValueError:
            result = {{'error': 'ValueError'}}
        else:
            if type(value) is list and all(
                type(pair) is tuple and len(pair) == 2
                and all(type(endpoint) is int for endpoint in pair)
                for pair in value
            ):
                result = {{'value': value}}
except Exception:
    pass
if ranges != original:
    result = None
print(json.dumps(result))
"""


def check_isolated(workdir, run_python):
    """Use a trusted callback that runs Python in a fresh networkless sandbox."""
    path = Path(workdir) / "join_ranges.py"
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return verdict(False, "join_ranges.py должен быть обычным файлом")
        with path.open("rb") as stream:
            source = stream.read(MAX_SOURCE_BYTES + 1)
    except FileNotFoundError:
        return verdict(False, "join_ranges.py не создан")
    except OSError:
        return verdict(False, "Не удалось прочитать join_ranges.py", outcome="infra_error")
    if len(source) > MAX_SOURCE_BYTES:
        return verdict(False, "Размер join_ranges.py превышает 16 KiB")
    for ranges, expected in CASES:
        try:
            stdout = run_python(isolated_program(source, ranges))
        except (OSError, RuntimeError):
            return verdict(False, "Изолированная проверка не завершилась", outcome="infra_error")
        try:
            observed = json.loads(stdout)
        except (TypeError, ValueError, RecursionError):
            return verdict(False, "Изолированная проверка вернула неверный формат")
        if json.dumps(observed, sort_keys=True) != json.dumps(expected, sort_keys=True):
            return verdict(False, "join_ranges не прошла скрытые тесты")
    return verdict(True, "ok")


if __name__ == "__main__":
    result = verdict(False, "Требуется изолированный исполнитель для скрытых тестов", outcome="infra_error")
    print(json.dumps(result, ensure_ascii=False))
