"""Order management (architecture §6). The only package that can send real orders, and only through
ZerodhaAdapter, which is constructed solely by live mode on the server. Everything is tested against FakeBroker."""
from .broker import (BrokerError, Rejected, NetworkUnknown, TokenExpired, RateLimited, OrderReq, OrderState, Quote)
from .oms import OrderManager, Limits, LegReq
