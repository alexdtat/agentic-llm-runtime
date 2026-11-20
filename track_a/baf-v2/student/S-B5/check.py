import json
import shlex
import sys
from pathlib import Path

EXPECTED = '16'


def check(workdir):
    try:
        actual = (workdir / "answer.txt").read_bytes().decode("utf-8")
        passed = actual in (EXPECTED, EXPECTED + '\n')
    except (OSError, UnicodeError):
        passed = False
    return {"pass": passed, "reason": "ok" if passed else 'Нужно значение именно показателя peak_load.'}


def command_matches(command, expected):
    if not isinstance(command, str) or "\n" in command or "\r" in command:
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) != len(expected):
        return False
    python, *paths = expected
    return (
        argv[0] in (python, f"/usr/bin/{python}", f"/usr/local/bin/{python}")
        and all(value in (path, f"./{path}", f"/work/{path}") for value, path in zip(argv[1:], paths))
    )


def check_trace(events):
    """Require a confirmed execution in the trusted v2 trace of this attempt."""
    task = json.loads(Path(__file__).with_name("task.json").read_text(encoding="utf-8"))
    required = task["required_tool"]
    matching_starts = set()
    for event in events:
        if event.get("schema_version") != 2:
            continue
        payload = event.get("payload", {})
        operation = event.get("session_id"), event.get("run_id"), payload.get("op_id")
        if not all(isinstance(part, str) and part for part in operation):
            continue
        if event.get("type") == "tool.started":
            call = payload.get("call", {})
            command = call.get("args", {}).get("command")
            if call.get("tool") == "RunCommand" and command_matches(command, required["argv"]):
                matching_starts.add(operation)
        elif event.get("type") == "tool.result" and operation in matching_starts:
            result = payload.get("result", {})
            code = result.get("exit_code")
            if (
                type(code) is int and code == required["exit_code"]
                and result.get("error_kind") is None and result.get("truncated") is False
            ):
                return {"pass": True, "reason": "ok"}
    return {"pass": False, "reason": "В трейсе нет подтверждённого выполнения указанной команды."}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
