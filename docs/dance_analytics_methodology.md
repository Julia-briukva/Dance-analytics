# Dance Analytics Methodology

This document defines dance-level analytics for one selected dancer.

The dancer is selected at runtime by external Compreg IDD. The system resolves it to the internal SQLite `dancers.id`, then all analytics filter by `marks.dancer_id`.

```sql
SELECT *
FROM marks_enriched
WHERE dancer_id = :internal_dancer_id
  AND mark_type = 'numeric_place'
  AND is_final_round = 1;
```

Crosses are not mixed into place analytics. They can support progression analytics later, but they are not numeric places.

## Program Split

```text
стандарт = W, T, V, F, Q
латина   = S, C, R, P, J
```

Program can also be inferred from protocol category suffixes such as `St` and `La`, but dance code is the primary source.

## Performance Metrics

Performance metrics are the primary metrics for coach-facing dance reports.

They measure the sport result of the dance, not the average of individual judge marks.

### final_avg_place

Primary metric.

Algorithm:

```text
protocol + round + dance
↓
extract the dancer's final dance result
↓
average those dance results across protocols
```

Formula:

```text
final_avg_place(dance) = avg(final_place for protocol + round + dance)
```

Interpretation: lower is better.

This metric is used for "Лучший по среднему итоговому месту" and "Худший по среднему итоговому месту".

The report must not use a generic "самый сильный танец" label without naming the metric.

### stability

Stability uses final dance results.

Formula:

```text
stability(dance) = sample_stddev(final_place for protocol + round + dance)
```

Lower `std_deviation` means the sport result of the dance is more stable.

This metric is used for:

- most stable dance;
- least stable dance;
- volatility diagnostics.

### improvement

Improvement uses date-level averages of final dance results.

Formula:

```text
date_final_avg_place(date, dance) = avg(final_place for dance on date)
trend_over_time = linear_regression_slope(date_index, date_final_avg_place)
```

Interpretation:

```text
negative slope -> improvement, because lower place is better
positive slope -> regression
```

This metric is used for:

- most improved dance;
- regression ranking;
- rolling average;
- tournament-to-tournament delta.

## Judge Metrics

Judge metrics are diagnostic. They describe judge-level scoring patterns and must not drive the main performance ranking.

### judge_avg_place

Formula:

```text
judge_avg_place(dance) = avg(judge_mark for dance)
```

This is the previous implementation of `avg_place`.

It remains useful for checking how individual judge marks are distributed, but it can differ from the final sport result.

Limitations:

- protocols with more judges have more weight;
- the metric averages judge marks, not the final dance result;
- it can produce a different dance ranking than `final_avg_place`.

### judge variance

Formula:

```text
judge_variance(dance) = variance(judge_mark for dance)
judge_std_deviation(dance) = sample_stddev(judge_mark for dance)
```

This is a diagnostic measure of spread in judge marks.

It should be read separately from performance stability, which uses final dance results.

## Derived Rankings

The report separates different meanings of dance strength/result quality. A dance can lead one metric and not lead another.

### best_by_final_average

```text
best_by_final_average = dance with lowest final_avg_place
```

User-facing label:

```text
Лучший по среднему итоговому месту
```

### best_by_median

```text
best_by_median = dance with lowest final_median_place
```

User-facing label:

```text
Лучший по медиане итогового места
```

### most_stable

```text
most_stable = dance with lowest final_std_deviation
```

User-facing label:

```text
Самый стабильный
```

### best_peak

```text
best_peak = dance with lowest single final_place
```

User-facing label:

```text
Лучший пик результата
```

### worst_by_final_average

```text
worst_by_final_average = dance with highest final_avg_place
```

User-facing label:

```text
Худший по среднему итоговому месту
```

### judge_level_best

```text
judge_level_best = dance with lowest judge_avg_place
```

User-facing label:

```text
Лучший по среднему судейскому месту
```

### most_improved

```text
most_improved = dance with strongest negative trend_over_time
```

## Ranking Mismatch Warning

The report compares:

```text
performance ranking by final_avg_place
judge diagnostic ranking by judge_avg_place
```

If the rankings differ, the report adds this warning:

```text
Судейская и результативная метрики дают разные рейтинги танцев.
```

## Confidence Thresholds

Dance-level rankings use these defaults:

```text
min_marks_for_ranking = 6
min_dates_for_trend = 2
preferred_dates_for_trend = 3
```

For performance metrics, `n_marks` means the number of final dance result rows, not the number of judge marks.

Confidence labels are practical warnings, not formal statistical guarantees.

## Statistical Reliability

User-facing judge analytics applies an inclusion rule:

```text
n_marks >= 10
```

Only judges with at least 10 available marks for the selected dancer are shown in HTML/PDF judge rankings.

Rationale:

- judges with 4 or 8 marks can appear as the strictest or softest due to noise;
- `n_marks >= 10` is still a practical threshold, not a formal statistical proof;
- lower-sample judges remain available in SQLite, JSON, and internal analytics, but are not shown as primary user-facing rankings.

The report text should describe judge behavior neutrally:

```text
строже среднего панели
мягче среднего панели
отклонение от панели
низкая уверенность
недостаточно данных
```

## Edge Cases

- If a dance has too few final result rows, include it in tables but mark it as low confidence.
- If a dance has only one date, trend is unavailable or low confidence.
- If a dance is missing from a program, list it in missing dances.
- If a protocol has crosses but no numeric final places, exclude it from place analytics and keep it in cross/progression analytics.
- If categories differ, report categories and protocols so the reader can decide whether to compare them together.
