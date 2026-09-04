def backfill_stock_symbols(self, count: int) -> int:
    active = set(self.stock_symbols)
    added = 0
    while added < count and self.reserve_symbols:
        candidate = self.reserve_symbols.pop(0)
        if candidate in active or candidate in self.invalid_symbols:
            continue
        self.stock_symbols.append(candidate)
        active.add(candidate)
        added += 1
    return added
