import json
import threading
import time
from datetime import datetime
from pathlib import Path


class QuoteTape:
    """Append-only tape of option quotes for currently-held positions.

    The reason this exists: every strategy parameter changed on
    2026-09-24/25 was validated by losing real money. The 5% stop was
    justified by "median win 5.3%, win rate 67% -> +1.9% per trade",
    arithmetic that held win rate CONSTANT while changing the very
    thing that determines it. That assumption could have been falsified
    in seconds against recorded ticks; instead it cost 48% of the
    account in one session.

    Trade history records what DID happen - entry, exit, P&L. It cannot
    answer "would a 5% stop have fired here", because that depends on
    the PRICE PATH between entry and exit, which nothing kept. This
    records that path.

    Deliberately bounded, on a host whose disk has already filled once:
    one line per held position per interval_seconds (2s by default, not
    the 0.5s the fast loop actually polls), a file per day, and the
    same retention the daily logs use. Three positions at 2s over a
    6.5-hour session is roughly 35k lines, about 2.5MB.

    Every method is best-effort and swallows its own failures. A
    recorder that can raise into the exit loop would be trading a
    measurement tool for the thing it is meant to protect.
    """

    def __init__(
        self,
        directory: str,
        timezone,
        log,
        interval_seconds: float = 2.0,
        retention_days: int = 5,
    ):
        self.directory = Path(directory)
        self.timezone = timezone
        self.log = log
        self.interval_seconds = float(interval_seconds)
        self.retention_days = int(retention_days)
        self._lock = threading.Lock()
        self._last_written: dict[str, float] = {}
        self._date = None
        self._stream = None

    def _stream_for_today(self):
        today = datetime.now(self.timezone).date()
        if self._stream is not None and today == self._date:
            return self._stream
        if self._stream is not None:
            self._stream.close()
        path = self.directory / f"{today:%Y-%m-%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a", encoding="utf-8")
        self._date = today
        self._prune()
        return self._stream

    def _prune(self) -> None:
        if self.retention_days <= 0:
            return
        try:
            files = sorted(self.directory.glob("*.jsonl"))
            for path in files[: -self.retention_days]:
                try:
                    path.unlink()
                except OSError:
                    continue
        except Exception:
            pass

    def record(self, samples) -> int:
        """Append one sample per held position, throttled per symbol.

        `samples` is an iterable of
        (option_symbol, bid, ask, last, cost, quantity) or, preferred,
        (option_symbol, bid, ask, last, cost, quantity, delta,
        underlying_price). Returns how many lines were written, for
        tests - the caller ignores it.

        DELTA AND THE UNDERLYING PRICE WERE ADDED 2026-10-01, after this
        tape could not answer the question that mattered most.

        PFE261009C00028000 lost $7.07 in 150 seconds because a 10% stop
        on a 0.62-delta contract is a 0.29% move in PFE, and PFE moves
        that many times an hour. An option is levered to its underlying
        by premium/(delta x spot), so the only correct way to scale a
        stop is per-contract from delta and spot.

        That fix cannot be validated against a tape holding neither.
        Replaying it needs the price path AND the leverage at each point:
        without delta, every replayed stop is the same mis-scaled
        percentage that caused the loss, so the replayer would have
        happily confirmed the broken design.

        Both are optional so a 6-tuple caller and every already-recorded
        file still work - the replayer treats a missing value as "not
        measurable" rather than zero.
        """
        written = 0
        try:
            now = time.monotonic()
            lines = []
            with self._lock:
                for sample in samples:
                    symbol, bid, ask, last, cost, quantity = sample[:6]
                    delta = sample[6] if len(sample) > 6 else None
                    underlying = sample[7] if len(sample) > 7 else None
                    if not symbol:
                        continue
                    previous = self._last_written.get(symbol)
                    if (
                        previous is not None
                        and now - previous < self.interval_seconds
                    ):
                        continue
                    self._last_written[symbol] = now
                    lines.append(
                        json.dumps(
                            {
                                "t": round(time.time(), 3),
                                "s": symbol,
                                "b": _num(bid),
                                "a": _num(ask),
                                "l": _num(last),
                                "c": _num(cost),
                                "q": _num(quantity),
                                # Leverage at this instant. Without these
                                # a replayed stop is necessarily the same
                                # mis-scaled percentage that caused the
                                # 2026-10-01 PFE loss.
                                "d": _num(delta),
                                "u": _num(underlying),
                            },
                            separators=(",", ":"),
                        )
                    )
                if not lines:
                    return 0
                stream = self._stream_for_today()
                stream.write("\n".join(lines) + "\n")
                stream.flush()
                written = len(lines)
        except Exception as exc:
            # Never propagate: this is a measurement tool sitting inside
            # the loop that submits stops.
            try:
                self.log.debug("TAPE   | write failed | %s", exc)
            except Exception:
                pass
        return written

    def forget(self, symbol: str) -> None:
        """Drop a closed position's throttle state so a re-entry into
        the same contract records immediately rather than waiting out
        the previous position's interval.
        """
        with self._lock:
            self._last_written.pop(symbol, None)

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
