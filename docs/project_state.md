# Project State

Этот документ фиксирует актуальное состояние проекта и должен обновляться после крупных изменений: новых этапов парсинга, изменения схемы базы, добавления аналитик, отчетов, UI или методик расчета.

## Цель проекта

Локальная аналитическая система для спортивных бальных танцев на базе данных Compreg.

Система должна собирать результаты турниров, хранить локальный HTML-cache, нормализовать протоколы в SQLite и строить аналитику по танцорам, судьям, танцам, динамике и стабильности результатов. Целевой пользовательский слой: отчеты и/или UI для регулярного анализа выступлений.

## Текущая архитектура

```text
Input: --idd
↓
Analytics
↓
Report JSON
↓
HTML
↓
PDF
↓
Future UI
```

Future product workflow:

```text
Input page
↓
IDD + period + city
↓
resolver / enrichment
↓
analytics
↓
JSON
↓
HTML report
↓
PDF later
```

Data pipeline:

```text
Compreg
↓
Crawler
↓
HTML cache
↓
Parser
↓
SQLite
↓
Analytics / Report Layer
```

System Requirement:

> Dancer ID является основным идентификатором аналитики.
>
> ФИО используется только как отображаемое поле и как будущий механизм поиска.
>
> Все analytics, reports, JSON, HTML, PDF и UI должны работать для любого танцора без изменения кода.

Фактические компоненты:

- `scripts/build_spb_database.py` собирает турниры Санкт-Петербурга, загружает `listcat.php`, находит protocol pages и сохраняет metadata.
- `scripts/update_database.py` запускает инкрементальное обновление после последней найденной даты турнира.
- `data/cache/listcat/` хранит локальный cache страниц категорий.
- `data/cache/protocols/` хранит локальный cache HTML-протоколов.
- `data/cache/missing/` хранит маркеры отсутствующих страниц.
- `scripts/parse_protocols.py` парсит локальные HTML-протоколы и заполняет нормализованные таблицы.
- `database/compreg_spb_2025_2026.sqlite` хранит турниры, протоколы, участников, судей и оценки.
- `scripts/analyze_dancer.py` строит расширенную аналитику по одному танцору по runtime `--idd`.
- `scripts/analyze_dances.py` строит dance-level analytics по одному танцору по runtime `--idd`.
- `scripts/build_dancer_report.py` собирает единый JSON report по runtime `--idd`.
- `templates/report.html.j2` и `scripts/render_html_report.py` рендерят пользовательский HTML report поверх готового JSON.
- `templates/index.html.j2` и `scripts/render_index_page.py` рендерят статический prototype стартовой страницы продукта.
- `reports/` содержит текущие отчеты и логи.

Текущий слой вывода: JSON report, HTML report, статический input-page prototype и текстовые диагностические отчеты. PDF и полноценный UI пока не реализованы.

### Деплой GitHub Pages для отчётов

GitHub Pages публикует статические HTML-файлы, а не Jinja-шаблоны.

После любых изменений в:

- `templates/report.html.j2`;
- встроенном JavaScript отчёта;
- логике фильтрации периода;
- логике аналитических расчётов;
- логике отображения данных;

недостаточно обновить только шаблон или исходный код.

Обязательный цикл публикации:

1. Пересобрать отчёты.
2. Обновить root-копии отчётов для GitHub Pages.
3. Убедиться, что корневые `dancer_*_report.html` содержат актуальный код.
4. Закоммитить обновлённые HTML/JSON отчёты, если они используются GitHub Pages.
5. Выполнить push.
6. Проверить опубликованную версию на GitHub Pages, а не только локальную версию.
7. После публикации выполнить hard refresh и убедиться, что отображается новая версия отчёта.

При проверке GitHub Pages нужно убедиться, что опубликованный HTML содержит актуальные маркеры новой версии: функции, `data-*` атрибуты или другие признаки изменения. Визуальной проверки страницы недостаточно.

## Current Status

Текущее фактическое состояние проекта соответствует содержанию базы и локального cache.

```text
tournaments in database  20
protocol pages in DB      1161
protocol HTML cache       1683
dancers                   1877
judges                    225
marks                     491031
external Compreg IDD rows      1
```

Текущий `protocol_parse_status` после parser fix:

```text
parsed   1123
partial    37
skipped     1
```

Детализация неуспешных статусов:

```text
partial  no marks parsed                        28
partial  unsupported_fkt_round_layout            9
skipped  no_round_sections_result_summary_only   1
```

Текущий count by `mark_type`:

```text
numeric_place       252394
cross               238637
not_available            0
aggregate_place          0
skipped_aggregate        0
unknown                  0
```

Affected rows after parser fix:

```text
total marks before fix   451749
total marks after fix    491031
net added marks           39282
parsed protocols delta      +28
partial protocols delta     -28
unknown marks delta         -30
```

Cleaned parser noise:

```text
raw '#Н/Д' rows after fix  0
raw '5,5' rows after fix   0
known garbage rows cleaned 62
```

The 62 cleaned garbage rows are previous char-wise artifacts from `#Н/Д` and `5,5`: 30 `unknown` rows plus 28 false `cross` rows from `Н`/`Д` and 4 false `numeric_place` rows from split `5,5`.

Работающие analytics:

- dancer-scoped marks lookup через `scripts/analytics.py`;
- расширенная аналитика одного танцора через `scripts/analyze_dancer.py`;
- judge strictness / softness;
- разрез judge analytics по Standard, Latin и all programs;
- финальная numeric-place аналитика;
- cross analytics отдельно от numeric-place analytics;
- dance analytics через `scripts/analyze_dances.py`;
- strongest / weakest dance;
- dance stability / volatility;
- trend over time;
- rolling average;
- tournament-to-tournament delta;
- missing dances и incomplete protocol warnings.
- Report Layer v1: `reports/dancer_2016461_report.json`.

## Реализовано

- Поиск турниров Санкт-Петербурга на Compreg за сезон 2025-2026.
- Перебор адресов вида `resultsdata/YYYY/MM/DDXX/listcat.php`.
- Централизованная city detection для Санкт-Петербурга.
- Разделение найденных турниров на принятые и отклоненные по городу.
- Локальный cache для `listcat.php`.
- Локальный cache для protocol pages.
- Маркеры missing pages, чтобы не загружать отсутствующие страницы повторно.
- SQLite database `database/compreg_spb_2025_2026.sqlite`.
- Таблицы metadata: `tournaments`, `protocols`, `checked_listcats`, `rejected_tournaments`.
- Нормализованные таблицы: `dancers`, `judges`, `protocol_dancers`, `protocol_judges`, `marks`, `protocol_parse_status`.
- View `marks_enriched` для аналитики по оценкам с контекстом турнира, протокола, судьи и танцора.
- Parser protocol pages:
  - metadata протокола;
  - участники;
  - судьи;
  - туры;
  - танцы;
  - marks;
  - обычная Compreg-разметка;
  - FKT/EADC-разметка `.round-data-box-fkt`;
  - тип оценки: `numeric_place`, `cross`, `not_available`, `aggregate_place`, `unknown`.
- Judge analytics для выбранного танцора.
- Strictness / softness analytics.
- Разделение strictness по программам: Standard, Latin, all.
- Cross analytics отдельно от numeric-place analytics.
- Методология judge strictness: `docs/analytics_methodology.md`.
- Dance analytics methodology: `docs/dance_analytics_methodology.md`.
- Dance analytics script:
  - `final_avg_place`;
  - `judge_avg_place`;
  - `median_place`;
  - `std_deviation`;
  - `variance`;
  - `best_by_final_average`;
  - `best_by_median`;
  - `most_stable`;
  - `best_peak`;
  - `worst_by_final_average`;
  - `judge_level_best`;
  - `most_stable_dance`;
  - `least_stable_dance`;
  - `trend_over_time`;
  - `rolling_average`;
  - `tournament_to_tournament_delta`;
  - `missing_dances`;
  - `incomplete_protocol_warnings`.
- Report Layer v1:
  - CLI input: `--idd`;
  - output: `reports/dancer_<idd>_report.json`;
  - `dancer.internal_dancer_id`, `dancer.idd`, `dancer.name`;
  - summary, programs, judges, dances, tournaments, warnings, metadata.
- HTML Report Layer:
  - рендерится из готового JSON без повторного расчета аналитики;
  - два пользовательских режима: родитель и тренер;
  - выводы разделены по программам: стандарт и латина;
  - стабильность отображается через progress bars.
- Static product input page prototype:
  - `reports/index.html`;
  - поля: IDD танцора, период выборки, город выступления;
  - кнопка ведет на пример готового отчета до появления backend workflow.
- Текущий контрольный отчет для `--idd 2016461`:
  - 25 протоколов;
  - 9 турниров / дат;
  - 1504 marks;
  - 726 `numeric_place`;
  - 778 `cross`;
  - 10 танцев;
  - 85 судей.

Текущее состояние базы:

```text
tournaments             20
rejected_tournaments    117
checked_listcats        18615
protocols               1161
protocol_judges         11705
protocol_dancers        12619
dancers                 1877
judges                  225
marks                   491031
protocol_parse_status   1161
```

Статус парсинга протоколов:

```text
parsed   1123
partial    37
skipped     1
```

Типы marks:

```text
numeric_place  252394
cross          238637
unknown             0
```

## В разработке

- Reproducible report generation после Report Layer v1.
- HTML report на основе JSON model.
- PDF export.
- Уточнение интерпретации dance-level метрик с учетом программ, категорий, возраста, класса и solo/pair.
- Проверка неполных протоколов и отсутствующих танцев.

## Known Issues

- Покрытие турниров неполное: в базе сейчас 20 турниров Санкт-Петербурга за сезонный диапазон, но есть 117 отклоненных tournament pages и большой набор missing pages. Нужно отдельно валидировать, не пропущены ли турниры из-за структуры URL, диапазона suffix или city detection.
- Возможны проблемы с city detection: определение города основано на алиасах и текстовых источниках страницы. Турниры с нестандартным заголовком, неполной metadata или городом только внутри protocol pages могут быть ошибочно отклонены или потребовать дополнительного protocol sampling.
- Parser ограничен текущей HTML-структурой Compreg. После parser fix осталось 28 `partial: no marks parsed`, 9 `partial: unsupported_fkt_round_layout` и 1 `skipped: no_round_sections_result_summary_only`.
- Остаточные 9 `unsupported_fkt_round_layout` не парсятся намеренно: в этих FKT/EADC протоколах встречаются raw strings длиной 13-15 при 11 распознанных судьях в canvas. Parser не создает judge-level marks из таких строк, чтобы не добавлять непроверенные оценки.
- Cross analytics неполная по смыслу: сохранены только записанные кресты, а отсутствующие кресты не нормализованы как отрицательные решения.
- `marks_enriched` зависит от runtime-функции `normalize_person_name`, поэтому прямые SQLite-запросы к view вне Python-кода могут падать.
- В текущем HTML-cache ячейки IDD участников пустые, поэтому база пока не может автоматически заполнить `dancers.external_ref` для всех танцоров. Для контрольного танцора `2016461` соответствие сохранено в данных: `external_ref -> dancers.id`.
- Автоматические regression tests добавлены для `parse_mark_string`, но пока нет HTML fixture tests для целых Compreg/FKT/EADC-протоколов.

## Data Strategy

- В SQLite можно хранить больше данных, чем показывается пользователю в HTML/PDF отчете.
- Пользовательский отчет должен быть интерпретируемым presentation layer, а не полным дампом базы.
- Заполнение `dancers.external_ref` / Compreg IDD для всех танцоров почти не увеличит размер базы: основной объем дают `marks` и локальный HTML cache.
- `external_ref` нужен как внешний стабильный идентификатор для dancer-agnostic analytics, report generation, future HTML/PDF и UI.
- Следующий data task: массово заполнить `dancers.external_ref` там, где это возможно.
- Для спорных или неоднозначных случаев нужно добавить `scripts/link_dancer_id.py`: ручная привязка Compreg IDD к `dancers.id` с явной проверкой имени, клуба, города и контекста протоколов.
- Массовое заполнение IDD должно быть data enrichment step, а не hardcoded mapping в application logic.

## Hidden / Non-user-facing Data

В пользовательском HTML/PDF отчете по умолчанию НЕ показываем:

- internal SQLite IDs;
- raw HTML paths;
- технические cache paths;
- parser debug logs;
- полные сырые judge-level marks;
- полные сырые cross marks;
- все участники турнира;
- все судьи турниров вне контекста выбранного танцора;
- низкоуверенные выводы как основные;
- FKT/EADC technical details;
- unsupported parser internals;
- любые утверждения о предвзятости судей.

В пользовательском HTML/PDF отчете показываем только агрегированную, интерпретируемую аналитику:

- summary;
- турниры выбранного танцора;
- динамику танцев;
- судейские отклонения от панели;
- data quality warnings;
- методологические ограничения.

Правило формулировок:

Не использовать:

- `bias`;
- `предвзятость`;
- `засуживает`;
- `несправедливо`.

Использовать:

- `строже среднего панели`;
- `мягче среднего панели`;
- `отклонение от панели`;
- `низкая уверенность`;
- `недостаточно данных`.

## Технический долг

- `README.md` частично устарел: в нем протокольный parser описан как следующий этап, хотя он уже реализован.
- Нет полного набора тестов parser и analytics.
- Нужен надежный источник или механизм заполнения `dancers.external_ref` для всех танцоров, потому что текущие protocol pages не содержат IDD в participant table.
- Нужна отдельная задача по FKT/EADC validation: проверить остаточные длинные raw strings, судейские позиции и соответствие отображению Compreg.
- Нужны тестовые HTML fixtures для обычных Compreg-протоколов и FKT/EADC-протоколов.
- Возможен future parser для длинных FKT raw strings, но только после ручной валидации структуры и правил соответствия judge positions.
- Нет миграций SQLite-схемы; схема развивается через `CREATE TABLE IF NOT EXISTS` и `ALTER TABLE ADD COLUMN`.
- View `marks_enriched` зависит от пользовательской SQLite-функции `normalize_person_name`, поэтому прямые запросы через `sqlite3` без регистрации функции могут падать.
- `scripts/analytics.py` обновлен до `--idd`, но остается легким helper-скриптом; основная аналитика живет в `analyze_dancer.py`, `analyze_dances.py` и `build_dancer_report.py`.
- Есть 37 `partial` протоколов и 1 `skipped` протокол.
- `unknown` marks сейчас равны 0 после parser fix.
- Для cross analytics сохранены только записанные кресты; отсутствующие кресты пока не нормализованы как отрицательные решения.
- Strictness считает panel mean с включением самого судьи; leave-one-judge-out вариант пока не реализован.
- Нет индексов, специально оптимизированных под аналитические запросы по `marks`, `dancers`, `judges`, `protocols`.
- Нет отдельного слоя доменных моделей / repository API; аналитика напрямую читает SQLite.
- Нет PDF export.
- Нет UI.
- Нужен единый release-step для отчетов: пересборка, обновление root-копий для GitHub Pages, проверка актуальных HTML-маркеров, commit и push.

## Следующие задачи

1. Собрать HTML report на основе JSON model.
2. Добавить PDF export для воспроизводимой генерации отчета одной командой.
3. Добавить механизм заполнения/валидации Compreg IDD для всех танцоров.
4. Добавить HTML fixtures для обычных Compreg и FKT/EADC parser tests.
5. Провести ручную FKT/EADC validation для остаточных длинных raw strings.

## Next Milestone

Ближайший milestone после dancer-agnostic refactor: reproducible report generation.

Состав milestone:

1. JSON report model:
   - реализован в `scripts/build_dancer_report.py`;
   - input `--idd`;
   - output `reports/dancer_<idd>_report.json`.
2. HTML report:
   - человекочитаемый отчет на основе JSON model;
   - отдельные блоки Standard и Latin;
   - предупреждения по качеству данных и ограничениям parser.
3. PDF export:
   - воспроизводимая генерация PDF одной командой;
   - стабильный layout для печати и отправки.

## Принятые аналитические решения

- Все аналитики по танцору должны фильтровать данные только по выбранному танцору, а не агрегировать всю таблицу `marks`.
- Основной внешний идентификатор CLI: Compreg `--idd`.
- Основная внутренняя связь для выбора данных танцора: `dancers.external_ref -> dancers.id -> marks.dancer_id`.
- ФИО не используется как фильтр в application logic; имя остается отображаемым полем и будущим механизмом поиска.
- Numeric places и crosses анализируются отдельно.
- Основная coach-facing dance analytics строится по `final_avg_place`: один итоговый результат на `protocol + round + dance`.
- `judge_avg_place` сохраняется как диагностическая метрика и не используется для performance rankings.
- В пользовательском отчете не используется общий ярлык "самый сильный танец" без указания метрики; показываются отдельные типы лидерства.
- `numeric_place` на уровне судей используется для judge analytics и diagnostic judge metrics.
- `cross` используется отдельно для анализа проходов/крестов и не смешивается с числовыми местами.
- Для текущего контрольного набора по `--idd 2016461` все `numeric_place` считаются финальными оценками, но методология допускает разделение final-only и all-numeric в будущем.
- Программы определяются по танцам:
  - Standard: `W`, `T`, `V`, `F`, `Q`;
  - Latin: `S`, `C`, `R`, `P`, `J`.
- Программа может дополнительно выводиться из категории по суффиксам `St` и `La`, но dance code является основным источником.
- В отчетах нужно явно показывать смешение категорий, программ и entry type, чтобы не делать чрезмерных выводов из неоднородных данных.
- Confidence thresholds используются как практические предупреждения, а не как строгая статистическая гарантия.

## Методики расчёта

Основные методики описаны в:

- `docs/analytics_methodology.md`
- `docs/dance_analytics_methodology.md`

### Strictness

`strictness` показывает, ставит ли судья выбранного танцора хуже или лучше среднего по панели в том же протоколе, туре и танце.

```text
deviation = judge_mark - panel_mean
strictness = avg(judge_mark - panel_mean)
softness = -strictness
```

Интерпретация:

```text
strictness > 0  судья ставит хуже среднего по панели
strictness = 0  судья совпадает со средним по панели
strictness < 0  судья ставит лучше среднего по панели
```

`panel_mean` считается локально:

```text
protocol_id + round + dance
```

Judge Analytics Inclusion Rule:

```text
В пользовательские отчёты включаются только судьи с количеством оценок не менее 10.
```

Судьи с `n_marks < 10` остаются в базе, JSON и внутренних аналитических расчетах, но не выводятся в пользовательских HTML/PDF рейтингах строгих и мягких судей.

### Dance Analytics

Все dance-level метрики считаются только по выбранному танцору:

```sql
WHERE dancer_id = :internal_dancer_id
  AND mark_type = 'numeric_place'
  AND is_final_round = 1
```

Основные метрики:

```text
final_avg_place = avg(final_place for protocol + round + dance)
judge_avg_place = avg(judge_mark)
median_place = median(final_place)
std_deviation = sample standard deviation of final_place
variance = std_deviation ^ 2
consistency_score = 1 / (1 + std_deviation)
volatility_score = std_deviation
```

Интерпретация:

- Для `final_avg_place` и `median_place` меньше значит лучше.
- Для `std_deviation` и `volatility_score` меньше значит стабильнее.
- Для `consistency_score` ближе к `1` значит стабильнее.
- `judge_avg_place` является диагностикой судейских оценок, а не основной метрикой спортивного результата танца.

Тренд по времени:

```text
date_final_avg_place(date, dance) = avg(final_place for dance on date)
trend_over_time = linear_regression_slope(date_index, date_final_avg_place)
```

Интерпретация тренда:

```text
negative slope -> улучшение, потому что место становится меньше
positive slope -> ухудшение, потому что место становится больше
```

Dance-level confidence thresholds:

```text
min_marks_for_ranking = 6
min_dates_for_trend = 2
preferred_dates_for_trend = 3
```

### Cross Analytics

Cross analytics пока описывает только записанные кресты.

Ограничение: отсутствующий крест пока не хранится как отрицательное решение, поэтому cross analytics нельзя читать как полную вероятность прохода или как аналог strictness для финальных мест.
