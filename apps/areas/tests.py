"""Tester för serviceområden."""

from django.test import TestCase


class AreaServiceRemovalTests(TestCase):
    """
    Tjänst-och-ort-sidorna är borttagna (2026-08-23): samma tjänstetext och
    samma ortstext med ortsnamnet inbytt i rubriken är doorway pages.
    Ortssidan visar i stället ALLA aktiva tjänster - firman utför alla
    tjänster i alla områden, så en koppling per område vore både felaktig
    och en underhållsbörda.
    """

    def setUp(self):
        from apps.areas.models import Area, AreaLevel
        from apps.services.models import Service, ServiceCategory

        self.area = Area.objects.create(name="Bromma", level=AreaLevel.DISTRICT, body="Lokal text")
        category = ServiceCategory.objects.create(name="Vatten")
        self.service = Service.objects.create(category=category, name="Spolning", is_active=True)

    def test_combination_url_is_gone(self):
        response = self.client.get(f"/rormokare/{self.area.slug}/{self.service.slug}/")
        self.assertEqual(response.status_code, 404)

    def test_url_name_no_longer_resolvable(self):
        from django.urls import NoReverseMatch, reverse

        with self.assertRaises(NoReverseMatch):
            reverse("areas:area_service_detail", kwargs={"slug": "x", "service_slug": "y"})

    def test_area_page_lists_all_services_without_any_link_rows(self):
        from apps.areas.models import AreaService

        self.assertEqual(AreaService.objects.count(), 0)
        html = self.client.get(self.area.get_absolute_url()).content.decode()
        self.assertIn("Spolning", html)

    def test_sitemap_has_no_combination_pages(self):
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertNotIn(f"/{self.area.slug}/{self.service.slug}/", xml)

    def test_admin_area_form_has_no_service_matrix(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user("areaadm", password="x", is_staff=True)
        self.client.force_login(user)
        html = self.client.get(f"/manage/serviceomraden/{self.area.pk}/").content.decode()
        self.assertNotIn("Tjänster och målgrupper", html)
        self.assertNotIn('name="svc_on"', html)


class CategoryCardTests(TestCase):
    """
    Ortssidan visar KATEGORIER, inte tjänster: 76 tjänster upprepade på 232
    ortssidor vore en katalog - samma tunna innehåll som tjänst-och-ort-
    sidorna. Antalet kategorier är dessutom stabilt när utbudet växer.
    """

    def setUp(self):
        from apps.areas.models import Area, AreaLevel
        from apps.services.models import Service, ServiceCategory

        self.bromma = Area.objects.create(name="Bromma", level=AreaLevel.DISTRICT, body="Text")
        self.solna = Area.objects.create(name="Solna", level=AreaLevel.DISTRICT, body="Text")
        for i in range(3):
            category = ServiceCategory.objects.create(name=f"Kategori {i}", order=i)
            for j in range(4):
                Service.objects.create(category=category, name=f"Tjänst {i}-{j}", is_active=True)

    def _examples(self, area):
        from apps.areas.views import _category_cards

        return [card["example"].name for card in _category_cards(area)]

    def test_one_card_per_category(self):
        from apps.areas.views import _category_cards

        self.assertEqual(len(_category_cards(self.bromma)), 3)

    def test_examples_are_stable_for_the_same_area(self):
        """
        Fröet är orten, inte slumpen: sidan ska se likadan ut varje gång
        någon besöker den eller Google hämtar den.
        """
        self.assertEqual(self._examples(self.bromma), self._examples(self.bromma))

    def test_different_areas_get_different_examples(self):
        """Det som gör 232 ortssidor mindre lika varandra, inte mer."""
        self.assertNotEqual(self._examples(self.bromma), self._examples(self.solna))

    def test_inactive_services_are_never_examples(self):
        from apps.services.models import Service

        Service.objects.update(is_active=False)
        Service.objects.filter(name="Tjänst 0-2").update(is_active=True)
        from apps.areas.views import _category_cards

        cards = _category_cards(self.bromma)
        self.assertEqual([c["example"].name for c in cards], ["Tjänst 0-2"])

    def test_category_without_active_services_is_skipped(self):
        from apps.services.models import Service, ServiceCategory

        ServiceCategory.objects.create(name="Tom kategori", order=9)
        from apps.areas.views import _category_cards

        names = [c["category"].name for c in _category_cards(self.bromma)]
        self.assertNotIn("Tom kategori", names)
        self.assertTrue(Service.objects.filter(is_active=True).exists())
