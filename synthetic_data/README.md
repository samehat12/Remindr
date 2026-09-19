# Synthetic dementia-support demo dataset

All records are simulated, deterministic, and contain no real patient information. They are intended for prototype, UI, and anomaly-engine testing only. They must not be used for clinical decisions, diagnosis, care recommendations, or model-performance claims.

## Files

- `patients.csv`: two fictional patient profiles. `SYN-001` is nominal; `SYN-002` includes preset divergent events on the final three days.
- `routine_events.csv`: scheduled check-ins and medication confirmations. This is the primary input to a rolling baseline or Z-score engine.
- `message_log.csv`: paired companion and patient messages for the conversation feed and confusion classifier mock.
- `mobility_log.csv`: four synthetic wearable-style activity windows per day.
- `daily_cognitive_signals.csv`: daily aggregates for plotting latency/confusion trends and validating a cognitive-drift calculation.

## Demo scenarios

Filter `routine_events.csv` for `scenario_anomaly=true` to get the four named acute demo cases: two delayed medication confirmations, one disorientation message, and one missed medication with confusion. `ground_truth_anomaly=true` also includes expected rolling Z-score alerts as the divergent profile gradually changes. The data begin on 2026-08-21 and are ordered chronologically.

## Suggested detector input

Use `patient_id`, `task_type`, `response_received_at`, and `reply_latency_minutes`; calculate baselines from prior observations of the same task. `rolling_baseline_mean_minutes`, `rolling_baseline_std_minutes`, and `rolling_z_score` show the expected result for a 14-observation rolling window. They are validation targets, not model features.

For cognitive-drift tests, use `daily_cognitive_signals.csv` to calculate a trailing 7-day mean or slope of `mean_confusion_score`, `mean_reply_latency_minutes`, `late_or_missed_count`, and `repeated_question_count`. `synthetic_cognitive_drift_score` and `drift_label` provide a transparent, non-clinical reference target. `SYN-002` begins a gradual simulated change after day 14; the final three days contain the acute demo incidents.
