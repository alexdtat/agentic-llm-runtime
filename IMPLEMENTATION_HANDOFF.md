# Agent Runtime v2. Передача реализации

## Docker и SSHFS на одном Linux-хосте

Добавлен профиль `mounted` для одного настроенного владельца и одного
экспорта. `mount-new GRANT` принимает имя настроенного экспорта. В режиме
fixture прежний контракт с `files` работает по-прежнему. Код в
`track_a/mounted_executor.py`, запуск, очистка и границы в
[prototypes/mounted-runtime/README.md](prototypes/mounted-runtime/README.md).
Это запуск оператором на одном хосте, с отдельным Compose для exporter и
broker. Экспорты между машинами, полный RO-режим и квота записи остаются
открытыми.

Синтетический прогон подтвердил цепочку CLI → Application → mock → Docker →
SSHFS и двусторонние изменения. Прямой SFTP отклонил доступ к соседнему
файлу, traversal и создание ссылок. Прочитать содержимое закрытых `.git`
и изменить эти метаданные проверенными способами не удалось. CLI cancel
удалил sandbox с отделившимся процессом. Docker-ресурсы, дочерний SSHFS и
вспомогательный self-bind сняты.
[Прогон](/tmp/agentic-mounted-runtime.33ui_t9v/smoke.retry.log) ·
[Очистка Docker и SSHFS](/tmp/agentic-mounted-runtime.33ui_t9v/cleanup.log) ·
[17 регрессионных тестов](/tmp/agentic-mounted-runtime.33ui_t9v/regression.log).

## Исходный офлайн-профиль

**Работает offline**. CLI обращается по HTTP к Application. Серверный харнес
обращается к отдельному mock Model Proxy и к отдельному Fixture Executor.
Инструменты меняют тестовый workspace исполнителя, результат виден через
события и collect. Режим не запускает shell и не подключает папки клиента.
Прежние API v0/v1 и исторический runner сохранены. Снимок исходников до
работы и исходный статус лежат в `/tmp/agent-runtime-before.0gm4bz`
(HEAD `31228ca4d0e85693f19424a1fae53e798a1339d6`). В снимок вошли
изменённые исходники и конфиги без .env, runtime/ и результатов.

## Что читать

| Путь | Функции |
|---|---|
| `track_a/application.py` | `create` хранит сессию, `start` принимает идемпотентный run, `execute` вызывает харнес, `finish` подтверждает остановку и атомарно сохраняет terminal + событие |
| `track_a/run_loop.py` | `run_agent` строит ограниченный контекст из истории, затем последовательно вызывает модель и инструменты |
| `track_a/model_proxy.py` | `ModelProxy.route` возвращает один тип ответа для mock и OpenAI-compatible adapter |
| `track_a/fixture_executor.py` | `use` проверяет активный run/generation, обрабатывает exec и lookup; `apply` работает только с данными fixture |
| `track_a/storage.py` | `SessionJournals` хранит отдельную SQLite БД на сессию; close оставляет минимальную запись о закрытии |
| `track_a/client.py`, `track_a/cli.py` | JSON API, SSE batch с cursor, отправка и отмена run, чтение результата |
| `track_a/fixture_attempt.py` | Свежая сессия → run → проверка cleanup → collect → сравнение данных → evidence → close |

При потере ответа на exec Application ищет тот же `op_id`. Если исход
неизвестен, run получает `interrupted`; команды не переигрываются при
перезапуске. Неподтверждённая остановка даёт `quarantined` и блокирует
следующий run. Fixture хранит операции вместе с их эффектом в одной
SQLite-транзакции. В профиле `mounted` намерение записывается в журнал
до вызова инструмента. При неизвестном исходе запись может остаться
в клиентской папке, а исполнитель не повторяет операцию.

## Запуск fixture вручную

В каталоге checkout, на Linux с доступным Docker. Значения ниже предназначены
только для локальной пробы. `--env-file /dev/null` исключает чтение локального .env.

```bash
export V2_OWNER_TOKENS='{"dev":"fixture-dev"}'
export V2_EXECUTOR_TOKEN=fixture-exec V2_MODEL_TOKEN=fixture-proxy
export PROBE_TOKEN=fixture-dev
docker compose --env-file /dev/null -p agentic-v2-fixture -f track_a/compose.fixture.yaml up --build -d --wait
sid=$(python3 -m track_a.cli fixture-new)
rid=$(python3 -m track_a.cli run "$sid" 'Create a smoke fixture')
python3 -m track_a.cli events "$sid" --after 0
python3 -m track_a.cli run-get "$sid" "$rid"
# После terminal completed
python3 -m track_a.cli run-collect "$sid" "$rid"
python3 -m track_a.cli session-close "$sid"
docker compose --env-file /dev/null -p agentic-v2-fixture -f track_a/compose.fixture.yaml down -v
```

`POST /v2/sessions` принимает `request_id` и map `files` (base64).
Под `/v2/sessions/{sid}` доступны snapshot, `POST /runs`,
`GET /runs/{rid}`, `POST /runs/{rid}/cancel`, `POST /runs/{rid}/collect`,
`POST /close` и `GET /events?after=N`. SSE отдаёт конечную пачку.
Клиент запрашивает следующую пачку с последним sequence. Просроченный
cursor даёт 410 и snapshot. Частичная схема ключевых DTO в
`track_a/protocol-v2.schema.json`. Валидаторы проверяют также семантику
base64, нормализацию пути и дубликаты.

## Проверки и границы

- `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest -v test_runtime_v2 test_runtime_state test_runtime_cli test_baf_checkers`.
  14 тестов прошли. [Лог](/tmp/agent-runtime-v2.prThM9/final-validation.log).
  Локальные сокеты потребовали снять ограничение песочницы для этой команды.
  В базовом conda Python отсутствовал jsonschema; системный Python его уже имел.
  В логе также есть предупреждения Python о незакрытых SQLite connections.
- Fixture Compose прошёл `config --quiet`. [Лог](/tmp/agent-runtime-v2.prThM9/compose-config.log).
  Сборка, запуск Compose и `down -v` в этой среде остались непроверенными.
  `git diff --check` прошёл; DTO-схема проверена валидатором Draft 2020-12.
- Границы текущего среза. Установка TLS, проверка свежести leases вне процесса,
  stream execution events, полный API schema, полноценный выбор model/harness
  профилей и live provider остались открыты. Отмена отключает дальнейшие шаги
  харнеса, сетевой вызов модели может закончиться позже. Хранение и остановка
  подтверждены только на fixture.
- При исходной офлайн-проверке из стандартного окружения не было доступа
  к Docker socket и `/dev/fuse`. В последующем прогоне Docker и FUSE были
  доступны с нужными разрешениями. Ограничения этого прогона указаны выше.
  Требование десктоп-клиента нужно уточнить отдельно от CLI для CI.
- Fixture attempt проверяет механику и разделяет pass/fail/infra_error.
  Полный курсовой набор, собственные задачи, безопасная изоляция чекеров
  исполняемого кода и live оценка остаются отдельной работой.
  [Задание A](https://raw.githubusercontent.com/deeppavlov/agentic-course-itmo/main/projects/A-agent-runtime.md).

Следующий практический шаг для двух машин после согласования среды. На одноразовой паре
Linux ВМ проверить SSHFS со scoped SFTP и обратным SSH-туннелем на отдельной
синтетической папке в пределах 1 MiB. Сопоставить с WebDAV/rclone и NFS
через отдельный защищённый канал. Для выбора потребуются проверяемая
защита .git, ссылок и rename, лимит записи на стороне экспорта, отзыв
доступа после обрыва и подтверждение остановки всех писателей.
Повторить проверку Docker executor с runtime-лимитами на серверной ВМ.
