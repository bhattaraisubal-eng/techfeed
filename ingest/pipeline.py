"""Run workers for the sources in the database, and store what they find.

The `sources` table is the only list of what to fetch. Both CLI modes use it:
  * `python -m ingest <worker>`          dry run: fetch and print, write nothing
  * `python -m ingest <worker> --store`  the real run, below

The real run does this, one source at a time:
  1. read the active sources for that worker from the `sources` table
  2. build a worker for ONE source (one feed, one channel, one show...)
  3. run it: fetch -> normalize -> validate
  4. upsert its items into content_items, with that source's id
  5. record the source's health: success, or failure and why
     (and its logo, if the worker found one)

One source at a time is what lets each item carry the right source_id, and
each source get its own health record. A failing source never stops the others.
"""

import os
from dataclasses import dataclass

import psycopg

from .config import ConfigError, require
from .http import RequestError, Validators
from .store import StoreResult, record_health, record_logo, record_validators, upsert_items
from .workers.arxiv import ArxivWorker
from .workers.base import RunResult, Worker
from .workers.blogs import BlogsWorker
from .workers.hackernews import HackerNewsWorker
from .workers.spotify import SpotifyWorker
from .workers.youtube import YouTubeWorker

# CLI name -> the value of sources.worker
CLI_TO_WORKER = {"youtube": "youtube", "hackernews": "hackernews", "blogs": "blog", "arxiv": "arxiv", "spotify": "spotify"}

# How many items to take per source when storing, if --limit isn't given.
# Blogs: the whole feed. Feeds aren't always sorted (OpenAI News isn't), so a
# cap could keep old posts and drop new ones; the upsert makes "all" cheap.
# The others come from APIs sorted newest-first, so a cap is safe, and it bounds
# API quota (YouTube) and the one-request-per-story cost (Hacker News).
STORE_LIMITS = {"blog": 100_000, "youtube": 200, "spotify": 200, "hackernews": 30, "arxiv": 100}

# A dry run is for looking, so keep it short unless --limit says otherwise.
DRY_RUN_LIMIT = 10


@dataclass
class Source:
    id: int | None      # None for a dry run of a locator that isn't in the table yet
    worker: str
    locator: str
    name: str
    http_etag: str | None = None
    http_last_modified: str | None = None


@dataclass
class SourceOutcome:
    source: Source
    result: RunResult | None = None   # None if the worker couldn't run at all
    stored: StoreResult | None = None
    error: str | None = None
    not_modified: bool = False        # the feed answered 304: nothing changed

    def summary(self) -> str:
        parts = [f"{self.source.worker} | {self.source.name}:"]
        if self.not_modified:
            parts.append("not modified (304)")
        elif self.result:
            parts.append(f"fetched {self.result.fetched}, valid {len(self.result.items)}, rejected {len(self.result.rejected)}")
        if self.stored and not self.not_modified:
            parts.append(f"-> {self.stored.summary()}")
        if self.error:
            parts.append(f"FAILED: {self.error}")
        return " ".join(parts)


def active_sources(conn, worker: str) -> list[Source]:
    rows = conn.execute(
        "SELECT id, worker, locator, name, http_etag, http_last_modified FROM sources"
        " WHERE worker = %s AND status = 'active' ORDER BY tier NULLS LAST, id",
        (worker,),
    ).fetchall()
    return [Source(*row) for row in rows]


def build_worker(source: Source, http, refetch: bool = False) -> Worker:
    """A worker that fetches exactly this one source.

    `locator` means something different for each worker: a feed URL, a
    channel ID, a show ID, an HN list name, or an arXiv query.

    refetch=True ignores the saved ETag / Last-Modified, so every feed is
    downloaded and reprocessed (e.g. after fixing a parsing bug).
    """
    if source.worker == "blog":
        validators = None
        if not refetch and (source.http_etag or source.http_last_modified):
            validators = {source.locator: Validators(source.http_etag, source.http_last_modified)}
        return BlogsWorker(http, [source.locator], validators)
    if source.worker == "youtube":
        return YouTubeWorker(http, require("YOUTUBE_API_KEY"), [source.locator])
    if source.worker == "spotify":
        # One worker per show means one access-token request per show (11 today).
        # Fine at this size; sharing one worker would save those requests.
        market = os.environ.get("SPOTIFY_MARKET", "").strip() or "US"
        return SpotifyWorker(http, require("SPOTIFY_CLIENT_ID"), require("SPOTIFY_CLIENT_SECRET"), [source.locator], market)
    if source.worker == "hackernews":
        return HackerNewsWorker(http, source.locator)
    if source.worker == "arxiv":
        return ArxivWorker(http, source.locator)
    raise ConfigError(f"no worker is built for {source.worker!r} sources yet")


def run_source(conn, http, source: Source, limit: int, refetch: bool = False, store: bool = True) -> SourceOutcome:
    """Fetch, store and record health for one source. Never raises for an
    expected failure (config, network, a constraint the validator missed):
    the failure is recorded on the source instead.

    store=False is the dry run: fetch, normalize and validate, then return
    without writing anything (no items, no health, no validators). `conn`
    isn't used, so it can be None.
    """
    outcome = SourceOutcome(source)
    worker = None

    try:
        # A dry run always downloads in full: a 304 would leave nothing to show.
        worker = build_worker(source, http, refetch=refetch or not store)
        outcome.result = worker.run(limit)
    except (ConfigError, RequestError) as error:
        outcome.error = str(error)
    outcome.not_modified = source.locator in getattr(worker, "not_modified", ())

    if outcome.result is not None:
        if store:
            try:
                outcome.stored = upsert_items(conn, source.id, outcome.result.items)
            except psycopg.Error as error:
                # upsert_items already rolled its batch back (all or nothing).
                outcome.error = f"store failed: {error}".strip()
        if outcome.error is None and outcome.result.errors:
            # The worker ran but couldn't fetch this source (e.g. the feed
            # returned 500, or isn't valid XML). For a one-source worker,
            # that means the source failed.
            outcome.error = "; ".join(outcome.result.errors)

    if not store:
        return outcome

    new_items = outcome.stored.inserted if outcome.stored else 0
    # Only workers that do conditional GET (blogs) have validators.
    validators = getattr(worker, "new_validators", {}).get(source.locator)
    logo = worker.source_logos.get(source.locator) if worker else None
    with conn.transaction():
        record_health(conn, source.id, outcome.error, new_items)
        if logo:
            record_logo(conn, source.id, logo)
        # Saved only when everything succeeded: if the store had failed, keeping
        # the old validators makes the next run download and store this feed again.
        if outcome.error is None and validators is not None:
            record_validators(conn, source.id, validators.etag, validators.last_modified)
    return outcome


def store_run(conn, http, cli_names: list[str], limit: int | None, on_outcome=None,
              refetch: bool = False, store: bool = True) -> list[SourceOutcome]:
    """Run every active source for the given workers. `on_outcome` is called
    after each source, so the CLI can print progress as it goes.
    store=False runs them all as a dry run (see run_source)."""
    outcomes = []
    for name in cli_names:
        worker = CLI_TO_WORKER[name]
        default_limit = STORE_LIMITS[worker] if store else DRY_RUN_LIMIT
        for source in active_sources(conn, worker):
            outcome = run_source(conn, http, source, limit or default_limit, refetch, store)
            outcomes.append(outcome)
            if on_outcome:
                on_outcome(outcome)
    return outcomes
