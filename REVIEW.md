# REVIEW.md

## Starter Code Defects

The following issues were investigated during implementation and endpoint testing. Include only defects confirmed by the original starter code and the final implementation diff; do not treat test-data anomalies alone as proof of a starter defect.

| # | Where (function / area) | What is wrong | How it was noticed (test, input, or symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | `record_date()` — overnight shift boundary | An early-morning punch-in for an overnight shift can be assigned to the wrong calendar shift date if the shift-end boundary is handled incorrectly. | Tested the overnight Support shift (22:00–06:00), including a punch-in at 06:00. | Compared the punch-in time with the overnight shift end and handled the inclusive shift-end boundary consistently. |
| 2 | `compute_late_minutes()` — shift-start calculation | Late minutes must be calculated relative to the correct shift start for the shift date assigned to the punch-in. | Checked an ordinary Engineering punch-in before 09:30 and reviewed overnight shift calculations. | Calculated elapsed seconds from the assigned shift's start and counted late minutes only when the delay is strictly greater than 10 minutes. |
| 3 | Attendance creation — duplicate employee/date | Concurrent punch-ins must not create multiple attendance records for the same employee and date. | Repeated punch-in for an employee/date that already had a record; the API returned `409 Conflict`. | Enforced uniqueness on `(emp_code, date)` and handled duplicate-key conflicts as a conflict response. |
| 4 | Punch-out validation and calculations | Punch-out must reject a missing punch-in, an existing punch-out, timestamps not after punch-in, and elapsed time over 24 hours. | Tested a missing record and a punch-out timestamp equal to punch-in; the API returned `404` and `422` respectively. | Added validation before calculating and saving work hours, half-day status, and overtime. |
| 5 | Attendance regularization | Changes need an audit trail so the previous and updated values can be reviewed. | Regularized a test record from `PRESENT` to `WFH` and inspected the response history. | Recorded the regularizing user, reason, timestamp, and field changes in `history`. |
| 6 | Attendance listing and query performance | Date-filtered attendance listing should use suitable indexes and respect pagination. | Used the explain endpoint for `2026-10-01` through `2026-10-14`; MongoDB selected `ix_attendance_date_employee`, examined 9 keys and 9 documents, and returned 9 records. | Added or retained the date/employee compound index and applied the requested page and page-size limit. |

## Areas Examined That Were Not Confirmed as Defects

- **Duplicate punch-in:** A repeated punch-in for an existing employee/date returned `409 Conflict`, as required. This test passed.
- **Punch-out ordering:** A punch-out equal to punch-in was rejected with `422`; a missing punch-in returned `404`.
- **Half-day calculation:** A test record with 0.50 work hours returned `half_day: true`, consistent with the 4.50-hour threshold.
- **Regularization history:** The test response retained the old and new status values and the reason. The history timestamp should be checked against the implementation's timestamp-generation logic before making claims about its exact time.
- **Leaderboard ranking:** The tested results used descending total late minutes, shared ranks for tied totals, and skipped the next rank after a tie.
- **Date-trend gap filling:** The trend response returned one row per date and marked Saturday/Sunday as non-working days. The moving-average calculation still needs comparison with the exact assignment rule.
- **Department headcount:** The response includes employees in department totals, but the test output alone does not prove that every employee who joined by the period end is included regardless of attendance logs. Verify this against the aggregation pipeline.
- **Analytics rounding and aggregation:** The observed values are plausible for the test records, but the response tests alone do not establish that every rounding edge case or all aggregation requirements are satisfied.

## Verification Notes

- `GET /health` returned `{"status":"ok"}`.
- `python -m pytest -q` reported `no tests ran`; no automated test suite was discovered in the project at that time.
- The attendance explain plan used `IXSCAN` with `ix_attendance_date_employee`. For the tested query, `totalKeysExamined` and `totalDocsExamined` were both 9, with a reported execution time of 2 ms.
- Endpoint checks were performed against the development database containing existing and manually created test records. These results should not be represented as a clean-database test.
