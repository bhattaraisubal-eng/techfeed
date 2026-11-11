-- Migration 002: mark YouTube Shorts, for the app's Shorts section.
--
-- db/schema.sql already has this for new databases. For an existing one:
--   /opt/homebrew/opt/postgresql@17/bin/psql -d techfeed -f db/migrations/002_add_is_short.sql
--
-- Existing rows get false. The next `python -m ingest youtube --store` sets
-- the real value, because the upsert updates is_short (the backfill).
-- Safe to run twice.

ALTER TABLE content_items
    ADD COLUMN IF NOT EXISTS is_short boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS content_items_shorts_idx
    ON content_items (published_at DESC) WHERE is_short;
