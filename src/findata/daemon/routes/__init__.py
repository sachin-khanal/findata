"""The daemon's /v1 routes, one module per concern; `create_daemon_app` mounts `ROUTERS`.

Each route's `name` is the operation recorded in the audit log.
"""

from findata.daemon.routes import audit, data, grants, items, jobs, link, status, sync

ROUTERS = tuple(m.router for m in (status, data, sync, link, jobs, items, grants, audit))
