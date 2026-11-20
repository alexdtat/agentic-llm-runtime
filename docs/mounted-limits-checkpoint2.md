# Лимиты mounted-профиля к ЧП2

На одном хосте команда RunCommand в mounted-контейнере измерила cgroup v2, права процесса, файловые системы и сетевые интерфейсы. Сессия закрылась, контейнеры, сеть и монтирования стенда удалены. [Журнал прогона](../results/t08-mounted-limits/mount-v2-1d581452cd2e/limits.log) · [Свидетельство обрыва SSHFS](../results/t08-mounted-failures/live-rhsykxdx/verification.md)

| Требование | Настройка mounted | Что подтверждено и какой live-замер отсутствует |
|---|---|---|
| A-5.1 · CPU | `--cpus 1` | В контейнере измерено `cpu.max = 100000 100000` (1 CPU). При нагрузке nr_throttled вырос с 2 до 22. |
| A-5.1 · RAM и swap | `--memory 256m --memory-swap 256m` | Измерено `memory.max = 268435456` и `memory.swap.max = 0`. При превышении лимита oom_kill вырос с 0 до 1, дочерний процесс получил SIGKILL. |
| A-5.1 · PIDs | `--pids-limit 64` | Измерено `pids.max = 64`. При pids.current = 64 следующий fork вернул EAGAIN. |
| A-5.1 · диск | `/tmp` — tmpfs 16 МиБ, `/work` — SSHFS bind | CPU-v1 подтвердил исчерпание `/work` tmpfs. Живой mounted-прогон подтвердил общий лимит 128 КиБ для shell и SFTP на синтетическом RW-экспорте. RunCommand подтвердил tmpfs 16 МиБ в `/tmp` и RW fuse.sshfs в `/work`. Для постоянной RW-папки лимит задаётся на стороне клиентского экспорта. |
| A-5.1 · время | Worker 6 с, ожидание `docker exec` 8 с. Application задаёт дедлайн 60 с и проверяет его на переходах. | 6/8 с подтверждены офлайн. V0 проверял реальный timeout с закрытием сессии. На mounted worker вернул timed_out, Application завершил run как interrupted/outcome_unknown. Сохранился один маркер, повтор отклонён. |
| A-5.2 · процесс | UID:GID `1000:1000`, `--cap-drop ALL`, `--read-only`, `no-new-privileges` | В mounted все четыре значения UID и GID равны 1000, CapEff и CapBnd равны нулю, NoNewPrivs равен 1, корень смонтирован ro. |
| A-5.3 · очистка | Остановка удаляет контейнер, неопределённый исход блокирует повтор. | Mounted на одном хосте подтвердил отмену с остановкой отделившегося писателя и очистку после ENOTCONN. При наблюдаемом ожидании FUSE отмена завершилась за 0,142 с, остановка и очистка подтверждены. |
| A-5.4 · сеть | Sandbox `--network none`, отдельный канал exporter/broker | RunCommand обнаружил только интерфейс lo. Подключение к адресу работающего локального mock из отдельного сетевого namespace получило ECONNREFUSED. |
| A-5.5 · секреты и модель | В запуск sandbox не передаются env-флаги; отдельные ключи монтируются в exporter/broker. Application вызывает модель через Model Proxy. | Тестовая переменная присутствовала у исполнителя и отсутствовала в sandbox. Docker socket в sandbox отсутствовал. Локальный mock Model Proxy получил запрос Application и наблюдение инструмента. |
| A-5.6 · накладные расходы | Курс требует cold start, задержку exec и накладные расходы памяти. | Выполнено 12 повторов на desktop. [Сырые значения, медиана и p95](checkpoint2-evidence/measurements.json). |

[Аргументы Docker](../track_a/executor.py) · [MountedExecutor](../track_a/mounted_executor.py) · [Worker](../track_a/worker.py) · [Application](../track_a/application.py) · [Exporter и broker](../prototypes/mounted-runtime/compose.yaml) · [Проверка CPU workflow](https://github.com/alexdtat/agentic-llm-runtime/actions/runs/44729876856) · [Тесты CPU-v1](../tests/test_runtime_integration.py)

## Проверка на одноразовом стенде

Контракт аргументов воспроизводится из корня checkout командой `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_mounted_limits`. Docker CLI и проверка монтирования заменены тестовыми заглушками. [Тесты контракта](../tests/test_mounted_limits.py)

На уже запущенном одноразовом mounted-стенде с cgroup v2 передайте следующий код как один инструмент `RunCommand` с Python. Он выводит только выбранные параметры процесса, монтирований и cgroup. Для `/tmp` создаётся один временный файл до 4 КиБ, который сразу закрывается и удаляется.

```python
import os
import tempfile
from pathlib import Path

assert "0::/" in Path("/proc/self/cgroup").read_text().splitlines(), "check cgroup v2 namespace and mount root"
cgroup = Path("/sys/fs/cgroup")
for name in ("cpu.max", "memory.max", "memory.swap.max", "pids.max", "pids.current"):
    print(name, (cgroup / name).read_text().strip())

status = ("Uid:", "Gid:", "CapEff:", "CapBnd:", "NoNewPrivs:")
print(*(line for line in Path("/proc/self/status").read_text().splitlines()
        if line.startswith(status)), sep="\n")
for line in Path("/proc/self/mountinfo").read_text().splitlines():
    mount, filesystem = line.split(" - ", 1)
    target, options = mount.split()[4:6]
    if target in {"/", "/tmp", "/work"}:
        print("mount", target, filesystem.split()[0], options)
print("interfaces", sorted(path.name for path in Path("/sys/class/net").iterdir()))
names = {"PROBE_TOKEN", "EXECUTOR_TOKEN", "MODEL_PROXY_TOKEN", "OWNER_TOKENS",
         "MODEL_API_KEY", "MOUNT_LIMITS_CANARY"}
print("server_variable_present", bool(names & os.environ.keys()))
print("docker_socket_visible", Path("/var/run/docker.sock").exists())

limit = os.statvfs("/tmp")
print("tmp_capacity_bytes", limit.f_blocks * limit.f_frsize)
assert limit.f_blocks * limit.f_frsize == 16 * 1024 * 1024
assert limit.f_bavail * limit.f_frsize >= 4096
with tempfile.TemporaryFile(dir="/tmp") as probe:
    probe.write(b"0" * 4096)
    probe.flush()
    print("tmp_probe_bytes", os.fstat(probe.fileno()).st_size)
```

Сверьте `cpu.max`: числовая квота равна периоду для одного CPU. Ожидаются UID и GID 1000, нулевые `CapEff` и `CapBnd`, `NoNewPrivs: 1`, root с `ro`, `/tmp` типа tmpfs и `/work` типа `fuse.sshfs` с режимом выданного гранта. Ожидается только `lo` и оба булевых признака доступа со значением `False`. При иной раскладке cgroup сопоставьте путь из `/proc/self/cgroup` с корнем соответствующего mountinfo перед чтением контроллеров. Canary задают только серверному процессу одноразового стенда случайным синтетическим значением. Его значение в команду агента и логи не передают.

Проверку времени выполняйте в отдельном одноразовом run. Отправьте `sleep 7` через mounted worker и зафиксируйте время, `interrupted/outcome_unknown`, отсутствие повтора и подтверждение stop. Показания cgroup, список интерфейсов и запись 4 КиБ дают данные о настройке. Результаты проверки границ и маршрута к mock Model Proxy приведены в [отчёте прогона](mounted-checkpoint2-failures.md).

Для mounted RW общий дисковый лимит можно задать на файловой системе клиентского экспорта. Для постоянной клиентской папки оператор заранее настраивает квоту на экспортируемом каталоге или выбирает каталог на выделенной ФС ограниченного размера и передаёт его в `prepare.py --client`. Tmpfs `/tmp` и лимит одного вызова `WriteFile` действуют на другие ресурсы.

Для проверки на одноразовом стенде используйте обычную [подготовку mounted](../prototypes/mounted-runtime/README.md#повторить-синтетическую-проверку), добавив `--bounded-export` к вызову `prepare.py`. Этот режим создаёт tmpfs 128 КиБ на стороне экспортёра и исключает `--client`. После `stack.sh up` выполните из корня checkout:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" python3 prototypes/mounted-runtime/smoke_bounded_export.py
```

Сценарий помещает в `/work` по 64 КиБ через `RunCommand` и прямой SFTP, проверяет отсутствие свободных блоков, отказ обеих дополнительных записей и сохранность исходных байтов. Затем освободите ресурсы стенда по инструкции mounted.

По выводу оператора, живой прогон проекта `mount-v2-3e9887275fc7` вернул `{"status": "passed", "limit_bytes": 131072}`. Дополнительная запись через shell завершилась с `errno=5` (EIO), через SFTP — отказом записи. Сценарий подтвердил сохранность исходных файлов и отсутствие дополнительных байтов. После прогона подтверждено удаление контейнеров, сети, SSHFS и вспомогательного bind mount. Временный каталог стенда сохранён.
