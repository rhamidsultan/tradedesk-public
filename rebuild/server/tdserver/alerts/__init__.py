"""Alerts: an outbox in the database, sent by the worker through Telegram and ntfy.

The free Apple account cannot use Apple push, so the phone hears about problems through these two apps.
Critical alerts repeat until acknowledged (from the app, the console, or the link in the message).
"""
