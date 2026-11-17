"""Paper broker: simulates the exact order flow used live on Robinhood.

  1. buy LIMIT (whole shares, good-for-day) at the plan's entry price
       - opens at/below the limit -> fills at the open plus half-spread, capped at the limit
       - trades down through the limit -> fills at the limit
       - gaps above the limit and never comes back -> no fill, order expires (no chasing)
  2. once filled, an OCO sell (GTC): take-profit LIMIT + stop-loss STOP-MARKET
       - gap below the stop -> fills at the open minus slippage (stops don't guarantee price)
       - stop and target both inside one daily bar -> assume the STOP hit first (conservative)
       - on the entry day itself only the stop is checked against the bar after the fill
  3. time exit: after `max_hold_days` sessions the position is sold at the close

Daily bars cannot show intrabar order, so every ambiguity resolves against us.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .costs import CostModel


@dataclass
class Position:
    symbol: str
    shares: float
    entry_price: float
    entry_date: str
    stop: float
    target: float
    setup: str
    plan: dict
    bars_held: int = 0


@dataclass
class PendingOrder:
    symbol: str
    shares: float
    limit: float
    created: str
    plan: dict


@dataclass
class Fill:
    kind: str           # entry / exit
    symbol: str
    date: str
    price: float
    shares: float
    reason: str
    fees: float = 0.0
    position: Position | None = None


@dataclass
class PaperBroker:
    cash: float
    costs: CostModel
    max_hold_days: int = 10
    positions: dict[str, Position] = field(default_factory=dict)
    pending: list[PendingOrder] = field(default_factory=list)

    def submit(self, plan: dict, day: str) -> None:
        self.pending.append(PendingOrder(plan["symbol"], plan["shares"], plan["entry"], day, plan))

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(p.shares * prices.get(s, p.entry_price) for s, p in self.positions.items())

    def try_entry(self, order: PendingOrder, day: str, bar: dict) -> Fill | None:
        o, l = bar["open"], bar["low"]
        if o <= order.limit:
            price = min(o + self.costs.half_spread(o), order.limit)
        elif l <= order.limit:
            price = order.limit
        else:
            return None  # gapped/ran away above the limit: no fill
        cost = price * order.shares + self.costs.buy_fees(price, order.shares)
        if cost > self.cash + 1e-9:
            return None  # never borrow
        self.cash -= cost
        p = order.plan
        pos = Position(order.symbol, order.shares, price, day, p["stop"], p["target"], p["setup"], p)
        self.positions[order.symbol] = pos
        return Fill("entry", order.symbol, day, price, order.shares, "buy limit filled",
                    self.costs.buy_fees(price, order.shares), pos)

    def _close(self, pos: Position, day: str, price: float, reason: str) -> Fill:
        fees = self.costs.sell_fees(price, pos.shares)
        self.cash += price * pos.shares - fees
        del self.positions[pos.symbol]
        return Fill("exit", pos.symbol, day, price, pos.shares, reason, fees, pos)

    def check_exit(self, pos: Position, day: str, bar: dict, entered_today: bool) -> Fill | None:
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        if entered_today:
            if l <= pos.stop:
                return self._close(pos, day, self.costs.stop_sell_fill(pos.stop), "stop-loss (same day)")
            return None
        if o <= pos.stop:
            return self._close(pos, day, self.costs.stop_sell_fill(o), "stop-loss (gap below stop)")
        if l <= pos.stop:
            return self._close(pos, day, self.costs.stop_sell_fill(pos.stop), "stop-loss")
        if o >= pos.target:
            return self._close(pos, day, o, "take-profit (gap above target)")
        if h >= pos.target:
            return self._close(pos, day, pos.target, "take-profit")
        pos.bars_held += 1
        if pos.bars_held >= self.max_hold_days:
            return self._close(pos, day, self.costs.market_sell_fill(c), f"time exit ({self.max_hold_days} sessions)")
        return None

    def process_day(self, day: str, bars: dict[str, dict]) -> list[Fill]:
        """Run one session: pending entries first (they are day orders), then exits."""
        fills: list[Fill] = []
        entered: set[str] = set()
        for order in self.pending:
            if order.symbol in self.positions or order.symbol not in bars:
                continue
            f = self.try_entry(order, day, bars[order.symbol])
            if f:
                fills.append(f)
                entered.add(order.symbol)
        self.pending = []  # unfilled day orders expire
        for sym, pos in list(self.positions.items()):
            if sym not in bars:
                continue
            f = self.check_exit(pos, day, bars[sym], sym in entered)
            if f:
                fills.append(f)
        return fills

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "pending": [asdict(p) for p in self.pending],
        }

    @classmethod
    def from_dict(cls, d: dict, costs: CostModel, max_hold_days: int) -> "PaperBroker":
        b = cls(d["cash"], costs, max_hold_days)
        b.positions = {k: Position(**v) for k, v in d.get("positions", {}).items()}
        b.pending = [PendingOrder(**p) for p in d.get("pending", [])]
        return b
