T08 mounted failures, 18 ноября 2026

Рабочая копия для дальнейшего хэндоффа находится в
`/home/alexdtat/Documents/Codex/2026-11-17/home-alexdtat-documents-codex-2026-11-2/agentic-runtime-t08`.
HEAD — `31228ca4d0e85693f19424a1fae53e798a1339d6`.
Скопированы 125 текущих tracked/untracked исходников, включая прежние незакоммиченные
изменения. Runtime, прежние results, caches и секретные env исключены.
Обычная папка `.git` содержит HEAD/index/objects/refs и минимальный core config.
Remote/credential config, hooks и reflogs не переносились.
Исходный checkout проверен read-only, его status и шесть сохранённых исходных файлов
совпадают с baseline. Проверка записана в `original-verification.json`.
Commit и push отсутствуют.

Baseline сохранён в соседнем `agentic-runtime-t08-baseline`.
Там находятся первоначальные status, tracked diff, HEAD, список файлов и копии
шести исходных файлов до правок. `incremental.patch` содержит только пять
изменённых/добавленных исходных файлов. `git apply --check` на первоначальном checkout
завершился с exit 0. Свидетельства этой проверки находятся в `patch-check.log`.

Исправлены три перехода MountedExecutor.

- Неуспешный start и начавшийся stop запрещают новый exec и запись позднего результата.
  Durable active intent сохраняет обязанность cleanup. Разрешение исполнения хранится
  только в памяти и выставляется после mount-ready. После рестарта оно отсутствует.
  Старые journals без новых полей загружаются и безопасно очищаются.
- До запуска collect сохраняется имя контейнера. Ошибка удаления сохраняет marker,
  start/release/повторный collect сначала подтверждают его очистку. Recovery удаляет
  marker после успешной сверки контейнеров scope. Ошибка recovery сохраняет marker.
- После recovery main открывает API и при отсутствующем mount. Lookup/stop/release
  доступны, новый start отклоняется. Health проверяет findmnt и отдаёт
  `503 mount_unavailable`, когда запись fuse.sshfs отсутствует или проверка не удалась.

В `.github/workflows/cpu.yml` добавлен offline шаг до fixture/build этапов.
Новые проверки используют настоящие MountedExecutor, State.remove/recover,
Application, run_agent и mock ModelProxy. Подменяются subprocess для Docker/findmnt
и private transport без открытия сокетов. Worker JSON и результаты инфраструктуры
синтетические. Тесты не запускают Docker, Compose, mounts или сетевые запросы.
Единственный настоящий child — краткий Python-процесс, завершающий себя через
`os._exit(73)` после сохранённого intent и fsync синтетического побочного эффекта.
Рестарт читает этот журнал и сохраняет единственный побочный эффект без replay.

| Проверка | Подтверждённое offline поведение |
| --- | --- |
| Findmnt, Docker run, mount-ready | Отказ до start и readiness не разрешает exec |
| Ошибка Docker exec с частичным stdout | Исход unknown, тот же и новый op_id в этом run не исполняются |
| Known result и потеря ответа | Lookup возвращает сохранённый результат, duplicate exec дедуплицируется после рестарта |
| Crash после intent/side effect | Recovery удаляет scope-контейнер, помечает run stopped, replay отсутствует |
| Remove/ps/recovery failures | Cleanup не подтверждается, active/collector intent сохраняется |
| Collect error, timeout, mount loss | Cleanup выполняется через finally, failed removal блокирует дальнейшие действия |
| Release/restart | Payload удалён из SQLite, tombstone и release идемпотентны |
| Stop и failed stop при выполняющемся exec | Поздний результат не записывается |
| Application с настоящим mounted backend | Known reply lookup без replay, unknown interruption, cleanup quarantine и запрет нового run/collect |
| Main после restart и потери mount | API обслуживает lookup/stop/release, start блокируется, health сообщает недоступность |

Команды выполнялись из рабочей копии с `PYTHONDONTWRITEBYTECODE=1`.

| Команда | Exit | Результат | Лог |
| --- | --- | --- | --- |
| `python3 -m unittest -v test_mounted_runtime test_mounted_failures` | 0 | 16 tests, OK | `mounted-tests.log` |
| `python3 -m unittest -v test_mounted_runtime test_mounted_failures test_runtime_state` | 0 | 19 tests, OK | `offline-regression.log` |
| Python heredoc из `baseline-command.txt` | 1, ожидаемо | 3 метода, 7 failed assertions/subtests | `baseline-red.log` |
| `git apply --check results/t08-mounted-failures/incremental.patch` из исходного checkout с абсолютным путём patch | 0 | Patch применим к текущему исходному состоянию | `patch-check.log` |

Baseline-red подставляет сохранённый класс MountedExecutor через importlib в те же
новые регрессии. Исходники baseline не изменяются. Проверка выявляет возможность exec
после failed start, потерю collector ownership и принятие позднего результата после
failed stop. На исправленной версии все эти проверки проходят.

Live-проверка обрыва SSHFS остаётся открытой. Findmnt/mountinfo и stat('/work') не
доказывают доступность удалённого экспорта при cached/disconnected FUSE. Требуются
реальные проверки зависшего I/O, исчезновения всех процессов после stop/cleanup и
восстановления после транспортного обрыва. Текущий прогон подтверждает решения
control plane при введённых инфраструктурных ответах. Проверка двух машин, полный
RO, квота записи и прочие открытые пункты T08 этим пакетом не закрыты. Полный Docker
CI и удалённый GitHub Actions в этом прогоне не исполнялись.

Дополнение о подготовке live SSHFS failure runner, 18 ноября 2026.

Добавлен `prototypes/mounted-runtime/live_failure.py`. Команда из корня checkout —
`PYTHONDONTWRITEBYTECODE=1 python3 prototypes/mounted-runtime/live_failure.py`.
Она использует prepare без --client и текущий stack.sh с готовыми образами.
Preflight выполняется до создания стенда, блокирует NNP, remote/context Docker,
недоступный FUSE/daemon/образы и sudo timestamp. Весь probe процесс использует
локальный Docker socket. Пароль sudo вводится оператором только в обычном терминале,
при необходимости через sudo -v перед запуском.

Команда делает единственную запись 9 байт и fsync. После handshake runner останавливает
точный exporter с проверенными project/service labels и подтверждает его остановку.
Затем command выполняет чтение через SSHFS. Для pass нужны IO_BEGIN после cut,
errno либо незавершённый I/O к worker timeout, реальный unknown/no replay, single-write
и подтверждённый stop. Sandbox/PID identities проверяются до общего аварийного cleanup.
Cleanup действует только после захвата свежих ресурсов. Он проверяет исчезновение
scope-контейнеров, сетей, SSHFS/self-bind mounts и сохранённых host PID/starttime.
Лимиты продукта не изменяются. Таймауты handshake и потеря I/O evidence дают ошибку.
Прозрачная наблюдающая обёртка сохраняет реальные ответы Docker без изменения результата.
HTTP bearer токены случайны, значения не логируются.

Текущие независимые проверки runner включают ранний отказ preflight, запрет другого
Docker routing, критерии I/O evidence, отказ cleanup чужого scope/mount и продолжение
собственного cleanup после ошибки сбора диагностики. Полный успешный live прогон
отсутствует. Скрипт требует проверки на согласованном обычном хосте.

| Команда | Exit | Результат | Лог |
| --- | --- | --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_mounted_failures.LiveFailureRunnerTests` | 0 | 5 tests, OK | `live-runner-tests.log` |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_mounted_runtime test_mounted_failures test_runtime_state` | 0 | 24 tests, OK | `live-runner-regression.log` |
| `PYTHONDONTWRITEBYTECODE=1 python3 prototypes/mounted-runtime/live_failure.py` | 2 | Preflight blocked, NoNewPrivs must be 0 | `live-preflight.log` |

Реальная попытка preflight завершилась до prepare, Docker и mount действий. Обрыв
SSHFS этим запуском не проверен. Данные успешного host запуска будут напечатаны в
уникальном `/tmp/agentic-mounted-runtime.*` и сохранятся там при успехе или ошибке.
Runner ограничен 100 секундами плюс 90 на cleanup. README описывает требования,
критерии результата, файлы логов и точную команду запуска.
Итоговый incremental.patch теперь содержит пять исходных файлов с этим дополнением.

**Обновление 18 ноября после запуска на хосте.** Live-прогон обрыва SSHFS на одном хосте прошёл. Получены ENOTCONN, один вызов worker, interrupted/outcome_unknown и подтверждённая очистка. Предыдущее сообщение о blocked относится к запуску из ограниченной среды. [Проверка и сохранённые результаты](live-rhsykxdx/verification.md) · [Итог runner](live-rhsykxdx/live-failure-summary.json).
