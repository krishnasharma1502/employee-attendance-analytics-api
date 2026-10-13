# DECISIONS.md

1. **Indexes:** I created a unique index on employee code to prevent duplicate employees. Compound indexes support department and joining-date queries. For attendance, a unique `(emp_code, date)` index prevents duplicate daily records, while date/employee and employee/punch-in indexes support filtering and ordered attendance queries.

2. **Punch-in race:** Two identical requests can arrive simultaneously. MongoDB's unique `(emp_code, date)` index allows only one record for that employee and date. The successful request returns `201`, while a duplicate-key error is caught and returns `409 Conflict`.

3. **Ties:** The late leaderboard sorts by total late minutes descending, then employee code ascending. Employees with equal totals share the same rank, and the next rank is skipped (for example, `1, 2, 2, 4`). Employee code provides deterministic ordering within ties.

4. **Headcount:** The department summary starts from employees and looks up their attendance logs. Employees with no logs are still included, so department headcount does not depend on attendance activity. Eligibility is based on joining on or before the period end.

5. **Scaling:** For 100× the data, I would review query plans and index usage, add pagination or caching where appropriate, and optimize aggregation pipelines. I would monitor database load and latency before choosing further scaling changes, avoiding speculative indexes without evidence.