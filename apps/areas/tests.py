"""
Tester för städerna (/webbyra/).

Omskrivna vid genomlysningen 2026-08-27: den gamla filen var byte-identisk
med systersajtens och testade rutter och beteenden som inte finns i ADX.
Här testas det ADX faktiskt gör: stadsöversikt, stadssida med rubriken
"Webbyrå i {stad}" (från modellen, se HeadingIsEditableTests), synlighetsarv
och doorway-vakten.
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
        html = self.client.get("/webbyra/").content.decode()
        self.assertIn(self.goteborg.get_absolute_url(), html)
        self.assertIn(self.malmo.get_absolute_url(), html)

    def test_inactive_city_is_absent_from_the_list(self):
        self.malmo.is_active = False
        self.malmo.save(update_fields=["is_active"])
        html = self.client.get("/webbyra/").content.decode()
        self.assertNotIn(self.malmo.get_absolute_url(), html)

    def test_city_page_shows_adx_heading_intro_and_body(self):
        html = self.client.get(self.goteborg.get_absolute_url()).content.decode()
        self.assertIn("Webbyrå i Göteborg", html)
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

    def test_empty_heading_falls_back_to_webbyra(self):
        from apps.areas.models import Area, AreaLevel

        area = Area.objects.create(name="Uppsala", level=AreaLevel.REGION, heading="")
        self.assertEqual(area.display_heading, "Webbyrå i Uppsala")

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
        response = self.client.get(f"/webbyra/{self.area.slug}/{self.service.slug}/")
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


class LegacyUrlRedirectTests(TestCase):
    """
    /digitalbyra/ -> /webbyra/ (2026-08-29).

    Sökordet byttes till "webbyrå"; adresserna får inte bara försvinna.
    En 301 flyttar över det som redan hunnit indexeras eller länkas.
    """

    def setUp(self):
        from apps.areas.models import Area, AreaLevel

        self.region = Area.objects.create(name="Uppsala län", level=AreaLevel.REGION)
        self.area = Area.objects.create(
            name="Uppsala", level=AreaLevel.MUNICIPALITY, parent=self.region
        )

    def test_old_index_redirects_permanently(self):
        response = self.client.get("/digitalbyra/")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/webbyra/")

    def test_old_area_page_redirects_permanently(self):
        response = self.client.get(f"/digitalbyra/{self.area.slug}/")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], f"/webbyra/{self.area.slug}/")

    def test_the_redirect_target_actually_serves_the_page(self):
        response = self.client.get(f"/digitalbyra/{self.area.slug}/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Webbyrå i Uppsala")

    def test_an_unknown_slug_redirects_then_404s(self):
        """Redirecten slår inte upp området - den riktiga vyn avgör."""
        response = self.client.get("/digitalbyra/finns-inte/", follow=True)
        self.assertEqual(response.status_code, 404)


class HeadingIsEditableTests(TestCase):
    """
    Rubriken kom från mallen, inte från modellen.

    area_detail.html hade "Digitalbyrå<br>i {{ area.name }}" hårdkodat, så
    redaktörens (och AI:ns) heading-fält skrevs men syntes aldrig - och ett
    sökordsbyte krävde en kodändring i stället för en textändring.
    """

    def setUp(self):
        from apps.areas.models import Area, AreaLevel

        self.area = Area.objects.create(name="Kiruna", level=AreaLevel.MUNICIPALITY)

    def test_default_heading_is_webbyra(self):
        html = self.client.get(f"/webbyra/{self.area.slug}/").content.decode()
        self.assertIn("Webbyrå i Kiruna", html)

    def test_a_custom_heading_reaches_the_page(self):
        self.area.heading = "Webbyrå och hemsidor i Kiruna"
        self.area.save()
        html = self.client.get(f"/webbyra/{self.area.slug}/").content.decode()
        self.assertIn("Webbyrå och hemsidor i Kiruna", html)
        self.assertNotIn("Webbyrå i Kiruna.", html)


class AreaListGroupingTests(TestCase):
    """
    Översikten grupperas per län.

    Med drygt hundra orter blir en enda alfabetisk lista obrukbar, och den
    blandar dessutom län, kommuner och stadsdelar som om de vore jämförbara.
    """

    def setUp(self):
        from apps.areas.models import Area, AreaLevel

        self.skane = Area.objects.create(name="Skåne län", level=AreaLevel.REGION)
        self.stockholm_lan = Area.objects.create(name="Stockholms län", level=AreaLevel.REGION)
        self.malmo = Area.objects.create(
            name="Malmö", level=AreaLevel.MUNICIPALITY, parent=self.skane
        )
        self.limhamn = Area.objects.create(
            name="Limhamn", level=AreaLevel.DISTRICT, parent=self.malmo
        )

    def test_every_region_gets_a_heading(self):
        html = self.client.get("/webbyra/").content.decode()
        self.assertIn("Skåne län", html)
        self.assertIn("Stockholms län", html)

    def test_a_district_is_listed_under_its_region(self):
        """Stadsdelen hör hemma under länet, inte under kommunen - översikten
        är en väg in, inte en avbild av hierarkin."""
        html = self.client.get("/webbyra/").content.decode()
        skane_block = html.split("Skåne län")[1].split("Stockholms län")[0]
        self.assertIn(self.malmo.get_absolute_url(), skane_block)
        self.assertIn(self.limhamn.get_absolute_url(), skane_block)

    def test_an_inactive_area_is_not_listed(self):
        self.malmo.is_active = False
        self.malmo.save(update_fields=["is_active"])
        html = self.client.get("/webbyra/").content.decode()
        self.assertNotIn(self.malmo.get_absolute_url(), html)

    def test_hiding_a_municipality_also_hides_its_districts(self):
        """Synlighetsarvet ska gälla även i listan, inte bara på sidan."""
        self.malmo.is_active = False
        self.malmo.save(update_fields=["is_active"])
        html = self.client.get("/webbyra/").content.decode()
        self.assertNotIn(self.limhamn.get_absolute_url(), html)


class AreaTitleTests(TestCase):
    """
    En satt meta_title är hela sidtiteln.

    Mallen la på sajtnamnet ovanpå, så en SEO-titel som redan slutade med
    "| ADX" blev "... | ADX - ADX" i webbläsarfliken och i sökresultatet.
    website/page.html gjorde redan rätt; den här mallen hade halkat efter.
    """

    def setUp(self):
        from apps.areas.models import Area, AreaLevel

        self.area = Area.objects.create(name="Kista", level=AreaLevel.MUNICIPALITY)

    def _title(self):
        import re

        html = self.client.get(self.area.get_absolute_url()).content.decode()
        return re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()

    def test_a_set_meta_title_is_used_verbatim(self):
        self.area.meta_title = "Webbyrå i Kista - hemsida & webbutveckling | ADX"
        self.area.save(update_fields=["meta_title"])
        self.assertEqual(self._title(), "Webbyrå i Kista - hemsida &amp; webbutveckling | ADX")

    def test_without_meta_title_the_site_name_is_appended(self):
        self.assertEqual(self._title(), "Kista - ADX")
