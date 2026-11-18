"""A fake IB Gateway for tests: the same method names and shapes ib_async uses, no network.

FakeLibrary() mimics the `ib_async` module (IB, Stock, MarketOrder, LimitOrder, StopOrder).
With real=True it uses the real ib_async contract/order classes (if installed) with the fake IB.
"""

from types import SimpleNamespace


class _Order(SimpleNamespace):
    pass


def _order(order_type, action, quantity, **extra):
    return _Order(orderType=order_type, action=action, totalQuantity=quantity, orderId=0, tif="",
                  outsideRth=False, orderRef="", account="", **extra)


class FakeIB:
    def __init__(self, world):
        self.world = world
        self.connected = False

    def connect(self, host, port, clientId=1, timeout=4, readonly=False, account=""):
        self.world.calls.append(("connect", host, port, clientId, readonly))
        if self.world.down:
            raise ConnectionRefusedError("gateway not running")
        self.connected = True

    def disconnect(self):
        self.connected = False
        self.world.calls.append(("disconnect",))

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return list(self.world.accounts)

    def accountSummary(self, account=""):
        return [SimpleNamespace(account=account, tag=t, value=v, currency=c, modelCode="")
                for t, v, c in self.world.summary]

    def positions(self, account=""):
        return [SimpleNamespace(account=self.world.accounts[0], contract=SimpleNamespace(symbol=s, secType="STK"),
                                position=q, avgCost=c) for s, q, c in self.world.positions]

    def openTrades(self):
        return list(self.world.trades)

    def qualifyContracts(self, *contracts):
        out = []
        for c in contracts:
            if c.symbol in self.world.known:
                c.conId = 1000 + len(c.symbol)
                out.append(c)
        return out

    def placeOrder(self, contract, order):
        self.world.next_id += 1
        order.orderId = self.world.next_id
        trade = SimpleNamespace(contract=contract, order=order, orderStatus=SimpleNamespace(status="PreSubmitted", filled=0.0))
        self.world.trades.append(trade)
        self.world.calls.append(("placeOrder", contract.symbol, order.action, order.totalQuantity, order.orderType))
        return trade

    def cancelOrder(self, order):
        self.world.calls.append(("cancelOrder", order.orderId))
        self.world.trades = [t for t in self.world.trades if t.order.orderId != order.orderId]


class FakeLibrary:
    def __init__(self, accounts=("DU1234567",), real=False):
        self.world = SimpleNamespace(accounts=list(accounts), down=False, calls=[], trades=[], next_id=0,
                                     known={"MU", "AAPL", "BRK.B"},
                                     summary=[("NetLiquidation", "1000000.0", "USD"), ("TotalCashValue", "999500.5", "USD"),
                                              ("BuyingPower", "4000000", "USD"), ("NetLiquidation", "1000000.0", "BASE")],
                                     positions=[("MU", 1.0, 101.25)])
        if real:
            import ib_async
            self.Stock, self.MarketOrder = ib_async.Stock, ib_async.MarketOrder
            self.LimitOrder, self.StopOrder = ib_async.LimitOrder, ib_async.StopOrder

    def IB(self):
        return FakeIB(self.world)

    @staticmethod
    def Stock(symbol, exchange, currency):
        return SimpleNamespace(symbol=symbol, exchange=exchange, currency=currency, secType="STK", conId=0)

    @staticmethod
    def MarketOrder(action, quantity):
        return _order("MKT", action, quantity)

    @staticmethod
    def LimitOrder(action, quantity, price):
        return _order("LMT", action, quantity, lmtPrice=price)

    @staticmethod
    def StopOrder(action, quantity, price):
        return _order("STP", action, quantity, auxPrice=price)
