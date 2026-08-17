"""Tests for get_order_status's field mapping and its "not found" contract.

Real finding from a live read-only check against Tiendanube: querying
/orders for a number that matches nothing returns HTTP 404, not an empty
200 list. The pre-existing `if not orders: return {"found": False, ...}`
branch was therefore dead code for the single most common real case (a
mistyped or nonexistent order number) -- it always raised instead, which
made a normal "this order doesn't exist" outcome look like an
infrastructure failure and skip the deterministic order-not-found handoff.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import tiendanube_tools  # noqa: E402


def _http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(response=response)


class GetOrderStatusTests(unittest.TestCase):
    @patch.object(tiendanube_tools, "_get")
    def test_404_from_the_search_endpoint_is_a_normal_not_found(self, get):
        get.side_effect = _http_error(404)
        result = tiendanube_tools.get_order_status("999999")
        self.assertEqual(result, {"found": False, "message": "No encontré esa orden."})

    @patch.object(tiendanube_tools, "_get")
    def test_empty_list_is_also_treated_as_not_found(self, get):
        get.return_value = []
        result = tiendanube_tools.get_order_status("999999")
        self.assertEqual(result, {"found": False, "message": "No encontré esa orden."})

    @patch.object(tiendanube_tools, "_get")
    def test_a_real_infrastructure_error_still_propagates(self, get):
        get.side_effect = _http_error(500)
        with self.assertRaises(requests.exceptions.HTTPError):
            tiendanube_tools.get_order_status("1234")

    @patch.object(tiendanube_tools, "_get")
    def test_the_logistics_truth_is_read_from_the_fulfillment(self, get):
        # Measured against 40 real orders: shipping_option,
        # shipping_tracking_number and shipping_carrier_name are empty on ALL
        # of them, so the fields this used to return were always None and Fred
        # never gave a real tracking number. Everything real lives in
        # fulfillments[], including whether this is a shipment or a pickup --
        # a distinction order.shipping_status cannot make.
        get.return_value = [{
            "number": 1234,
            "payment_status": "paid",
            "shipping_status": "shipped",
            "status": "open",
            "shipping_option": None,
            "shipping_tracking_number": None,
            "total": "36000.00",
            "fulfillments": [{
                "status": "DISPATCHED",
                "shipping": {"type": "ship", "carrier": {"name": "Envío Nube"}},
                "tracking_info": {"code": "RR123456789AR", "url": "https://t.example/RR"},
            }],
        }]
        result = tiendanube_tools.get_order_status("1234")
        self.assertEqual(result, {
            "found": True,
            "order_number": 1234,
            "payment_status": "paid",
            "shipping_status": "shipped",
            "status": "open",
            "fulfillment_status": "DISPATCHED",
            "shipping_type": "ship",
            "carrier": "Envío Nube",
            "tracking": "RR123456789AR",
            "tracking_url": "https://t.example/RR",
            "total": "36000.00",
        })

    @patch.object(tiendanube_tools, "_get")
    def test_an_order_without_fulfillments_reports_none_rather_than_guessing(self, get):
        get.return_value = [{
            "number": 99, "payment_status": "pending", "shipping_status": "unshipped",
            "status": "open", "total": "1000.00", "fulfillments": [],
        }]
        result = tiendanube_tools.get_order_status("99")
        self.assertIsNone(result["fulfillment_status"])
        self.assertIsNone(result["shipping_type"])
        self.assertIsNone(result["tracking"])

    @patch.object(tiendanube_tools, "_get")
    def test_an_empty_tracking_code_is_reported_as_absent(self, get):
        # Tiendanube returns tracking_info {"code": "", "url": ""} while an
        # order is still being prepared. Empty string is not a tracking number.
        get.return_value = [{
            "number": 7, "payment_status": "paid", "shipping_status": "unpacked",
            "status": "open", "total": "1.00",
            "fulfillments": [{
                "status": "UNPACKED",
                "shipping": {"type": "pickup", "carrier": {"name": ""}},
                "tracking_info": {"code": "", "url": ""},
            }],
        }]
        result = tiendanube_tools.get_order_status("7")
        self.assertIsNone(result["tracking"])
        self.assertIsNone(result["carrier"])
        self.assertEqual(result["shipping_type"], "pickup")


if __name__ == "__main__":
    unittest.main()
