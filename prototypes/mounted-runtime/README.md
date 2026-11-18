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

Проверки отказов без Docker, FUSE и сетевых соединений запускаются из корня checkout.
Они используют настоящие MountedExecutor, Application и mock ModelProxy, подменяя
findmnt, Docker CLI и private transport. Отдельный дочерний Python-процесс завершает
себя после durable intent и синтетического побочного эффекта для проверки recovery.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_mounted_runtime test_mounted_failures
```

Покрыты отказ start, неизвестный исход команды, восстановление и дедупликация,
потерянный ответ, отказ cleanup, quarantine, collect и очистка payload при release.
После неуспешного start/stop новый exec блокируется. Незавершённая очистка collect
сохраняется в журнале и блокирует start/release до подтверждённого удаления.
При рестарте исполнитель удаляет оставшиеся контейнеры своего scope до открытия API. Отсутствующий mount
отражается в health и блокирует новый start, lookup/stop/release остаются доступны.
Проверка health определяет только наличие записи fuse.sshfs через findmnt.

Эти offline проверки моделируют инфраструктурные ответы. Реальный обрыв SSHFS,
cached/disconnected FUSE, зависший I/O и исчезновение всех процессов после cleanup
требуют live-проверки. Само наличие mountinfo и успешный stat('/work') не доказывают,
что удалённый экспорт доступен. Свидетельства T08 сохранены в
results/t08-mounted-failures/report.md.

На обычном согласованном Linux-хосте фактический обрыв SSHFS проверяется одной
командой из корня checkout. Нужны UID 1000, `/dev/fuse`, доступ к локальному Docker,
Compose и уже готовые образы из описания выше. Для точных mount/umount требуется
действующий sudo timestamp. При необходимости выполните `sudo -v` в своём терминале
перед запуском. Runner использует только `sudo -n` и не запрашивает пароль.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 prototypes/mounted-runtime/live_failure.py
```

Runner сначала выполняет preflight. `NoNewPrivs=1`, иной Docker host/context,
недоступный daemon/FUSE или отсутствующий cached image останавливают проверку до
создания стенда. После успешного preflight весь процесс закреплён за локальным
Docker socket. Затем `prepare.py` без `--client` создаёт только синтетический экспорт
под уникальным `/tmp/agentic-mounted-runtime.*`, а `stack.sh` поднимает его ресурсы.
Существующие mounts, контейнеры или сеть вычисленного scope отклоняются до захвата
ресурсов. Cleanup ограничен успешно захваченными путями и scope.

Реальная команда через CLI/Application/mock/MountedExecutor один раз записывает
9 байт в `single-write.txt`, делает fsync и ждёт handshake в sandbox `/tmp`.
Runner подтверждает владение exporter по Docker labels, останавливает только его
точный ID, подтверждает остановку и разрешает команде читать `after-cut.txt` через
SSHFS. У управляющих docker exec задан `--workdir /tmp`. Для успешного результата
нужны marker `IO_BEGIN` после подтверждённого обрыва и errno операции либо отсутствие
завершения I/O до штатного worker timeout. Таймаут handshake, потерянный вывод или
успешное чтение без свидетельства I/O failure дают ошибку/inconclusive.
Продуктовые лимиты команды не меняются.

Проверка требует реального unknown outcome, отказа lookup/повторного exec/нового
op_id этого run, единственной записи и подтверждённого stop. До аварийного общего
cleanup отдельно проверяется исчезновение sandbox и его host PID со starttime,
чтобы cleanup стенда не скрывал ошибку product stop. Наблюдающая обёртка сохраняет
настоящие ответы Docker worker и partial stdout при транспортном timeout, передавая
их исполнителю без изменений. Инфраструктурные результаты в этом runner реальные.
HTTP токены случайны и не записываются в логи.

На stdout печатается каталог свидетельств. В нём сохраняются `live-failure.jsonl`,
`run-result.json`, `product-stop-check.json`, `owned-processes.json` и
`live-failure-summary.json`. При ошибке сохраняются traceback и результат cleanup.
Probe ограничен 100 секундами, cleanup — дополнительными 90. В finally вызывается
`stack.sh cleanup`, затем проверяются отсутствие принадлежащих стенду контейнеров,
сетей, SSHFS/self-bind mounts и сохранённых PID identities. Остаточный точный SSHFS
mount снимается обычным umount только после подтверждённого отсутствия контейнеров
и сети. При неполном cleanup итог остаётся failed, диагностический каталог сохраняется.
Exit 0 означает все проверки и cleanup подтверждены, exit 1 — ошибка/inconclusive,
exit 2 — preflight blocked. Текущий workspace отклонён на preflight по NoNewPrivs,
свидетельство находится в `results/t08-mounted-failures/live-preflight.log`.
