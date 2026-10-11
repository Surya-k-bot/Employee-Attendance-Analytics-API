"""Employee Attendance & Analytics API (contract v2.0).

Run from the candidate-kit root with ``uvicorn app.main:app --port 8000``.
"""
from __future__ import annotations

import os
import re
from calendar import monthrange
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError


load_dotenv()  # real environment variables take precedence over .env values
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Validation and time/serialization helpers
# --------------------------------------------------------------------------- #
def _check_epoch_ms(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer epoch-millisecond value")
    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError(f"must be between {MIN_EPOCH_MS} and {MAX_EPOCH_MS}")
    return value


def _date_string(value: str) -> str:
    try:
        parsed = Date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError("must use YYYY-MM-DD format")
    return value


class InputModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class EmployeeCreate(InputModel):
    emp_code: StrictStr = Field(pattern=r"^EMP\d{4,6}$")
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: StrictStr = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: StrictStr

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        return _date_string(value)

    @model_validator(mode="after")
    def distinct_shift_times(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(InputModel):
    emp_code: StrictStr
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: int | None) -> int | None:
        return _check_epoch_ms(value)

    @model_validator(mode="before")
    @classmethod
    def reject_null_punched_at(cls, value):
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an integer when supplied")
        return value


class PunchOutRequest(InputModel):
    emp_code: StrictStr
    punched_at: StrictInt | None = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: int | None) -> int | None:
        return _check_epoch_ms(value)

    @model_validator(mode="before")
    @classmethod
    def reject_null_punched_at(cls, value):
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an integer when supplied")
        return value


class RegularizeRequest(InputModel):
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_punch_time(cls, value: int | None) -> int | None:
        return _check_epoch_ms(value)

    @model_validator(mode="before")
    @classmethod
    def reject_null_regularization_fields(cls, value):
        if isinstance(value, dict):
            for field in ("status", "punch_in", "punch_out"):
                if field in value and value[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return value


def _now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def _from_epoch_ms(value: int) -> datetime:
    # R1: discard milliseconds before storing or performing any calculations.
    return datetime.fromtimestamp(value // 1000, tz=UTC).replace(tzinfo=None)


def _to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    delta = aware - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def _truncate_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _shift_start_utc(attendance_date: str, shift_start: str) -> datetime:
    day = Date.fromisoformat(attendance_date)
    hour, minute = map(int, shift_start.split(":"))
    return datetime.combine(day, time(hour, minute), IST).astimezone(UTC).replace(tzinfo=None)


def _shift_end_utc(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    day = Date.fromisoformat(attendance_date)
    start_minutes = int(shift_start[:2]) * 60 + int(shift_start[3:])
    end_minutes = int(shift_end[:2]) * 60 + int(shift_end[3:])
    if end_minutes <= start_minutes:
        day += timedelta(days=1)
    hour, minute = map(int, shift_end.split(":"))
    return datetime.combine(day, time(hour, minute), IST).astimezone(UTC).replace(tzinfo=None)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = _truncate_datetime(punch_in).replace(tzinfo=UTC).astimezone(IST)
    start_minutes = int(shift_start[:2]) * 60 + int(shift_start[3:])
    end_minutes = int(shift_end[:2]) * 60 + int(shift_end[3:])
    local_minutes = local.hour * 60 + local.minute
    day = local.date()
    if end_minutes <= start_minutes and local_minutes < end_minutes:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    punch_in = _truncate_datetime(punch_in)
    elapsed = int((punch_in - _shift_start_utc(attendance_date, shift_start)).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((_truncate_datetime(punch_out) - _truncate_datetime(punch_in)).total_seconds())
    value = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    elapsed = int((_truncate_datetime(punch_out) - _shift_end_utc(attendance_date, shift_start, shift_end)).total_seconds())
    return elapsed // 60 if elapsed >= 1800 else 0


def _employee_response(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _to_epoch_ms(doc["created_at"]),
    }


def _attendance_response(doc: dict) -> dict:
    history = []
    for entry in doc.get("history", []) or []:
        changes = {}
        for field, values in (entry.get("changes") or {}).items():
            pair = dict(values)
            if field in ("punch_in", "punch_out"):
                pair["from"] = _to_epoch_ms(pair.get("from"))
                pair["to"] = _to_epoch_ms(pair.get("to"))
            changes[field] = pair
        history.append({
            "at": _to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _to_epoch_ms(doc.get("punch_in")),
        "punch_out": _to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def _month_bounds(month: str) -> tuple[str, str, Date, Date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month_num = map(int, month.split("-"))
    try:
        start = Date(year, month_num, 1)
    except ValueError as exc:
        raise HTTPException(422, "month must be a valid YYYY-MM calendar month") from exc
    end = Date(year, month_num, monthrange(year, month_num)[1])
    return start.isoformat(), (end + timedelta(days=1)).isoformat(), start, end


def _working_days(joined_on: str, start: Date, end: Date) -> int:
    first = max(Date.fromisoformat(joined_on), start)
    if first > end:
        return 0
    days = 0
    current = first
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


def _round_half_up_expr(expression, places: int):
    scale = 10 ** places
    # All reported attendance quantities are non-negative; floor(x + .5) is half-up.
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}}, scale]}


def _workday_expr(date_expression="$_date"):
    return {"$and": [
        {"$ne": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_expression}}}, 1]},
        {"$ne": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_expression}}}, 7]},
    ]}


def _attendance_match_query(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds = {}
        if date_from is not None:
            bounds["$gte"] = date_from
        if date_to is not None:
            bounds["$lte"] = date_to
        query["date"] = bounds
    if status is not None:
        query["status"] = status
    return query


# --------------------------------------------------------------------------- #
# Shared MongoDB aggregation pipelines (also used by explain)
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, start: str, end_exclusive: str) -> list[dict]:
    present = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    workday = _workday_expr("$date")
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": end_exclusive}}},
        {"$addFields": {"_date": "$date"}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [present, workday]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(start: str, end_exclusive: str, end_inclusive: str, department: str | None) -> list[dict]:
    employee_match: dict = {"joined_on": {"$lte": end_inclusive}}
    if department is not None:
        employee_match["department"] = department
    date_range = {"$gte": start, "$lt": end_exclusive}
    workday = _workday_expr("$date")
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"date": date_range, "$expr": {"$eq": ["$emp_code", "$$code"]}}},
                {"$addFields": {"_date": "$date"}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, workday]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0,
                    ]}},
                    "work_hours_sum": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": ["$work_hours", None]}]},
                        "$work_hours", 0,
                    ]}},
                    "work_hours_count": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, {"$ne": ["$work_hours", None]}]},
                        1, 0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "stats",
        }},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {
            "present_days": 0, "work_hours_sum": 0, "work_hours_count": 0,
            "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0,
        }]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "work_hours_sum": {"$sum": "$stats.work_hours_sum"},
            "work_hours_count": {"$sum": "$stats.work_hours_count"},
            "late_count": {"$sum": "$stats.late_count"},
            "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"},
            "on_duty_count": {"$sum": "$stats.on_duty_count"},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                _round_half_up_expr({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2),
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(start: str, end_exclusive: str, department: str | None, limit: int) -> list[dict]:
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lt": end_exclusive}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee",
        }},
        {"$unwind": "$employee"},  # seeded orphan logs do not appear in the leaderboard
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


def department_trend_pipeline(department: str, start_date: Date, end_date: Date) -> list[dict]:
    days = (end_date - start_date).days
    return [
        {"$documents": [{"start": datetime.combine(start_date, time()), "end": datetime.combine(end_date, time())}]},
        {"$set": {"dates": {"$map": {
            "input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$start", "endDate": "$end", "unit": "day"}}, 1]}]},
            "as": "offset",
            "in": {"$dateAdd": {"startDate": "$start", "unit": "day", "amount": "$$offset"}},
        }}}},
        {"$unwind": "$dates"},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates", "timezone": "UTC"}},
            "_weekday": {"$dayOfWeek": "$dates"},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$project": {"_id": 0, "emp_code": 1}},
            ],
            "as": "joined_employees",
        }},
        {"$set": {"headcount": {"$size": "$joined_employees"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {
                    "from": "employees", "localField": "emp_code", "foreignField": "emp_code",
                    "pipeline": [{"$match": {"department": department}}], "as": "employee",
                }},
                {"$match": {"employee.0": {"$exists": True}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
            "as": "daily_logs",
        }},
        {"$set": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily_logs.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily_logs.late_count", 0]}, 0]},
            "is_working_day": {"$and": [{"$gt": ["$_weekday", 1]}, {"$lt": ["$_weekday", 7]}]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _round_half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"_moving_avg": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$_moving_avg", None]}, None,
                _round_half_up_expr("$_moving_avg", 4),
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


def _run_aggregation(collection: str, pipeline: list[dict], database_level: bool = False) -> list[dict]:
    if database_level:
        return list(db.command({"aggregate": 1, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True})["cursor"]["firstBatch"])
    return list(db[collection].aggregate(pipeline, allowDiskUse=True))


def _explain_aggregation(collection: str, pipeline: list[dict], database_level: bool = False) -> dict:
    target = 1 if database_level else collection
    explain = db.command({
        "explain": {"aggregate": target, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True},
        "verbosity": "executionStats",
    })
    return json_util.loads(json_util.dumps(explain))


# --------------------------------------------------------------------------- #
# Indexes and system
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punch_in")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_late_employee")


@app.get("/health", tags=["system"])
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employee endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = _now_utc_naive()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return _employee_response(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_employee_response(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance CRUD endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    if body.punched_at is None:
        punched_at = _now_utc_naive()
    else:
        punched_at = _from_epoch_ms(body.punched_at)
    day = attendance_date_for(punched_at, employee["shift_start"], employee["shift_end"])
    record = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return _attendance_response(record)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = _now_utc_naive() if body.punched_at is None else _from_epoch_ms(body.punched_at)
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    punch_in_time = _truncate_datetime(candidate["punch_in"])
    seconds = (punched_at - punch_in_time).total_seconds()
    if seconds <= 0 or seconds > 86_400:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    hours = compute_work_hours(punch_in_time, punched_at)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punched_at,
            "work_hours": hours,
            "overtime_minutes": compute_overtime(punched_at, employee["shift_start"], employee["shift_end"], candidate["date"]),
            "half_day": hours < 4.50,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return _attendance_response(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: str | None = None,
    date_from: Date | None = None,
    date_to: Date | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    start = date_from.isoformat() if date_from else None
    end = date_to.isoformat() if date_to else None
    query = _attendance_match_query(emp_code, start, end, status)
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_attendance_response(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: Annotated[str, Path(pattern=r"^\d{4}-\d{2}-\d{2}$")],
    body: RegularizeRequest,
):
    try:
        record_date = _date_string(date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if current is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    old_values = {
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    final_status = body.status if body.status is not None else old_values["status"]
    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in body.model_fields_set or "punch_out" in body.model_fields_set:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot be supplied with punch times")
        final_in = final_out = None
    else:
        final_in = _from_epoch_ms(body.punch_in) if "punch_in" in body.model_fields_set else old_values["punch_in"]
        final_out = _from_epoch_ms(body.punch_out) if "punch_out" in body.model_fields_set else old_values["punch_out"]
        if final_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if attendance_date_for(final_in, employee["shift_start"], employee["shift_end"]) != record_date:
            raise HTTPException(status_code=422, detail="punch_in must belong to the record attendance date")
        if final_out is not None:
            duration = (final_out - final_in).total_seconds()
            if duration <= 0 or duration > 86_400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    if final_status in ("ABSENT", "LEAVE"):
        new_values = {
            "status": final_status, "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
        }
    else:
        new_hours = compute_work_hours(final_in, final_out) if final_out is not None else None
        new_values = {
            "status": final_status,
            "punch_in": final_in,
            "punch_out": final_out,
            "work_hours": new_hours,
            "late_minutes": compute_late_minutes(final_in, employee["shift_start"], record_date),
            "overtime_minutes": compute_overtime(final_out, employee["shift_start"], employee["shift_end"], record_date) if final_out is not None else 0,
            "half_day": new_hours < 4.50 if new_hours is not None else False,
        }
    changes = {
        field: {"from": old_values[field], "to": new_values[field]}
        for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
        if old_values[field] != new_values[field]
    }
    if not changes:
        raise HTTPException(status_code=422, detail="regularization does not change the record")

    history_entry = {
        "at": _now_utc_naive(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    expected_history = current.get("history", [])
    history_filter = {"history": expected_history} if "history" in current else {"history": {"$exists": False}}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], **history_filter},
        {"$set": {field: new_values[field] for field in new_values}, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry the correction")
    return _attendance_response(updated)


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    start, end_exclusive, start_date, end_date = _month_bounds(month)
    rows = _run_aggregation("attendance_logs", employee_monthly_pipeline(emp_code, start, end_exclusive))
    metrics = rows[0] if rows else {}
    working_days = _working_days(employee["joined_on"], start_date, end_date)
    present_days = metrics.get("present_days", 0)
    attendance_pct = None if working_days == 0 else float(
        (Decimal(str(present_days)) * Decimal(100) / Decimal(working_days)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": metrics.get("leave_days", 0),
        "late_count": metrics.get("late_count", 0),
        "total_late_minutes": metrics.get("total_late_minutes", 0),
        "total_overtime_minutes": metrics.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: str | None = None,
):
    start, end_exclusive, _start_date, end_date = _month_bounds(month)
    rows = _run_aggregation("employees", department_summary_pipeline(start, end_exclusive, end_date.isoformat(), department))
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
):
    start, end_exclusive, _start_date, _end_date = _month_bounds(month)
    rows = _run_aggregation("attendance_logs", late_leaderboard_pipeline(start, end_exclusive, department, limit))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: Annotated[Date, Query(alias="from")],
    to_date: Annotated[Date, Query(alias="to")],
):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = department_trend_pipeline(department, from_date, to_date)
    rows = _run_aggregation("attendance_logs", pipeline, database_level=True)
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Query plan inspection
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Date | None = None,
    date_to: Date | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    from_date: Date | None = Query(default=None, alias="from"),
    to_date: Date | None = Query(default=None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        query = _attendance_match_query(emp_code, date_from.isoformat() if date_from else None, date_to.isoformat() if date_to else None, status)
        raw = db.command({
            "explain": {
                "find": "attendance_logs", "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size, "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        start, end_exclusive, _start_date, _end_date = _month_bounds(month)
        raw = _explain_aggregation("attendance_logs", employee_monthly_pipeline(emp_code, start, end_exclusive))
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, end_exclusive, _start_date, end_date = _month_bounds(month)
        raw = _explain_aggregation("employees", department_summary_pipeline(start, end_exclusive, end_date.isoformat(), department))
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, end_exclusive, _start_date, _end_date = _month_bounds(month)
        raw = _explain_aggregation("attendance_logs", late_leaderboard_pipeline(start, end_exclusive, department, limit))
        collection = "attendance_logs"
    else:
        if not department or from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(status_code=422, detail="trend range must be ordered and no longer than 92 days")
        raw = _explain_aggregation("attendance_logs", department_trend_pipeline(department, from_date, to_date), database_level=True)
        collection = "attendance_logs"
    return {"endpoint": endpoint, "collection": collection, "explain": json_util.loads(json_util.dumps(raw))}