# Среда исполнения LLM-агента — задание A

## Что это

Клиент отправляет задачу серверному харнесу. Харнес по очереди вызывает модель и инструменты, а команды агента выполняются в изолированном Docker-контейнере. В профиле ЧП2 рабочая папка клиента доступна контейнеру как `/work` через SSHFS.

**[Отчёт ЧП2: схема, трейсы, результаты БАФ и замеры](docs/checkpoint-2.md).** Между ThinkPad и desktop проверена запись и чтение в обе стороны. Полный БАФ выполнен отдельно на desktop с синтетическим экспортом и локальной Qwen. [Двуххостовая демонстрация](docs/two-host-checkpoint2.md) · [Результаты по попыткам](docs/checkpoint2-evidence/baf.json)

## Архитектура

В mounted v2 клиент обращается к Application по HTTP. Application проверяет владельца сессии и запускает агентный цикл. ModelProxy отправляет сообщения провайдеру модели. Когда модель запрашивает инструмент, MountedExecutor выполняет его в sandbox текущего запуска и возвращает результат харнесу. В двуххостовом режиме broker монтирует папку клиентского SFTP-экспортера через SSH-туннель; sandbox получает её через bind mount. [Подробная схема и жизненный цикл](docs/checkpoint-2.md) · [Архитектурные решения](track_a/docs/adr/)

Sandbox работает без сети, с UID 1000, файловой системой образа только для чтения и ограничениями ресурсов. Docker socket доступен исполнителю и даёт широкие права на хосте. Стенд рассчитан на одного доверенного оператора. [Проверки изоляции и отказов](docs/mounted-checkpoint2-failures.md) · [Лимиты](docs/mounted-limits-checkpoint2.md)

## Запуск сервера

**Для демонстрации ЧП2 с монтированием** нужны клиентский экспорт, SSH-туннель и серверный broker. Отдельные инструкции описывают подготовку, запуск и очистку для [двух машин](docs/two-host-checkpoint2.md) и для [синтетического однохостового стенда](prototypes/mounted-runtime/README.md).

**Для быстрого запуска CPU-v1** нужны Linux, Docker Engine и Compose 2.24.4+. Этот профиль использует временный `/work` и по умолчанию работает с mock-моделью без GPU. Для первой сборки нужен доступ к реестрам образов и Python-пакетов. Из корня репозитория:

```bash
export PROBE_TOKEN="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
docker compose up --build -d --wait
```

Можно также заполнить локальный `.env` по [.env.example](.env.example), заменив пример токена. По умолчанию gateway слушает `127.0.0.1:8080`. При `RUNTIME_PORT=0` узнать назначенный порт можно командой `docker compose port gateway 8080`.

Штатная остановка с удалением томов и журналов — `docker compose down -v --remove-orphans`. После аварийной остановки сначала поднимите тот же Compose-проект и дождитесь готовности executor, чтобы он удалил оставшиеся sandbox-контейнеры.

## Клиент

Реализован CLI. Для mounted v2 порядок подключения клиента и проверки изменений в выбранной папке приведён в [инструкции ThinkPad ↔ desktop](docs/two-host-checkpoint2.md). Пример ниже работает с **CPU-v1** после запуска сервера. Mock записывает в рабочую папку файл `mock.txt`.

```bash
sid=$(docker compose run --rm cli new --request-id demo-session)
tid=$(docker compose run --rm cli task "$sid" 'Проверка серверного пути' --request-id demo-task)
docker compose run --rm cli task-watch "$tid"
docker compose run --rm cli exec "$sid" -- cat /work/mock.txt
docker compose run --rm cli close "$sid"
```

После обрыва соединения `task-watch --after N` продолжает чтение событий после номера N. Активную задачу можно отменить через `task-cancel TASK_ID`. В CPU-v1 отмена и таймаут закрывают сессию и удаляют временные файлы. При обычном завершении они сохраняются до `close`.

## Тесты

Команды запускаются из корня репозитория. [Полный перечень шагов GitHub Actions](.github/workflows/cpu.yml) · [Успешная проверка сдаваемой ревизии ЧП2](https://github.com/alexdtat/agentic-llm-runtime/actions/runs/44729876856)

| Команда | Что проверяет |
|---|---|
| `python3 -m unittest -v tests.test_runtime_v2` | HTTP и CLI v2, историю сессии, отмену и восстановление |
| `python3 -m unittest -v tests.test_mounted_runtime tests.test_mounted_failures` | Mounted-профиль, режимы доступа и обработку сбоев офлайн |
| `python3 -m track_a.baf_smoke --output results/baf-smoke-ci` | Восемь простых задач со скриптовым mock; сохраняет результаты попыток |
| `python3 -m unittest -v tests.test_baf_smoke` | Границы smoke-теста и классификацию исходов |
| `make test` или `./scripts/check.sh` | Docker-интеграцию CPU-v1, исторический replay Б1–Б5, восстановление после перезапуска и очистку |

Docker-проверка создаёт отдельный Compose-проект и сохраняет журналы в `results/agentic-check-*/`. Для полного БАФ с реальной моделью есть [отдельная команда и требования к стенду](docs/baf-smoke-v2.md#полный-живой-mounted-баф). Для замеров A-3.6 и A-5.6 опубликованы [методика, сырые значения, медиана и p95](docs/checkpoint2-evidence/measurements.json).

## Структура

| Компонент | Где искать |
|---|---|
| Клиент и контракты | [CLI](track_a/cli.py), [HTTP-клиент](track_a/client.py), [схема v2](track_a/protocol-v2.schema.json) |
| Gateway и харнес v2 | [Application](track_a/application.py), [агентный цикл](track_a/run_loop.py) |
| LLM-прокси и провайдер | [ModelProxy](track_a/model_proxy.py), [адаптер модели](track_a/model.py) |
| Сандбокс и монтирование | [MountedExecutor](track_a/mounted_executor.py), [стенд, broker и двуххостовый runner](prototypes/mounted-runtime/) |
| БАФ | [Задачи v2](track_a/baf-v2/), [исторические фикстуры](track_a/baf/), [чекер результатов](track_a/baf_grade.py) |
| Отчёты | [ЧП2](docs/checkpoint-2.md), [свидетельства ЧП2](docs/checkpoint2-evidence/), [исторический пакет ЧП1](docs/checkpoint-1.md) |

## Модели

В полном БАФ для ЧП2 использована локальная **Qwen3.8-27B** через ModelProxy. Она сама генерировала действия. Доступ к целевой DeepSeek на момент прогона ещё не был получен; соответствие Qwen порогу Artificial Analysis Intelligence Index на дату выбора не подтверждено. В CI smoke v2 использует скриптовый mock. [Условия и ограничения эксперимента](docs/checkpoint-2.md) · [Критерий выбора модели](https://github.com/deeppavlov/agentic-course-itmo/blob/main/projects/README.md#0-1-модели)

Для внешнего провайдера в CPU-v1 задайте `MODEL_BACKEND=openai-compatible`, `MODEL_BASE_URL` с суффиксом `/v1`, `MODEL_NAME` и при необходимости `MODEL_API_KEY`, затем пересоздайте gateway. Адрес должен быть доступен из контейнера: его `127.0.0.1` указывает на сам контейнер. Ключ провайдера в CPU-v1 получает gateway. [Настройки CPU-v1](track_a/README.md)
