"""In-memory stand-in for PositionOpenTimeStore, shared by the fake
bots across the test suite.

Matches the two behaviours the production code depends on:

- note_open is IDEMPOTENT. A key already on record keeps its original
  timestamp, which is what makes a held position's age RESUME across a
  restart rather than resetting to zero (the bug that let a stalled
  position buy itself another full stale-exit window on every deploy).
- opened_before_today answers a calendar question, not an elapsed-time
  one, so a mid-session restart is never mistaken for a new day.
"""

from datetime import datetime, timezone


class FakeOpenTimes:
    def __init__(self, opened=None):
        self.opened_at = dict(opened or {})

    def note_open(self, key):
        if key not in self.opened_at:
            self.opened_at[key] = datetime.now(timezone.utc)
        return self.opened_at[key]

    def opened(self, key):
        return self.opened_at.get(key)

    def forget(self, key):
        self.opened_at.pop(key, None)

    def retain_only(self, keys):
        keep = set(keys)
        for key in [k for k in self.opened_at if k not in keep]:
            self.opened_at.pop(key, None)

    def opened_before_today(self, key):
        moment = self.opened_at.get(key)
        if moment is None:
            return False
        return moment.date() < datetime.now(timezone.utc).date()
