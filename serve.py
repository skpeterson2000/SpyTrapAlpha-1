#!/usr/bin/env python3
"""Track_My_Tracker — run the headless API server.

    ./.venv/bin/python serve.py            # 0.0.0.0:8080 (see config api.*)
"""

import uvicorn

from tmt import config as configmod

if __name__ == "__main__":
    cfg = configmod.load()["api"]
    uvicorn.run("tmt.api:app", host=cfg["host"], port=cfg["port"],
                log_level="info")
