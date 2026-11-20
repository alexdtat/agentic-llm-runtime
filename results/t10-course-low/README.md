Курсовые medium/trap B1.2–B5.3, 19 ноября 2026.

В `track_a/baf-v2` добавлены десять самостоятельных задач с точными сообщениями и
уровнями из `reference/course/baf-source/catalog.yaml`. Исторический каталог
`track_a/baf` и исходный курсовой каталог сохранены. Это подготовленные fixtures
и локально проверенные checker-интерфейсы. Оценка качества модели не проводилась.

| Задача | Fixture и проверка |
| --- | --- |
| B1.2 | contract.md содержит 15 явных страниц, 4414 слов и единственный срок оплаты 30 календарных дней |
| B1.3 | В полном договоре отсутствует размер неустойки, ответ НЕТ ДАННЫХ |
| B2.2 | Материализованы 200 Python-модулей, ровно 12 чтений DB_URL и строковые decoys; точная спецификация fixture-spec.json рядом с checker |
| B2.3 | Две parse_date, одна принимает только локальную дату, другая требует ISO 8601 timezone |
| B3.2 | UTF-8 CSV по таблице, даты YYYY-MM-DD, стабильная сортировка и точные значения |
| B3.3 | PyYAML safe_load проверяет mappings и строковые типы NO, ON и индексов 0123/0042 |
| B4.2 | Pure rename трёх файлов, AST/immutable checks и пять скрытых behavior assertions через isolated_python |
| B4.3 | Реальный cp1251 CSV с CRLF, изменяется только цена X, остальные байты сохраняются |
| B5.2 | 20 настоящих unittest cases, 3 падают; python3 run_tests.py выводит краткие имена и итог в пределах 1024 bytes |
| B5.3 | sh build.sh печатает OK, возвращает exit 1 и причину в stderr |

Каждый check.py сохраняет стандартный CLI `python check.py WORKDIR` и JSON
`{"pass": ..., "reason": ...}`. Checker ограничивает размеры и отклоняет symlink
в читаемых путях. Изначальные fixtures сохраняются, кроме явно mutable файлов B4.
Для YAML нужен отдельный grader dependency `track_a/baf-v2/grader-requirements.txt`,
PyYAML==6.0.3. Runtime requirements не изменялись и пакеты не скачивались.
При отсутствии YAML checker возвращает infra_error.

B4.2 предоставляет `check_isolated(workdir, run_python)`. Перед вызовом remote
executor должен получить текущие файлы workdir и выполнять trusted checker program
в изолированном workspace. Без isolated executor CLI возвращает infra_error.
Локальный positive тест подтверждает статическую часть и интерфейс через stub
run_python. Кандидатский Python на хосте не импортируется и не исполняется.
Пять behavior assertions ждут настоящего isolated grader; их прохождение этим
офлайн прогоном не подтверждено.

B5 предоставляет `check_with_evidence(workdir, tool_runs)`. Один record имеет
`request.argv` и `result` с exit_code, stdout, stderr, error_kind и truncated.
Нужен error_kind=command_failure, exit_code=1 и truncated=false. Метаданные
required_tool содержат точные argv. Нужны observed failures/причина stderr, одного
правильного файла недостаточно. CLI принимает вторым аргументом отдельный доверенный
JSON `{"tool_runs": [...]}` вне agent workspace. Без evidence возвращается
infra_error. Привязку authentic events к session/run/op и происхождение evidence
обеспечивает внешний grader. Checker не доказывает происхождение произвольного JSON.
Локальные positive тесты получают exit/stdout/stderr реальным коротким запуском
свежих доверенных B5 fixtures. Файлы агента при этом не исполняются.

Локальная команда из корня копии.

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest -v test_baf_v2_course_b1_b5
```

Результат — exit 0, 9 tests, positive/negative случаи всех десяти задач, CLI checks,
контроль 12/200, cp1251/CRLF, YAML traps, immutable/symlink нарушения, evidence без
нужного exit/stderr/команды и truncated evidence. Полный лог — tests.log.
Использован существующий /usr/bin/python3 с PyYAML 6.0.3. Модель, сеть, Docker,
mounts и пользовательские сервисы для этого набора не запускались.
