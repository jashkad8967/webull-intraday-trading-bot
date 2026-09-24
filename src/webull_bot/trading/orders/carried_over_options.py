import logging

log = logging.getLogger("webull-bot")


def close_carried_over_options(self, moment) -> None:
    """Flatten option positions carried in from an earlier session.

    This bot is intraday-flat by design - option_eod_close_time exists
    to guarantee it - but that close only runs inside its own window
    (option_eod_close_time..option_market_close_time). A bot that is
    not running during that window simply never flattens, and there
    was nothing that ever noticed afterwards.

    Live 2026-09-23: the host's disk filled, Docker wedged and the bot
    was DOWN from roughly 13:10 CT. It came back at 15:54 CT - after
    options had stopped trading at 15:00 - holding three positions
    (2x SOFI, 1x NFLX) that should have been closed at 14:50. Nothing
    could be done that evening, and nothing in the loop was going to
    treat them as overdue the next morning either: they would simply
    look like ordinary holdings, and (because position_opened_at is
    in-memory) like BRAND NEW ones, immune to the stale exit for
    another full window.

    So this runs once per day at the option open and closes anything
    whose recorded entry date is earlier than today. Deliberately
    keyed on the persisted entry DATE rather than on "everything open
    when the session starts": a mid-session restart must not be
    mistaken for a new day and dump legitimate intraday positions,
    which is exactly what a session-start snapshot would have done.

    Exits only - it never opens anything - so the worst case if the
    date record is missing is that a position is left to the normal
    stop/profit-lock/stale ladder, which is where it would have been
    anyway.
    """
    if self.carried_over_options_date == moment.date():
        return
    positions = self.cached_positions or []
    carried = []
    for position in positions:
        if position.get("instrument_type") != "OPTION":
            continue
        try:
            quantity = float(position.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            continue
        if quantity == 0:
            continue
        # Resolve the CONTRACT. position["symbol"] is the bare
        # underlying ("GME"), while position_open_times is written by
        # record_trade under the OCC contract key
        # ("OPTION:GME261009C00024000") - so building the key from the
        # position symbol produced "OPTION:GME", which matches nothing
        # and made this entire sweep incapable of ever firing.
        #
        # Found 2026-09-24 by sweeping for this exact bare-underlying-
        # vs-contract mismatch after it had already broken the stall
        # sweep (7cabc78), the manual-sell guards (132eb3a) and the
        # per-underlying cap (bc3a81e). Fourth instance, and the only
        # one that silently disabled a feature outright rather than
        # producing a visible broker rejection - which is why it went
        # unnoticed: a sweep that never fires looks exactly like a
        # sweep with nothing to do.
        contract = self.api.contract_from_position(position)
        option_symbol = str((contract or {}).get("symbol", "") or "")
        if not option_symbol:
            continue
        if self.position_open_times.opened_before_today(
            f"OPTION:{option_symbol}"
        ):
            carried.append(option_symbol)
    # Stamped only once positions are actually visible. cached_positions
    # is empty on the very first cycles after a restart, and stamping
    # then would mark the day handled before anything could be seen.
    if not positions:
        return
    self.carried_over_options_date = moment.date()
    if not carried:
        return
    log.warning(
        "CARRY  | %s option position(s) held over from a previous "
        "session | closing now: %s",
        len(carried),
        ", ".join(sorted(carried)),
    )
    self.close_instruments({"OPTION"})
