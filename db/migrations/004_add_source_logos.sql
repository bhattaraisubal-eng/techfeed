-- Migration 004: a logo per source, the fallback image for cards without a thumbnail.
--
-- db/schema.sql already has these for new databases. For an existing one:
--   /opt/homebrew/opt/postgresql@17/bin/psql -d techfeed -f db/migrations/004_add_source_logos.sql
--
--   logo_url       written by the pipeline: the logo the source itself provides
--   logo_override  written by the seed script: a hand-picked logo from sources.csv
--
-- Apply it to EVERY database (local and Neon) BEFORE deploying the code that
-- writes these columns. Safe to run twice.

ALTER TABLE sources ADD COLUMN IF NOT EXISTS logo_url text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS logo_override text;
