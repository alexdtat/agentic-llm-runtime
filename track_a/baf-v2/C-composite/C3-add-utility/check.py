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


REQUIRED_FILES = ("tracker/duration.py", "tracker/__init__.py", "tests/test_duration.py")
CASES = (
    ("0s", ["int", 0]),
    ("  2h  5m 6s  ", ["int", 7506]),
    ("1h4s", ["int", 3604]),
    ("90m", ["int", 5400]),
    ("01m90s", ["int", 150]),
    ("", ["ValueError"]),
    ("1m2h", ["ValueError"]),
    ("1s2s", ["ValueError"]),
    ("+2s", ["ValueError"]),
    ("１h", ["ValueError"]),
    ("1 h", ["ValueError"]),
    ("1h\t2m", ["ValueError"]),
    ("2H", ["ValueError"]),
)


def program_for(text):
    return f"""
import contextlib
import io
import json

def call_duration(function):
    try:
        value = function({text!r})
        return ["int", value] if type(value) is int else ["invalid"]
    except ValueError:
        return ["ValueError"]
    except Exception:
        return ["raised"]

with contextlib.redirect_stdout(io.StringIO()):
    from tracker import parse_duration
    from tracker.duration import parse_duration as parse_internal
    public = call_duration(parse_duration)
    internal = call_duration(parse_internal)
print(json.dumps([public, internal]))
"""


def tests_program(mode):
    return f"""
import contextlib
import io
import json
import unittest

with contextlib.redirect_stdout(io.StringIO()):
    if {mode!r} != "baseline":
        import tracker
        from tracker import duration

        original = duration.parse_duration

        def mutant(text):
            try:
                answer = original(text)
            except ValueError:
                if {mode!r} == "reject_invalid":
                    return 0
                raise
            return -1 if {mode!r} == "compute_valid" else answer

        duration.parse_duration = mutant
        tracker.parse_duration = mutant
    loader = unittest.defaultTestLoader
    pattern = "test*.py" if {mode!r} == "baseline" else "test_duration.py"
    suite = loader.discover("tests", pattern=pattern)
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
print(json.dumps([result.wasSuccessful(), result.testsRun, len(result.skipped), len(loader.errors)]))
"""


def check_behavior(run_python):
    for text, expected in CASES:
        if probe(run_python, program_for(text)) != [expected, expected]:
            raise IncorrectAnswer("parse_duration не прошла скрытые тесты")

    baseline = probe(run_python, tests_program("baseline"))
    if not isinstance(baseline, list) or len(baseline) != 4:
        raise IncorrectAnswer("Некорректный результат прогона тестов")
    ok, tests_run, skipped, errors = baseline
    if ok is not True or type(tests_run) is not int or tests_run < 3 or skipped != 0 or errors != 0:
        raise IncorrectAnswer("Тесты проекта и добавленные тесты должны выполняться и проходить")

    for mode in ("compute_valid", "reject_invalid"):
        result = probe(run_python, tests_program(mode))
        if not isinstance(result, list) or len(result) != 4:
            raise IncorrectAnswer("Некорректный результат прогона тестов")
        ok, tests_run, skipped, errors = result
        if ok is not False or type(tests_run) is not int or tests_run < 1 or skipped != 0 or errors != 0:
            raise IncorrectAnswer("Добавленные тесты должны проверять корректный результат и ValueError")


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
