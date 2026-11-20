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


REQUIRED_FILES = ("dispatch/batches.py",)
CASES = (
    (["one", "two", "three", "four", "five"], 2, [[["one", "two"], ["three", "four"], ["five"]], True]),
    ([{"order": 9}], 7, [[[{"order": 9}]], True]),
    (["third", "first", "third"], 1, [[["third"], ["first"], ["third"]], True]),
    ([2, 1, 2, 3], 2, [[[2, 1], [2, 3]], True]),
    ([], 3, [[], True]),
    ([], 0, ["ValueError"]),
    (["one"], -2, ["ValueError"]),
)


def program_for(orders, size):
    return f"""
import contextlib
import copy
import io
import json

result = ["invalid"]
try:
    with contextlib.redirect_stdout(io.StringIO()):
        from dispatch import plan_orders
        orders = {orders!r}
        original = copy.deepcopy(orders)
        try:
            output = plan_orders(orders, {size!r})
            result = [output, orders == original]
        except ValueError:
            result = ["ValueError"]
    result = json.dumps(result)
except Exception:
    result = json.dumps(["raised"])
print(result)
"""


TEST_PROGRAM = """
import contextlib
import io
import json
import unittest

with contextlib.redirect_stdout(io.StringIO()):
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
print(json.dumps([result.wasSuccessful(), result.testsRun, len(result.skipped)]))
"""


def check_behavior(run_python):
    for orders, size, expected in CASES:
        if probe(run_python, program_for(orders, size)) != expected:
            raise IncorrectAnswer("Группировка заказов не прошла скрытые тесты")
    result = probe(run_python, TEST_PROGRAM)
    if not isinstance(result, list) or len(result) != 3:
        raise IncorrectAnswer("Некорректный результат прогона тестов")
    ok, tests_run, skipped = result
    if ok is not True or type(tests_run) is not int or type(skipped) is not int or tests_run - skipped < 3:
        raise IncorrectAnswer("Исходные тесты проекта не прошли")


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
