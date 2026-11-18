"""Universal broker plug + Interactive Brokers PAPER adapter, against a fake IB Gateway (no network)."""

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_ibkr import FakeLibrary  # noqa: E402

from vicekrack.trading.brokers import OrderIntent, check_intent, open_broker  # noqa: E402
from vicekrack.trading.brokers.ibkr import IBKRPaperBroker, check_config, load_ibkr_config  # noqa: E402
from vicekrack.trading.errors import TradingError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_ibkr_config(root=ROOT)


def code_of(call):
    try:
        call()
    except TradingError as error:
        return error.as_dict()["code"]
    return None


def edited(**connection):
    config = deepcopy(CONFIG)
    config["connection"].update(connection)
    return config


class PaperLockTests(unittest.TestCase):
    def test_shipped_config_is_paper_gateway(self):
        self.assertEqual(CONFIG["mode"], "ibkr_paper")
        self.assertEqual(CONFIG["connection"]["port"], 4002)
        self.assertFalse(CONFIG["kill_switch"]["engaged"])

    def test_live_ports_are_refused_by_name(self):
        for port in (4001, 7496):
            self.assertEqual(code_of(lambda: check_config(edited(port=port))), "broker_live_port_refused")

    def test_other_ports_hosts_and_modes_are_refused(self):
        self.assertEqual(code_of(lambda: check_config(edited(port=8080))), "broker_port_not_paper")
        self.assertEqual(code_of(lambda: check_config(edited(host="10.0.0.5"))), "broker_host_not_local")
        live = deepcopy(CONFIG)
        live["mode"] = "ibkr_live"
        self.assertEqual(code_of(lambda: check_config(live)), "broker_not_paper")
        self.assertIsNone(code_of(lambda: check_config(edited(port=7497))))      # TWS paper is fine

    def test_non_paper_account_disconnects_immediately(self):
        library = FakeLibrary(accounts=("U7654321",))
        broker = IBKRPaperBroker(CONFIG, library=library)
        self.assertEqual(code_of(broker.connect), "broker_not_paper_account")
        self.assertIn(("disconnect",), library.world.calls)
        self.assertIsNone(broker.ib)
        mixed = FakeLibrary(accounts=("DU1", "U2"))
        self.assertEqual(code_of(IBKRPaperBroker(CONFIG, library=mixed).connect), "broker_not_paper_account")

    def test_gateway_not_running_gives_plain_message(self):
        library = FakeLibrary()
        library.world.down = True
        self.assertEqual(code_of(IBKRPaperBroker(CONFIG, library=library).connect), "broker_unreachable")

    def test_registry_opens_ibkr_and_rejects_unknown(self):
        broker = open_broker("ibkr", root=ROOT, library=FakeLibrary())
        self.assertEqual((broker.name, broker.mode, broker.readonly), ("ibkr", "paper", True))
        self.assertEqual(code_of(lambda: open_broker("kraken-live")), "broker_unknown")


class ReadTests(unittest.TestCase):
    def test_account_positions_and_orders_are_sanitized(self):
        library = FakeLibrary()
        with IBKRPaperBroker(CONFIG, library=library) as broker:
            account = broker.account()
            positions = broker.positions()
            orders = broker.open_orders()
        self.assertEqual(library.world.calls[0], ("connect", "127.0.0.1", 4002, 2, True))     # read-only by default
        self.assertNotIn("DU1234567", json.dumps(account))
        self.assertTrue(account["account"].startswith("ibkr-paper-"))
        self.assertEqual((account["net_liquidation"], account["cash"], account["currency"]), ("1000000", "999500.5", "USD"))
        self.assertEqual(positions, [{"symbol": "MU", "security": "STK", "quantity": "1", "average_cost": "101.25"}])
        self.assertEqual(orders, [])
        self.assertEqual(library.world.calls[-1], ("disconnect",))


class OrderTests(unittest.TestCase):
    def broker(self, readonly=False, config=CONFIG, real=False):
        self.library = FakeLibrary(real=real)
        return IBKRPaperBroker(config, library=self.library, readonly=readonly).connect()

    def test_read_only_connection_cannot_order(self):
        broker = self.broker(readonly=True)
        self.assertEqual(code_of(lambda: broker.place(OrderIntent("MU", "buy", 1, reference_price="100"))), "broker_read_only")
        self.assertFalse([c for c in self.library.world.calls if c[0] == "placeOrder"])

    def test_kill_switch_blocks_new_orders(self):
        config = deepcopy(CONFIG)
        config["kill_switch"]["engaged"] = True
        broker = self.broker(config=config)
        self.assertEqual(code_of(lambda: broker.place(OrderIntent("MU", "buy", 1, reference_price="100"))), "broker_kill_switch")

    def test_market_buy_then_protective_stop_then_cancel(self):
        broker = self.broker()
        buy = broker.place(OrderIntent("MU", "buy", 1, reference_price="100", tag="vk-test-buy"))
        self.assertEqual((buy["mode"], buy["status"], buy["order_type"]), ("paper", "PreSubmitted", "market"))
        stop = broker.place(OrderIntent("MU", "sell", 1, order_type="stop", stop_price="95.50", time_in_force="gtc"))
        sent = self.library.world.trades[-1].order
        self.assertEqual((sent.orderType, sent.auxPrice, sent.tif, sent.account), ("STP", 95.5, "GTC", "DU1234567"))
        self.assertEqual([o["order_id"] for o in broker.open_orders()], [buy["order_id"], stop["order_id"]])
        self.assertEqual(broker.cancel(stop["order_id"])["status"], "cancel_requested")
        self.assertEqual(code_of(lambda: broker.cancel(999)), "broker_order_not_found")

    def test_works_with_real_ib_async_order_classes(self):
        try:
            import ib_async  # noqa: F401
        except ImportError:
            self.skipTest("ib_async not installed")
        broker = self.broker(real=True)
        result = broker.place(OrderIntent("AAPL", "buy", 2, order_type="limit", limit_price="190.10"))
        sent = self.library.world.trades[-1].order
        self.assertEqual((type(sent).__name__, sent.orderType, sent.lmtPrice, sent.orderRef), ("LimitOrder", "LMT", 190.1, "vicekrack"))
        self.assertEqual(result["quantity"], 2)

    def test_unknown_symbol_is_refused_before_sending(self):
        broker = self.broker()
        self.assertEqual(code_of(lambda: broker.place(OrderIntent("ZZZZ", "buy", 1, reference_price="5"))), "broker_unknown_symbol")


class IntentRuleTests(unittest.TestCase):
    LIMITS = {"max_quantity": 500, "max_order_dollars": "2500.00"}

    def test_rules(self):
        cases = [
            (OrderIntent("mu", "buy", 1, reference_price="1"), "broker_bad_symbol"),
            (OrderIntent("MU", "short", 1, reference_price="1"), "broker_bad_side"),
            (OrderIntent("MU", "buy", 0, reference_price="1"), "broker_bad_quantity"),
            (OrderIntent("MU", "buy", 501, reference_price="1"), "broker_limit_quantity"),
            (OrderIntent("MU", "buy", 30, reference_price="100"), "broker_limit_dollars"),     # $3,000 > $2,500
            (OrderIntent("MU", "buy", 1), "broker_missing_price"),
            (OrderIntent("MU", "buy", 1, order_type="limit"), "broker_missing_price"),
            (OrderIntent("MU", "buy", 1, order_type="stop", stop_price="90"), "broker_bad_stop"),
            (OrderIntent("MU", "buy", 1, order_type="trailing", reference_price="1"), "broker_bad_order_type"),
        ]
        for intent, expected in cases:
            self.assertEqual(code_of(lambda: check_intent(intent, self.LIMITS)), expected, intent)
        self.assertTrue(check_intent(OrderIntent("BRK.B", "buy", 5, reference_price="450"), self.LIMITS))


class CommandTests(unittest.TestCase):
    def test_check_needs_allow_network(self):
        import io
        from contextlib import redirect_stdout
        from vicekrack.trading.brokers.cli import main
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["ibkr-paper-check"])
        self.assertEqual((code, json.loads(out.getvalue())["error"]["code"]), (1, "network_not_allowed"))

    def test_no_credentials_in_config(self):
        text = (ROOT / "config/broker.ibkr.paper.json").read_text().lower()
        for word in ("password", "secret", "token", "api_key", "username"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
