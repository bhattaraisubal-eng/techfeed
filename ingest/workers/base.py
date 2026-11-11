"""What every worker has in common.

A worker only has to answer two questions:
  * fetch(limit): "give me raw items from this source"
  * normalize(raw): "turn one raw item into a ContentItem"

`run()` is written once, here, and wires the stages together the same way
for every source: fetch -> normalize -> validate, with errors contained.
This pattern is called the *template method*: the base class owns the
algorithm, subclasses fill in the source-specific steps.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..models import ContentItem
from ..validate import validate


@dataclass
class Rejection:
    source_item_id: str | None
    reasons: list[str]


@dataclass
class RunResult:
    """What happened during one run of one worker."""

    source: str
    fetched: int = 0                  # raw items that reached normalize
    skipped: int = 0                  # deliberately ignored (dead HN posts, duplicates, ...)
    items: list[ContentItem] = field(default_factory=list)       # valid, ready to store
    rejected: list[Rejection] = field(default_factory=list)      # failed normalize or validate
    errors: list[str] = field(default_factory=list)              # fetch problems that didn't stop the run

    def summary(self) -> str:
        return (
            f"{self.source}: fetched {self.fetched}, valid {len(self.items)}, "
            f"rejected {len(self.rejected)}, skipped {self.skipped}, errors {len(self.errors)}"
        )


class Worker(ABC):
    source: str = ""  # each subclass sets this, e.g. "youtube"

    def __init__(self, http):
        # `http` is passed in rather than created here ("dependency injection"):
        # production passes a real HttpClient, tests pass a fake one.
        self.http = http
        self.fetch_errors: list[str] = []
        self.skipped = 0

    @abstractmethod
    def fetch(self, limit: int) -> Iterator[dict]:
        """Yield raw items. Record recoverable problems in self.fetch_errors."""

    @abstractmethod
    def normalize(self, raw: dict) -> ContentItem:
        """Convert one raw item. Raise KeyError/TypeError/ValueError if it can't be done."""

    def run(self, limit: int = 10) -> RunResult:
        self.fetch_errors = []
        self.skipped = 0
        result = RunResult(source=self.source)
        seen: set[str] = set()

        for raw in self.fetch(limit):
            result.fetched += 1

            # One bad item must not stop the run: catch, record, move on.
            try:
                item = self.normalize(raw)
            except (KeyError, TypeError, ValueError) as error:
                result.rejected.append(Rejection(_guess_id(raw), [f"could not normalize: {type(error).__name__}: {error}"]))
                continue

            reasons = validate(item)
            if reasons:
                result.rejected.append(Rejection(item.source_item_id, reasons))
                continue

            if item.source_item_id in seen:  # same item twice in one run
                result.skipped += 1
                continue
            seen.add(item.source_item_id)
            result.items.append(item)

        result.skipped += self.skipped
        result.errors.extend(self.fetch_errors)
        return result


def _guess_id(raw) -> str | None:
    """Best-effort ID for a rejection message when normalize itself failed."""
    if isinstance(raw, dict):
        for key in ("id", "guid", "arxiv_id", "link"):
            if raw.get(key):
                return str(raw[key])
    return None
