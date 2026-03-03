from __future__ import annotations

from flask import Flask

from .config import Config
from .db import init_db
from .imports import ImportProcessor
from .routes import bp


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    app.config["DB_PATH"] = str(app.config["DB_PATH"])
    app.config["UPLOAD_DIR"] = str(app.config["UPLOAD_DIR"])
    app.config["ARTIFACT_DIR"] = str(app.config["ARTIFACT_DIR"])
    app.config["FRONTEND_DIST_DIR"] = str(app.config["FRONTEND_DIST_DIR"])

    with app.app_context():
        init_db()

    app.extensions["import_processor"] = ImportProcessor(app)
    app.register_blueprint(bp)
    return app
