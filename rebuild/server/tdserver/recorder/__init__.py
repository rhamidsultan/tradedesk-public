"""Live market-data recording from Zerodha's price stream (read-only: subscribes to prices, places nothing).

1-minute bars go into the store's bars_1m (source kite_live) with best bid/ask in quotes_1m, the same layout
py/marketdata and py/engine already read. 1-second bars go into bars_1s and are kept for a set number of
trading days: Zerodha keeps no history finer than a minute, so a day not recorded cannot be fetched later.
"""
