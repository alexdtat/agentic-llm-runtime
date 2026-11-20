# Среда исполнения LLM-агента — задание A

CPU-runtime после пересмотра ЧП1: CLI отправляет задачу серверному харнесу;
харнес вызывает модель и выполняет инструменты через отдельный исполнитель.
У каждой сессии свой контейнер и временный `/work`.

**ЧП2.** В mounted-профиле проверены двустороннее монтирование ThinkPad ↔ desktop,
изоляция, очистка и полный прогон БАФ. [Результаты и ограничения ЧП2](docs/checkpoint-2.md).

В описанном ниже CPU-профиле по умолчанию работает
детерминированный `mock`, который создаёт `mock.txt`, а затем завершает задачу.
Он проверяет инфраструктуру, а не решает произвольные задачи.

[Аудит, схема, владельцы состояния и статус требований ЧП2](docs/system-design-review.md).
[Харнес](docs/harness.md), [протоколы](track_a/protocol-v1.md),
[решения](track_a/docs/adr/0004-cpu-runtime.md).

## Запуск сервера

Нужны Linux, Docker Engine и Compose **2.24.4+** (`include` и `!override`).
GPU, NVIDIA runtime, Python/venv на хосте и работающая модель не нужны.
Первой сборке нужны сеть и место для CPU-образов и Python-зависимостей тестов.
Базовые образы закреплены version + digest; прямые и транзитивные pip-зависимости
тестового образа закреплены в `requirements.txt`.

```bash
export PROBE_TOKEN="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
docker compose up --build -d --wait
```

Либо заполнить `.env` по `.env.example`, заменив placeholder. Стандартный порт —
`127.0.0.1:8080`; `RUNTIME_PORT=0` выбирает свободный порт, узнать его можно командой
`docker compose port gateway 8080`. Одна команда собирает gateway, executor и
sandbox image из исходников. Docker binary уже внутри executor; с хоста
монтируется только Docker socket. `runtime/`, ключи и `.env` не входят в build context.

## Задача через контейнерный CLI

```bash
sid=$(docker compose run --rm cli new --request-id demo-session)
tid=$(docker compose run --rm cli task "$sid" 'Проверка серверного пути' --request-id demo-task)
docker compose run --rm cli task-watch "$tid"
docker compose run --rm cli exec "$sid" -- cat /work/mock.txt
# exec печатает run ID; получить его события: cli watch RUN_ID
# Отмена активной задачи: cli task-cancel TASK_ID
docker compose run --rm cli close "$sid"
```

`task-watch --after N` показывает только события после sequence N; потеря
подключения не отменяет задачу. Повтор отправки с тем же `--request-id` возвращает
тот же объект. `complete` значит «цикл завершился», а не «ответ правильный».

**Cancel/timeout закрывают сессию и удаляют её временные файлы.** Для новой работы
создайте новую сессию. Это намеренное поведение CPU-v1, а не обещание будущей
семантики монтирования. Нормальное завершение сохраняет `/work` до `close`.

## Проверки одной командой

```bash
./scripts/check.sh
```

Скрипт создаёт отдельный Compose project и token, собирает образы, запускает
контейнерные CLI/unit/infra тесты и replay исторического Б1–Б5. Затем делает
SIGKILL **только тестовых** gateway/executor, проверяет восстановление и отсутствие
оставшихся сандбоксов. В конце удаляет тестовые контейнеры, сети и volumes.
Результаты экспортируются в `results/agentic-check-*/`; ошибки сохраняются тоже.
У runner нет Docker socket. При исполнении тестов сеть Compose `internal`,
у sandbox — `network=none`; реальная модель не вызывается. Сборка до начала тестов
может обращаться к registries. `make test` и `make smoke` вызывают ту же команду.

CI: `.github/workflows/cpu.yml`. Свидетельства конкретной проверки и границы
покрытия: [docs/validation-cpu-v1.md](docs/validation-cpu-v1.md).

## Внешняя модель

Явно задайте `MODEL_BACKEND=openai-compatible`, `MODEL_BASE_URL` (с `/v1`),
`MODEL_NAME` и при необходимости `MODEL_API_KEY`, затем пересоздайте gateway.
Адрес должен быть доступен **из контейнера**: `127.0.0.1` там означает сам gateway.
Проект не читает настройки HyperQwen, не запускает и не переключает его.
Секрет модели получает только gateway. Перенаправления API запрещены, чтобы
случайно не передать Authorization другому адресу. Самостоятельный сетевой
LLM proxy пока заменён модулем внутри gateway.

Новый серверный харнес имеет отдельные технические ограничения (60 секунд,
8 шагов, команда до 3 секунд, ответ модели до 512 токенов). Это **не** протокол
исторического эксперимента. Live entrypoints ЧП1 защищены от случайного запуска
в изменившейся среде: сначала согласовать и зафиксировать новую версию протокола.
Старые метрики, промпты, фикстуры, результаты и неудачные прогоны не изменены.
[BAF_RESULTS.md](BAF_RESULTS.md) и [CHECKPOINT_VALIDATION.json](CHECKPOINT_VALIDATION.json)
описывают ЧП1, а не текущую CPU-сборку. Replay — не дополнительная попытка модели.

## Остановка и границы безопасности

```bash
docker compose down -v --remove-orphans
```

При штатной остановке executor удаляет динамические сандбоксы; `-v` удаляет
журналы задач и команд. После SIGKILL сначала поднимите систему с **тем же именем
Compose project** и дождитесь health — startup reconciliation уберёт оставшиеся
контейнеры — и только потом делайте `down -v`. Удаление volumes сразу после
аварии не равно удалению динамических контейнеров. При недоступном Docker daemon
очистка не может считаться подтверждённой.

Docker socket у executor даёт широкие права на хосте. Это остаётся риском даже
при разделении сервисов. Sandbox: UID 1000, cap-drop ALL, readonly root,
no-new-privileges, без сети, с лимитами CPU/RAM/PIDs/tmpfs. Контроль всех лимитов,
устойчивость к враждебному коду и remote mounting ещё требуют работы.

Новые задачи хранят сообщения, tool output и ответы модели в SQLite; история
пока не имеет TTL/квоты. Не открывайте этот сервер в интернет. Текущая схема
рассчитана на одного доверенного оператора и один экземпляр каждого сервиса.

Требования курса: [reference/course/A-agent-runtime.md](reference/course/A-agent-runtime.md).
Исторический пакет ЧП1: [docs/checkpoint-1.md](docs/checkpoint-1.md),
[PACKAGE_MANIFEST.json](PACKAGE_MANIFEST.json) — контрольные суммы исходной ревизии,
не манифест текущего рабочего дерева.
