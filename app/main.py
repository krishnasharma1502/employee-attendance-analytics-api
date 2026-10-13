"""Employee Attendance & Analytics API - HROne assignment."""
import os
import re
import calendar
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from pydantic import StrictInt

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv(override=False)
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENT, "ABSENT", "LEAVE")
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punchin")


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def as_ist(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(IST)


def epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(as_utc(dt).timestamp() * 1000)


def parse_ms(value: Optional[int], default_now: bool = False) -> datetime:
    if value is None:
        if default_now:
            return truncate_seconds(datetime.now(UTC))
        raise HTTPException(422, "timestamp is required")
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_MS <= value <= MAX_MS:
        raise HTTPException(422, "timestamp must be epoch milliseconds in the supported range")
    return truncate_seconds(datetime.fromtimestamp(value / 1000, UTC))


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "Date must use YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, "Date must use YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must use YYYY-MM")
    year, month = map(int, value.split("-"))
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def shift_datetime(day: str, time_str: str) -> datetime:
    d = parse_date(day)
    h, m = map(int, time_str.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_datetime(day: str, shift_start: str, shift_end: str) -> datetime:
    end = shift_datetime(day, shift_end)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    if eh * 60 + em <= sh * 60 + sm:
        end += timedelta(days=1)
    return end


def record_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_ist(truncate_seconds(punch_in))
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    if eh * 60 + em <= sh * 60 + sm and local.hour * 60 + local.minute < eh * 60 + em:
        local -= timedelta(days=1)
    return local.date().isoformat()






def compute_late_minutes(
    punch_in: datetime, shift_start: str, shift_end: str, day: str
) -> int:
    local = as_ist(truncate_seconds(punch_in))
    start = shift_datetime(day, shift_start)

    start_minutes = sum(
        int(x) * factor
        for x, factor in zip(shift_start.split(":"), (60, 1))
    )
    end_minutes = sum(
        int(x) * factor
        for x, factor in zip(shift_end.split(":"), (60, 1))
    )
    is_overnight = end_minutes <= start_minutes

    
    seconds = int((local - start).total_seconds())

    # R2: late only when strictly more than 10 minutes after shift start.
    return seconds // 60 if seconds > 600 else 0




def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((truncate_seconds(as_utc(punch_out)) - truncate_seconds(as_utc(punch_in))).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    end = shift_end_datetime(day, shift_start, shift_end)
    minutes = int((as_ist(truncate_seconds(punch_out)) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def rounded(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1") if places == 0 else Decimal("0." + "0" * (places - 1) + "1")
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def serialize_history(history):
    out = []
    for entry in history or []:
        e = dict(entry)
        e["at"] = epoch_ms(e.get("at"))
        changes = {}
        for field, pair in (e.get("changes") or {}).items():
            pair = dict(pair)
            if field in ("punch_in", "punch_out"):
                pair["from"] = epoch_ms(pair.get("from"))
                pair["to"] = epoch_ms(pair.get("to"))
            changes[field] = pair
        e["changes"] = changes
        out.append(e)
    return out


def serialize_attendance(doc):
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["punch_in"] = epoch_ms(result.get("punch_in"))
    result["punch_out"] = epoch_ms(result.get("punch_out"))
    result["history"] = serialize_history(result.get("history", []))
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    return result


def require_employee(emp_code: str):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "Employee not found")
    return emp


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v)
        return v

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not MIN_MS <= v <= MAX_MS):
            raise ValueError("punched_at must be epoch milliseconds in the supported range")
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not MIN_MS <= v <= MAX_MS):
            raise ValueError("punched_at must be epoch milliseconds in the supported range")
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_instants(cls, v):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not MIN_MS <= v <= MAX_MS):
            raise ValueError("timestamps must be epoch milliseconds in the supported range")
        return v

    @model_validator(mode="after")
    def at_least_one_change(self):
        if self.status is None and self.punch_in is None and self.punch_out is None:
            raise ValueError("At least one of status, punch_in or punch_out is required")
        return self


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created = datetime.now(UTC).replace(microsecond=0)
    doc["created_at"] = created

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already ex ists")

    doc["created_at"] = epoch_ms(created)
    print("DEBUG create_employee response:", doc)
    print("DEBUG response value types:", {k: type(v).__name__ for k, v in doc.items()})
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": doc["created_at"],
    }



@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    docs = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for d in docs:
        d["created_at"] = epoch_ms(d.get("created_at"))
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = parse_ms(body.punched_at, default_now=True)
    d = record_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"], d),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    ts = parse_ms(body.punched_at, default_now=True)
    # Search newest eligible open record. The unique natural key means the update below can safely be conditional.
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not candidate:
        raise HTTPException(404, "No punch-in found")
    pin = candidate.get("punch_in")
    if pin is None:
        raise HTTPException(404, "No punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    delta = (as_utc(ts) - as_utc(pin)).total_seconds()
    if delta <= 0 or delta > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    work = compute_work_hours(pin, ts)
    overtime = compute_overtime(ts, candidate["date"], emp["shift_start"], emp["shift_end"])
    half = work < 4.50
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": candidate["date"], "punch_out": None, "punch_in": pin},
        {"$set": {"punch_out": ts, "work_hours": work, "overtime_minutes": overtime, "half_day": half}},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not exceed date_to")
    if status and status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date).isoformat()
    emp = require_employee(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not old:
        raise HTTPException(404, "Attendance record not found")
    if ("punch_in" in body.model_fields_set and body.punch_in is None) or ("punch_out" in body.model_fields_set and body.punch_out is None):
        raise HTTPException(422, "punch_in and punch_out must be epoch milliseconds when supplied")
    old_history = old.get("history", [])
    new_status = body.status if body.status is not None else old["status"]
    old_pin, old_pout = old.get("punch_in"), old.get("punch_out")
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        new_pin = new_pout = None
    else:
        new_pin = parse_ms(body.punch_in) if body.punch_in is not None else old_pin
        new_pout = parse_ms(body.punch_out) if body.punch_out is not None else old_pout
        if new_pin is None:
            raise HTTPException(422, "Presence status requires punch_in")
        if record_date(new_pin, emp["shift_start"], emp["shift_end"]) != day:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if new_pout is not None:
            seconds = (as_utc(new_pout) - as_utc(new_pin)).total_seconds()
            if seconds <= 0 or seconds > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    if new_status in ("ABSENT", "LEAVE"):
        work, late, overtime, half = None, 0, 0, False
    else:
        late = compute_late_minutes(
    new_pin, emp["shift_start"], emp["shift_end"], day
)
        if new_pout is None:
            work, overtime, half = None, 0, False
        else:
            work = compute_work_hours(new_pin, new_pout)
            overtime = compute_overtime(new_pout, day, emp["shift_start"], emp["shift_end"])
            half = work < 4.50
    proposed = {"status": new_status, "punch_in": new_pin, "punch_out": new_pout, "work_hours": work,
                "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    changes = {}
    for field, new_value in proposed.items():
        old_value = old.get(field)
        if field in ("late_minutes", "overtime_minutes"):
            old_value = old_value or 0
        if field == "half_day":
            old_value = bool(old_value)
        if field == "history":
            continue
        equal = (as_utc(old_value) == as_utc(new_value)) if isinstance(old_value, datetime) and isinstance(new_value, datetime) else old_value == new_value
        if not equal:
            before, after = old_value, new_value
            if field in ("punch_in", "punch_out"):
                before, after = epoch_ms(before), epoch_ms(after)
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "Request does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap prevents concurrent corrections overwriting one another's history.
    update_filter = {"emp_code": emp_code, "date": day, "history": old.get("history", [])} if "history" in old else {"emp_code": emp_code, "date": day, "history": {"$exists": False}}
    update = {"$set": proposed, "$push": {"history": entry}}
    updated = db.attendance_logs.find_one_and_update(update_filter, update, return_document=True)
    if updated is None:
        raise HTTPException(409, "Record changed concurrently; retry the correction")
    return serialize_attendance(updated)


def month_bounds(month: str):
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end


def weekday_expr(field="date"):
    return {"$dayOfWeek": {"date": {"$dateFromString": {"dateString": f"${field}", "format": "%Y-%m-%d"}}, "timezone": "UTC"}}


def working_days(start: date, end: date) -> int:
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = require_employee(emp_code)
    from_s, to_s, start, end = month_bounds(month)
    joined = max(start, parse_date(emp["joined_on"]))
    wd = working_days(joined, end) if joined <= end else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": from_s, "$lte": to_s}}},
        {"$set": {"_history": {"$ifNull": ["$history", []]}, "_half": {"$ifNull": ["$half_day", False]},
                   "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]},
                   "_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT)]}, {"$lte": ["$_weekday", 6]}, {"$gte": ["$_weekday", 2]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$_late"}, "total_overtime_minutes": {"$sum": "$_ot"}}}
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    row = rows[0] if rows else {}
    present_days = row.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": rounded(present_days),
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0),
            "attendance_pct": rounded(present_days / wd * 100, 4) if wd else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    from_s, to_s, start, end = month_bounds(month)
    emp_match = {"joined_on": {"$lte": to_s}}
    if department is not None:
        emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", from_s]}, {"$lte": ["$date", to_s]}]}}},
            {"$set": {"_half": {"$ifNull": ["$half_day", False]}, "_late": {"$ifNull": ["$late_minutes", 0]}, "_work": "$work_hours", "_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}
        ], "as": "logs"}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "logs": {"$push": "$logs"}}},
        {"$set": {"all_logs": {"$reduce": {"input": "$logs", "initialValue": [], "in": {"$concatArrays": ["$$value", "$$this"]}}}}},
        {"$set": {"present_logs": {"$filter": {"input": "$all_logs", "as": "l", "cond": {"$and": [{"$in": ["$$l.status", list(PRESENT)]}, {"$gte": ["$$l._weekday", 2]}, {"$lte": ["$$l._weekday", 6]}]}}},
                   "work_logs": {"$filter": {"input": "$all_logs", "as": "l", "cond": {"$and": [{"$in": ["$$l.status", list(PRESENT)]}, {"$ne": ["$$l.work_hours", None]}]}}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
                       "present_days": {"$sum": {"$map": {"input": "$present_logs", "as": "l", "in": {"$cond": ["$$l.half_day", 0.5, 1]}}}},
                       "avg_work_hours": {"$avg": "$work_logs.work_hours"},
                       "late_count": {"$size": {"$filter": {"input": "$all_logs", "as": "l", "cond": {"$gt": ["$$l._late", 0]}}}},
                       "total_late_minutes": {"$sum": "$all_logs._late"},
                       "leave_count": {"$size": {"$filter": {"input": "$all_logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
                       "on_duty_count": {"$size": {"$filter": {"input": "$all_logs", "as": "l", "cond": {"$eq": ["$$l.status", "ON_DUTY"]}}}}}},
        {"$sort": {"department": 1}}
    ]
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        if r["headcount"] == 0:
            continue
        r["present_days"] = rounded(r.get("present_days", 0))
        r["avg_work_hours"] = rounded(r.get("avg_work_hours"))
        r["total_late_minutes"] = r.get("total_late_minutes") or 0
        items.append(r)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    from_s, to_s, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": from_s, "$lte": to_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(..., alias="to")):
    start, end = parse_date(from_), parse_date(to)
    days = (end - start).days + 1
    if days <= 0:
        raise HTTPException(422, "to must be on or after from")
    if days > 92:
        raise HTTPException(422, "Date range must not exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "Department not found")
    start_s, end_s = start.isoformat(), end.isoformat()
    # Dates are generated by MongoDB; logs are joined through indexed employee/date keys.
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employee_codes": {"$push": "$emp_code"}}},
        {"$project": {"_id": 0, "employee_codes": 1, "dates": {"$map": {"input": {"$range": [0, days]}, "as": "i", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start_s}}, "unit": "day", "amount": "$$i"}}, "format": "%Y-%m-%d"}}}}}},
        {"$unwind": "$dates"},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$dates", "codes": "$employee_codes"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$set": {"_half": {"$ifNull": ["$half_day", False]}, "_late": {"$ifNull": ["$late_minutes", 0]}}}
        ], "as": "logs"}},
        {"$lookup": {"from": "employees", "let": {"day": "$dates"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
            {"$count": "n"}
        ], "as": "heads"}},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$heads.n", 0]}, 0]},
            "present_count": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENT)]}}},
                "as": "l", "in": {"$cond": ["$$l._half", 0.5, 1]}
            }}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": ["$$l._late", 0]}}}},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"date": {"$dateFromString": {"dateString": "$dates"}}}}, 2]}, {"$lte": [{"$dayOfWeek": {"date": {"$dateFromString": {"dateString": "$dates"}}}}, 6]}]}
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]},
            None
        ]}}},
        {"$setWindowFields": {"sortBy": {"dates": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$dates", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$sort": {"date": 1}}
    ]
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}






def explain_pipeline(collection, pipeline):
    raw = db.command({
        "explain": {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {}
        },
        "verbosity": "executionStats"
    })
    return json.loads(json_util.dumps(raw))



@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        if date_from: parse_date(date_from)
        if date_to: parse_date(date_to)
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not exceed date_to")
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status:
            if status not in STATUSES: raise HTTPException(422, "Invalid status")
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        pipeline = [{"$match": q}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}]
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_pipeline("attendance_logs", pipeline)}
    if endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        require_employee(emp_code)
        start, end, _, _ = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}}]
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_pipeline("attendance_logs", pipeline)}
    
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        start, end, _, _ = month_bounds(month)
        emp_match = {"joined_on": {"$lte": end}}
        if department is not None:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", start]},
                        {"$lte": ["$date", end]}
                    ]}}
                    }
                ],
                "as": "logs"
            }},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "logs": {"$push": "$logs"}
            }},
            {"$limit": 100000}
        ]
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_pipeline("employees", pipeline)
        }
        if not month: raise HTTPException(422, "month is required")
        start, end, _, _ = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}]
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_pipeline("attendance_logs", pipeline)}
    if endpoint == "department_trend":
        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")
        start, end = parse_date(from_), parse_date(to)
        days = (end - start).days + 1
        if days <= 0 or days > 92: raise HTTPException(422, "Invalid date range")
        pipeline = [{"$match": {"department": department}}, {"$limit": 100000}]
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_pipeline("employees", pipeline)}
    raise HTTPException(422, "Unknown endpoint")
