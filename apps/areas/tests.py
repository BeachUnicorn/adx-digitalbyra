"""
Tester för städerna (/digitalbyra/).

Omskrivna vid genomlysningen 2026-08-27: den gamla filen var byte-identisk
med systersajtens och testade rutter och beteenden som inte finns i ADX.
Här testas det ADX faktiskt gör: stadsöversikt, stadssida med hårdkodad
"Digitalbyrå i {stad}"-rubrik, synlighetsarv och doorway-vakten.
"""

from django.test import TestCase


class CityPageTests(TestCase):
    """Stadssidan i ADX-designen: hero + unik brödtext + alla aktiva
    tjänster + övriga städer. EN sida per stad (doorway-regeln)."""

    def setUp(self):
        from apps.areas.models import Area, AreaLevel
        from apps.services.models import Service, ServiceCategory

        self.goteborg = Area.objects.create(
            name="Göteborg",
            level=AreaLevel.REGION,
            intro="Vi bygger digitalt för Göteborg.",
            body="Första stycket.\n\nAndra stycket.",
        )
        self.malmo = Area.objects.create(name="Malmö", level=AreaLevel.REGION)
        category = ServiceCategory.objects.create(name="Utveckling")
        self.service = Service.objects.create(
            category=category, name="Webbutveckling", is_active=True
        )

    def test_city_list_links_every_active_city(self):
        html = self.client.get("/digitalbyra/").content.decode()
        self.assertIn(self.goteborg.get_absolute_url(), html)
        self.assertIn(self.malmo.get_absolute_url(), html)

    def test_inactive_city_is_absent_from_the_list(self):
        self.malmo.is_active = False
        self.malmo.save(update_fields=["is_active"])
        html = self.client.get("/digitalbyra/").content.decode()
        self.assertNotIn(self.malmo.get_absolute_url(), html)

    def test_city_page_shows_adx_heading_intro_and_body(self):
        html = self.client.get(self.goteborg.get_absolute_url()).content.decode()
        self.assertIn("Digitalbyrå", html)
        self.assertIn("Göteborg", html)
        self.assertIn("Vi bygger digitalt för Göteborg.", html)
        self.assertIn("Första stycket.", html)
        self.assertIn("Andra stycket.", html)

    def test_city_page_lists_all_active_services_and_other_cities(self):
        html = self.client.get(self.goteborg.get_absolute_url()).content.decode()
        self.assertIn("Webbutveckling", html)
        self.assertIn(self.malmo.get_absolute_url(), html)

    def test_hidden_city_is_404(self):
        self.goteborg.is_active = False
        self.goteborg.save(update_fields=["is_active"])
        response = self.client.get(self.goteborg.get_absolute_url())
        self.assertEqual(response.status_code, 404)

    def test_inactive_parent_hides_the_child_page(self):
        """Synlighetsarvet: släcks kommunen ska stadsdelen se ut att inte
        finnas, utan att dess egen flagga rörs."""
        from apps.areas.models import Area, AreaLevel

        kommun = Area.objects.create(
            name="Solna", level=AreaLevel.MUNICIPALITY, parent=self.goteborg
        )
        district = Area.objects.create(name="Råsunda", level=AreaLevel.DISTRICT, parent=kommun)
        self.assertEqual(self.client.get(district.get_absolute_url()).status_code, 200)

        kommun.is_active = False
        kommun.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(district.get_absolute_url()).status_code, 404)
        district.refresh_from_db()
        self.assertTrue(district.is_active)

    def test_sitemap_lists_visible_cities_only(self):
        self.malmo.is_active = False
        self.malmo.save(update_fields=["is_active"])
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertIn(self.goteborg.get_absolute_url(), xml)
        self.assertNotIn(self.malmo.get_absolute_url(), xml)


class DisplayHeadingTests(TestCase):
    """Rubrikfallbacken är ADX:s, inte systersajtens."""

    def test_empty_heading_falls_back_to_digitalbyra(self):
        from apps.areas.models import Area, AreaLevel

        area = Area.objects.create(name="Uppsala", level=AreaLevel.REGION, heading="")
        self.assertEqual(area.display_heading, "Digitalbyrå i Uppsala")

    def test_set_heading_wins(self):
        from apps.areas.models import Area, AreaLevel

        area = Area.objects.create(
            name="Uppsala", level=AreaLevel.REGION, heading="Webbyrå i Uppsala"
        )
        self.assertEqual(area.display_heading, "Webbyrå i Uppsala")


class DoorwayGuardTests(TestCase):
    """
    Tjänst-och-stad-sidorna finns inte (doorway-regeln): samma tjänstetext
    och samma stadstext med stadsnamnet inbytt i rubriken är doorway pages.
    Stadssidan visar i stället ALLA aktiva tjänster.
    """

    def setUp(self):
        from apps.areas.models import Area, AreaLevel
        from apps.services.models import Service, ServiceCategory

        self.area = Area.objects.create(name="Örebro", level=AreaLevel.REGION)
        category = ServiceCategory.objects.create(name="Utveckling")
        self.service = Service.objects.create(category=category, name="Automation", is_active=True)

    def test_combination_url_is_gone(self):
        response = self.client.get(f"/digitalbyra/{self.area.slug}/{self.service.slug}/")
        self.assertEqual(response.status_code, 404)

    def test_url_name_no_longer_resolvable(self):
        from django.urls import NoReverseMatch, reverse

        with self.assertRaises(NoReverseMatch):
            reverse("areas:area_service_detail", kwargs={"slug": "x", "service_slug": "y"})

    def test_area_page_lists_all_services_without_any_link_rows(self):
        from apps.areas.models import AreaService

        self.assertEqual(AreaService.objects.count(), 0)
        html = self.client.get(self.area.get_absolute_url()).content.decode()
        self.assertIn("Automation", html)

    def test_sitemap_has_no_combination_pages(self):
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertNotIn(f"/{self.area.slug}/{self.service.slug}/", xml)

    def test_manage_area_form_has_no_service_matrix(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user("areaadm", password="x", is_staff=True)
        self.client.force_login(user)
        html = self.client.get(f"/manage/serviceomraden/{self.area.pk}/").content.decode()
        self.assertNotIn("Tjänster och målgrupper", html)
        self.assertNotIn('name="svc_on"', html)
