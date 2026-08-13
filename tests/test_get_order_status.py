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
    def test_found_order_maps_every_field_including_tracking(self, get):
        get.return_value = [{
            "number": 1234,
            "payment_status": "paid",
            "shipping_status": "shipped",
            "status": "open",
            "shipping_option": "Correo Argentino",
            "shipping_tracking_number": "RR123456789AR",
            "total": "36000.00",
        }]
        result = tiendanube_tools.get_order_status("1234")
        self.assertEqual(result, {
            "found": True,
            "order_number": 1234,
            "payment_status": "paid",
            "shipping_status": "shipped",
            "status": "open",
            "shipping_method": "Correo Argentino",
            "tracking": "RR123456789AR",
            "total": "36000.00",
        })


if __name__ == "__main__":
    unittest.main()
