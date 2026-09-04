def _release_pending_order(self, order: dict) -> None:
    key = str(order.get("key") or "")
    action = str(order.get("action") or "")
    if action not in {"PROFIT", "STOP"} or ":" not in key:
        return
    instrument_type, symbol = key.split(":", 1)
    if instrument_type == "STOCK":
        self.pending_stock_exits.discard(symbol)
    elif instrument_type == "OPTION":
        self.pending_option_exits.discard(symbol)
