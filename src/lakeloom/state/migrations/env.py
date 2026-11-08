"""Alembic environment: runs on a connection handed in by ``lakeloom.state.db.upgrade``.

Developers can also run ``uv run alembic -x url=<sync url> upgrade head`` (or set
``sqlalchemy.url`` in alembic.ini); that path creates its own sync engine.
"""

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from lakeloom.state.models import Base

target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = context.get_x_argument(as_dictionary=True).get("url") or context.config.get_main_option(
        "sqlalchemy.url"
    )
    if not url:
        raise RuntimeError("no database URL: pass -x url=... or set sqlalchemy.url")
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run(conn)
        conn.commit()


run()
