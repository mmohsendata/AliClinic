from datetime import datetime, timedelta

import pytest
from filelock import FileLock
from werkzeug.datastructures import MultiDict
from werkzeug.security import check_password_hash, generate_password_hash

from app import bootstrap_admin, create_app
from app.services.attendance import (
    calculate_monthly_payroll,
    configure_attendance_settings,
    configure_employee,
    disable_employee_attendance,
    end_attendance,
    record_attendance_activity,
    review_excuse,
    start_attendance,
    submit_excuse,
)
from app.services.lab import (
    LabError,
    approve_order,
    create_inventory_item,
    create_order,
    create_patient,
    create_test,
    enter_result,
    import_starter_templates,
    record_inventory_purchase,
    set_test_inventory_recipe,
    update_sample,
    update_test_template,
)


@pytest.fixture
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-key-not-for-production",
            "DATA_FILE": str(tmp_path / "store.json"),
            "WTF_CSRF_ENABLED": False,
        }
    )
    yield application


@pytest.fixture
def actors(app):
    store = app.extensions["json_store"]
    admin = {"id": "admin-1", "username": "admin", "role": "admin"}
    reviewer = {"id": "reviewer-1", "username": "reviewer", "role": "reviewer"}
    store.update(
        lambda data: data["users"].extend([
            {
                **admin,
                "password_hash": generate_password_hash("test-password"),
                "active": True,
                "must_change_password": False,
            },
            {
                **reviewer,
                "password_hash": generate_password_hash("review-password"),
                "active": True,
                "must_change_password": False,
            },
        ])
    )
    return admin, reviewer


def test_patient_order_sample_result_approval_and_report(app, actors):
    store = app.extensions["json_store"]
    admin, reviewer = actors
    patient = create_patient(store, {"name": "مريم علي", "phone": "01000000000"}, admin)
    test = create_test(store, {"name": "تحليل تجريبي", "price": "120"}, admin)
    order = create_order(store, patient["id"], [test["id"], test["id"]], admin)
    assert len(order["test_items"]) == 1
    assert order["total"] == 120
    sample = store.read()["samples"][0]

    update_sample(store, sample["id"], "مستلمة", admin)
    enter_result(store, order["id"], test["id"], "قيمة مدخلة", admin)
    approve_order(store, order["id"], reviewer)

    saved = store.read()
    assert saved["orders"][0]["status"] == "معتمد"
    assert saved["results"][0]["status"] == "معتمد"
    assert saved["results"][0]["approved_by"] == reviewer["id"]
    assert saved["samples"][0]["status"] == "مكتملة"
    assert saved["audit_logs"]
    with pytest.raises(LabError, match="مقفلة"):
        enter_result(store, order["id"], test["id"], "تعديل غير مسموح", admin)
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_id"] = reviewer["id"]
        response = client.get(f"/orders/{order['id']}/report")
        assert response.status_code == 200
        assert "قيمة مدخلة" in response.get_data(as_text=True)


def test_cannot_approve_before_all_results_are_entered(app, actors):
    admin, reviewer = actors
    store = app.extensions["json_store"]
    patient = create_patient(store, {"name": "مريض", "phone": "01111111111"}, admin)
    test = create_test(store, {"name": "تحليل", "price": "50"}, admin)
    order = create_order(store, patient["id"], [test["id"]], admin)
    with pytest.raises(LabError, match="جميع النتائج"):
        approve_order(store, order["id"], reviewer)


def test_starter_lab_templates_are_imported_idempotently(app, actors):
    store = app.extensions["json_store"]
    admin, _ = actors

    added = import_starter_templates(store, admin)
    assert len(added) >= 15
    assert import_starter_templates(store, admin) == []
    saved = store.read()["tests"]
    cbc = next(test for test in saved if test["code"] == "CBC")
    assert cbc["result_template"][0]["name"] == "كريات الدم الحمراء (RBC)"
    assert cbc["reference_source_url"].startswith("https://")
    assert cbc["reference_verified"] is False
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = admin["id"]
    catalog_page = client.get("/catalog")
    orders_page = client.get("/orders")
    assert catalog_page.status_code == 200
    assert "إعداد قالب النتائج" in catalog_page.get_data(as_text=True)
    assert orders_page.status_code == 200
    assert "يلزم التحقق من القالب أولاً" in orders_page.get_data(as_text=True)


def test_structured_results_require_local_reference_verification_and_print(app, actors):
    store = app.extensions["json_store"]
    admin, reviewer = actors
    import_starter_templates(store, admin)
    cbc = next(test for test in store.read()["tests"] if test["code"] == "CBC")
    patient = create_patient(store, {"name": "مريض القالب", "phone": "01555555555"}, admin)

    template_form = [
        ("reference_source_url", cbc["reference_source_url"]),
        ("verify", "yes"),
    ]
    for component in cbc["result_template"]:
        template_form.extend(
            [
                ("component_name", component["name"]),
                ("component_unit", component["unit"] or "نص"),
                (
                    "component_range",
                    component["reference_range"] or "نطاق محلي معتمد",
                ),
            ]
        )
    saved_template = update_test_template(
        store, cbc["id"], MultiDict(template_form), admin
    )
    assert saved_template["reference_verified"] is True

    order = create_order(store, patient["id"], [cbc["id"]], admin)
    sample = next(sample for sample in store.read()["samples"] if sample["order_id"] == order["id"])
    update_sample(store, sample["id"], "مستلمة", admin)
    values = {
        component["id"]: str(index + 1)
        for index, component in enumerate(order["test_items"][0]["result_template"])
    }
    with pytest.raises(LabError, match="لكل حقول"):
        enter_result(store, order["id"], cbc["id"], "", admin, values={})

    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = admin["id"]
    response = client.post(
        f"/orders/{order['id']}/results",
        data={"test_id": cbc["id"], **{f"value_{key}": value for key, value in values.items()}},
    )
    assert response.status_code == 302
    result = store.read()["results"][0]
    assert result["values"] == values
    assert result["versions"][0]["values"] == values
    approve_order(store, order["id"], reviewer)

    with client.session_transaction() as session:
        session["user_id"] = reviewer["id"]
    response = client.get(f"/orders/{order['id']}/report")
    assert response.status_code == 200
    report = response.get_data(as_text=True)
    assert "طباعة التقرير" in report
    assert "الهيموغلوبين (Hb)" in report
    assert values["field-3"] in report
    assert cbc["reference_source_url"] in report


def test_unverified_reference_template_cannot_be_ordered(app, actors):
    store = app.extensions["json_store"]
    admin, _ = actors
    import_starter_templates(store, admin)
    glucose = next(test for test in store.read()["tests"] if test["code"] == "GLU-F")
    patient = create_patient(store, {"name": "مريض", "phone": "01666666666"}, admin)
    with pytest.raises(LabError, match="قبل إنشاء الطلب"):
        create_order(store, patient["id"], [glucose["id"]], admin)
    assert not store.read()["orders"]


def test_rejected_sample_requires_documented_recollection(app, actors):
    admin, _ = actors
    store = app.extensions["json_store"]
    patient = create_patient(store, {"name": "مريض", "phone": "01222222222"}, admin)
    test = create_test(store, {"name": "تحليل", "price": "50"}, admin)
    order = create_order(store, patient["id"], [test["id"]], admin)
    sample = store.read()["samples"][0]

    with pytest.raises(LabError, match="سبب رفض"):
        update_sample(store, sample["id"], "مرفوضة", admin)
    update_sample(store, sample["id"], "مرفوضة", admin, "العينة غير كافية")
    with pytest.raises(LabError, match="إعادة السحب"):
        update_sample(store, sample["id"], "مستلمة", admin)
    update_sample(store, sample["id"], "تم السحب", admin, "إعادة سحب موثقة")
    assert store.read()["samples"][0]["resolution_reason"] == "إعادة سحب موثقة"


def test_result_amendment_requires_reason_and_keeps_version_history(app, actors):
    admin, _ = actors
    store = app.extensions["json_store"]
    patient = create_patient(store, {"name": "مريض", "phone": "01333333333"}, admin)
    test = create_test(store, {"name": "تحليل", "price": "50"}, admin)
    order = create_order(store, patient["id"], [test["id"]], admin)
    sample = store.read()["samples"][0]
    update_sample(store, sample["id"], "مستلمة", admin)
    enter_result(store, order["id"], test["id"], "12", admin)

    with pytest.raises(LabError, match="سبباً موثقاً"):
        enter_result(store, order["id"], test["id"], "13", admin)
    enter_result(store, order["id"], test["id"], "13", admin, "تصحيح إدخال")
    saved_result = store.read()["results"][0]
    assert [version["value"] for version in saved_result["versions"]] == ["12", "13"]
    assert saved_result["versions"][1]["reason"] == "تصحيح إدخال"


def test_catalog_rejects_non_finite_prices(app, actors):
    admin, _ = actors
    with pytest.raises(LabError, match="رقماً صالحاً"):
        create_test(app.extensions["json_store"], {"name": "تحليل", "price": "NaN"}, admin)


def test_inventory_purchase_recipe_and_result_consumption_are_audited_once(app, actors):
    store = app.extensions["json_store"]
    admin, _ = actors
    item = create_inventory_item(
        store, {"name": "كاشف تجريبي", "unit": "مل", "reorder_level": "1"}, admin
    )
    record_inventory_purchase(
        store,
        {"item_id": item["id"], "quantity": "2", "unit_cost": "15", "supplier": "مورد"},
        admin,
    )
    test = create_test(store, {"name": "تحليل مخزون", "price": "50"}, admin)
    set_test_inventory_recipe(store, test["id"], item["id"], "3", admin)
    patient = create_patient(store, {"name": "مريض المخزون", "phone": "01444444444"}, admin)
    order = create_order(store, patient["id"], [test["id"]], admin)
    assert order["test_items"][0]["inventory_recipe"] == [
        {"item_id": item["id"], "item_name": item["name"], "unit": item["unit"], "quantity": 3.0}
    ]

    set_test_inventory_recipe(store, test["id"], item["id"], "1", admin)
    sample = next(sample for sample in store.read()["samples"] if sample["order_id"] == order["id"])
    update_sample(store, sample["id"], "مستلمة", admin)
    enter_result(store, order["id"], test["id"], "نتيجة", admin)
    enter_result(store, order["id"], test["id"], "نتيجة مصححة", admin, "تصحيح")

    saved = store.read()
    transactions = [
        transaction for transaction in saved["inventory_transactions"]
        if transaction.get("order_id") == order["id"]
    ]
    assert len(transactions) == 1
    assert transactions[0]["transaction_type"] == "consumption"
    assert transactions[0]["quantity"] == 3.0
    assert transactions[0]["item_name"] == item["name"]
    assert len([log for log in saved["audit_logs"] if log["action"] == "inventory.consumed"]) == 1
    assert sum(
        transaction["quantity"] if transaction["transaction_type"] == "purchase"
        else -transaction["quantity"]
        for transaction in saved["inventory_transactions"]
        if transaction["item_id"] == item["id"]
    ) == -1.0


def test_inventory_rejects_nonpositive_quantities_and_duplicate_items(app, actors):
    store = app.extensions["json_store"]
    admin, _ = actors
    create_inventory_item(store, {"name": "أنبوب", "unit": "قطعة"}, admin)
    with pytest.raises(LabError, match="مستخدم بالفعل"):
        create_inventory_item(store, {"name": "أنبوب", "unit": "قطعة"}, admin)
    with pytest.raises(LabError, match="أكبر من صفر"):
        record_inventory_purchase(
            store, {"item_id": "missing", "quantity": "0", "unit_cost": "0"}, admin
        )


def test_inventory_page_records_purchases_and_is_read_only_for_technicians(app, actors):
    store = app.extensions["json_store"]
    admin, _ = actors
    store.update(
        lambda data: data["users"].append(
            {
                "id": "inventory-tech",
                "username": "inventory-tech",
                "role": "technician",
                "password_hash": generate_password_hash("tech-password"),
                "active": True,
                "must_change_password": False,
            }
        )
    )
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = admin["id"]

    created = client.post(
        "/inventory",
        data={"action": "add_item", "name": "محلول", "unit": "مل", "reorder_level": "1"},
    )
    assert created.status_code == 302
    item = store.read()["inventory_items"][0]
    purchased = client.post(
        "/inventory",
        data={
            "action": "purchase",
            "item_id": item["id"],
            "quantity": "0.5",
            "unit_cost": "8",
            "supplier": "مورد",
            "reference": "INV-1",
        },
    )
    assert purchased.status_code == 302
    test = create_test(store, {"name": "تحليل المحلول", "price": "25"}, admin)
    set_test_inventory_recipe(store, test["id"], item["id"], "0.1", admin)
    page = client.get("/inventory")
    assert page.status_code == 200
    rendered = page.get_data(as_text=True)
    assert "محلول" in rendered
    assert "0.5" in rendered
    assert "تحليل المحلول" in rendered

    with client.session_transaction() as session:
        session["user_id"] = "inventory-tech"
    assert client.get("/inventory").status_code == 200
    rejected = client.post(
        "/inventory",
        data={
            "action": "purchase",
            "item_id": item["id"],
            "quantity": "4",
            "unit_cost": "8",
        },
    )
    assert rejected.status_code == 302
    assert len(store.read()["inventory_transactions"]) == 1


def test_attendance_tracks_active_time_lateness_and_approved_excuses(app, actors):
    store = app.extensions["json_store"]
    admin, reviewer = actors
    monday = datetime(2026, 1, 5, 8, 16).astimezone()
    configure_attendance_settings(
        store, {"idle_timeout_minutes": "10", "late_grace_minutes": "5"}, admin
    )
    configure_employee(
        store,
        MultiDict(
            [
                ("user_id", reviewer["id"]),
                ("hourly_rate", "20"),
                ("hours_per_shift", "8"),
                ("shift_start", "08:00"),
                ("shift_end", "16:00"),
                ("start_date", "2026-01-05"),
                ("weekdays", "0"),
                ("extra_info", "فني"),
            ]
        ),
        admin,
    )

    start_attendance(store, reviewer["id"], monday)
    for minute in range(24, 120, 8):
        record_attendance_activity(
            store, reviewer["id"], monday.replace(minute=0) + timedelta(minutes=minute)
        )
    end_attendance(store, reviewer["id"], monday.replace(hour=10, minute=0))

    payroll = calculate_monthly_payroll(
        store, 2026, 1, at=monday.replace(hour=10, minute=1)
    )[0]
    assert payroll["scheduled_shifts"] == 4
    assert payroll["scheduled_hours"] == 32
    assert payroll["attended_shifts"] == 1
    assert payroll["absent_shifts"] == 3
    assert payroll["worked_hours"] == pytest.approx(104 / 60)
    assert payroll["gross_pay"] == pytest.approx(34.67)
    assert payroll["late_penalty"] == pytest.approx(3.67)
    assert payroll["net_pay"] == pytest.approx(31.0)
    assert payroll["profile"]["extra_info"] == "فني"

    late_excuse = submit_excuse(
        store,
        reviewer["id"],
        {"shift_date": "2026-01-05", "reason": "تأخير مواصلات"},
    )
    review_excuse(store, late_excuse["id"], "approved", admin, "تم الاعتماد")
    absence_excuse = submit_excuse(
        store,
        reviewer["id"],
        {"shift_date": "2026-01-12", "reason": "إجازة معتمدة"},
    )
    review_excuse(store, absence_excuse["id"], "approved", admin)
    excused_payroll = calculate_monthly_payroll(
        store, 2026, 1, at=monday.replace(hour=10, minute=1)
    )[0]
    assert excused_payroll["late_penalty"] == 0
    assert excused_payroll["attended_shifts"] == 1
    assert excused_payroll["excused_shifts"] == 1
    assert excused_payroll["absent_shifts"] == 2
    assert excused_payroll["worked_hours"] == payroll["worked_hours"]
    disable_employee_attendance(store, reviewer["id"], admin)
    historical = calculate_monthly_payroll(
        store, 2026, 1, at=monday.replace(hour=10, minute=1)
    )[0]
    assert historical["net_pay"] == excused_payroll["net_pay"]


def test_attendance_closes_idle_sessions_at_timeout_and_resumes_on_activity(app, actors):
    store = app.extensions["json_store"]
    admin, reviewer = actors
    configure_employee(
        store,
        MultiDict(
            [
                ("user_id", reviewer["id"]),
                ("hourly_rate", "10"),
                ("hours_per_shift", "8"),
                ("shift_start", "08:00"),
                ("shift_end", "16:00"),
                ("start_date", "2026-01-05"),
                ("weekdays", "0"),
            ]
        ),
        admin,
    )
    with pytest.raises(LabError, match="يتعارض تاريخ بداية الجدول"):
        configure_employee(
            store,
            MultiDict(
                [
                    ("user_id", reviewer["id"]),
                    ("hourly_rate", "11"),
                    ("hours_per_shift", "8"),
                    ("shift_start", "08:00"),
                    ("shift_end", "16:00"),
                    ("start_date", "2026-01-05"),
                    ("weekdays", "0"),
                ]
            ),
            admin,
        )
    first_activity = datetime(2026, 1, 5, 8, 0).astimezone()
    start_attendance(store, reviewer["id"], first_activity)
    resumed_at = first_activity + timedelta(minutes=12)
    record_attendance_activity(store, reviewer["id"], resumed_at)

    sessions = store.read()["attendance_sessions"]
    assert len(sessions) == 2
    assert sessions[0]["close_reason"] == "idle"
    assert datetime.fromisoformat(sessions[0]["ended_at"]) == first_activity + timedelta(minutes=10)
    assert sessions[1]["active"] is True
    assert sessions[1]["started_at"] == resumed_at.isoformat(timespec="seconds")


def test_attendance_routes_enable_selected_employees_and_restrict_settings(app, actors):
    admin, reviewer = actors
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = admin["id"]
    response = client.post(
        "/attendance",
        data={
            "action": "configure_employee",
            "user_id": reviewer["id"],
            "hourly_rate": "15",
            "hours_per_shift": "8",
            "shift_start": "08:00",
            "shift_end": "16:00",
            "start_date": datetime.now().astimezone().date().isoformat(),
            "weekdays": [str(datetime.now().astimezone().weekday())],
            "extra_info": "موظف مختبر",
        },
    )
    assert response.status_code == 302
    assert client.get("/attendance").status_code == 200

    employee_client = app.test_client()
    logged_in = employee_client.post(
        "/login", data={"username": reviewer["username"], "password": "review-password"}
    )
    assert logged_in.status_code == 302
    assert any(
        session_record["user_id"] == reviewer["id"]
        and session_record["active"]
        for session_record in app.extensions["json_store"].read()["attendance_sessions"]
    )
    page = employee_client.get("/attendance")
    assert page.status_code == 200
    assert "موظف مختبر" not in page.get_data(as_text=True)
    assert "سجل الجلسات" in page.get_data(as_text=True)
    employee_client.post(
        "/attendance",
        data={"action": "settings", "idle_timeout_minutes": "1", "late_grace_minutes": "0"},
        follow_redirects=True,
    )
    assert app.extensions["json_store"].read()["attendance_settings"]["idle_timeout_minutes"] == 10


def test_demo_seed_fills_pages_with_marked_synthetic_data(app, actors):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["main", "seed-demo"])
    assert result.exit_code == 0, result.output

    client = app.test_client()
    admin, _ = actors
    with client.session_transaction() as session:
        session["user_id"] = admin["id"]
    assert "وضع العرض" in client.get("/dashboard").get_data(as_text=True)
    patients_page = client.get("/patients").get_data(as_text=True)
    catalog_page = client.get("/catalog").get_data(as_text=True)
    orders_page = client.get("/orders").get_data(as_text=True)
    assert "مريض تجريبي" in patients_page
    assert "DEMO-CBC" in catalog_page
    assert "نتيجة تجريبية" in orders_page
    data = app.extensions["json_store"].read()
    assert data["demo_seeded"] is True
    assert len(data["patients"]) == 3
    assert len(data["tests"]) == 4
    assert len(data["orders"]) == 2
    assert all(record["is_demo"] for key in ("patients", "tests", "orders") for record in data[key])
    assert runner.invoke(args=["main", "seed-demo"]).exit_code != 0


def test_first_login_shows_password_form_and_unlocks_pages(app):
    store = app.extensions["json_store"]
    store.update(
        lambda data: data["users"].append(
            {
                "id": "first-login-admin",
                "username": "first-login",
                "password_hash": generate_password_hash("initial-password"),
                "role": "admin",
                "active": True,
                "must_change_password": True,
            }
        )
    )
    client = app.test_client()
    login_response = client.post(
        "/login",
        data={"username": "first-login", "password": "initial-password"},
        follow_redirects=False,
    )
    assert login_response.status_code == 302
    assert login_response.headers["Location"].endswith("/account/password")

    password_page = client.get("/account/password")
    assert password_page.status_code == 200
    assert "غيّر كلمة المرور" in password_page.get_data(as_text=True)
    assert b'name="confirm_password"' in password_page.data

    changed = client.post(
        "/account/password",
        data={"password": "a-new-password-123", "confirm_password": "a-new-password-123"},
        follow_redirects=False,
    )
    assert changed.status_code == 302
    assert changed.headers["Location"].endswith("/dashboard")
    assert client.get("/dashboard").status_code == 200


def test_unauthenticated_password_page_explains_login_requirement(app):
    response = app.test_client().get("/account/password", follow_redirects=True)
    assert response.status_code == 200
    assert "سجّل الدخول أولاً" in response.get_data(as_text=True)
    assert 'name="username"' in response.get_data(as_text=True)


def test_health_endpoint_is_public_and_returns_ok(app):
    response = app.test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_bootstrap_admin_creates_hashed_temporary_admin_and_requires_password_change(app, monkeypatch):
    store = app.extensions["json_store"]
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "a-unique-temporary-password")
    bootstrap_admin(store)

    user = store.read()["users"][0]
    assert user["username"] == "admin"
    assert user["role"] == "admin"
    assert user["active"] is True
    assert user["must_change_password"] is True
    assert check_password_hash(user["password_hash"], "a-unique-temporary-password")


def test_app_startup_bootstraps_admin_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "a-unique-temporary-password")

    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-key-not-for-production",
            "DATA_FILE": str(tmp_path / "store.json"),
            "WTF_CSRF_ENABLED": False,
        }
    )

    users = application.extensions["json_store"].read()["users"]
    assert len(users) == 1
    assert users[0]["username"] == "admin"
    assert users[0]["must_change_password"] is True


def test_bootstrap_admin_never_overwrites_an_existing_active_admin(app, monkeypatch):
    store = app.extensions["json_store"]
    original = {
        "id": "existing-admin",
        "username": "existing",
        "password_hash": generate_password_hash("existing-password"),
        "role": "admin",
        "active": True,
        "must_change_password": False,
    }
    store.update(lambda data: data["users"].append(original.copy()))
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "a-unique-temporary-password")

    bootstrap_admin(store)

    assert store.read()["users"] == [original]


def test_bootstrap_admin_rejects_weak_password(app, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "admin")

    with pytest.raises(RuntimeError, match="at least 16"):
        bootstrap_admin(app.extensions["json_store"])


def test_bootstrap_admin_requires_both_environment_variables(app, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="Set both"):
        bootstrap_admin(app.extensions["json_store"])


def test_json_store_creates_a_fresh_lock_for_each_operation(app, monkeypatch):
    store = app.extensions["json_store"]
    locks = []
    original_lock = FileLock

    def recording_lock(*args, **kwargs):
        lock = original_lock(*args, **kwargs)
        locks.append(lock)
        return lock

    monkeypatch.setattr("app.repositories.json_store.FileLock", recording_lock)
    store.read()
    store.update(lambda data: data.update({"probe": True}))
    store.read()

    assert len(locks) == 3
    assert len({id(lock) for lock in locks}) == 3


def test_routes_require_auth_and_restrict_result_approval(app, actors):
    client = app.test_client()
    store = app.extensions["json_store"]
    assert client.get("/dashboard").status_code == 302
    client.post("/login", data={"username": "admin", "password": "test-password"})
    assert client.get("/dashboard").status_code == 200
    assert client.get("/patients").status_code == 200
    assert client.get("/catalog").status_code == 200
    assert client.get("/orders").status_code == 200
    assert client.get("/inventory").status_code == 200
    assert client.get("/users").status_code == 200
    created = client.post(
        "/users",
        data={
            "username": "new-tech",
            "role": "technician",
            "password": "initial-password",
            "confirm_password": "initial-password",
        },
        follow_redirects=True,
    )
    assert created.status_code == 200
    new_user = next(user for user in store.read()["users"] if user["username"] == "new-tech")
    assert new_user["must_change_password"] is True
    assert new_user["password_hash"] != "initial-password"
    admin, _ = actors
    patient = create_patient(store, {"name": "مريض", "phone": "01555555555"}, admin)
    test = create_test(store, {"name": "تحليل", "price": "25"}, admin)
    order = create_order(store, patient["id"], [test["id"]], admin)
    store.update(
        lambda data: data["users"].append(
            {
                "id": "receptionist-1",
                "username": "receptionist",
                "role": "receptionist",
                "password_hash": generate_password_hash("reception-password"),
                "active": True,
                "must_change_password": False,
            }
        )
    )
    with client.session_transaction() as session:
        session["user_id"] = "receptionist-1"
    assert client.get("/users").status_code == 302
    assert client.get("/inventory").status_code == 302
    response = client.post(f"/orders/{order['id']}/approve")
    assert response.status_code == 302
    assert store.read()["orders"][0]["status"] == "بانتظار سحب العينة"
