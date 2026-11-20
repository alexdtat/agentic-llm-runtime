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


REQUIRED_FILES = ("parse_logs.py",)
CASES = (
    (
        "2026-04-08T03:30:00Z|INFO|received\n"
        "2026-04-08T01:05:59-02:00|ERROR|device | broken\n"
        "2026-04-08T06:29:00+02:30|WARN|повтор\n"
        "2026-04-08T04:00:00Z|INFO|done\n",
        {
            "2026-04-08T03:00Z": {"total": 3, "errors": 1},
            "2026-04-08T04:00Z": {"total": 1, "errors": 0},
        },
    ),
    (
        "2026-01-01T00:10:00+00:30|ERROR|first\n"
        "2025-12-31T23:59:59Z|ERROR|second\n"
        "2025-12-31T23:31:00Z|WARN|третий\n"
        "2026-01-01T00:30:00Z|INFO|midnight\n"
        "2026-01-01T01:00:00Z|WARN|next\n",
        {
            "2025-12-31T23:00Z": {"total": 3, "errors": 2},
            "2026-01-01T00:00Z": {"total": 1, "errors": 0},
            "2026-01-01T01:00Z": {"total": 1, "errors": 0},
        },
    ),
    (
        "2024-02-29T00:59:59Z|WARN|leap\r\n"
        "2025-02-29T00:00:00Z|ERROR|invalid date\r\n"
        "2024-02-29T00:00:00|ERROR|missing offset\r\n"
        "2024-02-29T00:00:00Z|FATAL|invalid level\r\n"
        "2024-02-29T00:00:00Z|ERROR|\r\n"
        "2024-02-29T00:00:00Z|ERROR\r\n"
        "garbage\r\n\r\n",
        {"2024-02-29T00:00Z": {"total": 1, "errors": 0}},
    ),
    ("", {}),
)


def program_for(log):
    return f"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as folder:
    source = Path(folder) / "input.log"
    source.write_bytes({log.encode('utf-8')!r})
    proc = subprocess.run(
        [sys.executable, "parse_logs.py", str(source)],
        capture_output=True, encoding="utf-8", errors="replace",
    )
print(json.dumps([proc.returncode, proc.stdout]))
"""


def check_behavior(run_python):
    for log, expected in CASES:
        result = probe(run_python, program_for(log))
        if not isinstance(result, list) or len(result) != 2 or result[0] != 0:
            raise IncorrectAnswer("parse_logs.py завершилась с ошибкой")
        try:
            actual = json.loads(result[1])
        except (TypeError, ValueError) as exc:
            raise IncorrectAnswer("parse_logs.py не вывела JSON") from exc
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise IncorrectAnswer("Почасовые агрегаты не совпали")


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
