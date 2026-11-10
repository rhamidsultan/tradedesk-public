"""The Alembic migrations build exactly the tables the models describe."""
import pathlib

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from tdserver import models  # noqa: F401
from tdserver.db import Base, make_engine

ROOT = pathlib.Path(__file__).parents[1]


def test_upgrade_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
    command.downgrade(cfg, "base")
