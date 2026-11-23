import calendar
import math
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from app.services.lab import LabError, audit, find, now_iso, parse_price


WEEKDAYS = {
    0: "الاثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الأحد",
}


def local_now():
    return datetime.now().astimezone()


def configure_attendance_settings(store, values, actor):
    try:
        idle_timeout = int(values.get("idle_timeout_minutes", ""))
        grace_minutes = int(values.get("late_grace_minutes", ""))
    except (TypeError, ValueError) as exc:
        raise LabError("إعدادات الحضور يجب أن تكون أرقاماً صحيحة.") from exc
    if not 1 <= idle_timeout <= 240:
        raise LabError("مدة الخمول يجب أن تكون بين دقيقة و240 دقيقة.")
    if not 0 <= grace_minutes <= 240:
        raise LabError("فترة السماح يجب أن تكون بين صفر و240 دقيقة.")

    def save(data):
        data["attendance_settings"] = {
            "idle_timeout_minutes": idle_timeout,
            "late_grace_minutes": grace_minutes,
        }
        audit(data, actor, "attendance.settings_updated", actor["id"])
        return data["attendance_settings"]

    return store.update(save)


def configure_employee(store, values, actor):
    user_id = values.get("user_id", "")
    hourly_rate = parse_price(values.get("hourly_rate", ""))
    if hourly_rate < 0:
        raise LabError("الأجر بالساعة لا يمكن أن يكون سالباً.")
    start_date = parse_date(values.get("start_date", ""), "تاريخ بداية الجدول")
    end_date_text = values.get("end_date", "").strip()
    end_date = parse_date(end_date_text, "تاريخ نهاية الجدول") if end_date_text else None
    if end_date and end_date < start_date:
        raise LabError("تاريخ نهاية الجدول يجب ألا يسبق تاريخ بدايته.")
    try:
        weekdays = sorted({int(day) for day in values.getlist("weekdays")})
    except (TypeError, ValueError) as exc:
        raise LabError("أيام العمل المحددة غير صالحة.") from exc
    if not weekdays or any(day not in WEEKDAYS for day in weekdays):
        raise LabError("اختر يوم عمل واحداً على الأقل.")
    start_time = parse_time(values.get("shift_start", ""), "بداية الوردية")
    end_time = parse_time(values.get("shift_end", ""), "نهاية الوردية")
    shift_hours = parse_price(values.get("hours_per_shift", ""))
    if not 0 < shift_hours <= 24:
        raise LabError("ساعات الوردية يجب أن تكون أكبر من صفر ولا تتجاوز 24.")
    if start_time == end_time:
        raise LabError("وقت بداية الوردية ونهايتها لا يمكن أن يكونا متطابقين.")
    extra_info = values.get("extra_info", "").strip()
    if len(extra_info) > 500:
        raise LabError("المعلومات الإضافية يجب ألا تتجاوز 500 حرف.")

    def save(data):
        employee = find(data["users"], user_id, "المستخدم")
        if not employee["active"]:
            raise LabError("لا يمكن تفعيل الحضور لمستخدم معطل.")
        for schedule in data["attendance_schedules"]:
            if schedule["user_id"] != user_id:
                continue
            previous_start = date.fromisoformat(schedule["start_date"])
            previous_end = (
                date.fromisoformat(schedule["end_date"])
                if schedule.get("end_date") else None
            )
            overlaps = (
                (previous_end is None or start_date <= previous_end)
                and (end_date is None or previous_start <= end_date)
            )
            if overlaps and previous_start >= start_date:
                raise LabError(
                    "يتعارض تاريخ بداية الجدول مع جدول سابق؛ اختر تاريخاً لاحقاً أو فترة غير متداخلة."
                )
        profile = next(
            (item for item in data["attendance_profiles"] if item["user_id"] == user_id),
            None,
        )
        if profile is None:
            profile = {"id": str(uuid4()), "user_id": user_id}
            data["attendance_profiles"].append(profile)
        profile.update(
            {
                "enabled": True,
                "hourly_rate": hourly_rate,
                "extra_info": extra_info,
                "updated_at": now_iso(),
                "updated_by": actor["id"],
            }
        )
        for schedule in data["attendance_schedules"]:
            if schedule["user_id"] != user_id:
                continue
            previous_start = date.fromisoformat(schedule["start_date"])
            previous_end = (
                date.fromisoformat(schedule["end_date"])
                if schedule.get("end_date") else None
            )
            if previous_end and previous_end < start_date:
                continue
            if end_date and previous_start > end_date:
                continue
            if previous_start >= start_date:
                continue
            if previous_end is None or previous_end >= start_date:
                previous_day = start_date - timedelta(days=1)
                schedule["end_date"] = previous_day.isoformat()
        data["attendance_schedules"].append(
            {
                "id": str(uuid4()),
                "user_id": user_id,
                "weekdays": weekdays,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat() if end_date else None,
                "shift_start": start_time.strftime("%H:%M"),
                "shift_end": end_time.strftime("%H:%M"),
                "hours_per_shift": shift_hours,
                "created_at": now_iso(),
                "created_by": actor["id"],
            }
        )
        audit(data, actor, "attendance.employee_configured", user_id)
        return profile

    return store.update(save)


def disable_employee_attendance(store, user_id, actor):
    def disable(data):
        profile = next(
            (item for item in data["attendance_profiles"] if item["user_id"] == user_id),
            None,
        )
        if profile is None:
            raise LabError("المستخدم ليس لديه ملف حضور.")
        profile["enabled"] = False
        profile["updated_at"] = now_iso()
        profile["updated_by"] = actor["id"]
        end_date = local_now().date().isoformat()
        expire_user_sessions(
            data, user_id, data["attendance_settings"]["idle_timeout_minutes"], local_now()
        )
        today = date.fromisoformat(end_date)
        for schedule in data["attendance_schedules"]:
            if schedule["user_id"] != user_id:
                continue
            schedule_start = date.fromisoformat(schedule["start_date"])
            schedule_end = (
                date.fromisoformat(schedule["end_date"])
                if schedule.get("end_date") else today
            )
            if schedule_start > today:
                schedule["end_date"] = (schedule_start - timedelta(days=1)).isoformat()
            elif schedule_end > today:
                schedule["end_date"] = today.isoformat()
        for record in data["attendance_sessions"]:
            if record["user_id"] == user_id and record.get("active"):
                close_session(record, local_now(), "disabled")
        audit(data, actor, "attendance.employee_disabled", user_id)

    return store.update(disable)


def start_attendance(store, user_id, at=None):
    at = at or local_now()

    def start(data):
        profile = enabled_profile(data, user_id)
        if profile is None:
            return None
        expire_user_sessions(data, user_id, data["attendance_settings"]["idle_timeout_minutes"], at)
        if any(
            record["user_id"] == user_id and record.get("active")
            for record in data["attendance_sessions"]
        ):
            return None
        record = make_session(data, profile, user_id, at)
        data["attendance_sessions"].append(record)
        return record

    return store.update(start)


def record_attendance_activity(store, user_id, at=None):
    at = at or local_now()

    def touch(data):
        profile = enabled_profile(data, user_id)
        if profile is None:
            return None
        timeout = data["attendance_settings"]["idle_timeout_minutes"]
        expire_user_sessions(data, user_id, timeout, at)
        active = next(
            (
                record for record in reversed(data["attendance_sessions"])
                if record["user_id"] == user_id and record.get("active")
            ),
            None,
        )
        if active is None:
            active = make_session(data, profile, user_id, at)
            data["attendance_sessions"].append(active)
        else:
            active["last_activity_at"] = at.isoformat(timespec="seconds")
        return active

    return store.update(touch)


def end_attendance(store, user_id, at=None):
    at = at or local_now()

    def end(data):
        timeout = data["attendance_settings"]["idle_timeout_minutes"]
        expire_user_sessions(data, user_id, timeout, at)
        active = next(
            (
                record for record in reversed(data["attendance_sessions"])
                if record["user_id"] == user_id and record.get("active")
            ),
            None,
        )
        if active:
            close_session(active, at, "logout")
            return active
        return None

    return store.update(end)


def close_expired_sessions(store, at=None):
    at = at or local_now()

    def close(data):
        timeout = data["attendance_settings"]["idle_timeout_minutes"]
        for user_id in {record["user_id"] for record in data["attendance_sessions"]}:
            expire_user_sessions(data, user_id, timeout, at)

    return store.update(close)


def submit_excuse(store, user_id, values):
    try:
        shift_date = date.fromisoformat(values.get("shift_date", ""))
    except ValueError as exc:
        raise LabError("تاريخ الوردية غير صالح.") from exc
    reason = values.get("reason", "").strip()
    if not reason or len(reason) > 500:
        raise LabError("اكتب سبباً لا يتجاوز 500 حرف.")

    def submit(data):
        if enabled_profile(data, user_id) is None:
            raise LabError("لا يوجد لديك ملف حضور نشط.")
        schedule = schedule_for_date(data, user_id, shift_date)
        if schedule is None:
            raise LabError("لا توجد وردية مجدولة لك في هذا التاريخ.")
        if any(
            item["user_id"] == user_id and item["shift_date"] == shift_date.isoformat()
            and item["status"] in {"pending", "approved"}
            for item in data["attendance_excuses"]
        ):
            raise LabError("يوجد بالفعل عذر مسجل لهذه الوردية.")
        excuse = {
            "id": str(uuid4()),
            "user_id": user_id,
            "schedule_id": schedule["id"],
            "shift_date": shift_date.isoformat(),
            "reason": reason,
            "status": "pending",
            "submitted_at": now_iso(),
        }
        data["attendance_excuses"].append(excuse)
        return excuse

    return store.update(submit)


def review_excuse(store, excuse_id, decision, actor, note=""):
    if decision not in {"approved", "rejected"}:
        raise LabError("قرار العذر غير صالح.")
    note = note.strip()
    if len(note) > 500:
        raise LabError("ملاحظة المراجعة يجب ألا تتجاوز 500 حرف.")

    def review(data):
        excuse = find(data["attendance_excuses"], excuse_id, "العذر")
        if excuse["status"] != "pending":
            raise LabError("تمت مراجعة هذا العذر مسبقاً.")
        excuse["status"] = decision
        excuse["reviewed_at"] = now_iso()
        excuse["reviewed_by"] = actor["id"]
        excuse["review_note"] = note
        audit(data, actor, f"attendance.excuse_{decision}", excuse_id)
        return excuse

    return store.update(review)


def calculate_monthly_payroll(store, year, month, at=None):
    if not 1 <= month <= 12:
        raise LabError("الشهر المحدد غير صالح.")
    at = at or local_now()
    close_expired_sessions(store, at)
    data = store.read()
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    month_start_at = datetime.combine(month_start, time.min).astimezone()
    next_month_at = datetime.combine(
        month_end + timedelta(days=1), time.min
    ).astimezone()
    profiles = {item["user_id"]: item for item in data["attendance_profiles"]}
    users = {item["id"]: item for item in data["users"]}
    excuses = data["attendance_excuses"]
    result = []
    for user_id, profile in profiles.items():
        if user_id not in users:
            continue
        schedules = [
            item for item in data["attendance_schedules"] if item["user_id"] == user_id
        ]
        sessions = [
            item for item in data["attendance_sessions"]
            if item["user_id"] == user_id
            and datetime.fromisoformat(item["started_at"]).astimezone() < next_month_at
            and (
                datetime.fromisoformat(item["ended_at"]).astimezone()
                if item.get("ended_at") else at
            ) > month_start_at
        ]
        scheduled_dates = {}
        for schedule in schedules:
            schedule_start = date.fromisoformat(schedule["start_date"])
            schedule_end = (
                date.fromisoformat(schedule["end_date"])
                if schedule.get("end_date") else month_end
            )
            first = max(month_start, schedule_start)
            last = min(month_end, schedule_end)
            current = first
            while current <= last:
                if current.weekday() in schedule["weekdays"]:
                    scheduled_dates[current.isoformat()] = schedule
                current += timedelta(days=1)

        attended_dates = {
            item["shift_date"] for item in sessions if item.get("shift_date")
        }
        approved_excuses = {
            item["shift_date"] for item in excuses
            if item["user_id"] == user_id and item["status"] == "approved"
        }
        gross = 0.0
        worked_seconds = 0
        for record in sessions:
            ended_at = datetime.fromisoformat(record["ended_at"]) if record.get("ended_at") else at
            started_at = datetime.fromisoformat(record["started_at"])
            session_start = max(started_at.astimezone(), month_start_at)
            session_end = min(ended_at.astimezone(), next_month_at)
            seconds = max(0, (session_end - session_start).total_seconds())
            worked_seconds += int(seconds)
            gross += seconds / 3600 * record["hourly_rate"]
        penalty = 0.0
        for record in sessions:
            if (
                record.get("shift_date")
                and month_start <= date.fromisoformat(record["shift_date"]) <= month_end
                and record["shift_date"] not in approved_excuses
                and record.get("late_minutes", 0) > 0
            ):
                penalty += record["late_minutes"] / 60 * record["hourly_rate"]
        penalty = min(gross, penalty)
        expected = set(scheduled_dates)
        present = expected & attended_dates
        excused = (expected & approved_excuses) - present
        if not profile.get("enabled") and not sessions and not expected:
            continue
        result.append(
            {
                "user": users[user_id],
                "profile": profile,
                "scheduled_shifts": len(expected),
                "scheduled_hours": round(
                    sum(schedule["hours_per_shift"] for schedule in scheduled_dates.values()),
                    2,
                ),
                "attended_shifts": len(present),
                "excused_shifts": len(expected & excused),
                "absent_shifts": len(expected - present - excused),
                "worked_hours": worked_seconds / 3600,
                "gross_pay": round(gross, 2),
                "late_penalty": round(penalty, 2),
                "net_pay": round(gross - penalty, 2),
                "sessions": sessions,
            }
        )
    return result


def make_session(data, profile, user_id, at):
    schedule = schedule_for_date(data, user_id, at.date())
    late_minutes = 0
    shift_date = None
    schedule_id = None
    scheduled_start = None
    if schedule:
        shift_date = at.date().isoformat()
        schedule_id = schedule["id"]
        scheduled_start_dt = datetime.combine(
            at.date(), time.fromisoformat(schedule["shift_start"])
        ).astimezone()
        scheduled_start = scheduled_start_dt.isoformat(timespec="seconds")
        prior_session = any(
            item["user_id"] == user_id and item.get("schedule_id") == schedule_id
            and item.get("shift_date") == shift_date
            for item in data["attendance_sessions"]
        )
        grace = data["attendance_settings"]["late_grace_minutes"]
        late_seconds = max(0, (at - scheduled_start_dt).total_seconds() - grace * 60)
        if not prior_session and late_seconds > 0:
            late_minutes = math.ceil(late_seconds / 60)
    return {
        "id": str(uuid4()),
        "user_id": user_id,
        "schedule_id": schedule_id,
        "shift_date": shift_date,
        "scheduled_start": scheduled_start,
        "late_minutes": late_minutes,
        "hourly_rate": profile["hourly_rate"],
        "started_at": at.isoformat(timespec="seconds"),
        "last_activity_at": at.isoformat(timespec="seconds"),
        "ended_at": None,
        "active": True,
        "close_reason": None,
    }


def expire_user_sessions(data, user_id, timeout_minutes, at):
    for record in data["attendance_sessions"]:
        if record["user_id"] != user_id or not record.get("active"):
            continue
        last_activity = datetime.fromisoformat(record["last_activity_at"])
        if at - last_activity >= timedelta(minutes=timeout_minutes):
            close_session(
                record,
                last_activity + timedelta(minutes=timeout_minutes),
                "idle",
            )


def close_session(record, at, reason):
    started = datetime.fromisoformat(record["started_at"])
    ended = max(started, at)
    record["ended_at"] = ended.isoformat(timespec="seconds")
    record["active"] = False
    record["close_reason"] = reason


def schedule_for_date(data, user_id, shift_date):
    for schedule in reversed(data["attendance_schedules"]):
        if schedule["user_id"] != user_id:
            continue
        if shift_date < date.fromisoformat(schedule["start_date"]):
            continue
        if schedule.get("end_date") and shift_date > date.fromisoformat(schedule["end_date"]):
            continue
        if shift_date.weekday() not in schedule["weekdays"]:
            continue
        return schedule
    return None


def enabled_profile(data, user_id):
    return next(
        (
            item for item in data["attendance_profiles"]
            if item["user_id"] == user_id and item.get("enabled")
        ),
        None,
    )


def parse_date(value, label):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise LabError(f"{label} غير صالح.") from exc


def parse_time(value, label):
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise LabError(f"{label} غير صالح.") from exc
