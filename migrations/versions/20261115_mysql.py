"""Initial MySQL business schema."""
from alembic import op
from finagent.storage.schema import metadata

revision = "20261115_mysql"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError("Data-preserving downgrade requires an explicit export; automatic DROP is disabled")
