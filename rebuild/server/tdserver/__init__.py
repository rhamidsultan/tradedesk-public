"""TradeDesk rebuild server (phase 1: foundations).

Sign-in for the iPhone app with Secure Enclave device keys, the daily Kite login, the market calendar,
alerts through Telegram and ntfy, and the web safety console. Nothing in this package can place, modify
or cancel an order: order code arrives in phase 5 behind its own review.
"""

__version__ = "0.1.0"
