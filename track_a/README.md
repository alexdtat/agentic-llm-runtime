# Runtime A

Канонические команды сборки/проверок находятся в [корневом README](../README.md).
Из корня: `docker compose up --build -d --wait`; CPU-проверки: `./scripts/check.sh`.

Gateway (`server.py`) владеет задачами, циклом и модельным адаптером. Executor
(`executor.py`) владеет сессиями, командами и Docker. У gateway нет socket;
у executor нет model key; сандбокс не получает ни те, ни другие.

- [Клиентский контракт v1](protocol-v1.md): задача, события, результат и отмена.
- [Командный контракт v0](protocol-v0.md): сессия, запуск, получение, отмена, release.
- `cli.py`: task/task-watch/task-cancel и низкоуровневые new/exec/watch/cancel/close.
- [Системный дизайн и остаточные риски](../docs/system-design-review.md).

События пока передаются polling, результат stdout/stderr появляется после
завершения команды. `--after` восстанавливает позицию отображения, сервер хранит
всю последовательность. При cancel/timeout временный workdir уничтожается.

Legacy `harness.py` сохранён для интерпретации результатов ЧП1. Новый серверный
цикл — `Gateway.execute`, технический mock — `model.py`. Их нельзя смешивать
при подсчёте попыток и оценке качества.
