# DECISIONS.md

1. **Indexes.**
   - `employees`: `emp_code` (unique). It guarantees no duplicate codes and serves lookups and list sorting.
   - `employees`: `(department, emp_code)`. It serves the department filter plus sort, and trend headcount.
   - `employees`: `joined_on`. It serves the department summary's headcount match.
   - `attendance_logs`: `(emp_code, date)` (unique). It enforces one record per day and serves punch-out, PATCH, the employee monthly report and the trend.
   - `attendance_logs`: `(date -1, emp_code)`. It matches the list sort and the month-range analytics.
   - I rejected an index on `status`. It has only five values, so it is too unselective.

2. **Punch-in race.** Both requests read the employee, compute the same date and insert. The unique `(emp_code, date)` index lets exactly one insert succeed. The other raises `DuplicateKeyError`, which I return as 409. There is no check-then-insert.

3. **Ties.** I use `$setWindowFields` with `$rank`: standard competition ranking, so 1, 2, 2, 4. I then keep `rank <= limit`, so everyone tied at the cutoff is returned, ordered by `emp_code`.

4. **Headcount.** The summary pipeline starts from `employees` (`joined_on <= month end`) and `$lookup`s their logs. Each employee is counted once even with an empty `logs` array.

5. **100x.** Precompute daily per-department rollups (updated on punch and PATCH) so analytics read small summary documents instead of scanning raw logs.