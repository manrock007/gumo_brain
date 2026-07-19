"""multi-worker DB-claim columns (Epic F2)

Adds jobs.claimed_by / jobs.claimed_at — the Postgres DB-claim queue's
ownership columns. Matches the additive db.py MIGRATIONS entries in lockstep
(the static guard tests/test_dbdriver.py::test_every_migrations_column_in_alembic_baseline
scans every committed revision, so both must carry these names).

Neutral defaults; existing rows are unaffected. This is the worked example of
the additive-revision rule: every future MIGRATIONS column gets a revision like
this one.

Revision ID: 0002_worker_claim
Revises: 0001_baseline
"""
from alembic import op

revision = "0002_worker_claim"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_by text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_at double precision")
    # a partial index over the claim candidate set keeps claim_next_job's
    # SELECT ... FOR UPDATE SKIP LOCKED cheap under load.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs (updated_at) "
        "WHERE status IN ('received','queued') AND claimed_by = ''")


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_jobs_claimable")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS claimed_at")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS claimed_by")
