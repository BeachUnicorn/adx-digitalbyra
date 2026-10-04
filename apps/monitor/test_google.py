"""
Googles data i övervakningen: PageSpeed fullt ut, Chrome UX Report,
Search Console och Business Profile. Tolkningen, larmen (bara till byrån,
en gång per problem), saknad behörighet och åtkomst, kvotfel, "för lite
trafik", byråns detaljsida och kundens sammanfattning.

Inget test anropar Google: urlopen i google_api och checks är utbytta, och
access_token ger en låtsasnyckel. Ett anrop som ingen väntat sig fäller testet.
"""

import io
import json
from contextlib import ExitStack
from datetime import date, timedelta
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.common.sentry import scrub_text
from apps.flamingo import google_ads
from apps.flamingo.models import GoogleAdsConnection
from apps.projects.models import Customer

from . import checks, google_api, google_checks
from .models import Check, Kind, MonitoredDomain, settings_for
from .runner import run_all, run_google

API_KEY = "AIzaSyTESTKEY0123456789abcdefghij"
SETTINGS = {
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "EMAIL_HOST_USER": "x",
    "EMAIL_HOST_PASSWORD": "y",
    "INQUIRY_NOTIFICATION_EMAIL": "staff@example.com",
    "PAGESPEED_API_KEY": API_KEY,
    "CRUX_API_KEY": "",
    # Ingen riktig inloggning hos Google i testerna, vad .env än säger.
    "GOOGLE_ADS_REFRESH_TOKEN": "",
    "GOOGLE_ADS_CLIENT_ID": "",
    "GOOGLE_ADS_CLIENT_SECRET": "",
}
TODAY = date(2026, 10, 4)


# ---------------------------------------------------------------------------
# Låtsas-Google
# ---------------------------------------------------------------------------


class Response(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def http_error(url, status, payload):
    return HTTPError(url, status, "fel", {}, io.BytesIO(json.dumps(payload).encode()))


def google_error(status, code, reason="", message="", metadata=None):
    detail = {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}
    if metadata:
        detail["metadata"] = metadata
    return {"error": {"code": status, "status": code, "message": message, "details": [detail]}}


class FakeGoogle:
    """Svarar efter (metod, värd, sökvägens början). routes: [(metod, värd,
    början, svar)] där svaret är ett dict, (status, dict) eller en funktion
    av (body, query) som ger något av dem."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, request, timeout=None):
        parts = urlsplit(request.full_url)
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        query = parse_qs(parts.query)
        self.calls.append(
            {
                "method": method,
                "host": parts.hostname,
                "path": unquote(parts.path),
                "body": body,
                "query": query,
                "headers": dict(request.header_items()),
                "url": request.full_url,
            }
        )
        for m, host, prefix, answer in self.routes:
            if m == method and host == parts.hostname and unquote(parts.path).startswith(prefix):
                if callable(answer):
                    answer = answer(body, query)
                status, payload = answer if isinstance(answer, tuple) else (200, answer)
                if status >= 400:
                    raise http_error(request.full_url, status, payload)
                return Response(payload, status)
        raise AssertionError(f"Oväntat anrop till Google: {method} {parts.hostname}{parts.path}")

    def count(self, host, prefix=""):
        return sum(1 for c in self.calls if c["host"] == host and c["path"].startswith(prefix))


def patched(fake):
    """urlopen utbytt och en låtsasnyckel från inloggningen."""
    stack = ExitStack()
    stack.enter_context(mock.patch.object(google_api, "urlopen", fake))
    stack.enter_context(
        mock.patch.object(google_ads, "access_token", return_value="ya29.lasnyckel")
    )
    stack.enter_context(mock.patch.object(timezone, "localdate", return_value=TODAY))
    return stack


# ---------------------------------------------------------------------------
# Svar i Googles form
# ---------------------------------------------------------------------------


def crux_record(lcp, inp=None, cls=None):
    n = len(lcp)
    periods = []
    for i in range(n):
        end = date(2026, 9, 27) - timedelta(days=7 * (n - 1 - i))
        start = end - timedelta(days=27)
        periods.append(
            {
                "firstDate": {"year": start.year, "month": start.month, "day": start.day},
                "lastDate": {"year": end.year, "month": end.month, "day": end.day},
            }
        )
    metrics = {"largest_contentful_paint": {"percentilesTimeseries": {"p75s": lcp}}}
    if inp:
        metrics["interaction_to_next_paint"] = {"percentilesTimeseries": {"p75s": inp}}
    if cls:
        metrics["cumulative_layout_shift"] = {"percentilesTimeseries": {"p75s": cls}}
    return {
        "record": {
            "key": {"origin": "https://www.nordan.se", "formFactor": "PHONE"},
            "metrics": metrics,
            "collectionPeriods": periods,
        }
    }


NOT_FOUND = (404, google_error(404, "NOT_FOUND", message="chrome ux report data not found"))

PSI = {
    "lighthouseResult": {
        "categories": {
            "performance": {"score": 0.81},
            "accessibility": {"score": 0.95},
            "best-practices": {"score": 1},
            "seo": {"score": 0.92},
        },
        "audits": {
            "largest-contentful-paint": {"displayValue": "2,9 s"},
            "cumulative-layout-shift": {"displayValue": "0,02"},
            "total-blocking-time": {"displayValue": "120 ms"},
            "first-contentful-paint": {"displayValue": "1,4 s"},
            "speed-index": {"displayValue": "3,1 s"},
            "total-byte-weight": {"numericValue": 1048576},
        },
    },
    "loadingExperience": {
        "id": "https://www.nordan.se/",
        "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2100, "category": "FAST"},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 260, "category": "AVERAGE"},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 5, "category": "FAST"},
            "FIRST_CONTENTFUL_PAINT_MS": {"percentile": 1500, "category": "FAST"},
            "EXPERIMENTAL_TIME_TO_FIRST_BYTE": {"percentile": 900, "category": "AVERAGE"},
        },
        "overall_category": "AVERAGE",
    },
    "originLoadingExperience": {
        "id": "https://www.nordan.se",
        "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2300, "category": "FAST"},
        },
        "overall_category": "FAST",
    },
}

SITES = {
    "siteEntry": [
        {"siteUrl": "sc-domain:nordan.se", "permissionLevel": "siteFullUser"},
        {"siteUrl": "https://annan.se/", "permissionLevel": "siteOwner"},
    ]
}


def analytics(cur_clicks=400, prev_clicks=1000, home_verdict="PASS", sitemap_errors=0):
    def answer(body, _query):
        dims = body.get("dimensions") or []
        if not dims:
            current = body["endDate"] == "2026-10-01"
            clicks = cur_clicks if current else prev_clicks
            return {
                "rows": [
                    {"clicks": clicks, "impressions": clicks * 20, "ctr": 0.05, "position": 8.25}
                ]
            }
        if dims == ["date"]:
            return {
                "rows": [
                    {"keys": ["2026-09-30"], "clicks": 12},
                    {"keys": ["2026-10-01"], "clicks": 9},
                ]
            }
        if dims == ["query"]:
            return {
                "rows": [
                    {
                        "keys": ["snickare umeå"],
                        "clicks": 50,
                        "impressions": 900,
                        "ctr": 0.055,
                        "position": 3.2,
                    },
                    {
                        "keys": ["nordan bygg"],
                        "clicks": 40,
                        "impressions": 100,
                        "ctr": 0.4,
                        "position": 1.0,
                    },
                ]
            }
        return {
            "rows": [
                {
                    "keys": ["https://www.nordan.se/kok/"],
                    "clicks": 30,
                    "impressions": 600,
                    "ctr": 0.05,
                    "position": 5,
                },
                {
                    "keys": ["https://andra.se/x/"],
                    "clicks": 1,
                    "impressions": 2,
                    "ctr": 0.5,
                    "position": 9,
                },
            ]
        }

    def inspect(body, _query):
        verdict = home_verdict if urlsplit(body["inspectionUrl"]).path == "/" else "PASS"
        return {
            "inspectionResult": {
                "inspectionResultLink": "https://search.google.com/search-console/inspect?x",
                "indexStatusResult": {
                    "verdict": verdict,
                    "coverageState": "Submitted and indexed"
                    if verdict == "PASS"
                    else "Crawled - currently not indexed",
                    "robotsTxtState": "ALLOWED",
                    "indexingState": "INDEXING_ALLOWED",
                    "pageFetchState": "SUCCESSFUL",
                    "lastCrawlTime": "2026-10-01T08:00:00Z",
                },
            }
        }

    return [
        (
            "GET",
            "www.googleapis.com",
            "/webmasters/v3/sites/sc-domain:nordan.se/sitemaps",
            {
                "sitemap": [
                    {
                        "path": "https://www.nordan.se/sitemap.xml",
                        "lastDownloaded": "2026-10-02T03:00:00Z",
                        "lastSubmitted": "2026-01-01T00:00:00Z",
                        "isPending": False,
                        "errors": str(sitemap_errors),
                        "warnings": "1",
                        "type": "sitemap",
                    }
                ]
            },
        ),
        (
            "POST",
            "www.googleapis.com",
            "/webmasters/v3/sites/sc-domain:nordan.se/searchAnalytics",
            answer,
        ),
        ("GET", "www.googleapis.com", "/webmasters/v3/sites", SITES),
        ("POST", "searchconsole.googleapis.com", "/v1/urlInspection/index:inspect", inspect),
    ]


LOCATION = {
    "name": "locations/111",
    "title": "Nordan Bygg AB",
    "storefrontAddress": {
        "addressLines": ["Storgatan 1"],
        "postalCode": "903 26",
        "locality": "Umeå",
        "regionCode": "SE",
    },
    "phoneNumbers": {"primaryPhone": "090-12 34 56"},
    "websiteUri": "https://www.nordan.se/",
    "categories": {
        "primaryCategory": {"displayName": "Snickare"},
        "additionalCategories": [{"displayName": "Byggfirma"}],
    },
    "regularHours": {
        "periods": [
            {
                "openDay": "MONDAY",
                "openTime": {"hours": 7},
                "closeDay": "MONDAY",
                "closeTime": {"hours": 16, "minutes": 30},
            }
        ]
    },
    "openInfo": {"status": "OPEN"},
    "metadata": {"hasVoiceOfMerchant": True, "mapsUri": "https://maps.google.com/?cid=1"},
}


def perf_series(current_calls=10, previous_calls=40):
    def values(cur, prev):
        out = []
        for day, value in (("2026-09-20", cur), ("2026-08-20", prev)):
            y, m, d = (int(x) for x in day.split("-"))
            out.append({"date": {"year": y, "month": m, "day": d}, "value": str(value)})
        return out

    return {
        "multiDailyMetricTimeSeries": [
            {
                "dailyMetricTimeSeries": [
                    {
                        "dailyMetric": "CALL_CLICKS",
                        "timeSeries": {"datedValues": values(current_calls, previous_calls)},
                    },
                    {
                        "dailyMetric": "WEBSITE_CLICKS",
                        "timeSeries": {"datedValues": values(55, 50)},
                    },
                    {
                        "dailyMetric": "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
                        "timeSeries": {"datedValues": values(700, 600)},
                    },
                    {
                        "dailyMetric": "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
                        "timeSeries": {"datedValues": values(300, 200)},
                    },
                    {
                        "dailyMetric": "BUSINESS_DIRECTION_REQUESTS",
                        "timeSeries": {
                            "datedValues": [{"date": {"year": 2026, "month": 9, "day": 1}}]
                        },
                    },
                ]
            }
        ]
    }


def gbp_routes(location=None, performance=None):
    return [
        (
            "GET",
            "mybusinessaccountmanagement.googleapis.com",
            "/v1/accounts",
            {"accounts": [{"name": "accounts/9", "accountName": "ADX"}]},
        ),
        (
            "GET",
            "mybusinessbusinessinformation.googleapis.com",
            "/v1/accounts/9/locations",
            {
                "locations": [
                    {
                        "name": "locations/222",
                        "title": "Annan AB",
                        "websiteUri": "https://annan.se/",
                    },
                    {
                        "name": "locations/111",
                        "title": "Nordan Bygg AB",
                        "websiteUri": "https://www.nordan.se/",
                    },
                ]
            },
        ),
        (
            "GET",
            "mybusinessbusinessinformation.googleapis.com",
            "/v1/locations/111",
            location or LOCATION,
        ),
        (
            "GET",
            "businessprofileperformance.googleapis.com",
            "/v1/locations/111:fetch",
            performance or perf_series(),
        ),
        (
            "GET",
            "mybusiness.googleapis.com",
            "/v4/accounts/9/locations/111/reviews",
            {"averageRating": 4.7, "totalReviewCount": 23, "reviews": []},
        ),
    ]


# ---------------------------------------------------------------------------
# Fixtur
# ---------------------------------------------------------------------------


class Fixture(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("byra", password="x", is_staff=True)
        self.acme = Customer.objects.create(name="Nordan AB")
        self.contact = User.objects.create_user(
            "anna@nordan.se", email="anna@nordan.se", password="x"
        )
        self.acme.users.add(self.contact)
        self.domain = MonitoredDomain.objects.create(
            customer=self.acme, name="nordan.se", is_primary=True
        )
        Check.objects.create(
            domain=self.domain,
            kind=Kind.UPTIME,
            ok=True,
            ms=100,
            data={"ok": True, "final_url": "https://www.nordan.se/"},
        )
        self.monitor = settings_for(self.acme)
        for field in ("show_ssl", "show_domain", "show_email", "show_security"):
            setattr(self.monitor, field, False)
        self.monitor.show_search = True
        self.monitor.show_gbp = True
        self.monitor.save()

    def client_for(self, user):
        c = Client()
        c.force_login(user)
        return c


# ---------------------------------------------------------------------------
# PageSpeed
# ---------------------------------------------------------------------------


@override_settings(**SETTINGS)
class PageSpeedTests(TestCase):
    def test_all_four_categories_lab_and_field_data_are_stored(self):
        seen = []

        def fake(request, timeout=None):
            seen.append(request.full_url)
            return Response(PSI)

        with mock.patch.object(checks, "urlopen", fake):
            result = checks.check_performance("nordan.se")
        self.assertTrue(result["ok"])
        query = parse_qs(urlsplit(seen[0]).query)
        self.assertEqual(
            query["category"], ["performance", "accessibility", "best-practices", "seo"]
        )
        mobile = result["strategies"]["mobile"]
        self.assertEqual(mobile["score"], 81, "prestandapoängen ligger kvar där den låg")
        self.assertEqual(
            mobile["categories"],
            {"performance": 81, "accessibility": 95, "best-practices": 100, "seo": 92},
        )
        self.assertEqual(
            (mobile["lcp"], mobile["fcp"], mobile["bytes_kb"]), ("2,9 s", "1,4 s", 1024)
        )
        field = mobile["field"]
        self.assertEqual(field["overall"], "AVERAGE")
        self.assertEqual(field["metrics"]["lcp"], {"p75": 2100, "category": "FAST"})
        self.assertEqual(field["metrics"]["cls"]["p75"], 0.05, "CLS skickas gånger 100")
        self.assertEqual(field["metrics"]["inp"]["category"], "AVERAGE")
        self.assertEqual(mobile["origin_field"]["overall"], "FAST")

    def test_a_page_without_field_data_has_none(self):
        payload = {"lighthouseResult": PSI["lighthouseResult"], "loadingExperience": {"id": "x"}}
        with mock.patch.object(checks, "urlopen", lambda r, timeout=None: Response(payload)):
            result = checks.check_performance("nordan.se")
        self.assertIsNone(result["strategies"]["desktop"]["field"])

    def test_the_key_never_lands_in_the_stored_error(self):
        def boom(request, timeout=None):
            raise OSError(f"kunde inte nå {request.full_url}")

        with mock.patch.object(checks, "urlopen", boom):
            result = checks.check_performance("nordan.se")
        self.assertFalse(result["ok"])
        self.assertNotIn(API_KEY, json.dumps(result))


# ---------------------------------------------------------------------------
# Chrome UX Report
# ---------------------------------------------------------------------------


@override_settings(**SETTINGS)
class CruxTests(Fixture):
    def test_request_shape_and_parsing(self):
        fake = FakeGoogle(
            [
                (
                    "POST",
                    "chromeuxreport.googleapis.com",
                    "/v1/records:queryHistoryRecord",
                    lambda body, q: (
                        crux_record([2000, None, 2400], [150, 180, 190], ["0.05", "0.06", "NaN"])
                        if body["formFactor"] == "PHONE"
                        else NOT_FOUND
                    ),
                ),
            ]
        )
        with patched(fake):
            result = google_checks.fetch_crux("https://www.nordan.se")
        call = fake.calls[0]
        self.assertEqual(call["body"]["origin"], "https://www.nordan.se")
        self.assertEqual(
            call["body"]["metrics"],
            ["largest_contentful_paint", "interaction_to_next_paint", "cumulative_layout_shift"],
        )
        self.assertEqual({c["body"]["formFactor"] for c in fake.calls}, {"PHONE", "DESKTOP"})
        self.assertNotIn("key=", call["url"], "nyckeln skickas i en header, inte i adressen")
        self.assertEqual(call["headers"].get("X-goog-api-key"), API_KEY)
        phone = result["form_factors"]["phone"]
        self.assertEqual(phone["metrics"]["lcp"], [2000, None, 2400])
        self.assertEqual(phone["metrics"]["cls"], [0.05, 0.06, None])
        self.assertEqual(phone["periods"][-1], "2026-09-27")
        self.assertEqual(result["form_factors"]["desktop"], {"no_data": True})
        self.assertTrue(result["ok"])
        self.assertFalse(result["no_data"])

    def test_small_sites_are_calm_not_an_error(self):
        fake = FakeGoogle([("POST", "chromeuxreport.googleapis.com", "/v1/", NOT_FOUND)])
        with patched(fake):
            run_google(self.domain, {Kind.CRUX})
        check = self.domain.latest(Kind.CRUX)
        self.assertTrue(check.ok)
        self.assertTrue(check.data["no_data"])
        html = self.client_for(self.contact).get("/kund/status/").content.decode()
        self.assertIn("För lite trafik för Googles mätning", html)
        self.assertNotIn("NOT_FOUND", html)
        page = self.client_for(self.staff).get(
            f"/manage/overvakning/doman/{self.domain.pk}/google/"
        )
        self.assertContains(page, "För lite trafik för Googles mätning")

    def test_crossing_from_good_for_two_periods_alerts_once(self):
        series = crux_record([2000, 2100, 2300, 2700, 2900], cls=["0.05"] * 5)
        fake = FakeGoogle(
            [
                (
                    "POST",
                    "chromeuxreport.googleapis.com",
                    "/v1/",
                    lambda body, q: series if body["formFactor"] == "PHONE" else NOT_FOUND,
                )
            ]
        )
        with patched(fake):
            attention = run_google(self.domain, {Kind.CRUX})
        self.assertEqual(len(attention), 1)
        self.assertIn(
            "Största elementet (LCP) har gått från bra till behöver förbättras", attention[0]
        )
        self.assertIn("telefon", attention[0])
        with patched(fake):
            again = run_google(self.domain, {Kind.CRUX})
        self.assertEqual(again, [], "samma övergång larmar inte varje dag")

    def test_one_bad_period_is_not_enough(self):
        alerts = google_checks.crux_alerts(
            {
                "ok": True,
                "form_factors": {
                    "phone": {"periods": ["a", "b", "c"], "metrics": {"inp": [150, 180, 260]}}
                },
            }
        )
        self.assertEqual(alerts, [])
        alerts = google_checks.crux_alerts(
            {
                "ok": True,
                "form_factors": {
                    "phone": {"periods": ["a", "b", "c"], "metrics": {"inp": [150, 520, 260]}}
                },
            }
        )
        self.assertEqual(len(alerts), 1)

    def test_without_any_key_crux_is_not_fetched(self):
        fake = FakeGoogle([])
        with override_settings(PAGESPEED_API_KEY="", CRUX_API_KEY=""), patched(fake):
            run_google(self.domain, {Kind.CRUX})
        self.assertEqual(fake.calls, [])
        self.assertIsNone(self.domain.latest(Kind.CRUX))

    def test_crux_key_wins_over_pagespeed_key(self):
        with override_settings(CRUX_API_KEY="AIzaEGENNYCKEL000000000000000"):
            self.assertEqual(google_api.crux_api_key(), "AIzaEGENNYCKEL000000000000000")
        self.assertEqual(google_api.crux_api_key(), API_KEY)

    def test_api_not_enabled_is_a_setup_error_for_staff_only(self):
        fake = FakeGoogle(
            [
                (
                    "POST",
                    "chromeuxreport.googleapis.com",
                    "/v1/",
                    (
                        403,
                        google_error(
                            403, "PERMISSION_DENIED", "SERVICE_DISABLED", f"key {API_KEY} disabled"
                        ),
                    ),
                )
            ]
        )
        with patched(fake):
            run_google(self.domain, {Kind.CRUX})
        check = self.domain.latest(Kind.CRUX)
        self.assertFalse(check.ok)
        self.assertEqual(check.data["error_kind"], google_api.KIND_NOT_ENABLED)
        self.assertIn("Chrome UX Report API är inte påslaget", check.data["error"])
        self.assertNotIn(API_KEY, json.dumps(check.data))
        html = self.client_for(self.contact).get("/kund/status/").content.decode()
        self.assertNotIn("påslaget", html)


# ---------------------------------------------------------------------------
# Felen från Google
# ---------------------------------------------------------------------------


class ErrorTests(TestCase):
    def test_kinds(self):
        cases = [
            (
                "search",
                403,
                google_error(403, "PERMISSION_DENIED", "SERVICE_DISABLED"),
                google_api.KIND_NOT_ENABLED,
            ),
            (
                "search",
                403,
                google_error(403, "PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"),
                google_api.KIND_SCOPE,
            ),
            (
                "gbp",
                429,
                google_error(
                    429,
                    "RESOURCE_EXHAUSTED",
                    "RATE_LIMIT_EXCEEDED",
                    metadata={"quota_limit_value": "0"},
                ),
                google_api.KIND_QUOTA_ZERO,
            ),
            (
                "search",
                429,
                google_error(
                    429,
                    "RESOURCE_EXHAUSTED",
                    "RATE_LIMIT_EXCEEDED",
                    metadata={"quota_limit_value": "2000"},
                ),
                google_api.KIND_QUOTA,
            ),
            (
                "search",
                403,
                google_error(
                    403, "PERMISSION_DENIED", message="User does not have sufficient permission"
                ),
                google_api.KIND_PERMISSION,
            ),
            ("crux", 404, NOT_FOUND[1], google_api.KIND_NOT_FOUND),
            ("gbp", 500, {}, google_api.KIND_UNAVAILABLE),
        ]
        for api, status, payload, kind in cases:
            with self.subTest(kind=kind):
                self.assertEqual(google_api.error_for(api, status, payload).kind, kind)

    def test_gbp_quota_zero_explains_the_access_request(self):
        error = google_api.error_for(
            "gbp",
            429,
            google_error(
                429,
                "RESOURCE_EXHAUSTED",
                "RATE_LIMIT_EXCEEDED",
                metadata={"quota_limit_value": "0"},
            ),
        )
        self.assertIn("support.google.com/business/contact/api_default", error.message)
        self.assertIn("300", error.message)
        self.assertTrue(error.is_setup)

    def test_tokens_are_scrubbed_from_google_text(self):
        error = google_api.error_for(
            "search",
            400,
            {"error": {"message": "bad ya29.hemlig-nyckel and key AIzaSyHEMLIG0000000000000000"}},
        )
        self.assertNotIn("ya29.hemlig", error.message)
        self.assertNotIn("AIzaSyHEMLIG", error.message)

    def test_only_googles_fixed_hosts(self):
        with self.assertRaises(google_api.GoogleApiError):
            google_api._http("GET", "https://evil.example.com/v1/x")
        with self.assertRaises(google_api.GoogleApiError):
            google_api._http("GET", "https://www.googleapis.com/drive/v3/files")

    def test_sentry_scrubs_google_api_keys(self):
        self.assertNotIn("AIzaSy", scrub_text(f"PSI-fel för {API_KEY}"))


# ---------------------------------------------------------------------------
# Search Console
# ---------------------------------------------------------------------------


@override_settings(**SETTINGS)
class SearchTests(Fixture):
    def test_property_matching(self):
        sites = [
            {"site_url": "https://www.nordan.se/", "permission": "siteFullUser"},
            {"site_url": "sc-domain:nordan.se", "permission": "siteUnverifiedUser"},
        ]
        self.assertEqual(
            google_checks.match_property("nordan.se", sites), ("https://www.nordan.se/", "auto")
        )
        self.assertEqual(
            google_checks.match_property("nordan.se", sites, "sc-domain:nordan.se"), (None, "")
        )
        self.assertEqual(google_checks.match_property("x.se", sites), (None, ""))

    def test_full_fetch_and_alerts_go_to_staff_only(self):
        self.monitor.show_gbp = False
        self.monitor.save()
        fake = FakeGoogle(
            analytics(cur_clicks=400, prev_clicks=1000, home_verdict="NEUTRAL", sitemap_errors=3)
        )
        with (
            patched(fake),
            override_settings(PAGESPEED_API_KEY=""),
            mock.patch.object(
                checks, "check_performance", return_value={"ok": True, "strategies": {}}
            ),
        ):
            _count, attention = run_all(daily=True, domains=[self.domain])
        data = self.domain.latest(Kind.SEARCH).data
        self.assertEqual(data["property"], "sc-domain:nordan.se")
        self.assertEqual(data["current"]["clicks"], 400)
        self.assertEqual(data["previous"]["clicks"], 1000)
        self.assertEqual(data["current"]["ctr"], 5.0)
        self.assertEqual(
            data["window"],
            {
                "start": "2026-09-04",
                "end": "2026-10-01",
                "prev_start": "2026-08-07",
                "prev_end": "2026-09-03",
            },
        )
        self.assertEqual(data["top_queries"][0]["key"], "snickare umeå")
        self.assertEqual(data["daily"]["clicks"][-2:], [12, 9])
        self.assertEqual(data["sitemaps"][0]["errors"], 3)
        inspected = [i["url"] for i in data["inspections"]]
        self.assertEqual(
            inspected,
            ["https://www.nordan.se/", "https://www.nordan.se/kok/"],
            "startsidan och sajtens egna toppsidor, inte andras",
        )
        texts = [t for _d, t in attention]
        self.assertTrue(any("minskat med 60 %" in t for t in texts))
        self.assertTrue(any("sitemap.xml har 3 fel" in t for t in texts))
        self.assertTrue(any("startsidan är inte indexerad" in t for t in texts))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["staff@example.com"])
        self.assertNotIn("anna@nordan.se", mail.outbox[0].to)
        # Kunden ser en enkel sammanfattning, inga larmtexter eller egendomar.
        html = self.client_for(self.contact).get("/kund/status/").content.decode()
        self.assertIn("Google-sök", html)
        self.assertIn("400 besök från Googles sökresultat", html)
        self.assertIn("-60 %", html)
        self.assertIn("Nej, vi tittar på det", html)
        self.assertNotIn("sc-domain", html)
        self.assertNotIn("sitemap.xml", html)

    def test_no_big_drop_on_small_volume(self):
        self.assertFalse(google_checks.is_drop(5, 20, google_checks.SEARCH_MIN_CLICKS))
        self.assertTrue(google_checks.is_drop(20, 100, google_checks.SEARCH_MIN_CLICKS))
        self.assertFalse(google_checks.is_drop(70, 100, google_checks.SEARCH_MIN_CLICKS))

    def test_inspections_stay_within_ten_per_day(self):
        fake = FakeGoogle(analytics())
        for _ in range(4):
            with patched(fake):
                run_google(self.domain, {Kind.SEARCH})
        total = sum(c.data.get("inspected", 0) for c in Check.objects.filter(kind=Kind.SEARCH))
        self.assertLessEqual(total, google_checks.MAX_INSPECTIONS_PER_DAY)
        self.assertLessEqual(fake.count("searchconsole.googleapis.com"), 10)

    def test_inspection_quota_stops_further_inspections(self):
        routes = analytics()
        routes[-1] = (
            "POST",
            "searchconsole.googleapis.com",
            "/v1/",
            (429, google_error(429, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED")),
        )
        fake = FakeGoogle(routes)
        with patched(fake):
            run_google(self.domain, {Kind.SEARCH})
        data = self.domain.latest(Kind.SEARCH).data
        self.assertEqual(fake.count("searchconsole.googleapis.com"), 1)
        self.assertIn("kvoten", data["errors"]["inspection"])
        self.assertEqual(data["current"]["clicks"], 400, "resten hämtas ändå")

    def test_no_access_shows_how_to_grant_it(self):
        GoogleAdsConnection.objects.create(
            pk=GoogleAdsConnection.SOLO_PK, google_email="giovanni@palermo.se"
        )
        fake = FakeGoogle(
            [("GET", "www.googleapis.com", "/webmasters/v3/sites", {"siteEntry": []})]
        )
        with patched(fake):
            attention = run_google(self.domain, {Kind.SEARCH})
        self.assertEqual(attention, [])
        self.assertFalse(self.domain.latest(Kind.SEARCH).data["access"])
        page = self.client_for(self.staff).get(
            f"/manage/overvakning/doman/{self.domain.pk}/google/"
        )
        self.assertContains(page, "Ingen åtkomst i Search Console")
        self.assertContains(page, "Lägg till användare: giovanni@palermo.se")
        html = self.client_for(self.contact).get("/kund/status/").content.decode()
        self.assertNotIn("Ingen åtkomst", html)
        self.assertIn("Kopplas in:", html)

    @override_settings(GOOGLE_ADS_CLIENT_ID="id", GOOGLE_ADS_CLIENT_SECRET="hemlig")
    def test_missing_scope_needs_no_call_and_says_reconnect(self):
        GoogleAdsConnection.get_solo().set_refresh_token(
            "1//refresh-test-0000000", scopes=f"{google_ads.ADWORDS_SCOPE} openid email"
        )
        fake = FakeGoogle([])
        with patched(fake):
            run_google(self.domain, {Kind.SEARCH, Kind.GBP})
        self.assertEqual(fake.calls, [])
        search = self.domain.latest(Kind.SEARCH)
        self.assertEqual(search.data["error_kind"], google_api.KIND_SCOPE)
        self.assertIn("Koppla om för att läsa Search Console", search.data["error"])
        self.assertIn(
            "Koppla om för att läsa Business Profile", self.domain.latest(Kind.GBP).data["error"]
        )
        page = self.client_for(self.staff).get("/manage/flamingo/google/").content.decode()
        self.assertIn("Koppla om för att läsa Search Console", page)
        self.assertIn("Koppla om för att läsa Business Profile", page)
        detail = self.client_for(self.staff).get(
            f"/manage/overvakning/doman/{self.domain.pk}/google/"
        )
        self.assertContains(detail, "Koppla om för att läsa Search Console")

    def test_scope_insufficient_from_google_is_remembered(self):
        GoogleAdsConnection.get_solo().set_refresh_token(
            "1//refresh-test-0000000",
            scopes=f"{google_ads.ADWORDS_SCOPE} {google_ads.WEBMASTERS_SCOPE}",
        )
        fake = FakeGoogle(
            [
                (
                    "GET",
                    "www.googleapis.com",
                    "/webmasters/v3/sites",
                    (
                        403,
                        google_error(403, "PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"),
                    ),
                )
            ]
        )
        with (
            patched(fake),
            override_settings(GOOGLE_ADS_CLIENT_ID="id", GOOGLE_ADS_CLIENT_SECRET="s"),
        ):
            run_google(self.domain, {Kind.SEARCH})
            self.assertEqual(
                google_ads.scope_state(google_ads.WEBMASTERS_SCOPE), google_ads.SCOPE_MISSING
            )
            self.assertEqual(
                google_ads.scope_state(google_ads.ADWORDS_SCOPE),
                google_ads.SCOPE_GRANTED,
                "Flamingos behörighet rörs inte",
            )

    def test_not_connected(self):
        with mock.patch.object(google_api, "urlopen", FakeGoogle([])):
            run_google(self.domain, {Kind.SEARCH})
        data = self.domain.latest(Kind.SEARCH).data
        self.assertEqual(data["error_kind"], google_api.KIND_NOT_CONNECTED)

    def test_staff_can_choose_the_property(self):
        c = self.client_for(self.staff)
        url = f"/manage/overvakning/doman/{self.domain.pk}/google/"
        c.post(url, {"action": "property", "search_property": "https://www.nordan.se/"})
        self.domain.refresh_from_db()
        self.assertEqual(self.domain.search_property, "https://www.nordan.se/")
        c.post(url, {"action": "property", "search_property": "javascript:alert(1)"})
        self.domain.refresh_from_db()
        self.assertEqual(self.domain.search_property, "https://www.nordan.se/")
        self.assertEqual(self.client_for(self.contact).get(url).status_code, 302)


# ---------------------------------------------------------------------------
# Google Business Profile
# ---------------------------------------------------------------------------


@override_settings(**SETTINGS)
class BusinessProfileTests(Fixture):
    def test_auto_match_basics_metrics_and_rating(self):
        fake = FakeGoogle(gbp_routes())
        with patched(fake):
            attention = run_google(self.domain, {Kind.GBP})
        data = self.domain.latest(Kind.GBP).data
        self.assertEqual(
            (data["location"], data["account"], data["matched"]),
            ("locations/111", "accounts/9", "auto"),
        )
        self.assertEqual(data["address"], "Storgatan 1, 903 26 Umeå")
        self.assertEqual(data["categories"], ["Snickare", "Byggfirma"])
        self.assertEqual(data["hours"][0], ["Måndag", "07:00-16:30"])
        self.assertEqual(data["hours"][1], ["Tisdag", "Stängt"])
        self.assertTrue(data["verified"])
        self.assertEqual(data["metrics"]["impressions"], {"current": 1000, "previous": 800})
        self.assertEqual(data["metrics"]["calls"], {"current": 10, "previous": 40})
        self.assertEqual(data["metrics"]["directions"], {"current": 0, "previous": 0})
        self.assertEqual((data["rating"], data["review_count"]), (4.7, 23))
        self.assertEqual(
            attention, ["samtal från profilen har minskat med 75 % (40 till 10 på 28 dagar)"]
        )
        perf_call = next(
            c for c in fake.calls if c["host"] == "businessprofileperformance.googleapis.com"
        )
        self.assertIn("CALL_CLICKS", perf_call["query"]["dailyMetrics"])
        self.assertEqual(perf_call["query"]["dailyRange.startDate.year"], ["2026"])
        info_call = next(c for c in fake.calls if c["path"] == "/v1/locations/111")
        self.assertIn("openInfo", info_call["query"]["readMask"][0])
        # Kunden: profilen öppen, betyget och siffrorna.
        html = self.client_for(self.contact).get("/kund/status/").content.decode()
        self.assertIn("Företagsprofil på Google", html)
        self.assertIn("4,7 av 5", html)
        self.assertIn("Samtal", html)
        self.assertNotIn("locations/111", html)
        # Byrån: hela bilden.
        page = self.client_for(self.staff).get(
            f"/manage/overvakning/doman/{self.domain.pk}/google/"
        )
        self.assertContains(page, "090-12 34 56")
        self.assertContains(page, "matchad på webbadress")
        self.assertContains(page, "4,7 av 5 (23 omdömen)")

    def test_alerts_for_wrong_website_closed_and_unverified(self):
        location = {
            **LOCATION,
            "websiteUri": "https://nordan.wixsite.com/bygg",
            "openInfo": {"status": "CLOSED_TEMPORARILY"},
            "metadata": {"hasVoiceOfMerchant": False},
        }
        self.domain.gbp_location, self.domain.gbp_account = "locations/111", "accounts/9"
        self.domain.save()
        fake = FakeGoogle(gbp_routes(location=location, performance=perf_series(30, 30)))
        with patched(fake):
            attention = run_google(self.domain, {Kind.GBP})
        self.assertEqual(
            fake.count("mybusinessaccountmanagement.googleapis.com"), 0, "vald plats: ingen lista"
        )
        joined = " ".join(attention)
        self.assertIn("länkar till https://nordan.wixsite.com/bygg", joined)
        self.assertIn("tillfälligt stängd", joined)
        self.assertIn("inte verifierad", joined)
        html = self.client_for(self.contact).get("/kund/status/").content.decode()
        self.assertIn("Profilen visas som tillfälligt stängd på Google.", html)

    def test_quota_zero_is_explained_and_never_reaches_the_customer(self):
        fake = FakeGoogle(
            [
                (
                    "GET",
                    "mybusinessaccountmanagement.googleapis.com",
                    "/v1/accounts",
                    (
                        429,
                        google_error(
                            429,
                            "RESOURCE_EXHAUSTED",
                            "RATE_LIMIT_EXCEEDED",
                            metadata={"quota_limit_value": "0"},
                        ),
                    ),
                )
            ]
        )
        with patched(fake):
            attention = run_google(self.domain, {Kind.GBP})
        self.assertEqual(attention, [])
        check = self.domain.latest(Kind.GBP)
        self.assertFalse(check.ok)
        self.assertTrue(check.data["setup"])
        page = self.client_for(self.staff).get(
            f"/manage/overvakning/doman/{self.domain.pk}/google/"
        )
        self.assertContains(page, "Google har inte godkänt ADX för Business Profile-API:erna")
        self.assertContains(page, "api_default")
        html = self.client_for(self.contact).get("/kund/status/").content.decode()
        self.assertNotIn("godkänt", html)

    def test_api_not_enabled(self):
        fake = FakeGoogle(
            [
                (
                    "GET",
                    "mybusinessaccountmanagement.googleapis.com",
                    "/v1/accounts",
                    (403, google_error(403, "PERMISSION_DENIED", "SERVICE_DISABLED")),
                )
            ]
        )
        with patched(fake):
            run_google(self.domain, {Kind.GBP})
        self.assertIn(
            "My Business Account Management API", self.domain.latest(Kind.GBP).data["error"]
        )

    def test_reviews_failing_does_not_lose_the_rest(self):
        routes = gbp_routes()
        routes[-1] = (
            "GET",
            "mybusiness.googleapis.com",
            "/v4/",
            (403, google_error(403, "PERMISSION_DENIED", "SERVICE_DISABLED")),
        )
        fake = FakeGoogle(routes)
        with patched(fake):
            run_google(self.domain, {Kind.GBP})
        data = self.domain.latest(Kind.GBP).data
        self.assertTrue(data["ok"])
        self.assertIn("reviews", data["errors"])
        self.assertEqual(data["title"], "Nordan Bygg AB")

    def test_staff_can_pick_a_location(self):
        c = self.client_for(self.staff)
        url = f"/manage/overvakning/doman/{self.domain.pk}/google/"
        with patched(FakeGoogle(gbp_routes() + analytics())):
            c.post(url, {"action": "choices"})
        page = c.get(url).content.decode()
        self.assertIn("Annan AB", page)
        self.assertIn("sc-domain:nordan.se", page)
        c.post(url, {"action": "location", "gbp": "accounts/9|locations/111"})
        self.domain.refresh_from_db()
        self.assertEqual(
            (self.domain.gbp_account, self.domain.gbp_location, self.domain.gbp_title),
            ("accounts/9", "locations/111", "Nordan Bygg AB"),
        )
        c.post(url, {"action": "location", "gbp": "accounts/9|../../x"})
        self.domain.refresh_from_db()
        self.assertEqual(self.domain.gbp_location, "locations/111")
        c.post(url, {"action": "location", "gbp": ""})
        self.domain.refresh_from_db()
        self.assertEqual(self.domain.gbp_location, "")


# ---------------------------------------------------------------------------
# Visningen
# ---------------------------------------------------------------------------


@override_settings(**SETTINGS)
class RenderingTests(Fixture):
    def seed(self):
        Check.objects.create(
            domain=self.domain,
            kind=Kind.PERFORMANCE,
            ok=True,
            data={
                "ok": True,
                "strategies": {
                    "mobile": checks.parse_pagespeed(PSI),
                    "desktop": checks.parse_pagespeed(PSI),
                },
            },
        )
        phone = google_checks.parse_crux(
            crux_record([2000, 2100, 2200], [150, 160, 170], ["0.01", "0.02", "0.02"])
        )
        Check.objects.create(
            domain=self.domain,
            kind=Kind.CRUX,
            ok=True,
            data={
                "ok": True,
                "origin": "https://www.nordan.se",
                "form_factors": {"phone": phone, "desktop": {"no_data": True}},
            },
        )

    def test_portal_summary_shows_scores_and_real_users(self):
        self.seed()
        html = self.client_for(self.contact).get("/kund/status/").content.decode()
        self.assertIn("Riktiga besökare", html)
        self.assertIn("Besökarna upplever sidan som snabb och stabil.", html)
        self.assertIn("Tillgänglighet", html)
        self.assertIn("mobil 95, dator 95 av 100", html)
        self.assertIn('class="st-row__spark"', html)

    def test_manage_detail_shows_lab_and_field_clearly(self):
        self.seed()
        page = self.client_for(self.staff).get(
            f"/manage/overvakning/doman/{self.domain.pk}/google/"
        )
        self.assertContains(page, "Poäng i labbet")
        self.assertContains(page, "Riktiga besökare enligt PageSpeed")
        self.assertContains(page, "Sökmotoroptimering (SEO)")
        self.assertContains(page, "Behöver förbättras")  # INP 260 ms i fältdatan
        self.assertContains(page, "Riktiga besökare över tid")
        self.assertContains(page, "2,2 s")
        self.assertContains(page, "<thead>", count=2)

    def test_view_as_customer_sees_exactly_what_the_customer_sees(self):
        self.seed()
        Check.objects.create(
            domain=self.domain,
            kind=Kind.SEARCH,
            ok=True,
            data={
                "ok": True,
                "access": True,
                "property": "sc-domain:nordan.se",
                "current": {"clicks": 80, "impressions": 900, "ctr": 8.9, "position": 4.0},
                "previous": {"clicks": 60, "impressions": 800, "ctr": 7.5, "position": 5.0},
                "window": {"start": "2026-09-04", "end": "2026-10-01"},
                "daily": {"clicks": [1, 2, 3]},
                "alert_texts": ["hemligt för kunden"],
            },
        )
        customer = self.client_for(self.contact).get("/kund/status/").content.decode()
        staff = self.client_for(self.staff)
        staff.post(f"/manage/kunder/{self.acme.pk}/visa-som/")
        as_customer = staff.get("/kund/status/").content.decode()

        def main(html):
            return html[html.index('<div class="tv-head">') : html.rindex("</section>")]

        self.assertEqual(main(customer), main(as_customer))
        self.assertIn("+33 %", customer)
        self.assertNotIn("hemligt för kunden", customer)

    def test_drift_links_to_the_google_page_and_counts_google_alerts(self):
        Check.objects.create(
            domain=self.domain,
            kind=Kind.GBP,
            ok=True,
            data={"ok": True, "linked": True, "alert_texts": ["a", "b"]},
        )
        html = self.client_for(self.staff).get("/manage/drift/").content.decode()
        self.assertIn(f"/manage/overvakning/doman/{self.domain.pk}/google/", html)
        self.assertIn("Google 2", html)
        card = self.client_for(self.staff).get(f"/manage/kunder/{self.acme.pk}/").content.decode()
        self.assertIn("Google-data", card)

    def test_the_customer_is_never_mailed(self):
        crux = [("POST", "chromeuxreport.googleapis.com", "/v1/", NOT_FOUND)]
        fake = FakeGoogle(crux + gbp_routes() + analytics(cur_clicks=10, home_verdict="FAIL"))
        with (
            patched(fake),
            mock.patch.object(
                checks, "check_performance", return_value={"ok": True, "strategies": {}}
            ),
        ):
            run_all(daily=True, domains=[self.domain])
        self.assertEqual(
            {c.kind for c in Check.objects.filter(domain=self.domain)} - {"uptime", "performance"},
            {"crux", "search", "gbp"},
        )
        self.assertEqual(len(mail.outbox), 1, "larmen går till byrån")
        for message in mail.outbox:
            self.assertEqual(message.to, ["staff@example.com"])
