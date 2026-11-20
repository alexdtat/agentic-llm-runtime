"""Run hidden probes only through the trusted isolated Python callback."""
import json
import stat
from pathlib import Path


class IncorrectAnswer(Exception):
    pass


def verdict(ok, reason, *, outcome=None):
    return {"pass": ok, "reason": reason, "outcome": outcome or ("pass" if ok else "fail")}


def probe(run_python, program):
    try:
        return json.loads(run_python(program))
    except (TypeError, ValueError) as exc:
        raise IncorrectAnswer("Изолированный тест вернул неверный результат") from exc


REQUIRED_FILES = ("days.py",)
CASES = (
    ("", "2024-03-01", None),
    ("2024-03-01", "  ", None),
    ("  28.02.2024 г. ", "2024-03-01", 2),
    ("2024-03-01", "29.02.2024 г.", -1),
    ("29.02.1900 г.", "1900-03-01", None),
    ("2000-02-28", "2000-03-01", 2),
    ("2023-12-31", "2024-01-02", 2),
    ("2022-12-31", "2024-01-01", 366),
    ("2024-03-01", "01.03.2024 г.", 0),
    ("2024-03-01", "01.03.2024 г.", None),
    ("２０２４-03-01", "2024-03-01", None),
    ("2024-3-01", "2024-03-01", None),
)


def program_for(a, b):
    return f"""
import contextlib
import io
import json

try:
    with contextlib.redirect_stdout(io.StringIO()):
        from days import days_between
        value = days_between({a!r}, {b!r})
    result = [type(value).__name__, value] if value is None or type(value) is int else ["invalid"]
except Exception:
    result = ["raised"]
print(json.dumps(result))
"""


def check_behavior(run_python):
    for a, b, expected in CASES:
        actual = probe(run_python, program_for(a, b))
        if actual != [type(expected).__name__, expected]:
            raise IncorrectAnswer("days_between не прошла скрытые тесты")


def check_isolated(workdir, run_python):
    """Use a fresh sandbox whose working directory contains the collected snapshot."""
    for relative in REQUIRED_FILES:
        path = Path(workdir) / relative
        try:
            regular = stat.S_ISREG(path.lstat().st_mode)
        except FileNotFoundError:
            return verdict(False, f"Файл {relative} не создан")
        except OSError:
            return verdict(False, f"Не удалось проверить файл {relative}", outcome="infra_error")
        if not regular:
            return verdict(False, f"{relative} должен быть обычным файлом")
    try:
        check_behavior(run_python)
    except IncorrectAnswer as exc:
        return verdict(False, str(exc))
    except (OSError, RuntimeError):
        return verdict(False, "Изолированная проверка не завершилась", outcome="infra_error")
    return verdict(True, "ok")


if __name__ == "__main__":
    result = verdict(
        False,
        "Требуется изолированный исполнитель для скрытых тестов",
        outcome="infra_error",
    )
    print(json.dumps(result, ensure_ascii=False))
