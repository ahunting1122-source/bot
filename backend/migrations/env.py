from alembic import context
from sqlalchemy import create_engine

from app.config import Settings
from app.storage import Base

settings = Settings()
settings.prepare()
url = context.config.attributes.get("database_url", f"sqlite:///{settings.data_dir / 'app.sqlite'}")
engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
