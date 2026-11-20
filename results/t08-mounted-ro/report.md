# T08 · Mounted read-only · A-3.3

Оператор выбирает `prepare.py --access-mode ro` и `MOUNT_ACCESS_MODE=ro`
для одного экспорта и гранта. Исходная папка в exporter, SFTP, SSHFS и
`/work` в sandbox ограничены чтением. Известный отказ `WriteFile`
сохраняется по `op_id`. Чтение и collect остаются доступны. Сессии
с прежними журналами используют RW. Для смены режима нужна новая аренда.

| Проверка | Результат |
|---|---|
| Mounted + runtime state offline | **33 из 33**, включая 3 проверки повторной очистки. [Журнал](offline-regression.log) |
| Точный шаг CPU CI в чистой временной копии исходников | **27 из 27** |
| Offline с дополнительными CLI и БАФ checkers, системный Python | **36 из 36** |
| Render двух конфигураций через Compose 2.40.3 | RO использует read-only источник, отдельный sshd config с `-R`, `SSHFS_ACCESS_MODE=ro` и три read-only маски `.git`. RW сохраняет запись, исходный sshd config и маски. |
| Отказ запуска live smoke без подготовленного каталога | Exit 1 до обращения к Docker |
| Хостовая обёртка в ограниченной среде | Exit 2, `status=blocked` по `NoNewPrivs` до подготовки экспорта |
| Хостовая обёртка с подменой внешних команд | 4 сценария. Успех, ошибка setup, ошибка smoke и ошибка cleanup. При ошибке cleanup self-bind остаётся нетронутым. |
| Синтаксис Python и shell, `git diff --check` | Пройдены |

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_mounted_runtime test_mounted_failures test_runtime_state
```

Live-проверка SFTP, SSHFS, VFS sandbox и очистки описана в
[README](../../prototypes/mounted-runtime/README.md#режим-read-only-и-проверка-a-33).
Offline-тесты подменяют ответы Docker,
mount discovery и worker. В этой среде loopback HTTP для отдельной suite v2
блокируется ограничением на создание сокетов. Исторические результаты и БАФ
не редактировались.

## Фактический прогон на хосте

Smoke завершился с кодом 0. Прямой SFTP прочитал файл и отклонил четыре
мутации. Sandbox отклонил пять мутаций, чтение и collect прошли, пять
клиентских файлов сохранили исходные байты. Очистка завершилась с кодом 137
примерно через 65 секунд, оба её лога были пусты. Прогон в целом имеет статус
`failed`. Результаты воспроизведены из ранее прочитанного хостового evidence
`/tmp/agentic-mounted-ro-evidence.bOIRZf6e`. [Smoke stdout](live-host-smoke.stdout.log) · [Общий статус](live-host-summary.json)

Вероятная точка зависания — проверка `test -L` для `mount/work` в начале
`stack.sh`. Она выполнялась до первого диагностического сообщения и могла
войти в зависший FUSE. Это объяснение по коду, хостовой трассировки вызова нет.
Исправление читает `/proc/self/mountinfo` в cleanup. Повторная очистка из
sandbox не запускалась, фактические остатки проверяются на исходном хосте.

```sh
python3 prototypes/mounted-runtime/recover_readonly.py /tmp/agentic-mounted-ro-evidence.bOIRZf6e
```

Dry run печатает оба mount ID. После разрешения на очистку подставьте их в
порядке `self_bind_id sshfs_id`, используя `none` для отсутствующего mount.

```sh
sudo -v && python3 prototypes/mounted-runtime/recover_readonly.py \
  /tmp/agentic-mounted-ro-evidence.bOIRZf6e --apply SELF_BIND_ID SSHFS_ID
```
