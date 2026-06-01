# Analytics methodology

This document defines how dancer analytics are calculated from normalized Compreg protocol data.

## 1. Strictness

`strictness` measures whether a judge places the selected dancer worse or better than the judging panel average in the same protocol, round, and dance.

In ballroom protocols, a smaller numeric place is better and a larger numeric place is worse. Therefore, a judge is stricter when their numeric place is larger than the panel average.

Formula for one mark:

```text
deviation = judge_mark - panel_mean
```

Formula for one judge over all included marks:

```text
strictness = avg(judge_mark - panel_mean)
```

Interpretation:

```text
strictness > 0  judge placed the dancer worse than the panel average
strictness = 0  judge matched the panel average on average
strictness < 0  judge placed the dancer better than the panel average
```

`softness` is the inverse metric:

```text
softness = avg(panel_mean - judge_mark)
softness = -strictness
```

## 2. Panel mean

`panel_mean` is calculated for the selected dancer within one exact scoring context:

```text
protocol_id + round + dance
```

For each dancer, dance, and round, all numeric marks from the panel are averaged:

```text
panel_mean = avg(all judge numeric places for this dancer in this protocol, round, dance)
```

The panel mean is not calculated across tournaments, categories, dates, or dances. It is local to one protocol and one dance round.

## 3. Why positive deviation means stricter

Numeric places are ordered so that lower is better:

```text
1st place is better than 2nd place
2nd place is better than 3rd place
```

If the panel average is `3.727` and a judge gives `6`, then the judge placed the dancer worse than the panel average:

```text
deviation = 6.000 - 3.727 = 2.273
```

The positive value means stricter.

If another judge gives `1`, then the judge placed the dancer better than the panel average:

```text
deviation = 1.000 - 3.727 = -2.727
```

The negative value means softer.

## 4. Included marks

The final-place strictness ranking uses only marks for the selected dancer and only normalized numeric places:

```text
mark_type = numeric_place
is_final_round = 1
```

The query scope is restricted to the selected normalized dancer name. The analytics must not aggregate over the full `marks` table.

Crosses are analyzed separately:

```text
mark_type = cross
```

Numeric places and crosses are never mixed in one strictness calculation.

Current note: for the checked dancer data, all `numeric_place` rows are final rows. If future protocols contain numeric places outside finals, `final only` and `all numeric places` can differ.

## 5. Confidence thresholds

The main judge strictness ranking uses:

```text
n_marks >= 12
```

Judges below this threshold are shown in a separate low-confidence block:

```text
n_marks < 12
```

Dance-level summaries use a smaller warning threshold:

```text
n_marks < 3 -> low confidence
```

These thresholds are practical defaults, not statistical guarantees.

## 6. Why n_marks matters

`n_marks` is the number of individual judge marks used to calculate a metric.

Small samples can produce extreme values. For example, a judge who marked only one final can look very strict if they gave several high places in that final, but this may not represent their general judging tendency toward the dancer.

Example:

```text
Judge A: strictness = 3.261, n_marks = 8
Judge B: strictness = 1.828, n_marks = 14
```

Judge A has a stronger observed deviation, but Judge B has more observations. The main ranking therefore uses the `n_marks >= 12` threshold and moves Judge A to the low-confidence block.

## 7. Example calculations

Example single mark:

```text
protocol_id: 73068
dance: F
judge: Агеева Наталья
judge_mark: 6.000
panel_mean: 3.727

deviation = 6.000 - 3.727 = 2.273
```

This is a stricter-than-panel mark.

Example softer mark from the same context:

```text
protocol_id: 73068
dance: F
judge: Едигарьева Татьяна
judge_mark: 1.000
panel_mean: 3.727

deviation = 1.000 - 3.727 = -2.727
```

This is a softer-than-panel mark.

Example judge aggregate:

```text
judge: Клековкин Алексей
n_marks: 14
avg_judge_mark: 6.429
avg_panel_mean: 4.601

avg_deviation = 6.429 - 4.601 = 1.828
strictness = 1.828
softness = -1.828
```

This judge is stricter than the panel average for the selected dancer in the included final marks.

## 8. Limitations

The metric is descriptive, not causal. It shows how one judge's marks differ from the panel average for one selected dancer.

Important limitations:

- It does not prove judge bias.
- It depends on the selected dancer's actual performance in each dance and event.
- It depends on which protocols were crawled and parsed successfully.
- It can be unstable when `n_marks` is small.
- It is sensitive to mixing categories, ages, programs, and rounds, so reports should show these dimensions explicitly.
- It compares a judge to the panel that includes that same judge. For larger panels this is acceptable for descriptive analytics, but a leave-one-judge-out panel mean may be added later for a stricter statistical comparison.
- Cross analytics are not equivalent to numeric-place strictness unless missing crosses are also normalized as negative decisions.

