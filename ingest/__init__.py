"""Tech Feed ingestion pipeline.

Every source goes through the same four stages:

    fetch  ->  normalize  ->  validate  ->  store
    (raw API    (one common     (reject bad    (PostgreSQL —
     data)       ContentItem)    items)         not built yet)

Run a worker from the project root:

    python -m ingest youtube --limit 5
    python -m ingest all
"""
