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
unset DOCKER_CONTEXT
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD"
PROBE_ROOT="$(python3 prototypes/mounted-runtime/prepare.py)" &&
export PROBE_ROOT &&
test -d "$PROBE_ROOT/mount/work" &&
sudo mount --bind "$PROBE_ROOT/mount" "$PROBE_ROOT/mount" &&
sudo mount --make-private "$PROBE_ROOT/mount" &&
sudo mount --make-rshared "$PROBE_ROOT/mount" &&
prototypes/mounted-runtime/stack.sh up
```

После успешного `up` для обычной проверки выполните отдельно.

```sh
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

## Лимиты изнутри mounted-контейнера

Для этого прогона подготовьте свежий синтетический RW-стенд по шагам выше до
`stack.sh up` включительно. Нужен локальный Docker с cgroup v2, где
`/proc/self/cgroup` внутри sandbox показывает `0::/`. Оставьте `PROBE_ROOT`
экспортированным, уберите `DOCKER_CONTEXT` и выполните из корня checkout в
оболочке без трассировки команд (`set +x`).

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" DOCKER_HOST=unix:///var/run/docker.sock \
MOUNT_LIMITS_CANARY="$(python3 -c 'import secrets; print(secrets.token_hex(16))')" \
python3 prototypes/mounted-runtime/smoke_limits.py
```

Сценарий измеряет лимиты и признаки изоляции одним `RunCommand` в отдельном
run. Canary задаётся на стороне исполнителя, в отчёт попадают только признаки
наличия. Успех требует `"status": "passed"` и `"cleanup_state": "confirmed"`.
При расхождении или неподтверждённом закрытии сессии код возврата ненулевой.

После прогона с любым кодом выполните очистку из раздела выше. `stack.sh cleanup`
проверяет отсутствие контейнеров, сети проекта и SSHFS. После снятия self-bind
проверьте обе точки монтирования. Команда должна завершиться с кодом 0 и без
вывода.

```sh
awk -v mount="$PROBE_ROOT/mount" '
    $5 == mount || $5 == mount "/work" { print; remaining = 1 }
    END { exit (remaining ? 1 : 0) }
' /proc/self/mountinfo
```

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
  Оператор задаёт `MOUNT_ACCESS_MODE=ro` для RO-экспорта. По умолчанию
  `MOUNT_ACCESS_MODE=rw`. Грант привязан к настроенному экспортёру и режиму,
  а CLI и API принимают только имя гранта. Режим сохраняется в журнале
  аренды. При изменении режима оператор закрывает старую сессию и создаёт новую.
  `DOCKER_HOST` должен быть `unix:///var/run/docker.sock`, `DOCKER_CONTEXT`
  отсутствовать. Application и Model Proxy используют обычные точки входа.
- После остановки collect читает из mount до 16 файлов, названных в успешных
  ReadFile/WriteFile данного run, максимум 6144 байта на файл. Изменения через
  RunCommand доступны на клиенте сразу, collect не сканирует всю папку.
  Предел ReadFile также 6144 байта. Внешняя запись и SQLite не образуют общей
  транзакции. При неизвестном исходе инструмент не повторяется, уже видимые
  клиенту изменения могут остаться.
- `session-close` освобождает аренду, lifecycle самого экспорта принадлежит
  `stack.sh cleanup`. Текущая проверка не покрывает две машины, квоту записи,
  враждебное изменение экспортируемого дерева
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
Проверка health ищет запись fuse.sshfs через findmnt. Для RO также проверяются
флаги VFS точной точки монтирования.

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

## Режим read-only и проверка A-3.3

Используйте RO для чтения, поиска и анализа без правок клиента. Используйте RW
для задач, которым нужно менять клиентскую папку. В обоих режимах накладки
скрывают исходное содержимое существующих `.git`, а ключи и состояние харнеса
размещаются вне экспортируемого каталога.

`prepare.py --access-mode ro` включает OpenSSH `internal-sftp -R` и монтирует
клиентский источник в exporter только для чтения. Broker добавляет `-o ro`
для SSHFS. Исполнитель с `MOUNT_ACCESS_MODE=ro` требует RO SSHFS на хосте и
монтирует `/work` в sandbox только для чтения. Перед запуском инструментов
проверяется флаг `ST_RDONLY` внутри контейнера. `WriteFile` возвращает
сохранённый в журнале `command_failure`. Остальные инструменты могут выполняться
в том же run. Системные вызовы `RunCommand` ограничены режимом `/work` на уровне
ядра. Collect использует отдельный RO bind и читает успешные артефакты.

Для проверки на согласованном Linux-хосте используйте отдельный синтетический
стенд с теми же пререквизитами и cached images, что и для RW. Выполните из
корня checkout.

```sh
sudo -v && bash prototypes/mounted-runtime/live_readonly.sh
```

Обёртка проверяет доступ к локальному Docker, FUSE, cached images и `sudo -n`
до подготовки. Затем создаёт свежий синтетический экспорт и запускает
`smoke_readonly.py` с лимитом 120 секунд.

Проверка сверяет синтетические файлы и фактический источник exporter до операций.
Прямой SFTP читает файл и получает четыре отказа в создании, перезаписи,
переименовании и удалении. Через CLI проверяются отказ `WriteFile`, четыре
`EROFS` из `RunCommand`, успешные `ReadFile` и collect, неизменность
байтов пяти файлов и отсутствие sandbox после закрытия сессии.

Обёртка завершает стенд и при ошибке или сигнале. Она снимает свой self-bind
после подтверждённого `stack.sh cleanup`, проверяет отсутствие монтирований
и удаляет синтетические файлы. При сбое очистки сохраняет каталог стенда.
Путь к отдельному каталогу свидетельств в `/tmp` выводится в начале работы.
В нём сохраняются `summary.json` и раздельные stdout/stderr, включая smoke
и cleanup. Exit 0 и `"status": "passed"` требуют успешных smoke и cleanup.
Exit 1 сообщает об ошибке проверки или очистки, exit 2 означает отказ preflight.

Если smoke прошёл, а cleanup завершился ошибкой, запустите dry run на том же
хосте с исходным каталогом свидетельств.

```sh
python3 prototypes/mounted-runtime/recover_readonly.py /tmp/agentic-mounted-ro-evidence.XXXXXXXX
```

После согласования повторной очистки подставьте выведенные `self_bind_id`
и `sshfs_id` вместо значений ниже. Для отсутствующего mount используйте
`none`. Скрипт сверяет IDs, снимает остаточный SSHFS только после подтверждения
удаления scoped Docker-ресурсов и проверяет таблицу монтирований после снятия
self-bind. Журнал `recovery.*.log` добавляется к исходным свидетельствам.

```sh
sudo -v && python3 prototypes/mounted-runtime/recover_readonly.py \
  /tmp/agentic-mounted-ro-evidence.XXXXXXXX --apply SELF_BIND_ID SSHFS_ID
```

Offline-регрессия RO и RW входит в указанную выше команду `unittest`.
Результаты доступны в `results/t08-mounted-ro/report.md`.
