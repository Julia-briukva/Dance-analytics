# Dance

Первый этап backend/data layer для турниров Санкт-Петербурга сезона 2025-2026.

## Что создаётся

- `database/compreg_spb_2025_2026.sqlite` - локальная SQLite-база.
- `data/cache/listcat/` - кэш страниц категорий турниров.
- `data/cache/protocols/` - кэш HTML протоколов.
- `data/cache/missing/` - отметки об отсутствующих страницах, чтобы не скачивать их повторно.
- `reports/` - место для будущих выгрузок.

## Установка

```bash
python3 -m pip install -r requirements.txt
```

## Запуск

```bash
python3 scripts/build_spb_database.py
```

Полезные параметры:

```bash
python3 scripts/build_spb_database.py --workers 12 --max-suffix 50
python3 scripts/build_spb_database.py --start-date 2025-09-01 --end-date 2026-08-31
python3 scripts/build_spb_database.py --refresh
python3 scripts/build_spb_database.py --debug-city
python3 scripts/update_database.py
python3 scripts/parse_protocols.py
python3 scripts/analytics.py --idd 2016461
```

По умолчанию скрипт перебирает даты с `2025-09-01` по `2026-08-31`, проверяет адреса вида:

```text
https://compreg.ru/resultsdata/YYYY/MM/DDXX/listcat.php
```

где `XX` - порядковый номер турнира в этот день от `00` до `50`.

`checked_listcats` хранит уже проверенные `listcat.php`, поэтому повторный запуск не делает HTTP-запрос для URL, которые уже были проверены как существующие или missing.

Для будущих обновлений используйте:

```bash
python3 scripts/update_database.py
```

Он проверяет только даты после последней найденной даты турнира в базе.

## Нормализация протоколов

После загрузки HTML-протоколов запустите:

```bash
python3 scripts/parse_protocols.py
```

Скрипт читает локальный кэш `data/cache/protocols/`, не обращается к сети и заполняет:

- `protocol_judges` - судьи протокола, их индекс и позиция;
- `protocol_dancers` - участники протокола, номер, IDD, клуб, город, класс, место;
- `marks` - нормализованные оценки по схеме protocol/round/dance/judge/dancer/mark/place;
- `protocol_parse_status` - статус парсинга, число туров, участников, судей и оценок.

Лог парсинга сохраняется в:

```text
reports/parse_protocols.log
```

## Аналитика по конкретному танцору

Используйте helper, который принимает внешний Compreg IDD и внутри разрешает его в `dancers.id`.
Все рабочие analytics-фильтры идут по идентификаторам, а ФИО используется только как отображаемое поле:

```bash
python3 scripts/analytics.py --idd 2016461
python3 scripts/analyze_dancer.py --idd 2016461
python3 scripts/analyze_dances.py --idd 2016461
python3 scripts/build_dancer_report.py --idd 2016461
```

В коде доступна функция `get_marks_for_dancer(conn, compreg_idd)`. Она возвращает строки из `marks_enriched` только для выбранного Compreg IDD через связку `external_ref -> dancers.id -> marks.dancer_id`.

## Текущий объём этапа

Скрипт сохраняет metadata по турнирам и протоколам:

- найденный URL `listcat.php`;
- локальный путь к HTML;
- ID турнира из `LoadCategoryRes(...)`;
- ID протокола;
- URL и локальный путь протокола.
- raw/normalized city, источник city detection и rejected-диагностику.
- `checked_listcats` с `url`, `exists`, `checked_at`, `city_detected`, статусом и путём к кэшу.

## City detection

Город определяется централизованно через `normalize_city_name()` и словарь `CITY_ALIASES` в `scripts/build_spb_database.py`.
Чтобы добавить другой город, достаточно добавить новый ключ в `CITY_ALIASES` и человекочитаемое имя в `CITY_LABELS`.

Debug-лог сохраняется в:

```text
reports/city_detection_debug.log
```

Таблицы `dancers`, `judges`, `marks` уже созданы в схеме, но глубокий разбор содержимого протоколов будет следующим этапом.
