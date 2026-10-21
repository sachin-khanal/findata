"""Alembic environment: runs migrations on the connection `findata.schema` hands us.

There is no alembic.ini and no URL here on purpose. The database is SQLCipher-encrypted, so
the only correct way to open it is the app's own engine (`findata.db.make_engine`).
"""

from alembic import context

from findata.db import Base

config = context.config
connection = config.attributes["connection"]

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=True,  # SQLite can't ALTER most things in place; batch mode rebuilds tables
)

with context.begin_transaction():
    context.run_migrations()
