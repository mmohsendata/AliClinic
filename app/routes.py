from functools import wraps
from datetime import datetime
from uuid import uuid4

import click
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from app.services.attendance import (
    WEEKDAYS,
    calculate_monthly_payroll,
    close_expired_sessions,
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
    approve_predefined_template,
    create_inventory_item,
    create_order,
    create_patient,
    create_test,
    enter_result,
    get_predefined_templates,
    now_iso,
    record_inventory_purchase,
    remove_test_inventory_recipe,
    set_test_inventory_recipe,
    update_sample,
    update_test_template,
)

bp = Blueprint("main", __name__)

ROLE_LABELS = {
    "admin": "مدير النظام",
    "receptionist": "موظف استقبال",
    "technician": "فني مختبر",
    "reviewer": "مراجع نتائج",
}
ASSIGNABLE_ROLES = ("admin", "receptionist", "technician", "reviewer")


def store():
    return current_app.extensions["json_store"]


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return next((user for user in store().read()["users"] if user["id"] == user_id and user["active"]), None)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            session.clear()
            flash("سجّل الدخول أولاً للمتابعة.", "error")
            return redirect(url_for("main.login"))
        if user.get("must_change_password") and request.endpoint != "main.change_password":
            return redirect(url_for("main.change_password"))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = current_user()
            if user["role"] not in roles:
                flash("ليس لديك صلاحية لتنفيذ هذا الإجراء.", "error")
                return redirect(url_for("main.dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorate


@bp.app_context_processor
def inject_user():
    active_user = current_user()
    data = store().read()
    return {
        "user": active_user,
        "role_labels": ROLE_LABELS,
        "demo_mode": data.get("demo_seeded", False),
        "attendance_enabled": bool(
            active_user
            and (
                active_user["role"] == "admin"
                or any(
                    profile["user_id"] == active_user["id"] and profile.get("enabled")
                    for profile in data["attendance_profiles"]
                )
            )
        ),
    }


@bp.cli.command("create-admin")
@click.option("--username", prompt="اسم المستخدم")
def create_admin(username):
    """Create the first active administrator; set a new password at first login."""
    from getpass import getpass
    from uuid import uuid4

    password = getpass("كلمة المرور الأولية (12 حرفاً على الأقل): ")
    if len(password) < 12:
        raise click.ClickException("يجب أن تتكون كلمة المرور من 12 حرفاً على الأقل.")

    def create(data):
        if any(user["role"] == "admin" and user["active"] for user in data["users"]):
            raise click.ClickException("يوجد مدير نظام نشط بالفعل.")
        data["users"].append(
            {
                "id": str(uuid4()),
                "username": username.strip(),
                "password_hash": generate_password_hash(password),
                "role": "admin",
                "active": True,
                "must_change_password": True,
            }
        )
    store().update(create)
    click.echo("تم إنشاء المدير. سجّل الدخول وغيّر كلمة المرور فوراً.")


@bp.cli.command("seed-demo")
def seed_demo():
    """Add clearly marked fictional records to an otherwise empty data store."""
    data = store().read()
    collections = ("patients", "tests", "orders", "samples", "results")
    if data.get("demo_seeded") or any(data[key] for key in collections):
        raise click.ClickException(
            "بيانات التشغيل موجودة بالفعل؛ لم تتم إضافة بيانات تجريبية لتجنب خلطها."
        )
    actor = next(
        (user for user in data["users"] if user["active"] and user["role"] == "admin"),
        None,
    )
    if actor is None:
        raise click.ClickException("أنشئ حساب مدير نشطاً أولاً.")

    demo_patients = [
        create_patient(
            store(),
            {"name": "مريض تجريبي ١", "english_name": "Demo Patient One", "phone": "01000000001", "gender": "ذكر"},
            actor,
        ),
        create_patient(
            store(),
            {"name": "مريضة تجريبية ٢", "english_name": "Demo Patient Two", "phone": "01000000002", "gender": "أنثى"},
            actor,
        ),
        create_patient(
            store(),
            {"name": "مريض تجريبي ٣", "english_name": "Demo Patient Three", "phone": "01000000003", "gender": "غير محدد"},
            actor,
        ),
    ]
    demo_tests = [
        create_test(store(), {"name": "CBC — صورة دم كاملة (تجريبي)", "code": "DEMO-CBC", "price": "250", "sample_type": "دم كامل"}, actor),
        create_test(store(), {"name": "سكر صائم (تجريبي)", "code": "DEMO-GLU", "price": "100", "sample_type": "مصل الدم"}, actor),
        create_test(store(), {"name": "HbA1c (تجريبي)", "code": "DEMO-A1C", "price": "300", "sample_type": "دم كامل"}, actor),
        create_test(store(), {"name": "تحليل بول كامل (تجريبي)", "code": "DEMO-UA", "price": "150", "sample_type": "بول"}, actor),
    ]
    approved_order = create_order(
        store(), demo_patients[0]["id"], [demo_tests[0]["id"], demo_tests[1]["id"]], actor
    )
    for sample in store().read()["samples"]:
        if sample["order_id"] == approved_order["id"]:
            update_sample(store(), sample["id"], "مستلمة", actor)
    for item in approved_order["test_items"]:
        enter_result(
            store(), approved_order["id"], item["test_id"],
            "نتيجة تجريبية — غير صالحة للتشخيص", actor,
        )
    approve_order(store(), approved_order["id"], actor)

    pending_order = create_order(
        store(), demo_patients[1]["id"], [demo_tests[2]["id"], demo_tests[3]["id"]], actor
    )

    def mark_demo(records):
        for collection in collections:
            for record in records[collection]:
                record["is_demo"] = True
        records["demo_seeded"] = True
        records["demo_seeded_at"] = now_iso()

    store().update(mark_demo)
    click.echo(
        f"Demo records added: {len(demo_patients)} patients, {len(demo_tests)} tests, "
        f"2 orders ({approved_order['number']}, {pending_order['number']})."
    )


@bp.get("/login")
@bp.post("/login")
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        data = store().read()
        user = next((item for item in data["users"] if item["username"] == username and item["active"]), None)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            start_attendance(store(), user["id"])
            return redirect(url_for("main.change_password" if user.get("must_change_password") else "main.dashboard"))
        flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
    return render_template("login.html")


@bp.post("/logout")
@login_required
def logout():
    end_attendance(store(), current_user()["id"])
    session.clear()
    return redirect(url_for("main.login"))


@bp.get("/account/password")
@bp.post("/account/password")
@login_required
def change_password():
    user = current_user()
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 12:
            flash("يجب أن تتكون كلمة المرور من 12 حرفاً على الأقل.", "error")
        elif password != confirm:
            flash("كلمتا المرور غير متطابقتين.", "error")
        else:
            def update(data):
                target = next(item for item in data["users"] if item["id"] == user["id"])
                target["password_hash"] = generate_password_hash(password)
                target["must_change_password"] = False
            store().update(update)
            flash("تم تحديث كلمة المرور.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("change_password.html")


@bp.get("/dashboard")
@login_required
def dashboard():
    data = store().read()
    return render_template(
        "dashboard.html",
        patients=len(data["patients"]),
        tests=len(data["tests"]),
        orders=len(data["orders"]),
        pending=sum(1 for order in data["orders"] if order["status"] == "بانتظار المراجعة"),
        revenue=sum(order["paid"] for order in data["orders"]),
        recent_orders=sorted(data["orders"], key=lambda item: item["created_at"], reverse=True)[:6],
    )


@bp.get("/users")
@bp.post("/users")
@roles_required("admin")
def users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        role = request.form.get("role", "")
        if not username or len(username) > 80:
            flash("أدخل اسم مستخدم صحيحاً لا يتجاوز 80 حرفاً.", "error")
        elif role not in ASSIGNABLE_ROLES:
            flash("الدور المحدد غير صالح.", "error")
        elif len(password) < 12:
            flash("يجب أن تتكون كلمة المرور من 12 حرفاً على الأقل.", "error")
        elif password != confirm:
            flash("كلمتا المرور غير متطابقتين.", "error")
        else:
            def create(data):
                if any(item["username"].casefold() == username.casefold() for item in data["users"]):
                    raise LabError("اسم المستخدم مستخدم بالفعل.")
                data["users"].append(
                    {
                        "id": str(uuid4()),
                        "username": username,
                        "password_hash": generate_password_hash(password),
                        "role": role,
                        "active": True,
                        "must_change_password": True,
                    }
                )
            try:
                store().update(create)
                flash("تم إنشاء المستخدم. سيُطلب منه تغيير كلمة المرور عند أول دخول.", "success")
                return redirect(url_for("main.users"))
            except LabError as exc:
                flash(str(exc), "error")
    data = store().read()
    return render_template("users.html", users=data["users"], assignable_roles=ASSIGNABLE_ROLES)


@bp.get("/patients")
@bp.post("/patients")
@roles_required("admin", "receptionist")
def patients():
    if request.method == "POST":
        try:
            create_patient(store(), request.form, current_user())
            flash("تم تسجيل المريض بنجاح.", "success")
            return redirect(url_for("main.patients"))
        except LabError as exc:
            flash(str(exc), "error")
    data = store().read()
    query = request.args.get("q", "").strip().casefold()
    records = [
        item for item in data["patients"]
        if not query or query in item["name"].casefold() or query in item["phone"].casefold()
        or query in item["medical_record_number"].casefold()
    ]
    return render_template("patients.html", patients=records)


@bp.get("/catalog")
@roles_required("admin", "receptionist", "reviewer")
def catalog():
    data = store().read()
    existing_codes = {test["code"] for test in data["tests"]}
    predefined_templates = [
        {**template, "already_added": template["code"] in existing_codes}
        for template in get_predefined_templates()
    ]
    return render_template(
        "catalog.html", tests=data["tests"], predefined_templates=predefined_templates
    )


@bp.post("/catalog")
@roles_required("admin", "receptionist")
def catalog_create():
    try:
        create_test(store(), request.form, current_user())
        flash("تمت إضافة التحليل.", "success")
    except LabError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.catalog"))


@bp.post("/catalog/<test_id>/template")
@roles_required("admin")
def catalog_template(test_id):
    try:
        test = update_test_template(store(), test_id, request.form, current_user())
        if test["template_approved"]:
            flash("تم حفظ قالب النتائج واعتماده للطلبات.", "success")
        else:
            flash("تم حفظ القالب؛ ما زال بانتظار المراجعة والاعتماد.", "success")
    except LabError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.catalog"))


@bp.post("/catalog/predefined/<code>/approve")
@roles_required("admin", "reviewer")
def approve_catalog_template(code):
    try:
        test = approve_predefined_template(store(), code, request.form, current_user())
        flash(f"تمت مراجعة التحليل {test['name']} واعتماده في الكتالوج.", "success")
    except LabError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.catalog"))


@bp.get("/inventory")
@bp.post("/inventory")
@roles_required("admin", "technician")
def inventory():
    if request.method == "POST":
        if current_user()["role"] != "admin":
            flash("تسجيل المشتريات وإعداد وصفات الاستخدام متاحان لمدير النظام فقط.", "error")
            return redirect(url_for("main.inventory"))
        action = request.form.get("action", "")
        try:
            if action == "add_item":
                create_inventory_item(store(), request.form, current_user())
                flash("تمت إضافة عنصر المخزون.", "success")
            elif action == "purchase":
                record_inventory_purchase(store(), request.form, current_user())
                flash("تم تسجيل عملية الشراء وإضافة الكمية إلى المخزون.", "success")
            elif action == "set_recipe":
                set_test_inventory_recipe(
                    store(), request.form.get("test_id", ""), request.form.get("item_id", ""),
                    request.form.get("quantity", ""), current_user(),
                )
                flash("تم حفظ كمية الاستخدام للتحليل.", "success")
            elif action == "remove_recipe":
                remove_test_inventory_recipe(
                    store(), request.form.get("test_id", ""), request.form.get("item_id", ""),
                    current_user(),
                )
                flash("تم حذف عنصر الاستخدام من وصفة التحليل.", "success")
            else:
                raise LabError("إجراء المخزون غير صالح.")
            return redirect(url_for("main.inventory"))
        except LabError as exc:
            flash(str(exc), "error")

    data = store().read()
    balances = {item["id"]: 0.0 for item in data["inventory_items"]}
    for transaction in data["inventory_transactions"]:
        if transaction["item_id"] in balances:
            delta = transaction["quantity"]
            balances[transaction["item_id"]] += (
                delta if transaction["transaction_type"] == "purchase" else -delta
            )
    items = [
        {
            **item,
            "balance": round(balances[item["id"]], 3),
            "low_stock": balances[item["id"]] <= item["reorder_level"],
        }
        for item in data["inventory_items"]
    ]
    tests = [
        {
            **test,
            "inventory_recipe": [
                {
                    **recipe,
                    "item_name": next(
                        (
                            item["name"]
                            for item in data["inventory_items"]
                            if item["id"] == recipe["item_id"]
                        ),
                        "عنصر غير متاح",
                    ),
                    "unit": next(
                        (
                            item["unit"]
                            for item in data["inventory_items"]
                            if item["id"] == recipe["item_id"]
                        ),
                        "",
                    ),
                }
                for recipe in test.get("inventory_recipe", [])
            ],
        }
        for test in data["tests"]
    ]
    return render_template(
        "inventory.html",
        items=items,
        tests=tests,
        transactions=sorted(
            data["inventory_transactions"], key=lambda item: item["at"], reverse=True
        )[:30],
    )


@bp.get("/attendance")
@bp.post("/attendance")
@login_required
def attendance():
    actor = current_user()
    data = store().read()
    profile = next(
        (
            item for item in data["attendance_profiles"]
            if item["user_id"] == actor["id"] and item.get("enabled")
        ),
        None,
    )
    if actor["role"] != "admin" and profile is None:
        flash("الحضور غير مفعل لهذا الحساب.", "error")
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if actor["role"] == "admin":
                if action == "settings":
                    configure_attendance_settings(store(), request.form, actor)
                    flash("تم تحديث إعدادات الحضور.", "success")
                elif action == "configure_employee":
                    configure_employee(store(), request.form, actor)
                    flash("تم حفظ ملف الموظف وجدول الورديات.", "success")
                elif action == "disable_employee":
                    disable_employee_attendance(
                        store(), request.form.get("user_id", ""), actor
                    )
                    flash("تم إيقاف تتبع الحضور لهذا المستخدم.", "success")
                elif action in {"approve_excuse", "reject_excuse"}:
                    review_excuse(
                        store(),
                        request.form.get("excuse_id", ""),
                        "approved" if action == "approve_excuse" else "rejected",
                        actor,
                        request.form.get("review_note", ""),
                    )
                    flash("تمت مراجعة العذر.", "success")
                else:
                    raise LabError("إجراء الحضور غير صالح.")
            elif action == "submit_excuse":
                submit_excuse(store(), actor["id"], request.form)
                flash("تم إرسال العذر إلى المدير للمراجعة.", "success")
            else:
                raise LabError("إجراء الحضور غير صالح.")
            return redirect(url_for("main.attendance"))
        except LabError as exc:
            flash(str(exc), "error")

    data = store().read()
    current_time = datetime.now().astimezone()
    try:
        year = int(request.args.get("year", current_time.year))
        month = int(request.args.get("month", current_time.month))
        payroll = calculate_monthly_payroll(store(), year, month)
    except (ValueError, LabError):
        flash("الفترة المحددة غير صالحة.", "error")
        year, month = current_time.year, current_time.month
        payroll = calculate_monthly_payroll(store(), year, month)
    data = store().read()
    users_by_id = {item["id"]: item for item in data["users"]}
    profiles = [
        {
            **item,
            "username": users_by_id.get(item["user_id"], {}).get("username", "—"),
            "role": users_by_id.get(item["user_id"], {}).get("role", "—"),
            "active": users_by_id.get(item["user_id"], {}).get("active", False),
            "schedule": next(
                (
                    schedule for schedule in reversed(data["attendance_schedules"])
                    if schedule["user_id"] == item["user_id"]
                ),
                None,
            ),
        }
        for item in data["attendance_profiles"]
    ]
    pending_excuses = [
        {
            **excuse,
            "username": users_by_id.get(excuse["user_id"], {}).get("username", "—"),
        }
        for excuse in data["attendance_excuses"]
        if excuse["status"] == "pending"
    ]
    monthly_sessions = [
        {
            **record,
            "username": users_by_id.get(record["user_id"], {}).get("username", "—"),
        }
        for record in data["attendance_sessions"]
        if datetime.fromisoformat(record["started_at"]).astimezone().year == year
        and datetime.fromisoformat(record["started_at"]).astimezone().month == month
    ]
    own_sessions = sorted(
        [
            item for item in data["attendance_sessions"]
            if item["user_id"] == actor["id"]
        ],
        key=lambda item: item["started_at"],
        reverse=True,
    )[:10]
    own_excuses = sorted(
        [
            item for item in data["attendance_excuses"]
            if item["user_id"] == actor["id"]
        ],
        key=lambda item: item["submitted_at"],
        reverse=True,
    )[:10]
    selected_payroll = next(
        (item for item in payroll if item["user"]["id"] == actor["id"]), None
    )
    return render_template(
        "attendance.html",
        settings=data["attendance_settings"],
        profiles=profiles,
        employees=[
            item for item in data["users"] if item["active"] and item["role"] != "admin"
        ],
        pending_excuses=pending_excuses,
        monthly_sessions=sorted(
            monthly_sessions, key=lambda item: item["started_at"], reverse=True
        ),
        own_sessions=own_sessions,
        own_excuses=own_excuses,
        payroll=payroll,
        selected_payroll=selected_payroll,
        year=year,
        month=month,
        weekdays=WEEKDAYS,
    )


@bp.post("/attendance/activity")
@login_required
def attendance_activity():
    record_attendance_activity(store(), current_user()["id"])
    return {"status": "ok"}, 200


@bp.get("/orders")
@bp.post("/orders")
@roles_required("admin", "receptionist")
def orders():
    if request.method == "POST":
        try:
            test_ids = request.form.getlist("test_ids")
            order = create_order(store(), request.form.get("patient_id", ""), test_ids, current_user())
            flash(f"تم إنشاء الطلب {order['number']} بقيمة {order['total']:.2f} ج.م.", "success")
            return redirect(url_for("main.orders"))
        except LabError as exc:
            flash(str(exc), "error")
    data = store().read()
    return render_template("orders.html", orders=sorted(data["orders"], key=lambda item: item["created_at"], reverse=True),
                           patients=data["patients"], tests=[item for item in data["tests"] if item["active"]],
                           samples=data["samples"], results=data["results"])


@bp.post("/samples/<sample_id>/status")
@roles_required("admin", "technician")
def sample_status(sample_id):
    try:
        update_sample(
            store(), sample_id, request.form.get("status", ""), current_user(),
            request.form.get("reason", ""),
        )
        flash("تم تحديث حالة العينة.", "success")
    except LabError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.orders"))


@bp.post("/orders/<order_id>/results")
@roles_required("admin", "technician")
def result_entry(order_id):
    try:
        enter_result(
            store(), order_id, request.form.get("test_id", ""), request.form.get("value", ""),
            current_user(), request.form.get("reason", ""),
            {
                key.removeprefix("value_"): value
                for key, value in request.form.items()
                if key.startswith("value_")
            },
        )
        flash("تم حفظ النتيجة كمسودة.", "success")
    except LabError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.orders"))


@bp.post("/orders/<order_id>/approve")
@roles_required("admin", "reviewer")
def result_approval(order_id):
    try:
        approve_order(store(), order_id, current_user())
        flash("تم اعتماد نتائج الطلب وقفلها.", "success")
    except LabError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.orders"))


@bp.get("/orders/<order_id>/report")
@roles_required("admin", "reviewer", "technician", "receptionist")
def report(order_id):
    data = store().read()
    order = next((item for item in data["orders"] if item["id"] == order_id), None)
    if order is None:
        flash("الطلب غير موجود.", "error")
        return redirect(url_for("main.orders"))
    if order["status"] not in {"معتمد", "مكتمل"}:
        flash("لا يمكن عرض تقرير قبل اعتماد النتائج.", "error")
        return redirect(url_for("main.orders"))
    results = [item for item in data["results"] if item["order_id"] == order_id]
    patient = next(
        (item for item in data["patients"] if item["id"] == order["patient_id"]), None
    )
    report_rows = []
    report_sources = []
    for item in order["test_items"]:
        result = next((entry for entry in results if entry["test_id"] == item["test_id"]), None)
        template = item.get("result_template", [])
        if item.get("reference_source_url"):
            report_sources.append(
                {"name": item["name"], "url": item["reference_source_url"]}
            )
        if template:
            for component in template:
                report_rows.append(
                    {
                        "test_name": item["name"],
                        "component_name": component["name"],
                        "value": (result or {}).get("values", {}).get(component["id"], "—"),
                        "unit": component.get("unit", ""),
                        "reference_range": component.get("reference_range", ""),
                        "reference_verified": item.get("reference_verified", False),
                    }
                )
        else:
            report_rows.append(
                {
                    "test_name": item["name"],
                    "component_name": "",
                    "value": (result or {}).get("value", "—"),
                    "unit": "",
                    "reference_range": "",
                    "reference_verified": False,
                }
            )
    return render_template(
        "report.html",
        order=order,
        patient=patient,
        report_rows=report_rows,
        report_sources=report_sources,
        has_draft_references=any(
            not item.get("reference_verified") for item in order["test_items"]
        ),
        results=results,
    )
