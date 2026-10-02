from datetime import date

from livepanel.brokers.moomoo import classify_flow, instrument_from_code
from livepanel.models import AssetClass


def test_option_code():
    i = instrument_from_code("US.AAPL261120C75000", "USD")
    assert i.asset_class == AssetClass.OPTION and i.underlying == "AAPL"
    assert i.strike == 75.0 and i.right == "C" and i.expiry == date(2026, 11, 20) and i.multiplier == 100
    assert i.symbol == "AAPL 11/20 75C"


def test_dotted_underlying_and_fractional_strike():
    i = instrument_from_code("US.BRK.B261218P487500", "USD")
    assert i.underlying == "BRK.B" and i.strike == 487.5 and i.right == "P"


def test_stocks_and_other_markets():
    assert instrument_from_code("US.INTC", "USD").asset_class == AssetClass.STOCK
    my = instrument_from_code("MY.4456", "MYR")
    assert my.market == "MY" and my.currency == "MYR" and my.asset_class == AssetClass.STOCK


def test_cash_flow_classification():
    assert classify_flow("Bank Transfer Deposits", "") == "deposit"
    assert classify_flow("Bank Transfer Withdrawals", "") == "withdrawal"
    assert classify_flow("Others", "INTER-ACCOUNT TRANSFER TO 1234") == "other"     # internal move, not a withdrawal
    assert classify_flow("Auto Currency Exchange", "MYR -> USD") == "fx"
    assert classify_flow("Others", "WISE_PAY 123") == "deposit"
    assert classify_flow("Coupon Deposit", "") == "other"
    assert classify_flow("Others", "") == "other"
