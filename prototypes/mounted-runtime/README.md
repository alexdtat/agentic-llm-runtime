# v2 с Docker и клиентской папкой через SSHFS

Локальная проверка на одном Linux-хосте. CLI обращается к v2 Application, mock
выбирает инструменты, Docker запускает их в `/work`. Это SSHFS поверх штатного
OpenSSH SFTP. Сеть sandbox отключена, Docker socket есть только у процесса
исполнителя на хосте. В режиме fixture по-прежнему используются `fixture-new`
и карта файлов, без Docker и mount.

## Повторить синтетическую проверку

Из корня checkout, с установленными Docker Compose и FUSE и готовыми образами
`agentic-sandbox:cpu-v1`, `course-feasibility:2026-11-06`. Скрипт подготовки
делает отдельный временный каталог с тестовыми файлами и ключами. Он печатает
путь, дальнейшие действия ограничены этим путём.

```sh
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD"
PROBE_ROOT="$(python3 prototypes/mounted-runtime/prepare.py)" && export PROBE_ROOT
test -d "$PROBE_ROOT/mount/work"
sudo mount --bind "$PROBE_ROOT/mount" "$PROBE_ROOT/mount" &&
sudo mount --make-rshared "$PROBE_ROOT/mount"
prototypes/mounted-runtime/stack.sh up &&
python3 prototypes/mounted-runtime/smoke.py
```

После прогона, в том числе после ошибки, сначала убрать процессы и SSHFS,
потом снять вспомогательный mount. Не продолжать к umount при ошибке cleanup.

```sh
prototypes/mounted-runtime/stack.sh cleanup &&
sudo umount -- "$PROBE_ROOT/mount"
```

Если cleanup сообщает об оставшемся child mount, проверить
`findmnt -rnC --mountpoint "$PROBE_ROOT/mount/work" -o TARGET,FSTYPE`. После
подтверждения отсутствия контейнеров и точного типа `fuse.sshfs` снять его
обычным `sudo umount -- "$PROBE_ROOT/mount/work"` и повторить cleanup. Логи и
синтетические данные остаются под `/tmp`. Экспортёр и broker ограничены 180
секундами, команда sandbox шестью секундами, образы берутся из кэша.

## Границы

- Chroot SFTP содержит только `/work`. Перед запуском сканируем выбранную папку,
  отклоняем ссылки, hardlinks, специальные файлы и вложенные mountpoints. В
  SFTP запрещены операции создания symlink/hardlink. Существующие `.git` файлы
  и каталоги на любой глубине закрыты пустыми bind-накладками только для чтения.
  Доступное имя накладки остаётся пустым. В других местах shell или SFTP
  могут создать новые `.git`. Это защита существующих метаданных при
  доверенном клиенте. Между подготовкой и остановкой клиент может изменять
  содержимое обычных файлов, но должен сохранять структуру папки,
  Git-метаданные, ссылки и mounts. Probe использует только синтетические
  данные. Отдельный `prepare.py --client PATH` предназначен для доверенного
  оператора, который разделяет эти условия.
- Приложение с `MOUNT_OWNER` принимает `mount-new GRANT`. Grant сопоставляется
  с одним заранее выбранным `SSHFS_MOUNT` в Private Execution API, клиент
  передаёт только имя. Для профиля нужен один владелец и отдельный каталог
  состояния. Одновременно разрешена одна сессия на экспорт. Запуск на
  хосте через `python3 -m track_a.mounted_executor` читает `SSHFS_MOUNT`,
  `MOUNT_GRANT`, `RUNTIME_SCOPE`, `STATE_DIR`, `PROBE_TOKEN` и `PORT`.
  `DOCKER_HOST` должен быть `unix:///var/run/docker.sock`, `DOCKER_CONTEXT`
  отсутствовать. Application и Model Proxy используют обычные точки входа.
- После остановки collect читает из mount до 16 файлов, названных в успешных
  ReadFile/WriteFile данного run, максимум 6144 байта на файл. Изменения через
  RunCommand доступны на клиенте сразу, collect не сканирует всю папку.
  Предел ReadFile также 6144 байта. Внешняя запись и SQLite не образуют общей
  транзакции. При неизвестном исходе инструмент не повторяется, уже видимые
  клиенту изменения могут остаться.
- `session-close` освобождает аренду, lifecycle самого экспорта принадлежит
  `stack.sh cleanup`. Текущая проверка не покрывает две машины, RO-экспорт для
  всех файлов, квоту записи, враждебное изменение экспортируемого дерева
  клиентом и прерывание уже зависшего I/O. Ресурсные лимиты относятся к
  контейнерам, у клиентской папки файловой квоты нет.
