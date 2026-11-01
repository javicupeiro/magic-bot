"""Exercise catalogue behavior with in-memory HTTP responses only."""

import unittest
from unittest.mock import patch

import httpx

from backend.cards.mtg_api import BASE_URL, MtgApiClient


def printing(name="Sample", **changes):
    return {"id": name, "name": name, "cmc": 1, "colors": ["White"],
            "colorIdentity": ["W", "R"], "subtypes": ["Warrior"], **changes}


class CatalogueTests(unittest.TestCase):
    def api(self, handler, **config):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return MtgApiClient(client=client, retries=0, **config)

    def test_filter_comparators_zero_color_and_identity(self):
        cards = [printing(str(value), cmc=value) for value in (0, 1, 2)]
        cards += [printing("colorless", colors=[], cmc=0), printing("missing", cmc=None)]
        cases = [({"cmc": 0}, ["0", "colorless"], "0"),
                 ({"cmc": "0.0"}, ["0", "colorless"], "0"),
                 ({"cmc": "0.5"}, [], None),
                 ({"cmc": "<2"}, ["0", "1", "colorless"], None),
                 ({"cmc": "<=2"}, ["0", "1", "2", "colorless"], None),
                 ({"color": "W", "cmc": "<2", "subtype": "Warrior"}, ["0", "1"], None),
                 ({"color": "Red"}, [], None),
                 ({"color_identity": "R"}, [card["name"] for card in cards], None)]
        for filters, expected, remote_cost in cases:
            with self.subTest(filters=filters):
                def handler(request):
                    self.assertEqual(request.url.params.get("cmc"), remote_cost)
                    self.assertNotIn("colorIdentity", request.url.params)
                    if filters.get("color"):
                        self.assertEqual(request.url.params["colors"], {"W": "W", "Red": "R"}[filters["color"]])
                    return httpx.Response(200, json={"cards": cards})
                result = self.api(handler).search_cards(**filters)
                self.assertEqual([card.name for card in result.items], expected)
                self.assertEqual(result.filters["requested"]["cmc"], filters.get("cmc"))
                self.assertEqual(result.status, "ok" if expected else "empty")

    def test_pagination_deduplication_and_source_preservation(self):
        calls = []
        def handler(request):
            page = int(request.url.params["page"])
            calls.append(page)
            self.assertEqual(request.url.host, "api.magicthegathering.io")
            return httpx.Response(200, json={"cards": [printing(id=str(page), set=str(page),
                                 text=f"Text {page}", imageUrl=f"https://example.org/{page}")]},
                                 headers={"Total-Count": "2"})
        result = self.api(handler, page_size=1, max_pages=2).search_cards(cmc="<2")
        self.assertEqual(calls, [1, 2])
        self.assertFalse(result.partial)
        card, = result.items
        self.assertEqual([raw["id"] for raw in card.printings], ["1", "2"])
        self.assertEqual(card.printings[1]["text"], "Text 2")
        self.assertEqual(card.image_url, "https://example.org/1")
        result = self.api(handler, page_size=1, max_pages=1).search_cards(cmc=0)
        self.assertEqual((result.status, result.partial, result.items), ("partial", True, []))

    def test_links_and_unknown_totals_never_claim_early_completion(self):
        for headers in ({}, {"Link": f'<{BASE_URL}/cards?page=2>; rel="next"'}):
            with self.subTest(headers=headers):
                result = self.api(lambda _: httpx.Response(200, json={"cards": [printing()]}, headers=headers),
                                  page_size=1, max_pages=1).search_cards()
                self.assertTrue(result.partial)
        result = self.api(lambda _: httpx.Response(200, json={"cards": []}, headers={
            "Total-Count": "0", "Link": f'<{BASE_URL}/cards?page=2>; rel="next"'})).search_cards()
        self.assertEqual((result.status, result.partial, result.pages_fetched), ("empty", False, 1))

    def test_errors_are_distinct_from_empty_and_retry_only_transient_failures(self):
        cases = [(httpx.ReadTimeout("slow"), "timeout", None, 2),
                 (httpx.ConnectError("offline"), "network", None, 2),
                 (httpx.Response(503), "http", 503, 2),
                 (httpx.Response(429), "http", 429, 2),
                 (httpx.Response(403, text="Rate Limit Exceeded"), "http", 403, 2),
                 (httpx.Response(403), "http", 403, 1),
                 (httpx.Response(404), "http", 404, 1),
                 (httpx.Response(200, text="not JSON"), "invalid_response", None, 1),
                 (httpx.Response(200, json={"cards": [{}]}), "invalid_response", None, 1)]
        for response, kind, status, attempts in cases:
            with self.subTest(kind=kind, status=status, response=response):
                calls = []
                def handler(request):
                    calls.append(request)
                    if isinstance(response, Exception):
                        raise response
                    return response
                api = self.api(handler)
                api.retries = 1
                with patch("backend.cards.mtg_api.time.sleep"):
                    result = api.search_cards()
                self.assertEqual((result.status, result.partial), ("error", False))
                self.assertEqual((result.error.kind, result.error.status_code), (kind, status))
                self.assertEqual(len(calls), attempts)
        result = self.api(lambda _: httpx.Response(200, json={"cards": []})).search_cards()
        self.assertEqual((result.status, result.error), ("empty", None))

    def test_later_page_error_preserves_results_and_partiality(self):
        def handler(request):
            if request.url.params["page"] == "2":
                raise httpx.ReadTimeout("slow")
            return httpx.Response(200, json={"cards": [printing()]}, headers={"Total-Count": "2"})
        result = self.api(handler, page_size=1).get_card_by_name("Sample")
        self.assertEqual((result.status, result.pages_fetched, result.error.kind), ("partial", 1, "timeout"))
        self.assertEqual(len(result.items), 1)

    def test_exact_localized_resolution_and_ambiguity(self):
        cards = [printing("First", foreignNames=[{"language": "Spanish", "name": "Ejemplo", "text": "Texto"}]),
                 printing("Second", foreignNames=[{"language": "Spanish", "name": "Ejemplo"}]),
                 printing("Example Extended")]
        def handler(request):
            self.assertEqual(request.url.params["language"], "Spanish")
            return httpx.Response(200, json={"cards": cards})
        api = self.api(handler)
        result = api.get_card_by_name(" ejemplo ", language="Spanish")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual([card.name for card in result.items], ["First", "Second"])
        self.assertEqual(result.items[0].printings[0]["foreignNames"][0]["text"], "Texto")
        self.assertEqual(api.get_card_by_name("Ejempl", language="Spanish").status, "empty")
        english = self.api(lambda _: httpx.Response(200, json={"cards": [printing("Example Extended")]}))
        self.assertEqual(english.get_card_by_name("Example").status, "empty")

    def test_sets_are_date_filtered_and_sorted_locally(self):
        sets = [{"code": "B", "name": "Second", "releaseDate": "2020-02-02"},
                {"code": "A", "name": "First", "releaseDate": "2020-01-01"},
                {"code": "X", "name": "Unknown"}]
        def handler(request):
            self.assertNotIn("releaseDate", request.url.params)
            return httpx.Response(200, json={"sets": sets})
        api = self.api(handler)
        result = api.list_sets(released_from="2020-01-01", released_to="2020-02-02")
        self.assertEqual([item["code"] for item in result.items], ["A", "B"])
        self.assertEqual([item["code"] for item in api.list_sets().items], ["A", "B", "X"])

    def test_invalid_filters_and_limits_fail_before_http(self):
        def unexpected(_):
            self.fail("Invalid input must not send HTTP requests")
        api = self.api(unexpected)
        for options in ({"cmc": -1}, {"cmc": ">2"}, {"color": "purple"}, {"name": " "},
                        {"language": "Spanish"}, {"cmc": float("nan")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                api.search_cards(**options)
        with self.assertRaises(ValueError):
            api.list_sets(released_from="2021-01-01", released_to="2020-01-01")
        for config in ({"max_pages": 0}, {"page_size": 101}, {"retries": 4}, {"timeout": 0}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                MtgApiClient(**config)


if __name__ == "__main__":
    unittest.main()
