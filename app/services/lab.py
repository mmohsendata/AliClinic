import math
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from app.services.lab_templates import STARTER_TEMPLATES


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
            "result_template": [],
            "reference_source_url": "",
            "reference_verified": False,
            "inventory_recipe": [],
        }
        data["tests"].append(test)
        audit(data, actor, "test.created", test["id"])
        return test

    return store.update(create)


def import_starter_templates(store, actor):
    def import_templates(data):
        existing_codes = {test["code"] for test in data["tests"]}
        added = []
        for template in STARTER_TEMPLATES:
            if template["code"] in existing_codes:
                continue
            test = {
                "id": str(uuid4()),
                "price": 0.0,
                "active": True,
                "reference_verified": False,
                "inventory_recipe": [],
                **deepcopy(template),
            }
            data["tests"].append(test)
            existing_codes.add(test["code"])
            added.append(test)
            audit(data, actor, "test.template_imported", test["id"])
        return added

    return store.update(import_templates)


def update_test_template(store, test_id, values, actor):
    names = values.getlist("component_name")
    units = values.getlist("component_unit")
    ranges = values.getlist("component_range")
    if len(names) != len(units) or len(names) != len(ranges):
        raise LabError("أدخل حقول القالب بوحدات ونطاقات متطابقة.")
    if len(names) > 60:
        raise LabError("القالب لا يمكن أن يتجاوز 60 حقلاً.")

    components = []
    rows = list(zip(names, units, ranges))
    if any(name.strip() or unit.strip() or reference_range.strip() for name, unit, reference_range in rows):
        for index, (name, unit, reference_range) in enumerate(rows):
            name, unit, reference_range = name.strip(), unit.strip(), reference_range.strip()
            if not name or len(name) > 120 or len(unit) > 32 or len(reference_range) > 160:
                raise LabError("تحقق من أسماء الحقول والوحدات والنطاقات المرجعية.")
            components.append(
                {
                    "id": f"field-{index + 1}",
                    "name": name,
                    "unit": unit,
                    "reference_range": reference_range,
                }
            )

    source_url = values.get("reference_source_url", "").strip()
    try:
        parsed_source = urlsplit(source_url)
    except ValueError:
        parsed_source = None
    valid_source = (
        parsed_source is not None
        and parsed_source.scheme == "https"
        and bool(parsed_source.hostname)
        and not parsed_source.username
        and not parsed_source.password
    )
    if source_url and (len(source_url) > 500 or not valid_source):
        raise LabError("رابط المصدر يجب أن يبدأ بـ https:// وألا يتجاوز 500 حرف.")
    verified = values.get("verify") == "yes"
    if verified and (
        not components
        or not source_url
        or any(not component["reference_range"] for component in components)
    ):
        raise LabError("لا يمكن اعتماد القالب قبل إدخال المصدر والنطاق المرجعي لكل حقل.")

    def save(data):
        test = find(data["tests"], test_id, "التحليل")
        test["result_template"] = components
        test["reference_source_url"] = source_url
        test["reference_verified"] = verified
        if verified:
            test["reference_verified_by"] = actor["id"]
            test["reference_verified_at"] = now_iso()
        else:
            test.pop("reference_verified_by", None)
            test.pop("reference_verified_at", None)
        audit(data, actor, "test.template_updated", test_id)
        return test

    return store.update(save)


def create_order(store, patient_id, test_ids, actor):
    def create(data):
        data["demo_seeded"] = False
        patient = find(data["patients"], patient_id, "المريض")
        tests = [find(data["tests"], test_id, "التحليل") for test_id in dict.fromkeys(test_ids)]
        tests = [test for test in tests if test["active"]]
        if not tests:
            raise LabError("اختر تحليلاً نشطاً واحداً على الأقل.")
        if any(test.get("result_template") and not test.get("reference_verified") for test in tests):
            raise LabError("يجب اعتماد قالب النتائج ونطاقاته محلياً قبل إنشاء الطلب.")
        order = {
            "id": str(uuid4()),
            "number": f"LAB-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:5].upper()}",
            "patient_id": patient["id"],
            "patient_name": patient["name"],
            "test_items": [
                {
                    "test_id": test["id"],
                    "name": test["name"],
                    "price": test["price"],
                    "result_template": deepcopy(test.get("result_template", [])),
                    "reference_source_url": test.get("reference_source_url", ""),
                    "reference_verified": bool(test.get("reference_verified")),
                    "inventory_recipe": [
                        {
                            "item_id": recipe["item_id"],
                            "item_name": find(
                                data["inventory_items"], recipe["item_id"], "عنصر المخزون"
                            )["name"],
                            "unit": find(
                                data["inventory_items"], recipe["item_id"], "عنصر المخزون"
                            )["unit"],
                            "quantity": recipe["quantity"],
                        }
                        for recipe in test.get("inventory_recipe", [])
                    ],
                }
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


def enter_result(store, order_id, test_id, value, actor, reason="", values=None):
    value = (value or "").strip()
    reason = reason.strip()
    values = {
        key: "" if result is None else str(result).strip()
        for key, result in (values or {}).items()
    }

    def save(data):
        result_value = value
        order = find(data["orders"], order_id, "الطلب")
        order_item = next(
            (item for item in order["test_items"] if item["test_id"] == test_id), None
        )
        if order_item is None:
            raise LabError("التحليل غير موجود في الطلب.")
        template = order_item.get("result_template", [])
        if template:
            expected_ids = {component["id"] for component in template}
            if set(values) != expected_ids or any(not result for result in values.values()):
                raise LabError("أدخل نتيجة لكل حقول قالب التحليل.")
            result_value = ""
        elif not result_value:
            raise LabError("أدخل نتيجة التحليل.")
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
            for recipe in order_item.get("inventory_recipe", []):
                if any(
                    transaction["transaction_type"] == "consumption"
                    and transaction["order_id"] == order_id
                    and transaction["test_id"] == test_id
                    and transaction["item_id"] == recipe["item_id"]
                    for transaction in data["inventory_transactions"]
                ):
                    continue
                transaction = {
                    "id": str(uuid4()),
                    "transaction_type": "consumption",
                    "item_id": recipe["item_id"],
                    "item_name": recipe["item_name"],
                    "quantity": recipe["quantity"],
                    "unit": recipe["unit"],
                    "order_id": order_id,
                    "order_number": order["number"],
                    "test_id": test_id,
                    "test_name": order_item["name"],
                    "at": now_iso(),
                    "by": actor["id"],
                }
                data["inventory_transactions"].append(transaction)
                audit(data, actor, "inventory.consumed", transaction["id"])
        version = {"value": result_value, "at": now_iso(), "by": actor["id"]}
        if template:
            version["values"] = values.copy()
        if reason:
            version["reason"] = reason
        result["versions"].append(version)
        if template:
            result["values"] = values.copy()
            result.pop("value", None)
        else:
            result["value"] = result_value
        result["status"] = "مدخلة"
        sample["status"] = "قيد المعالجة"
        sample["timeline"].append({"status": "قيد المعالجة", "at": now_iso(), "by": actor["id"]})
        order["status"] = "بانتظار المراجعة"
        audit(data, actor, "result.entered", result["id"])
        return result

    return store.update(save)


def create_inventory_item(store, values, actor):
    name = values.get("name", "").strip()
    unit = values.get("unit", "").strip()
    reorder_level = parse_nonnegative_amount(values.get("reorder_level", "0"), "حد التنبيه")
    if not name or len(name) > 120 or not unit or len(unit) > 32:
        raise LabError("أدخل اسم عنصر ووحدة قياس صحيحين.")

    def create(data):
        if any(item["name"].casefold() == name.casefold() for item in data["inventory_items"]):
            raise LabError("اسم عنصر المخزون مستخدم بالفعل.")
        item = {
            "id": str(uuid4()),
            "name": name,
            "unit": unit,
            "reorder_level": reorder_level,
            "created_at": now_iso(),
        }
        data["inventory_items"].append(item)
        audit(data, actor, "inventory.item_created", item["id"])
        return item

    return store.update(create)


def record_inventory_purchase(store, values, actor):
    item_id = values.get("item_id", "")
    quantity = parse_positive_amount(values.get("quantity", ""), "الكمية")
    unit_cost = parse_price(values.get("unit_cost", ""))
    if unit_cost < 0:
        raise LabError("تكلفة الوحدة لا يمكن أن تكون سالبة.")
    supplier = values.get("supplier", "").strip()
    reference = values.get("reference", "").strip()
    if len(supplier) > 120 or len(reference) > 80:
        raise LabError("بيانات المورد أو المرجع أطول من المسموح.")

    def record(data):
        item = find(data["inventory_items"], item_id, "عنصر المخزون")
        transaction = {
            "id": str(uuid4()),
            "transaction_type": "purchase",
            "item_id": item["id"],
            "item_name": item["name"],
            "quantity": quantity,
            "unit": item["unit"],
            "unit_cost": unit_cost,
            "supplier": supplier,
            "reference": reference,
            "at": now_iso(),
            "by": actor["id"],
        }
        data["inventory_transactions"].append(transaction)
        audit(data, actor, "inventory.purchase_recorded", transaction["id"])
        return transaction

    return store.update(record)


def set_test_inventory_recipe(store, test_id, item_id, quantity, actor):
    amount = parse_positive_amount(quantity, "كمية الاستخدام")

    def set_recipe(data):
        test = find(data["tests"], test_id, "التحليل")
        find(data["inventory_items"], item_id, "عنصر المخزون")
        recipe = test.setdefault("inventory_recipe", [])
        existing = next((entry for entry in recipe if entry["item_id"] == item_id), None)
        if existing:
            existing["quantity"] = amount
        else:
            recipe.append({"item_id": item_id, "quantity": amount})
        audit(data, actor, "inventory.recipe_updated", test_id)
        return test

    return store.update(set_recipe)


def remove_test_inventory_recipe(store, test_id, item_id, actor):
    def remove_recipe(data):
        test = find(data["tests"], test_id, "التحليل")
        recipe = test.setdefault("inventory_recipe", [])
        updated = [entry for entry in recipe if entry["item_id"] != item_id]
        if len(updated) == len(recipe):
            raise LabError("عنصر الاستخدام غير موجود في وصفة التحليل.")
        test["inventory_recipe"] = updated
        audit(data, actor, "inventory.recipe_removed", test_id)
        return test

    return store.update(remove_recipe)


def parse_positive_amount(value, label):
    amount = parse_inventory_amount(value, label)
    if amount <= 0:
        raise LabError(f"{label} يجب أن يكون أكبر من صفر.")
    return amount


def parse_nonnegative_amount(value, label):
    amount = parse_inventory_amount(value, label)
    if amount < 0:
        raise LabError(f"{label} لا يمكن أن يكون سالباً.")
    return amount


def parse_inventory_amount(value, label):
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise LabError(f"{label} يجب أن يكون رقماً.") from exc
    if not math.isfinite(amount):
        raise LabError(f"{label} يجب أن يكون رقماً صالحاً.")
    return round(amount, 3)


def approve_order(store, order_id, actor):
    def approve(data):
        order = find(data["orders"], order_id, "الطلب")
        matching = [r for r in data["results"] if r["order_id"] == order_id]
        if len(matching) != len(order["test_items"]):
            raise LabError("لا يمكن اعتماد الطلب قبل إدخال جميع النتائج.")
        for item in order["test_items"]:
            if item.get("result_template") and not item.get("reference_verified"):
                raise LabError("يجب اعتماد الوحدات والنطاقات المرجعية محلياً قبل اعتماد النتائج.")
            result = next(
                (record for record in matching if record["test_id"] == item["test_id"]), None
            )
            if result is None:
                raise LabError("لا يمكن اعتماد الطلب قبل إدخال جميع النتائج.")
            if item.get("result_template"):
                result_values = result.get("values", {})
                if any(not result_values.get(component["id"]) for component in item["result_template"]):
                    raise LabError("لا يمكن اعتماد الطلب قبل إدخال جميع النتائج.")
            elif not result.get("value"):
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
