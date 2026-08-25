"""
exchange/ipo.py — initial public offerings: bookbuild, pricing, allocation.

The lifecycle, mirroring a real deal:

    ANNOUNCED   session open: symbol, price range, shares, window published
    OPEN        subscription window: bots submit (quantity, max_price)
    PRICED      window closes: the offer prices at the highest level that
                fills the book (undersubscribed → bottom of range);
                oversubscribed → pro-rata allocation; cash debited
    LISTED      the security starts trading AT the offer price while its
                (hidden) fundamental sits wherever the draw put it — the
                first hour of trading IS the discovery of the pop

The pop: true value = offer × exp(N(POP_MU, POP_SIGMA)), drawn
deterministically from (symbol, FUNDAMENTAL_SEED) so every venue and every
process agrees. With the defaults, the average deal pops ~+12% and roughly
a quarter break issue — leaving money on the table vs the winner's curse,
which is the whole lesson.

Pure logic, no I/O: the exchange drives the state machine from its tick
loop and owns the cash/position writes.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from dataclasses import dataclass, field

import plugins.securities.defaults as securities

#: Distribution of ln(true_value / offer_price). Mean +12%, wide tails.
POP_MU = 0.12
POP_SIGMA = 0.18

#: A single indication may not exceed this fraction of the deal (audit C8):
#: uncapped, one bot returning a huge quantity (e.g. 10,000,000 on a 4,000-
#: share deal) swamped the pro-rata weights to corner the book, or — with no
#: cash — forced the clearing price to the top of the range and voided the
#: deal for everyone. 1.0 means "you cannot indicate for more shares than
#: exist"; combined with the collateral check in the exchange handler this
#: restores the winner's-curse trade-off. Enforced in server._handle_ipo_
#: subscribe (not here) so allocation-math unit tests can set up freely.
MAX_INDICATION_FRACTION = float(os.environ.get("IPO_MAX_INDICATION_FRACTION", "1.0"))


def pop_factor(symbol: str, seed: str | None = None) -> float:
    """true_value / offer multiplier, deterministic in (symbol, seed)."""
    seed = seed if seed is not None else securities.FUNDAMENTAL_SEED
    digest = hashlib.sha256(f"ipo:{symbol}:{seed}".encode()).digest()
    # Box–Muller from two uniform draws off the hash.
    u1 = max(1e-12, struct.unpack("<Q", digest[:8])[0] / 2**64)
    u2 = struct.unpack("<Q", digest[8:16])[0] / 2**64
    z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
    return math.exp(POP_MU + POP_SIGMA * z)


@dataclass
class IPOOffering:
    """One deal's book and state."""

    symbol: str
    name: str
    shares: int
    range_lo: float
    range_hi: float
    open_tick: int
    close_tick: int
    list_tick: int
    state: str = "announced"       # announced|open|priced|listed
    #: bot_id → (quantity, max_price). Resubmission REPLACES — one
    #: indication per bot, like a real book.
    subs: dict[str, tuple[int, float]] = field(default_factory=dict)
    offer_price: float | None = None
    allocations: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_event(cls, ev: dict) -> "IPOOffering | None":
        try:
            lo, hi = ev["offer_range"][:2]
            window = ev.get("window") or [0, 0]
            return cls(
                symbol=str(ev["symbol"]),
                name=str(ev.get("name", ev["symbol"])),
                shares=int(ev["shares"]),
                range_lo=float(lo), range_hi=float(hi),
                open_tick=int(window[0]), close_tick=int(window[1]),
                list_tick=int(ev["tick"]),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    # Bookbuild
    # ------------------------------------------------------------------

    def subscribe(self, bot_id: str, quantity: int,
                  max_price: float) -> tuple[bool, str]:
        if self.state != "open":
            return False, f"{self.symbol} book is not open (state={self.state})"
        if quantity <= 0:
            return False, "quantity must be positive"
        px = min(max(float(max_price), self.range_lo), self.range_hi)
        self.subs[bot_id] = (int(quantity), px)
        return True, (f"indication recorded: {quantity} {self.symbol} "
                      f"up to ${px:.2f}")

    def demand_at(self, price: float) -> int:
        return sum(q for q, mp in self.subs.values() if mp >= price)

    def price_and_allocate(self, cash_of) -> None:
        """Set the offer price and allocate pro-rata.

        `cash_of(bot_id) -> float` caps each allocation at what the bot can
        actually pay; freed shares go back to the issuer (unsold), never to
        other bidders — nobody gets more than their indication.
        """
        candidates = sorted({mp for _, mp in self.subs.values()}
                            | {self.range_lo}, reverse=True)
        offer = self.range_lo
        for p in candidates:
            if self.demand_at(p) >= self.shares:
                offer = p
                break
        self.offer_price = offer

        eligible = {b: q for b, (q, mp) in self.subs.items() if mp >= offer}
        total = sum(eligible.values())
        remaining = self.shares
        allocs: dict[str, int] = {}
        if total <= remaining:
            allocs = dict(eligible)
        else:
            # Pro-rata with largest remainder, ordered for determinism.
            shares_f = {b: q * remaining / total for b, q in eligible.items()}
            allocs = {b: int(v) for b, v in shares_f.items()}
            left = remaining - sum(allocs.values())
            for b in sorted(shares_f,
                            key=lambda b: (shares_f[b] - int(shares_f[b])),
                            reverse=True):
                if left <= 0:
                    break
                allocs[b] += 1
                left -= 1
        # Affordability cap, applied last.
        for b in list(allocs):
            afford = int(max(0.0, cash_of(b)) // offer)
            allocs[b] = min(allocs[b], afford)
        self.allocations = {b: q for b, q in allocs.items() if q > 0}
        self.state = "priced"

    def to_public(self) -> dict:
        """What everyone may see. The book's contents stay private."""
        return {
            "symbol": self.symbol, "name": self.name, "shares": self.shares,
            "offer_range": [self.range_lo, self.range_hi],
            "window": [self.open_tick, self.close_tick],
            "list_tick": self.list_tick, "state": self.state,
            "offer_price": self.offer_price,
        }
