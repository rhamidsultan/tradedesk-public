"""Alembic environment: uses TD2_DATABASE_URL and the models' metadata."""
from alembic import context

from tdserver import models  # noqa: F401  (registers tables)
from tdserver.db import Base, make_engine
from tdserver.settings import get_settings

url = context.config.attributes.get("url") or get_settings().database_url
target_metadata = Base.metadata


def run() -> None:
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run()
