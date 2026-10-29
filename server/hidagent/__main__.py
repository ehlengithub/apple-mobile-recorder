import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

debug = os.environ.get("HIDAGENT_DEBUG") == "1"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logging.getLogger("hidagent").setLevel(logging.DEBUG if debug else logging.INFO)
uvicorn.run("hidagent.app:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8990)),
            access_log=os.environ.get("HIDAGENT_HTTP_LOG") == "1")
