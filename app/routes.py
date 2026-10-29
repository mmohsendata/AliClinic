from functools import wraps
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

from app.services.lab import (
    LabError,
    approve_order,
    create_order,
    create_patient,
    create_test,
    enter_result,
    now_iso,
    update_sample,
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
    return {
        "user": current_user(),
        "role_labels": ROLE_LABELS,
        "demo_mode": store().read().get("demo_seeded", False),
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
            return redirect(url_for("main.change_password" if user.get("must_change_password") else "main.dashboard"))
        flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
    return render_template("login.html")


@bp.post("/logout")
@login_required
def logout():
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
@bp.post("/catalog")
@roles_required("admin", "receptionist")
def catalog():
    if request.method == "POST":
        try:
            create_test(store(), request.form, current_user())
            flash("تمت إضافة التحليل.", "success")
            return redirect(url_for("main.catalog"))
        except LabError as exc:
            flash(str(exc), "error")
    data = store().read()
    return render_template("catalog.html", tests=data["tests"])


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
    return render_template("report.html", order=order, results=results)
