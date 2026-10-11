"""python -m frequency.api — serve the multi-user API and page."""
import logging
import sys

import uvicorn

from .. import db, settings
from .app import create_app


def main():
    s = settings.get().require("database_url", "secret_key")
    pending = db.pending()
    if pending:
        sys.exit(f"Database schema is behind ({', '.join(pending)}). Run: python -m frequency.cli migrate")
    sys.path.insert(0, str(settings.ROOT))                # the single-user modules (presenter, recommend, ...)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if s.host not in ("127.0.0.1", "localhost", "::1") and not s.secure_cookies:
        print("Note: serving beyond this machine without FREQUENCY_SECURE_COOKIES=1. Put HTTPS in front before "
              "anyone signs in over the network (see docs/DEPLOYMENT.md).")
    # access_log=False: uvicorn's log line includes query strings (OAuth codes); our middleware logs paths only.
    uvicorn.run(create_app(), host=s.host, port=s.port, access_log=False, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
