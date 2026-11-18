# Проверка CPU-v1 — 6 ноября 2026

Результаты этого этапа не заменяют `CHECKPOINT_VALIDATION.json` первого чекпоинта.
Исторические результаты и условия модели остались неизменны.

## Проверенная среда

Linux x86_64, Docker Engine 29.1.3, Compose 2.40.3+ds1-0ubuntu1;
[метаданные Docker](../results/cpu-v1-validation-2026-11-06/docker-server.json),
[версия Compose](../results/cpu-v1-validation-2026-11-06/compose-version.txt),
[IDs/размеры образов по docker inspect](../results/cpu-v1-validation-2026-11-06/images.txt).
Python 3.12.12 находится в образе. CPU runc, без device requests, GPU/HyperQwen не использовались.

Исходный HEAD — `31228ca4d0e85693f19424a1fae53e798a1339d6`, поверх него текущий
незакоммиченный diff. Commit/push не выполнялись. Базы впервые скачаны в этой
проверке; собственные CPU-образы собраны из исходников. Повторные сборки использовали
кэш слоёв. Security/vulnerability scan образов не выполнялся.

## Контейнерный набор

Команда: `./scripts/check.sh`.

[Полный лог](../results/cpu-v1-validation-2026-11-06/container-suite.log): **14 тестовых
методов — OK**; внутри replay проверяются все пять исторических задач Б1–Б5.
Реальных вызовов модели нет. Дополнительно выполнена фаза SIGKILL/recovery и
проверка Docker daemon на оставшиеся сандбоксы.

Подтверждено:

- CLI forwarding/ошибка авторизации/вывод terminal; API malformed JSON и token;
- request idempotency и конфликт payload, в том числе после закрытия/перезапуска;
- один result и неизменность terminal при повторной отмене;
- отмена, timeout и закрытие сессии; detached descendant после нормального выхода;
- отказ ложно подтверждать cleanup при ошибке daemon (unit с injected failure);
- серверный mock agent, task cancel во время инструмента и позднего ответа модели;
- JSON Schema объектов сессии, команды и задачи на реальном API;
- UID/capabilities/no-new-privileges/readonly root, отсутствие socket и token/model
  env в сандбоксе, отсутствие внешней сети, ограничение workdir tmpfs;
- разделение сессий и запрет параллельной команды/стороннего exec активной задачи;
- исторический replay с независимым checker и неизменными fixture/protocol;
- после SIGKILL: interrupted вместо переисполнения и удаление собственных сандбоксов.

[Результаты crash recovery](../results/cpu-v1-validation-2026-11-06/suite/restart-result.json),
[проверка cleanup](../results/cpu-v1-validation-2026-11-06/suite/cleanup.json).
Снимки исходников в summary.json содержат SHA256 выполнявшихся Python-файлов.
CI workflow добавлен, но запуск GitHub Actions в удалённом репозитории не выполнялся.

## Отдельная чистая копия исходников

Из tracked + новых исходников создан export без `.git`, `runtime`, `.venv` и
операторских данных. Сохранён только исторический replay fixture, необходимый
для сборки тестового образа. Проверены корневая команда
`docker compose up --build -d --wait` и контейнерные CLI new/task/task-watch/exec/watch.
Сессия намеренно оставлена открытой перед `down -v`, затем подтверждено отсутствие
её контейнера в daemon.

[Лог](../results/cpu-v1-validation-2026-11-06/clean-source.log),
[манифест export](../results/cpu-v1-validation-2026-11-06/clean-source-manifest.json).
Это проверка чистого **дерева исходников на том же Docker daemon**, с кэшированными
базовыми образами, а не новый компьютер/новая ОС и не опубликованный commit.
После неё менялась только документация.

Итоговая инвентаризация:
[сандбоксы](../results/cpu-v1-validation-2026-11-06/remaining-sandboxes.txt) и
[тестовые volumes](../results/cpu-v1-validation-2026-11-06/remaining-test-volumes.txt) — пустые.
Собранные образы сохранены для повторного запуска.

## Что не проверено и не заявляется

Реальный модельный HTTP endpoint, remote mounting, две машины, SSE/live stdout,
CPU/RAM/PIDs под нагрузкой, OOM/fork-bomb и полная hostile-code матрица,
symlink races коллектора, восстановление при потере самого SQLite volume,
масштабирование и latency benchmarks. Docker socket остаётся привилегией хоста;
перенос в executor этого риска не снимает. Подробнее: [аудит](system-design-review.md).
