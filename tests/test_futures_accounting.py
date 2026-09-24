from decimal import Decimal

from koval_backtrader.futures_accounting import calculate_futures_snapshot

D = Decimal


def test_long_cross_margin_golden_vector_is_calculated_independently():
    result = calculate_futures_snapshot(
        initial_wallet_balance=D("100"),
        realized_pnl=D("0"),
        trading_fees=D("1"),
        funding=D("-1"),
        liquidation_fees=D("0"),
        side="buy",
        quantity=D("10"),
        entry_price=D("100"),
        mark_price=D("99"),
        leverage=D("10"),
        contract_size=D("1"),
        maintenance_rate=D("0.005"),
        maintenance_amount=D("0"),
    )

    assert result == {
        "margin_mode": "cross",
        "wallet_balance": D("98"),
        "unrealized_pnl": D("-10"),
        "equity": D("88"),
        "margin_used": D("99"),
        "available_balance": D("-11"),
        "position_margin_equity": D("88"),
        "maintenance_margin": D("4.950"),
        "estimated_liquidation_price": D("902") / D("9.95"),
        "liquidated": False,
    }


def test_short_cross_margin_golden_vector_is_calculated_independently():
    result = calculate_futures_snapshot(
        initial_wallet_balance=D("100"),
        realized_pnl=D("0"),
        trading_fees=D("1"),
        funding=D("2"),
        liquidation_fees=D("0"),
        side="sell",
        quantity=D("10"),
        entry_price=D("100"),
        mark_price=D("101"),
        leverage=D("10"),
        contract_size=D("1"),
        maintenance_rate=D("0.005"),
        maintenance_amount=D("0"),
    )

    assert result["wallet_balance"] == D("101")
    assert result["unrealized_pnl"] == D("-10")
    assert result["equity"] == D("91")
    assert result["maintenance_margin"] == D("5.050")
    assert result["estimated_liquidation_price"] == D("1101") / D("10.05")
    assert result["liquidated"] is False


def test_isolated_margin_vector_matches_the_published_accounting_contract():
    result = calculate_futures_snapshot(
        initial_wallet_balance=D("1000"),
        realized_pnl=D("0"),
        trading_fees=D("0"),
        funding=D("0"),
        liquidation_fees=D("0"),
        side="buy",
        quantity=D("1"),
        entry_price=D("100"),
        mark_price=D("85"),
        leverage=D("10"),
        contract_size=D("1"),
        maintenance_rate=D("0.005"),
        maintenance_amount=D("0"),
        margin_mode="isolated",
        isolated_margin=D("20"),
    )

    assert result["margin_mode"] == "isolated"
    assert result["equity"] == D("985")
    assert result["margin_used"] == D("20")
    assert result["available_balance"] == D("980")
    assert result["position_margin_equity"] == D("5")
    assert result["estimated_liquidation_price"] == D("80") / D("0.995")
    assert result["liquidated"] is False
