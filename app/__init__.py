import os
from pathlib import Path

from flask import Flask, redirect, url_for
from flask_wtf.csrf import CSRFProtect

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
    csrf.init_app(app)
    app.register_blueprint(bp)
    app.cli.add_command(bp.cli)

    @app.get("/")
    def index():
        return redirect(url_for("main.dashboard"))

    return app
