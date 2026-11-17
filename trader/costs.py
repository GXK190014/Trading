"""Transaction-cost model: spread, slippage and regulatory sell fees.

Robinhood charges no commission on stock trades, but every round trip still pays
the bid/ask spread, some slippage (worse on stop-market exits, which fill as
market orders in a falling market), and small SEC/FINRA fees on sells.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    commission_per_order: float = 0.0
    min_half_spread: float = 0.01
    half_spread_pct: float = 0.0005
    slippage_pct: float = 0.0005
    stop_slippage_pct: float = 0.0015
    sec_fee_rate: float = 0.0000278
    taf_per_share: float = 0.000166
    # Percentage cost on each side's notional. 0 for Robinhood stocks; for Robinhood crypto
    # (market-maker routing) the bid/ask markup measured on 2026-11-17 was ~0.95% per side.
    fee_pct: float = 0.0

    @classmethod
    def from_settings(cls, cfg: dict) -> "CostModel":
        return cls(**{k: float(v) for k, v in cfg.items() if k in cls.__dataclass_fields__})

    def half_spread(self, price: float) -> float:
        return max(self.min_half_spread, price * self.half_spread_pct)

    def market_buy_fill(self, price: float) -> float:
        return price + self.half_spread(price) + price * self.slippage_pct

    def market_sell_fill(self, price: float) -> float:
        return max(0.01, price - self.half_spread(price) - price * self.slippage_pct)

    def stop_sell_fill(self, trigger_or_open: float) -> float:
        """A stop-market sell fills below its trigger: spread plus extra slippage."""
        p = trigger_or_open
        return max(0.01, p - self.half_spread(p) - p * self.stop_slippage_pct)

    def sell_fees(self, price: float, shares: float) -> float:
        return ((self.sec_fee_rate + self.fee_pct) * price * shares + self.taf_per_share * shares
                + self.commission_per_order)

    def buy_fees(self, price: float, shares: float) -> float:
        return self.fee_pct * price * shares + self.commission_per_order

    def entry_cost_per_share(self, entry: float) -> float:
        return self.fee_pct * entry

    def stop_exit_cost_per_share(self, stop: float) -> float:
        """Expected loss beyond the stop price when a stop-market exit fills."""
        return (stop - self.stop_sell_fill(stop)) + self.sell_fees(stop, 1.0)

    def risk_per_share(self, entry: float, stop: float) -> float:
        """Everything lost per share if the stop is hit: distance + entry cost + exit costs."""
        return (entry - stop) + self.entry_cost_per_share(entry) + self.stop_exit_cost_per_share(stop)

    def net_reward_per_share(self, entry: float, target: float) -> float:
        return (target - entry) - self.entry_cost_per_share(entry) - self.sell_fees(target, 1.0)

    def target_for(self, entry: float, stop: float, reward_risk: float) -> float:
        """Lowest target whose reward, net of every cost, is `reward_risk` times the full risk."""
        need = entry + reward_risk * self.risk_per_share(entry, stop) + self.entry_cost_per_share(entry)
        need += self.taf_per_share + self.commission_per_order
        return need / (1.0 - self.sec_fee_rate - self.fee_pct)
