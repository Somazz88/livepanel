"""IbkrBroker against a fake gateway shaped like ib_async's objects.
This checks our mapping and threading; it does NOT prove the real ib_async calls behave the same."""
import itertools
from types import SimpleNamespace as NS

from livepanel.brokers.ibkr import IbkrBroker
from livepanel.models import AssetClass, Side


class FakeOrder:
    _ids = itertools.count(100)

    def __init__(self, action, totalQuantity, lmtPrice):
        self.action, self.totalQuantity, self.lmtPrice = action, totalQuantity, lmtPrice
        self.account, self.tif, self.outsideRth, self.orderId = "", "", False, 0


class FakeIB:
    def __init__(self):
        self.placed, self.cancelled = [], []
        opt = NS(secType="OPT", conId=111, symbol="INTC", lastTradeDateOrContractMonth="20261120", strike=120.0,
                 right="C", multiplier="100", currency="USD", exchange="", primaryExchange="", localSymbol="INTC  261120C00120000")
        stk = NS(secType="STK", conId=222, symbol="AAPL", lastTradeDateOrContractMonth="", strike=0.0, right="",
                 multiplier="", currency="USD", exchange="", primaryExchange="NASDAQ", localSymbol="AAPL")
        self.items = [NS(contract=opt, position=10.0, marketPrice=12.5, marketValue=12500.0, averageCost=1180.0,
                         unrealizedPNL=700.0, realizedPNL=0.0, account="U7654321"),
                      NS(contract=stk, position=-20.0, marketPrice=228.4, marketValue=-4568.0, averageCost=230.0,
                         unrealizedPNL=32.0, realizedPNL=0.0, account="U7654321")]

    def connect(self, host, port, clientId, readonly, timeout):
        self.readonly = readonly

    def reqMarketDataType(self, t):
        self.mdt = t

    def managedAccounts(self):
        return ["U7654321"]

    def accountSummary(self, a):
        return [NS(tag="NetLiquidation", value="50123.45", currency="USD", account=a)]

    def accountValues(self, a):
        return []

    def portfolio(self, a):
        return self.items

    def reqPnLSingle(self, a, model, conId):
        return NS(dailyPnL={111: 85.0, 222: float("nan")}[conId])

    def sleep(self, s):
        pass

    def reqMktData(self, c, gen, snap, reg):
        return {111: NS(bid=12.45, ask=12.65, bidSize=30, askSize=12, last=12.51),
                222: NS(bid=228.38, ask=228.41, bidSize=300, askSize=200, last=228.4)}[c.conId]

    def reqContractDetails(self, c):
        return [NS(marketRuleIds="32,32" if c.secType == "OPT" else "26,26")]

    def reqMarketRule(self, rid):
        return [NS(lowEdge=0.0, increment=0.01), NS(lowEdge=3.0, increment=0.05)] if rid == 32 else [NS(lowEdge=0.0, increment=0.01)]

    def placeOrder(self, c, o):
        if not o.orderId:
            o.orderId = next(FakeOrder._ids)
        self.placed.append((c.conId, o.action, o.totalQuantity, o.lmtPrice, o.outsideRth, o.account))
        return NS(contract=c, order=o, orderStatus=NS(status="Submitted", filled=0, avgFillPrice=0.0))

    def cancelOrder(self, o):
        self.cancelled.append(o.orderId)

    def disconnect(self):
        pass


def make(readonly=False):
    ib = FakeIB()
    b = IbkrBroker(readonly=readonly, ib_factory=lambda: ib, limit_order_cls=FakeOrder)
    b.connect()
    return b, ib


def test_read_side_mapping():
    b, ib = make(readonly=True)
    assert ib.readonly is True
    snap = b.snapshot()
    assert snap[0].account_id == "IB-4321" and snap[0].equity == 50123.45
    pos = {p.instrument.symbol: p for p in b.positions()}
    o = pos["INTC 11/20 120C"]
    assert o.instrument.asset_class == AssetClass.OPTION and o.instrument.multiplier == 100
    assert o.avg_price == 11.80                       # averageCost includes the multiplier
    assert o.today_pnl == 85.0
    s = pos["AAPL"]
    assert s.qty == -20 and s.today_pnl is None       # nan from IB -> unknown, not 0
    q = b.quotes([o.instrument, s.instrument])
    assert q["111"].tick == 0.05 and q["222"].tick == 0.01   # option above $3 trades in nickels
    assert q["111"].bid == 12.45
    b.close()


def test_readonly_refuses_orders():
    b, ib = make(readonly=True)
    p = b.positions()[0]
    try:
        b.place_limit(p.account_id, p.instrument, Side.SELL, 10, 12.55)
        raise AssertionError("should refuse")
    except Exception as e:
        assert "read-only" in str(e)
    assert not ib.placed


def test_order_round_trip_uses_real_account_and_outside_rth():
    b, ib = make(readonly=False)
    stk = [p for p in b.positions() if p.instrument.symbol == "AAPL"][0]
    oid = b.place_limit(stk.account_id, stk.instrument, Side.BUY, 20, 228.40, session="pre")
    assert ib.placed[-1] == (222, "BUY", 20, 228.40, True, "U7654321")
    b.modify_price(stk.account_id, oid, stk.instrument, 20, 228.41)
    assert ib.placed[-1][3] == 228.41 and len({x[0] for x in ib.placed}) == 1
    st = b.order_states(stk.account_id, [oid])
    assert st[oid].status == "working"
    b.cancel(stk.account_id, oid)
    assert ib.cancelled == [int(oid)]
