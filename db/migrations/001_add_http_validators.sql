-- Migration 001: conditional GET validators on sources.
--
-- db/schema.sql creates NEW databases with these columns already. A database
-- created before this change needs this file applied once:
--   /opt/homebrew/opt/postgresql@17/bin/psql -d techfeed -f db/migrations/001_add_http_validators.sql
--
-- IF NOT EXISTS makes it safe to run twice.

ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS http_etag          text,
    ADD COLUMN IF NOT EXISTS http_last_modified text;
