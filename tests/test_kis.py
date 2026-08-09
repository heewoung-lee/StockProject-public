import sys
import unittest
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.kis import KisApiError, KisCredentials, KisRequest, KisVtsClient, urllib_transport
from stockbot.models import Order


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = []

    def push(self, response):
        self.responses.append(response)

    def __call__(self, request):
        self.calls.append(request)
        if not self.responses:
            return {"rt_cd": "0", "output": {}}
        return self.responses.pop(0)


def credentials():
    return KisCredentials(
        app_key="test-app-key",
        app_secret="test-app-secret",
        account_no="12345678",
        account_product_code="01",
    )


class KisVtsClientTest(unittest.TestCase):
    def test_issue_access_token_posts_client_credentials_to_vts_token_endpoint(self):
        transport = FakeTransport()
        transport.push({"access_token": "token-123", "token_type": "Bearer"})
        client = KisVtsClient(credentials(), transport=transport)

        token = client.issue_access_token()

        self.assertEqual("token-123", token)
        request = transport.calls[0]
        self.assertEqual("POST", request.method)
        self.assertEqual("/oauth2/tokenP", request.path)
        self.assertEqual("client_credentials", request.json["grant_type"])
        self.assertEqual("test-app-key", request.json["appkey"])
        self.assertEqual("test-app-secret", request.json["appsecret"])

    def test_inquire_price_uses_domestic_stock_price_endpoint_and_tr_id(self):
        transport = FakeTransport()
        transport.push({"access_token": "token-123"})
        transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000"}})
        client = KisVtsClient(credentials(), transport=transport)
        client.issue_access_token()

        response = client.inquire_price("005930")

        self.assertEqual("70000", response["output"]["stck_prpr"])
        request = transport.calls[1]
        self.assertEqual("GET", request.method)
        self.assertEqual("/uapi/domestic-stock/v1/quotations/inquire-price", request.path)
        self.assertEqual("FHKST01010100", request.headers["tr_id"])
        self.assertEqual("Bearer token-123", request.headers["authorization"])
        self.assertEqual("J", request.params["FID_COND_MRKT_DIV_CODE"])
        self.assertEqual("005930", request.params["FID_INPUT_ISCD"])

    def test_inquire_balance_uses_vts_balance_tr_id_and_account_params(self):
        transport = FakeTransport()
        transport.push({"access_token": "token-123"})
        transport.push({"rt_cd": "0", "output1": [], "output2": []})
        client = KisVtsClient(credentials(), transport=transport)
        client.issue_access_token()

        client.inquire_balance()

        request = transport.calls[1]
        self.assertEqual("GET", request.method)
        self.assertEqual("/uapi/domestic-stock/v1/trading/inquire-balance", request.path)
        self.assertEqual("VTTC8434R", request.headers["tr_id"])
        self.assertEqual("12345678", request.params["CANO"])
        self.assertEqual("01", request.params["ACNT_PRDT_CD"])
        self.assertEqual("N", request.params["AFHR_FLPR_YN"])
        self.assertEqual("02", request.params["INQR_DVSN"])
        self.assertEqual("00", request.params["PRCS_DVSN"])

    def test_price_bar_maps_inquire_price_response_to_market_bar(self):
        timestamp = datetime(2026, 6, 10, 9, 3, tzinfo=timezone.utc)
        transport = FakeTransport()
        transport.push({"access_token": "token-123"})
        transport.push(
            {
                "rt_cd": "0",
                "output": {
                    "stck_prpr": "70000",
                    "stck_oprc": "69500",
                    "stck_hgpr": "70500",
                    "stck_lwpr": "69000",
                    "acml_vol": "123456",
                },
            }
        )
        client = KisVtsClient(credentials(), transport=transport)
        client.issue_access_token()

        bar = client.price_bar("005930", timestamp=timestamp)

        self.assertEqual("005930", bar.symbol)
        self.assertEqual(Decimal("70000"), bar.close)
        self.assertEqual(123456, bar.volume)
        self.assertEqual(timestamp, bar.timestamp)
        self.assertEqual(["/oauth2/tokenP", "/uapi/domestic-stock/v1/quotations/inquire-price"], [call.path for call in transport.calls])

    def test_account_snapshot_maps_inquire_balance_response_without_order_endpoint(self):
        timestamp = datetime(2026, 6, 10, 9, 4, tzinfo=timezone.utc)
        transport = FakeTransport()
        transport.push({"access_token": "token-123"})
        transport.push(
            {
                "rt_cd": "0",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "2", "pchs_avg_pric": "69000", "prpr": "70000"},
                ],
                "output2": [{"dnca_tot_amt": "1000000"}],
            }
        )
        client = KisVtsClient(credentials(), transport=transport)
        client.issue_access_token()

        snapshot = client.account_snapshot(timestamp=timestamp)

        self.assertEqual(Decimal("1000000"), snapshot.cash)
        self.assertEqual(["005930"], list(snapshot.positions))
        self.assertEqual(Decimal("1140000"), snapshot.equity)
        self.assertNotIn("/uapi/domestic-stock/v1/trading/order-cash", [call.path for call in transport.calls])

    def test_place_cash_order_maps_buy_and_sell_to_vts_tr_ids(self):
        transport = FakeTransport()
        transport.push({"access_token": "token-123"})
        transport.push({"rt_cd": "0", "output": {"ODNO": "1"}})
        transport.push({"rt_cd": "0", "output": {"ODNO": "2"}})
        client = KisVtsClient(credentials(), transport=transport, allow_order_placement=True)
        client.issue_access_token()

        client.place_cash_order(Order.buy("005930", 3, "entry"), order_price=Decimal("70000"))
        client.place_cash_order(Order.sell("005930", 2, "exit"), order_price=Decimal("71000"))

        buy_request = transport.calls[1]
        sell_request = transport.calls[2]
        self.assertEqual("VTTC0012U", buy_request.headers["tr_id"])
        self.assertEqual("VTTC0011U", sell_request.headers["tr_id"])
        self.assertEqual("/uapi/domestic-stock/v1/trading/order-cash", buy_request.path)
        self.assertEqual("005930", buy_request.json["PDNO"])
        self.assertEqual("3", buy_request.json["ORD_QTY"])
        self.assertEqual("70000", buy_request.json["ORD_UNPR"])
        self.assertEqual("00", buy_request.json["ORD_DVSN"])
        self.assertEqual("00", sell_request.json["ORD_DVSN"])
        self.assertEqual("KRX", buy_request.json["EXCG_ID_DVSN_CD"])
        self.assertEqual("01", sell_request.json["SLL_TYPE"])

    def test_api_error_raises_with_code_and_message(self):
        transport = FakeTransport()
        transport.push({"access_token": "token-123"})
        transport.push({"rt_cd": "1", "msg_cd": "EGW001", "msg1": "failed"})
        client = KisVtsClient(credentials(), transport=transport)
        client.issue_access_token()

        with self.assertRaisesRegex(KisApiError, "EGW001"):
            client.inquire_price("005930")

    def test_credentials_repr_does_not_leak_secret(self):
        rendered = repr(credentials())

        self.assertNotIn("test-app-key", rendered)
        self.assertNotIn("test-app-secret", rendered)
        self.assertNotIn("12345678", rendered)

    def test_request_repr_does_not_leak_secret_or_bearer_token(self):
        transport = FakeTransport()
        transport.push({"access_token": "token-123"})
        transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000"}})
        client = KisVtsClient(credentials(), transport=transport)
        client.issue_access_token()
        client.inquire_price("005930")

        token_request_repr = repr(transport.calls[0])
        price_request_repr = repr(transport.calls[1])

        self.assertNotIn("test-app-secret", token_request_repr)
        self.assertNotIn("token-123", price_request_repr)
        self.assertNotIn("Bearer", price_request_repr)
        self.assertNotIn("authorization", price_request_repr)
        self.assertNotIn("params=", price_request_repr)
        self.assertNotIn("json=", token_request_repr)

    def test_client_rejects_non_vts_base_url(self):
        with self.assertRaisesRegex(ValueError, "KIS VTS client only supports"):
            KisVtsClient(credentials(), base_url="https://openapi.koreainvestment.com:9443")

    def test_cash_order_requires_explicit_order_placement_gate(self):
        transport = FakeTransport()
        transport.push({"access_token": "token-123"})
        client = KisVtsClient(credentials(), transport=transport)
        client.issue_access_token()
        call_count_before_order = len(transport.calls)

        with self.assertRaisesRegex(ValueError, "allow_order_placement=True"):
            client.place_cash_order(Order.buy("005930", 1, "entry"), order_price=Decimal("70000"))
        self.assertEqual(call_count_before_order, len(transport.calls))

    def test_production_code_does_not_enable_vts_order_placement(self):
        root = Path(__file__).resolve().parents[1]
        offenders = []
        pattern = re.compile(r"KisVtsClient\([^)]*allow_order_placement\s*=\s*True", re.DOTALL)
        for path in (root / "src" / "stockbot").glob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)

        self.assertEqual([], offenders)

    def test_urllib_transport_wraps_read_timeout_as_kis_api_error(self):
        kis_request = KisRequest(
            method="GET",
            base_url="https://openapivts.koreainvestment.com:29443",
            path="/uapi/domestic-stock/v1/quotations/inquire-price",
            headers={},
            timeout=1.0,
        )

        with patch("stockbot.kis.urllib.request.urlopen", side_effect=TimeoutError("The read operation timed out")):
            with self.assertRaisesRegex(KisApiError, "KIS network timeout"):
                urllib_transport(kis_request)


if __name__ == "__main__":
    unittest.main()
