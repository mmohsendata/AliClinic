import os
from pathlib import Path
from uuid import uuid4

from flask import Flask, redirect, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash

from app.repositories.json_store import JsonStore
from app.routes import bp

csrf = CSRFProtect()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        DATA_FILE=os.environ.get(
            "DATA_FILE", str(Path(app.root_path).parent / "data" / "store.json")
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
        WTF_CSRF_TIME_LIMIT=60 * 60,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("Set SECRET_KEY in the environment before starting the app.")

    app.extensions["json_store"] = JsonStore(app.config["DATA_FILE"])
    bootstrap_admin(app.extensions["json_store"])
    csrf.init_app(app)
    app.register_blueprint(bp)
    app.cli.add_command(bp.cli)

    @app.get("/")
    def index():
        return redirect(url_for("main.dashboard"))

    @app.get("/health")
    def health():
        return {"status": "ok"}, 200

    return app


def bootstrap_admin(json_store):
    username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "").strip()
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username and not password:
        return
    if not username or not password:
        raise RuntimeError(
            "Set both BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD, or unset both."
        )

    def create_if_missing(data):
        if any(user["role"] == "admin" and user["active"] for user in data["users"]):
            return
        if len(password) < 16:
            raise RuntimeError(
                "BOOTSTRAP_ADMIN_PASSWORD must contain at least 16 characters."
            )
        if any(user["username"].casefold() == username.casefold() for user in data["users"]):
            raise RuntimeError(
                "BOOTSTRAP_ADMIN_USERNAME is already assigned to another account."
            )
        data["users"].append(
            {
                "id": str(uuid4()),
                "username": username,
                "password_hash": generate_password_hash(password),
                "role": "admin",
                "active": True,
                "must_change_password": True,
            }
        )

    json_store.update(create_if_missing)
