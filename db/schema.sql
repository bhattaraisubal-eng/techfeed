-- Tech Feed database schema.
--
-- Apply with:
--   /opt/homebrew/opt/postgresql@17/bin/psql -d techfeed -f db/schema.sql


-- One row per thing a worker fetches: a blog feed, a YouTube channel,
-- a Spotify show, an HN list, an arXiv query, or (later) an HN domain search.
CREATE TABLE sources (
    -- Surrogate key: a small number other tables can point to.
    id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Which worker reads this row, and what it should fetch.
    -- The meaning of `locator` depends on `worker`: a feed URL for 'blog',
    -- a channel ID for 'youtube', a show ID for 'spotify', and so on.
    worker                text NOT NULL
                          CHECK (worker IN ('blog', 'youtube', 'spotify', 'hackernews', 'arxiv', 'hn_search')),
    locator               text NOT NULL,

    -- For people: what the source is called and where to read it in a browser.
    name                  text NOT NULL,
    homepage_url          text,

    -- Curation: priority tier (1 = AI labs ... 4 = expert writers), and whether
    -- the publisher runs the feed itself (false = third-party, e.g. Olshansk).
    tier                  smallint CHECK (tier BETWEEN 1 AND 4),
    official              boolean NOT NULL DEFAULT true,
    status                text NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'dormant', 'disabled')),
    notes                 text,
    created_at            timestamptz NOT NULL DEFAULT now(),

    -- Health, updated after every run.
    last_fetched_at       timestamptz,          -- last attempt, successful or not
    last_success_at       timestamptz,          -- last attempt that worked
    last_new_item_at      timestamptz,          -- last run that found something new
    consecutive_failures  integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_error            text,

    -- Conditional GET: the validators the server sent with the last stored
    -- response. Sent back as If-None-Match / If-Modified-Since, so a feed that
    -- hasn't changed answers "304 Not Modified" with no body. Written by the
    -- pipeline only after the items were stored (see ingest/pipeline.py).
    http_etag             text,
    http_last_modified    text,

    -- The same feed (or channel, or query) can't be added twice.
    UNIQUE (worker, locator)
);


-- One row per piece of content: an article, video, podcast episode,
-- HN story, or paper. Filled by the store stage's upsert.
CREATE TABLE content_items (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Identity: the platform plus the ID the platform itself assigns.
    -- Python's ContentItem.source becomes `platform` here, because "source"
    -- means a row in `sources`. An article found by two feeds (e.g. OpenAI
    -- News and OpenAI Research) has the same key, so it's stored once.
    platform          text NOT NULL
                      CHECK (platform IN ('blog', 'youtube', 'spotify', 'hackernews', 'arxiv')),
    source_item_id    text NOT NULL,

    -- The source that found this item first. The upsert never changes it.
    -- ON DELETE RESTRICT: a source that still has items can't be deleted.
    -- Set its status to 'disabled' instead, so its items (and their history) stay.
    source_id         bigint NOT NULL REFERENCES sources (id) ON DELETE RESTRICT,

    -- The normalized fields, one column per ContentItem field.
    content_type      text NOT NULL
                      CHECK (content_type IN ('video', 'podcast_episode', 'discussion', 'article', 'paper')),
    title             text NOT NULL,
    url               text NOT NULL,
    published_at      timestamptz NOT NULL,     -- the publisher's claim, in UTC
    author            text,
    description       text,
    duration_seconds  integer CHECK (duration_seconds >= 0),
    -- A YouTube Short: a vertical video of 3 minutes or less (see is_youtube_short
    -- in ingest/workers/youtube.py). Still content_type 'video'; this marks the
    -- format, for the app's Shorts section. Always false for other platforms.
    is_short          boolean NOT NULL DEFAULT false,
    tags              text[] NOT NULL DEFAULT '{}',   -- the source's own tags, e.g. {Developers,"Open Source"}

    -- The untouched source data: "keep the raw, derive the rest".
    raw               jsonb NOT NULL,

    -- Our own facts, unlike published_at: when we first and last saw the item.
    -- "New today" means first_seen_at is today.
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (last_seen_at >= first_seen_at),

    -- The key the upsert matches on: rerunning a worker never creates duplicates.
    UNIQUE (platform, source_item_id)
);

-- The feed shows the newest items first.
CREATE INDEX content_items_published_at_idx ON content_items (published_at DESC);

-- The Shorts section: newest Shorts first. A partial index holds only the rows
-- matching its WHERE, so it stays small (about a third of videos) and the
-- query below can read it directly:
--   SELECT ... FROM content_items WHERE is_short ORDER BY published_at DESC LIMIT 50;
CREATE INDEX content_items_shorts_idx ON content_items (published_at DESC) WHERE is_short;

-- "All items from this source", for health stats. PostgreSQL also uses it to
-- check ON DELETE RESTRICT quickly. (It doesn't index foreign keys on its own.)
CREATE INDEX content_items_source_id_idx ON content_items (source_id);
