"""The Dip watch's rule in one place: dipwatch.py paper-trades it and trader.py trades it for real, so the exits and the
two kinds of entry must be the same for both. Imports nothing, so the trader (little memory) can load it."""

TP, SL, HOLD = 0.5, 0.5, 30     # sold at x1.5, x0.5 or after 30 minutes
DELAY_MS = 1500                 # the paper trade buys 1.5 s after the signal's price reading
SIGNAL_FOR = 20                 # s: the trader acts on an entry only this long after its reading (the price moves fast after a dip)

# dipwatch.py records two kinds of entry, "dip" (age, market cap and the fall) and "full" (a dip that also has the 5+ SOL buy
# and the 110+ SOL pool). Each is a strategy of the Trading tab, switched ON or OFF there like the Lab's strategies. (Names have no
# colon: the Trading tab's tables cut a name at the first one, which would make the two look alike.)
STRATEGIES = {
    "full": {"id": "dip-watch-full", "name": "Dip watch full signal", "tp": TP, "sl": SL, "hold": HOLD,
             "note": "Buys on the Dip watch's full signal (all five conditions)"},
    "dip": {"id": "dip-watch-dip", "name": "Dip watch dip entry", "tp": TP, "sl": SL, "hold": HOLD,
            "note": "Buys on the Dip watch's dip entry"},
}
KIND_OF = {s["id"]: kind for kind, s in STRATEGIES.items()}
