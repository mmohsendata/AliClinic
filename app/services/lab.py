import math
from datetime import datetime, timezone
from uuid import uuid4


ORDER_STATUSES = [
    "جديد",
    "بانتظار سحب العينة",
    "تم سحب العينة",
    "قيد التحليل",
    "بانتظار المراجعة",
    "معتمد",
    "مكتمل",
    "ملغي",
]


class LabError(ValueError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_patient(store, values, actor):
    name = values.get("name", "").strip()
    phone = values.get("phone", "").strip()
    if not name or not phone:
        raise LabError("الاسم ورقم الهاتف مطلوبان.")

    def create(data):
        data["demo_seeded"] = False
        if any(patient["phone"] == phone for patient in data["patients"]):
            raise LabError("يوجد ملف مريض مسجل برقم الهاتف نفسه.")
        patient = {
            "id": str(uuid4()),
            "medical_record_number": f"MR-{uuid4().hex[:8].upper()}",
            "name": name,
            "english_name": values.get("english_name", "").strip(),
            "gender": values.get("gender", "غير محدد"),
            "date_of_birth": values.get("date_of_birth", "").strip(),
            "phone": phone,
            "national_id": values.get("national_id", "").strip(),
            "created_at": now_iso(),
        }
        data["patients"].append(patient)
        audit(data, actor, "patient.created", patient["id"])
        return patient

    return store.update(create)


def create_test(store, values, actor):
    name = values.get("name", "").strip()
    price = parse_price(values.get("price", ""))
    if not name or price < 0:
        raise LabError("اسم التحليل والسعر الصحيح مطلوبان.")

    def create(data):
        data["demo_seeded"] = False
        code = values.get("code", "").strip().upper()
        if code and any(test["code"] == code for test in data["tests"]):
            raise LabError("رمز التحليل مستخدم بالفعل.")
        test = {
            "id": str(uuid4()),
            "code": code or f"T-{uuid4().hex[:6].upper()}",
            "name": name,
            "english_name": values.get("english_name", "").strip(),
            "category": values.get("category", "عام").strip(),
            "price": price,
            "sample_type": values.get("sample_type", "").strip(),
            "active": True,
            "reference_range": "",
        }
        data["tests"].append(test)
        audit(data, actor, "test.created", test["id"])
        return test

    return store.update(create)


def create_order(store, patient_id, test_ids, actor):
    def create(data):
        data["demo_seeded"] = False
        patient = find(data["patients"], patient_id, "المريض")
        tests = [find(data["tests"], test_id, "التحليل") for test_id in dict.fromkeys(test_ids)]
        tests = [test for test in tests if test["active"]]
        if not tests:
            raise LabError("اختر تحليلاً نشطاً واحداً على الأقل.")
        order = {
            "id": str(uuid4()),
            "number": f"LAB-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:5].upper()}",
            "patient_id": patient["id"],
            "patient_name": patient["name"],
            "test_items": [
                {"test_id": test["id"], "name": test["name"], "price": test["price"]}
                for test in tests
            ],
            "total": round(sum(test["price"] for test in tests), 2),
            "paid": 0.0,
            "status": "بانتظار سحب العينة",
            "created_at": now_iso(),
            "created_by": actor["id"],
        }
        data["orders"].append(order)
        for item in order["test_items"]:
            data["samples"].append(
                {
                    "id": str(uuid4()),
                    "order_id": order["id"],
                    "test_id": item["test_id"],
                    "identifier": f"S-{uuid4().hex[:10].upper()}",
                    "status": "بانتظار السحب",
                    "timeline": [{"status": "بانتظار السحب", "at": now_iso(), "by": actor["id"]}],
                }
            )
        audit(data, actor, "order.created", order["id"])
        return order

    return store.update(create)


def update_sample(store, sample_id, status, actor, reason=""):
    allowed = {"تم السحب", "مستلمة", "مرفوضة", "قيد المعالجة", "مكتملة"}
    if status not in allowed:
        raise LabError("حالة العينة غير صالحة.")

    reason = reason.strip()

    def update(data):
        sample = find(data["samples"], sample_id, "العينة")
        if status == "مرفوضة" and not reason:
            raise LabError("اكتب سبب رفض العينة لتوثيقه.")
        if sample["status"] == "مرفوضة":
            if status != "تم السحب" or not reason:
                raise LabError("العينة المرفوضة تتطلب توثيق إعادة السحب قبل المتابعة.")
            sample["resolution_reason"] = reason
        if status == "مرفوضة":
            sample["rejection_reason"] = reason
        sample["status"] = status
        sample["timeline"].append({"status": status, "at": now_iso(), "by": actor["id"]})
        refresh_order_status(data, sample["order_id"])
        audit(data, actor, "sample.status_changed", sample_id)
        return sample

    return store.update(update)


def enter_result(store, order_id, test_id, value, actor, reason=""):
    value = value.strip()
    reason = reason.strip()
    if not value:
        raise LabError("أدخل نتيجة التحليل.")

    def save(data):
        order = find(data["orders"], order_id, "الطلب")
        sample = next(
            (s for s in data["samples"] if s["order_id"] == order_id and s["test_id"] == test_id),
            None,
        )
        if not sample or sample["status"] in {"مرفوضة", "بانتظار السحب"}:
            raise LabError("لا يمكن تسجيل نتيجة قبل استلام عينة صالحة.")
        if order["status"] in {"معتمد", "مكتمل", "ملغي"}:
            raise LabError("نتائج الطلب المعتمد مقفلة، ولا يمكن تعديل طلب معتمد أو مكتمل أو ملغي.")
        result = next(
            (r for r in data["results"] if r["order_id"] == order_id and r["test_id"] == test_id),
            None,
        )
        if result and result["status"] == "معتمد":
            raise LabError("النتيجة المعتمدة مقفلة؛ يتطلب التعديل مسار تصحيح موثقاً.")
        if result and not reason:
            raise LabError("تعديل النتيجة يتطلب سبباً موثقاً.")
        if result is None:
            result = {
                "id": str(uuid4()),
                "order_id": order_id,
                "test_id": test_id,
                "status": "مدخلة",
                "versions": [],
            }
            data["results"].append(result)
        version = {"value": value, "at": now_iso(), "by": actor["id"]}
        if reason:
            version["reason"] = reason
        result["versions"].append(version)
        result["value"] = value
        result["status"] = "مدخلة"
        sample["status"] = "قيد المعالجة"
        sample["timeline"].append({"status": "قيد المعالجة", "at": now_iso(), "by": actor["id"]})
        order["status"] = "بانتظار المراجعة"
        audit(data, actor, "result.entered", result["id"])
        return result

    return store.update(save)


def approve_order(store, order_id, actor):
    def approve(data):
        order = find(data["orders"], order_id, "الطلب")
        matching = [r for r in data["results"] if r["order_id"] == order_id]
        if len(matching) != len(order["test_items"]) or any(not r.get("value") for r in matching):
            raise LabError("لا يمكن اعتماد الطلب قبل إدخال جميع النتائج.")
        for result in matching:
            result["status"] = "معتمد"
            result["approved_at"] = now_iso()
            result["approved_by"] = actor["id"]
        for sample in data["samples"]:
            if sample["order_id"] == order_id:
                sample["status"] = "مكتملة"
                sample["timeline"].append(
                    {"status": "مكتملة", "at": now_iso(), "by": actor["id"]}
                )
        order["status"] = "معتمد"
        order["approved_at"] = now_iso()
        order["approved_by"] = actor["id"]
        audit(data, actor, "order.approved", order_id)
        return order

    return store.update(approve)


def audit(data, actor, action, record_id):
    data["audit_logs"].append(
        {
            "id": str(uuid4()),
            "actor_id": actor["id"],
            "actor_name": actor["username"],
            "action": action,
            "record_id": record_id,
            "at": now_iso(),
        }
    )


def find(records, record_id, label):
    record = next((item for item in records if item["id"] == record_id), None)
    if record is None:
        raise LabError(f"{label} غير موجود.")
    return record


def refresh_order_status(data, order_id):
    order = find(data["orders"], order_id, "الطلب")
    samples = [sample for sample in data["samples"] if sample["order_id"] == order_id]
    if samples and all(sample["status"] in {"تم السحب", "مستلمة", "قيد المعالجة", "مكتملة"} for sample in samples):
        order["status"] = "تم سحب العينة"


def parse_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise LabError("السعر يجب أن يكون رقماً.") from exc
    if not math.isfinite(price):
        raise LabError("السعر يجب أن يكون رقماً صالحاً.")
    return round(price, 2)
