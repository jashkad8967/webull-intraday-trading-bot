import logging

log = logging.getLogger("webull-bot")


def filter_by_historical_volatility(self, symbols: list[str]) -> list[str]:
    if (
        not self.config.historical_volatility_filter_enabled
        or not symbols
    ):
        return symbols
    floor = float(self.config.min_historical_volatility_percent)
    log.info(
        "VOLFILT | scoring %s symbols | lookback=%sd | floor=%.2f%%",
        len(symbols),
        self.config.historical_volatility_days,
        floor,
    )
    try:
        scores = self.api.historical_volatility(
            symbols,
            self.config.historical_volatility_days,
        )
    except Exception as exc:
        log.warning("VOLFILT | disabled this cycle | %s", exc)
        return symbols
    covered = [symbol for symbol in symbols if symbol in scores]
    if len(covered) < max(1, len(symbols) // 2):
        log.warning(
            "VOLFILT | insufficient coverage (%s/%s) | keeping full universe",
            len(covered),
            len(symbols),
        )
        return symbols
    qualifying = [symbol for symbol in covered if scores[symbol] >= floor]
    if not qualifying:
        log.warning(
            "VOLFILT | no symbols cleared floor | keeping full universe"
        )
        return symbols
    ordered = sorted(
        qualifying,
        key=lambda symbol: scores[symbol],
        reverse=True,
    )
    log.info(
        "VOLFILT | kept %s of %s | top=%s",
        len(ordered),
        len(symbols),
        ",".join(
            f"{symbol}:{scores[symbol]:.1f}%" for symbol in ordered[:5]
        ),
    )
    return ordered
