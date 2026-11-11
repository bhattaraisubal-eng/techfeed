"""Command line entry point: `python -m ingest <worker>`.

Sources always come from the `sources` table (seeded from data/sources.csv).
Two modes:

  * dry run (default): fetch, normalize and validate each active source, and
    print valid items as JSON lines on stdout. Writes nothing to the database.
    Summaries and warnings go to stderr, so you can save just the data:
        python -m ingest youtube > youtube.jsonl

  * --store: the same, but items are upserted into content_items and each
    source's health is recorded. See ingest/pipeline.py.

To try a source that isn't in the table yet (dry run only):
    python -m ingest blogs --locator https://some-new-blog.com/feed
"""

import argparse
import json
import sys

from .config import ConfigError, load_env_file
from .db import connect
from .http import HttpClient
from .pipeline import CLI_TO_WORKER, DRY_RUN_LIMIT, Source, run_source, store_run


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest",
                                     description="Fetch, normalize and validate the sources in the database.")
    parser.add_argument("source", choices=[*CLI_TO_WORKER, "all"])
    parser.add_argument("--limit", type=positive_int, default=None,
                        help=f"max items per source (default {DRY_RUN_LIMIT}; with --store, "
                             "per-worker defaults in ingest/pipeline.py)")
    parser.add_argument("--raw", action="store_true", help="dry run: include the raw source data in each output line")
    parser.add_argument("--show-rejected", action="store_true", help="list rejected items and why")
    parser.add_argument("--store", action="store_true", help="store items in content_items and record source health")
    parser.add_argument("--refetch", action="store_true",
                        help="with --store: ignore saved ETag/Last-Modified and reprocess every feed")
    parser.add_argument("--locator", help="dry run one source that isn't in the sources table "
                                          "(a feed URL, channel ID, show ID, HN list or arXiv query)")
    args = parser.parse_args(argv)
    if args.refetch and not args.store:
        parser.error("--refetch only makes sense with --store")
    if args.locator and args.store:
        parser.error("--locator is for trying a source; add it to data/sources.csv and reseed to store it")
    if args.locator and args.source == "all":
        parser.error("--locator needs one worker, e.g. `blogs`, not `all`")

    load_env_file()
    http = HttpClient()
    names = list(CLI_TO_WORKER) if args.source == "all" else [args.source]

    def show(outcome):
        if not args.store:
            for item in outcome.result.items if outcome.result else []:
                print(json.dumps(item.to_dict(include_raw=args.raw), ensure_ascii=False))
        print(outcome.summary(), file=sys.stderr)
        if args.show_rejected and outcome.result:
            for rejection in outcome.result.rejected:
                print(f"  rejected {rejection.source_item_id}: {'; '.join(rejection.reasons)}", file=sys.stderr)

    try:
        if args.locator:
            source = Source(None, CLI_TO_WORKER[args.source], args.locator, f"{args.locator} (not in sources)")
            outcomes = [run_source(None, http, source, args.limit or DRY_RUN_LIMIT, store=False)]
            show(outcomes[0])
        else:
            with connect(autocommit=True) as conn:
                outcomes = store_run(conn, http, names, args.limit, on_outcome=show,
                                     refetch=args.refetch, store=args.store)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print_totals(outcomes, args.store)
    return 1 if any(o.error for o in outcomes) else 0


def print_totals(outcomes, stored: bool) -> None:
    failed = sum(1 for o in outcomes if o.error)
    totals = f"\n{len(outcomes)} sources: {len(outcomes) - failed} ok, {failed} failed"
    if stored:
        new = sum(o.stored.inserted for o in outcomes if o.stored)
        seen = sum(o.stored.seen_again for o in outcomes if o.stored)
        unchanged = sum(1 for o in outcomes if o.not_modified)
        totals += f" | {unchanged} not modified | {new} new items, {seen} seen again"
    else:
        valid = sum(len(o.result.items) for o in outcomes if o.result)
        totals += f" | {valid} valid items (dry run: nothing stored)"
    print(totals, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
