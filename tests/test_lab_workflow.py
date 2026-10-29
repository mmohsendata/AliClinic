import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.services.lab import (
    LabError,
    approve_order,
    create_order,
    create_patient,
    create_test,
    enter_result,
    update_sample,
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


def test_routes_require_auth_and_restrict_result_approval(app, actors):
    client = app.test_client()
    store = app.extensions["json_store"]
    assert client.get("/dashboard").status_code == 302
    client.post("/login", data={"username": "admin", "password": "test-password"})
    assert client.get("/dashboard").status_code == 200
    assert client.get("/patients").status_code == 200
    assert client.get("/catalog").status_code == 200
    assert client.get("/orders").status_code == 200
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
    response = client.post(f"/orders/{order['id']}/approve")
    assert response.status_code == 302
    assert store.read()["orders"][0]["status"] == "بانتظار سحب العينة"
