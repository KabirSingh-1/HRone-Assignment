# REVIEW.md

Defects found in the starter `app/main.py` (line numbers refer to the starter).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` L22 | Client is not `tz_aware`, so PyMongo returns **naive** datetimes. Any arithmetic or epoch conversion silently assumes the server's local zone. | Read a seeded `punch_in` and call `.timestamp()` on a non-UTC machine: off by the server's offset. | `MongoClient(..., tz_aware=True, tzinfo=UTC)`; every datetime in the app is aware UTC. |
| 2 | `compute_late_minutes` L35 | Shift start is built with `punch_in.replace(hour, minute)` on a UTC/local datetime, not on the IST date. The 09:30 is compared against a UTC clock. | Punch-in 10:05 IST (04:35 UTC) gives 0 instead of 35. | Build shift start with `datetime.combine(attendance_date, shift_start, IST)`. |
| 3 | `compute_late_minutes` L36-37 | The grace check is done on minutes truncated by `int()`, so "strictly more than 10:00" is lost. | 09:40:30 gives 0. It should be 10 (R2: 09:40:01 gives 10). | Compare **seconds** `> 600`, then return `seconds // 60`. |
| 4 | `compute_late_minutes` | Overnight shifts are not handled: the shift start can be on the previous calendar day. | Shift 22:00-06:00, punch-in 00:30: the starter gives 0. R1/R2 give 150. | Use the attendance date from R1 to build the shift start. |
| 5 | `compute_work_hours` L41 | `round()` is banker's/float rounding, not half-up. | 09:00:00 to 18:07:30 is 9.125 h. `round()` gives 9.12; R4 says 9.13. | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` L47 | The 30-minute minimum (R3) is missing. | Punch-out at 18:35 gives 5; it should be 0 (sample #1). | Return minutes only if `>= 30`. |
| 7 | `compute_overtime` L46 | Overnight shift end is put on the same date as the shift start. | Overnight sample #7 (out 06:40 next day) gives 1480 instead of 40. | Shift end is +1 day when `shift_end <= shift_start`. |
| 8 | `EmployeeIn` L53-60 | No validation at all: emp_code pattern, email, lengths, HH:MM, a real `joined_on` date, `shift_start != shift_end`. | `{"emp_code":"x","shift_start":"25:99","joined_on":"hello"}` is accepted (201). | Pydantic `Field(pattern=..., min/max_length)`, a date validator and a model validator. Bad input gives 422. |
| 9 | `create_employee` L79-83 | Check-then-insert with no unique index. Two concurrent creates both pass the check. | Fire 10 parallel POSTs with the same emp_code: several 201s and duplicate documents. | Unique index on `emp_code`; insert and catch `DuplicateKeyError`, which returns 409. |
| 10 | `create_employee` L82, L85 | `created_at = datetime.now()` is naive server-local time, and it is returned as a datetime, not epoch ms. | The response has `"created_at": "2026-..."` (a string). The contract wants an integer. | `datetime.now(UTC)` is stored; `dt_to_ms()` in the response. |
| 11 | `list_employees` L93 | `skip = page * page_size`, so page 1 skips the first page. | Default request with 5 employees returns `items: []`. | `skip = (page - 1) * page_size`. |
| 12 | `list_employees` L94 | `total` counts `{}`, not the filtered query. | `?department=Sales` returns total = all employees. | `count_documents(q)`. |
| 13 | `list_employees` L95 | No sort. The contract says emp_code ascending, and pagination is unstable without an order. | The order changes between calls and pages overlap. | `.sort("emp_code", 1)`, backed by the unique index. |
| 14 | `list_employees`, `list_attendance` | `page` and `page_size` are not validated (R10: page >= 1, page_size 1-100). | `page=0` gives a negative skip, which is a 500. `page_size=100000` dumps everything. | `Query(1, ge=1)` and `Query(20, ge=1, le=100)`, which give 422. |
| 15 | `PunchInIn` L65 | `Optional[int]` in lax mode accepts `"1783312500000"`, `1783312500000.0`, `true` and seconds-looking values. | Send `punched_at: 1783312500` (seconds): accepted and stored in 1970. | `StrictInt` with `ge=1e11` and `le=4102444800000`, so those inputs get 422. |
| 16 | `PunchInIn` L66 | `status` accepts any string, including `ABSENT`, `LEAVE` or `"foo"`. | Punch-in with `status: "LEAVE"` returns 201. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 17 | `punch_in` L101, L113 | An unknown employee gives `emp = None`, then `emp["shift_start"]` raises TypeError, which is a **500**. | Punch in `EMP9999`: 500 instead of 404. | `get_employee_or_404()`. |
| 18 | `punch_in` L102 | `datetime.fromtimestamp(ms/1000)` uses the **server's local zone**, gives a naive value, and keeps milliseconds (R1 says truncate to seconds). | On a UTC server the date and lateness are computed on a UTC clock. 09:40:00.900 keeps its .900. | `datetime.fromtimestamp(ms // 1000, tz=UTC)`. |
| 19 | `punch_in` L103 | `date` is the server-local calendar date. It ignores IST and the overnight rule (R1). | Punch-in 02:00 IST is stored with the previous UTC date. An overnight 05:00 punch-in gets "today" instead of yesterday. | `attendance_date()`: IST date, minus 1 day when overnight and before shift_end. |
| 20 | `punch_in` L104-118 | Check-then-insert race, and no unique `(emp_code, date)` index. | Two parallel identical punch-ins both return 201, creating duplicate records. | Unique index on `(emp_code, date)`; insert and catch `DuplicateKeyError`, which gives 409. |
| 21 | `punch_in` L119-121 | The response contains `id` (the ObjectId) and BSON datetimes. The contract has no id; instants are epoch ms. | The response has `"id": "66a..."` and `"punch_in": "2026-07-06T..."`. | `serialize_record()` returns only contract fields, with instants as ms. |
| 22 | `list_attendance` L144-147 | Loads **every matching document into Python** and sorts and paginates in memory. | At 100k records: slow, high memory; explain shows COLLSCAN. | `find(q).sort(...).skip().limit()` in MongoDB, backed by the `(date -1, emp_code 1)` index; `count_documents(q)` for total. |
| 23 | `list_attendance` L145 | Sorted by date only. The contract says date desc, **then emp_code asc**. | Records on the same date come back in arbitrary order. | Sort `[("date",-1),("emp_code",1)]`. |
| 24 | `list_attendance` | No validation: `date_from > date_to` should be 422; bad dates and an unknown `status` are accepted. | `date_from=2026-07-10&date_to=2026-07-01` returns 200 with an empty list. | Pattern, real-date check, range check and `Status` Literal; all give 422. |
| 25 | `list_attendance` L149 | Leaks `_id` as `id`, returns datetimes rather than ms, and does not default the missing `history`/`half_day` of legacy records. | Sample #9 comes back without `half_day` or `history`. | `serialize_record()` with documented defaults. |
| 26 | `health` L72-74 | Always 200, even when MongoDB is down. | Stop Mongo: /health still says ok. | `client.admin.command("ping")`; 503 on failure. |
| 27 | module level | No indexes are created anywhere. The seeded DB only has `_id`. | Every list and analytics query is a COLLSCAN; races are possible. | `ensure_indexes()` in the FastAPI lifespan (idempotent), retried from /health if Mongo was not ready. |

## Looked at and decided were *not* defects

- **`load_dotenv()` (L20):** its default is `override=False`, so real environment variables already win over `.env`, as the spec asks. I made it explicit.
- **`list_attendance` skip `(page - 1) * page_size` (L147):** correct, unlike `list_employees`.
- **Comparing `date` as strings with `$gte`/`$lte` (L139-141):** correct, because zero-padded `YYYY-MM-DD` strings sort the same as dates.
- **Identifying employees by `emp_code`:** correct per the contract. `_id` must stay internal.
- **409 on a second punch-in for the same date:** correct behaviour. Only the mechanism, check-then-insert, was wrong.
- **The helpers themselves:** reused in spirit (same names and responsibilities), but their signatures changed to take aware datetimes for shift start and end, so the IST and overnight logic lives in one place (`shift_window`).