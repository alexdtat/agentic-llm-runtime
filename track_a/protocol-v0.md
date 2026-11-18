# Командный протокол v0 — CPU-v1

Private gateway↔executor API; тот же набор маршрутов gateway оставляет для
headless client и fixture/replay runner. Все запросы, кроме health, требуют
Bearer token. Контракт JSON: [schema](protocol-v0.schema.json). Mount/image/host
path/volume/network не задаются клиентом; лишние поля отклоняются.

| Метод | Путь | Вход → результат |
|---|---|---|
| GET | `/v0/health` | readiness |
| POST | `/v0/sessions` | create_session → session |
| GET | `/v0/sessions/{id}` | session |
| POST | `/v0/sessions/{id}/runs` | create_run → run |
| GET | `/v0/runs/{id}` | run с накопленными events |
| POST | `/v0/runs/{id}/cancel` | empty → run |
| DELETE | `/v0/sessions/{id}` | empty → session |

`request_id`: 1–80 ASCII букв/цифр/`_`/`-`. Run содержит argv (1–100 строк без NUL)
и timeout_seconds (0.05–10). Тело HTTP до 16384 байт; на чтение — 5 секунд.
JSON syntax/type — 400, отсутствующий объект — 404, конфликт/занятая сессия — 409,
неподтверждённая очистка/недоступная зависимость — 503. Подробности внутренних
исключений клиенту не раскрываются.

Ключ создания сессии глобален для одного сервера; ключ команды ограничен
сессией. Повтор неизменного запроса возвращает прежний объект даже после
закрытия/рестарта. Другой payload — 409. Журнал переживает restart, но удаляется
через `down -v`; после удаления журнала идемпотентность не обещается.

На сессию одна команда. Events: started (sequence 0), затем ровно один result
(sequence 1), когда исход определён. При невозможности связаться с daemon cancel
не обещает terminal: сначала нужна подтверждённая очистка. Stdout/stderr ограничены
65536 байт каждый; временные файлы вывода дополнительно ограничены tmpfs.
Это **polling законченного результата**, не поток stdout.

`error_kind`: command (exit != 0, включая отсутствующую программу), timeout,
cancelled, sandbox (worker/контейнер), transport (неопределённый docker exec),
interrupted (восстановление), cleanup_failed. Автоматического повтора команды нет.
После HTTP timeout сначала GET по известному ID или тот же POST/request_id;
нельзя создавать новый ключ, не выяснив предыдущий исход.

**Уточнение семантики относительно ЧП1:** cancel/timeout уничтожают контейнер и
временный `/work`, закрывая сессию. Это гарантирует остановку также отделённых
процессов. Уже записанный terminal неизменен; нормальное завершение оставляет
сессию открытой. Исторический тест отмены с чтением файла после cancel неприменим;
новая семантика проверяется `test_runtime_integration.py`.

Provision/release представлены session routes; exec поддерживает чтение/запись
через argv, collect — доверенные exec в `SessionTools`. Отдельные типизированные
ReadFile/WriteFile/Collect endpoints пока не реализованы. Recovery закрывает
сессии и выдаёт interrupted незавершённым операциям, не возобновляя их.
