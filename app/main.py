"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded, but real environment variables win)

Layout of this file
  1. Configuration and database client
  2. Constants and types
  3. Time helpers         (epoch ms <-> BSON datetime, IST, attendance date R1)
  4. Business rules       (R2 - R5, half-up rounding R8)
  5. Serializers          (Mongo document -> API JSON)
  6. Indexes and app startup
  7. Request models
  8. Endpoints: health, employees, attendance
  9. Analytics: pipeline builders + endpoints
 10. Admin: explain
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration and database client
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # real environment variables take priority over .env

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones,
# so every datetime in this process is aware and comparisons/conversions are safe.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# --------------------------------------------------------------------------- #
# 2. Constants and types
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_BELOW_HOURS = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
NON_PRESENCE = ("ABSENT", "LEAVE")

MIN_EPOCH_MS = 100_000_000_000   # anything smaller looks like seconds -> 422
MAX_EPOCH_MS = 4_102_444_800_000

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

# strict=True: reject "1783312500000" (string), 1783312500000.0 (float) and true (bool)
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]

# Fields a regularization can change, in the order they appear in history[].changes
TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def unprocessable(msg: str, loc: tuple = ("body",)) -> HTTPException:
    """A 422 whose body has the same shape as FastAPI's own validation errors."""
    return HTTPException(status_code=422, detail=[{"type": "value_error", "loc": list(loc), "msg": msg}])


# --------------------------------------------------------------------------- #
# 3. Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    """Aware (or naive-UTC) datetime -> integer epoch ms. Exact integer maths, no float."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_overnight(emp: dict) -> bool:
    # "HH:MM" strings are zero-padded, so string comparison == time comparison
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(ts: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in; for an overnight shift, a punch-in
    earlier than shift_end belongs to the shift that started the previous day."""
    local = ts.astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_window(day: date, emp: dict) -> tuple[datetime, datetime]:
    """Shift start and end instants for the shift that starts on `day` (IST)."""
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def parse_date(value: str, loc: tuple) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable("must be a real calendar date YYYY-MM-DD", loc)


def month_range(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')"""
    year, mon = map(int, month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def count_working_days(first: date, last: date, joined_on: str) -> int:
    """R7: Mon-Fri between first and last inclusive, starting no earlier than joined_on.
    Pure calendar arithmetic (at most 31 iterations), not data processing."""
    start = max(first, date.fromisoformat(joined_on))
    if start > last:
        return 0
    return sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 4. Business rules
# --------------------------------------------------------------------------- #
def half_up(value: Any, places: int) -> Optional[float]:
    """R8 rounding: half-up (not banker's, not float). Accepts float, int, Decimal or Decimal128."""
    if value is None:
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    elif not isinstance(value, Decimal):
        value = Decimal(str(value))  # str() gives the shortest repr, so 9.125 stays 9.125
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, shift_end: datetime) -> int:
    """R3: floor minutes after shift end, counted only when >= 30."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600 rounded to 2 decimals half-up, using Decimal (exact)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive_fields(emp: dict, day: date, status: str, punch_in: Optional[datetime],
                  punch_out: Optional[datetime]) -> dict:
    """All four derived fields for a record, per the table in DATA_MODEL.md."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    shift_start, shift_end = shift_window(day, emp)
    late = compute_late_minutes(punch_in, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": float(hours),
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, shift_end),
        "half_day": hours < HALF_DAY_BELOW_HOURS,  # R5 compares the *rounded* value
    }


def check_punch_out(punch_in: datetime, punch_out: datetime) -> None:
    if punch_out <= punch_in:
        raise unprocessable("punch_out must be after punch_in")
    if punch_out - punch_in > MAX_SHIFT:
        raise unprocessable("punch_out must be within 24 hours of punch_in")


# --------------------------------------------------------------------------- #
# 5. Serializers: never leak _id, always return instants as epoch ms
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")


def serialize_employee(doc: dict) -> dict:
    out = {k: doc.get(k) for k in EMPLOYEE_FIELDS}
    out["created_at"] = dt_to_ms(doc.get("created_at"))
    return out


def _to_api(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_history(entries: Optional[list]) -> list:
    return [
        {
            "at": dt_to_ms(e.get("at")),
            "by": e.get("by"),
            "reason": e.get("reason"),
            "changes": {
                field: {"from": _to_api(c.get("from")), "to": _to_api(c.get("to"))}
                for field, c in (e.get("changes") or {}).items()
            },
        }
        for e in (entries or [])
    ]


def serialize_record(doc: dict) -> dict:
    """Missing fields in legacy records get their documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history")),
    }


# --------------------------------------------------------------------------- #
# 6. Indexes and app startup
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    global _indexes_ready
    # employees
    db.employees.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_emp_code")
    db.employees.create_index([("joined_on", 1)], name="joined_on")
    # attendance_logs
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_code_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp_code")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # DB not up yet: /health retries, the app still starts
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 7. Request models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # rejects 2026-02-30
        return v

    @model_validator(mode="after")
    def shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class PunchInRequest(PunchOutRequest):
    status: PresenceStatus = "PRESENT"


class RegularizeRequest(BaseModel):
    # Unknown keys (including derived fields like work_hours) are ignored, per the contract.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null; omit it to keep the stored value")
        return self


# --------------------------------------------------------------------------- #
# 8. Endpoints: health, employees, attendance
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        # Uniqueness is enforced by the unique index, not by a check-then-insert.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": db.employees.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, day, body.status, ts, None),
        "history": [],
    }
    try:
        # Race-safe: the unique (emp_code, date) index lets exactly one concurrent insert win.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in <= ts. One record per date and date follows punch_in,
    # so "latest date" == "latest punch_in"; served by the (emp_code, date) index.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code,
         "date": {"$lte": ts.astimezone(IST).date().isoformat()},
         "punch_in": {"$lte": ts}},
        sort=[("date", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_out(rec["punch_in"], ts)

    derived = derive_fields(emp, date.fromisoformat(rec["date"]), rec["status"], rec["punch_in"], ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # atomic guard: only one concurrent punch-out wins
        {"$set": {"punch_out": ts,
                  "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"],
                  "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


def attendance_list_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        if date_from:
            parse_date(date_from, ("query", "date_from"))
        if date_to:
            parse_date(date_to, ("query", "date_to"))
        if date_from and date_to and date_from > date_to:
            raise unprocessable("date_from must be on or before date_to", ("query", "date_from"))
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from  # ISO strings compare correctly as strings
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_list_filter(emp_code, date_from, date_to, status)
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": db.attendance_logs.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):  # noqa: A002 - name fixed by contract
    record_day = parse_date(date, ("path", "date"))
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set
    new_status = body.status if "status" in sent else rec["status"]

    if new_status in NON_PRESENCE:
        if "punch_in" in sent or "punch_out" in sent:
            raise unprocessable(f"{new_status} clears punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else rec.get("punch_out")
        if new_in is None:
            raise unprocessable("a presence status requires punch_in")
        if attendance_date(new_in, emp) != record_day:
            raise unprocessable("punch_in must stay on the record's attendance date")
        if new_out is not None:
            check_punch_out(new_in, new_out)

    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                  **derive_fields(emp, record_day, new_status, new_in, new_out)}
    old_values = {
        "status": rec.get("status"),
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {f: {"from": old_values[f], "to": new_values[f]}
               for f in TRACKED_FIELDS if old_values[f] != new_values[f]}
    if not changes:
        raise unprocessable("the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the write only applies if the record still looks exactly like
    # what we read (same punches, status and history length). A concurrent PATCH or
    # punch-out makes this filter miss -> 409, so no history entry is ever lost.
    guard = {"_id": rec["_id"], "status": rec["status"],
             "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    history = rec.get("history")
    if isinstance(history, list):
        guard["history"] = {"$size": len(history)}
        update = {"$set": new_values, "$push": {"history": entry}}
    else:  # legacy record: history missing/null
        guard["history"] = None
        update = {"$set": {**new_values, "history": [entry]}}

    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# 9. Analytics
#    Each endpoint has a *_pipeline() builder so /admin/explain runs exactly the
#    same pipeline the endpoint runs.
# --------------------------------------------------------------------------- #
# Reusable aggregation expressions over one attendance_logs document
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}  # R5, R6
WEEKDAY_PRESENT_VALUE = {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}  # R7
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def count_if(expr: dict) -> dict:
    return {"$cond": [expr, 1, 0]}


def round_half_up_expr(expr: Any, places: int) -> dict:
    """Half-up rounding inside MongoDB ($round is banker's). Exact with Decimal128 input."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, Decimal128("0.5")]}}, factor]}


def to_number(value: Any) -> Any:
    return float(value.to_decimal()) if isinstance(value, Decimal128) else value


# ---- employee monthly ------------------------------------------------------ #
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},  # IXSCAN uniq_emp_code_date
        {"$group": {
            "_id": None,
            "present_days": {"$sum": WEEKDAY_PRESENT_VALUE},
            "leave_days": {"$sum": count_if({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MINUTES},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    agg = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)), {})
    first, last = month_range(month)
    working_days = count_working_days(date.fromisoformat(first), date.fromisoformat(last), emp["joined_on"])
    present_days = Decimal(str(agg.get("present_days", 0)))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(present_days / working_days * 100, 2) if working_days else None,
    }


# ---- department summary ---------------------------------------------------- #
def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Runs on employees so people with zero logs are still counted in headcount (R9)."""
    first, last = month_range(month)
    match: dict = {"joined_on": {"$lte": last}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {  # indexed join on (emp_code, date)
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {
                    "_id": 0,
                    "present": WEEKDAY_PRESENT_VALUE,
                    "late": IS_LATE,
                    "late_minutes": LATE_MINUTES,
                    "leave": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty": count_if({"$eq": ["$status", "ON_DUTY"]}),
                    # Decimal128 so the average is exact before half-up rounding
                    "wh": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, None]},
                    "wh_n": count_if(HAS_WORK_HOURS),
                }},
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$sum": "$logs.present"}},
            "late_count": {"$sum": {"$sum": "$logs.late"}},
            "total_late_minutes": {"$sum": {"$sum": "$logs.late_minutes"}},
            "leave_count": {"$sum": {"$sum": "$logs.leave"}},
            "on_duty_count": {"$sum": {"$sum": "$logs.on_duty"}},
            "wh_sum": {"$sum": {"$sum": "$logs.wh"}},
            "wh_n": {"$sum": {"$sum": "$logs.wh_n"}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1, "present_days": 1, "late_count": 1, "total_late_minutes": 1,
            "leave_count": 1, "on_duty_count": 1,
            # average over records, not an average of per-employee averages
            "avg_work_hours": {"$cond": [{"$gt": ["$wh_n", 0]}, {"$divide": ["$wh_sum", "$wh_n"]}, None]},
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    rows = db.employees.aggregate(department_summary_pipeline(month, department))
    items = [
        {**row,
         "present_days": float(row["present_days"]),
         "avg_work_hours": half_up(row["avg_work_hours"], 2)}
        for row in rows
    ]
    return {"month": month, "items": items}


# ---- late leaderboard ------------------------------------------------------ #
def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_range(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},  # IXSCAN date index
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # Standard competition ranking (1, 2, 2, 4): ties share a rank, the next rank is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


# ---- department trend ------------------------------------------------------ #
def department_codes_or_404(department: str) -> list[str]:
    codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    if not codes:
        raise HTTPException(404, "unknown department")
    return codes


def check_trend_range(from_: str, to: str) -> None:
    start = parse_date(from_, ("query", "from"))
    end = parse_date(to, ("query", "to"))
    if end < start:
        raise unprocessable("to must be on or after from", ("query", "to"))
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"range must not exceed {MAX_TREND_DAYS} days", ("query", "to"))


def department_trend_pipeline(department: str, codes: list[str], from_: str, to: str) -> list:
    start = datetime.combine(date.fromisoformat(from_), time(), UTC)
    end = datetime.combine(date.fromisoformat(to), time(), UTC)
    rate = {"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}
    return [
        # 1. this department's logs in range (IXSCAN uniq_emp_code_date)
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_, "$lte": to}}},
        # 2. one row per day that has records
        {"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_VALUE}, "late_count": {"$sum": IS_LATE}}},
        {"$project": {"_id": 0, "present_count": 1, "late_count": 1,
                      "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        # 3. make sure both ends of the range exist, even if they (or all days) have no records
        {"$unionWith": {"pipeline": [{"$documents": [
            {"day": start, "present_count": 0, "late_count": 0},
            {"day": end, "present_count": 0, "late_count": 0},
        ]}]}},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        # 4. gap filling: exactly one row per calendar day, weekends included
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": "full"}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        # 5. headcount on that day = department employees with joined_on <= day (R9), logs or not
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                         {"$count": "n"}],
            "as": "hc",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        # 6. rate only on working days with headcount > 0; rounded half-up to 4 dp
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, round_half_up_expr(rate, 4), None]}}},
        # 7. 7-day moving average over the rows inside the range only ($avg skips nulls)
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_PATTERN),
    to: str = Query(..., pattern=DATE_PATTERN),
):
    check_trend_range(from_, to)
    codes = department_codes_or_404(department)
    rows = db.attendance_logs.aggregate(department_trend_pipeline(department, codes, from_, to))
    items = [
        {**row,
         "present_count": to_number(row["present_count"]),
         "attendance_rate": half_up(row["attendance_rate"], 4),
         "moving_avg_7d": half_up(row["moving_avg_7d"], 4)}
        for row in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 10. Admin: explain the main query of an endpoint, built by the same builder
# --------------------------------------------------------------------------- #
def require(**params) -> None:
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise HTTPException(422, [{"type": "missing", "loc": ["query", m], "msg": "Field required"}
                                  for m in missing])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_list_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            require(department=department, **{"from": from_}, to=to)
            check_trend_range(from_, to)
            codes = department_codes_or_404(department)
            collection = "attendance_logs"
            pipeline = department_trend_pipeline(department, codes, from_, to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command("explain", command, verbosity="executionStats")
    # json_util turns BSON types (ObjectId, Timestamp, Int64, dates) into plain JSON
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}