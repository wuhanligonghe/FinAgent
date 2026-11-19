"""Upgrade with the maintenance URL; application constructors never run DDL."""
from alembic import context
from finagent.storage.database import get_database
from finagent.storage.schema import metadata

if context.is_offline_mode():
    context.configure(dialect_name="mysql", target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    database = get_database()
    with database.engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
