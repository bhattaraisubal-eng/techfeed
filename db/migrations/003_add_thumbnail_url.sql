-- Migration 003: each item's own image, for the app's cards.
--
-- db/schema.sql already has this for new databases. For an existing one:
--   /opt/homebrew/opt/postgresql@17/bin/psql -d techfeed -f db/migrations/003_add_thumbnail_url.sql
--
-- Apply it to EVERY database the code writes to (local and Neon) BEFORE
-- deploying code that writes thumbnail_url, or the store's INSERT fails on
-- an unknown column. Existing rows get NULL until the next run fills them
-- (blogs need --refetch, since unchanged feeds answer 304). Safe to run twice.

ALTER TABLE content_items ADD COLUMN IF NOT EXISTS thumbnail_url text;
