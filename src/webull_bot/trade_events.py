import queue
import threading


class TradeEventStreamService:
    """Phase 0 of the polling-to-streaming migration (see the plan): a
    read-only observer for Webull's gRPC order/position event stream.
    Only logs what it receives - no trading behavior depends on this
    yet, since the exact payload schema isn't confirmed from real
    traffic.

    Runs webull.trade.trade_events_client.TradeEventsClient.do_subscribe
    on its own daemon thread (that call is synchronous and blocks
    forever, with its own built-in reconnect/backoff - see
    DefaultSubscribeRetryPolicy). Received events are hand off to the
    main thread via a plain thread-safe queue, drained once per poll
    cycle - same cross-thread handoff shape as
    AnalystDataService.snapshot().
    """

    def __init__(self, config, log):
        self.config = config
        self.log = log
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        from webull.trade.trade_events_client import TradeEventsClient

        _silence_sdk_prints(self.log)
        client = TradeEventsClient(
            self.config.webull_app_key,
            self.config.webull_app_secret,
            self.config.webull_region_id,
        )
        client.on_events_message = self._on_events_message
        client.on_connect = self._on_connect
        client.on_log = self._on_log
        try:
            client.do_subscribe([self.config.account_id])
        except Exception as exc:
            # do_subscribe only returns/raises on a non-retryable error -
            # every retryable disconnect is already handled internally
            # by the SDK's own retry policy. This thread ending silently
            # would look identical to a healthy idle stream from the
            # outside, so this must be loud.
            self.log.error("EVENTS | stream ended (non-retryable) | %s", exc)

    def _on_connect(self, *args, **kwargs) -> None:
        self.log.info("EVENTS | subscribed | %s", args or kwargs)

    def _on_log(self, level, message) -> None:
        self.log.log(level, "EVENTS | %s", message)

    def _on_events_message(
        self, event_type, subscribe_type, payload, raw_message
    ) -> None:
        try:
            self._queue.put_nowait((event_type, subscribe_type, payload))
        except queue.Full:
            # Draining fell behind - drop the oldest rather than block
            # the SDK's own dispatch thread, which is also its network
            # read loop.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait((event_type, subscribe_type, payload))
            except queue.Full:
                pass

    def drain(self) -> list[tuple[int, int, dict]]:
        """Called once per poll cycle from the main thread. Phase 0: the
        caller just logs each event (see AutoTrader.log_trade_events) -
        nothing here feeds a trading decision yet.
        """
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events


_prints_silenced = False


def _silence_sdk_prints(log) -> None:
    """The SDK unconditionally print()s signing metadata and connection
    details on every connect/reconnect (trade_events_client.py's
    _build_request and do_subscribe) - confirmed by reading the
    installed SDK source, not just its docs. That bypasses the bot's own
    logging entirely and would spam container logs with connection
    internals (including the request signature) on every reconnect.

    Patches the *module-level* print name inside trade_events_client
    (Python resolves an unqualified print() through the module's own
    globals before falling back to the builtin, so this only affects
    that module) - deliberately not contextlib.redirect_stdout, which
    reassigns the process-global sys.stdout and would corrupt the main
    thread's own logging output for as long as this runs (do_subscribe
    blocks forever on a background thread).
    """
    global _prints_silenced
    if _prints_silenced:
        return
    _prints_silenced = True
    import webull.trade.trade_events_client as trade_events_client_module

    def _filtered_print(*args, **kwargs) -> None:
        message = " ".join(str(arg) for arg in args).strip()
        if message:
            log.debug("EVENTS | %s", message)

    trade_events_client_module.print = _filtered_print
