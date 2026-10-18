
import os
from gunicorn.app.base import BaseApplication
from app import app


class StandaloneApplication(BaseApplication):
    def __init__(self, application, options=None):
        self.application = application
        self.options = options or {}
        super().__init__()

    def load_config(self):
        for key, value in self.options.items():
            if key in self.cfg.settings and value is not None:
                self.cfg.set(key, value)

    def load(self):
        return self.application


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))

    StandaloneApplication(
        app,
        {
            "bind": f"0.0.0.0:{port}",
            "workers": 1
        }
    ).run()
