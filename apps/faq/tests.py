"""
FAQ-sidornas väg tillbaka: länkmotorns FAQ-gren.

Sektionssidorna var återvändsgränder - en besökare (eller Google) som
landade där hade ingen väg vidare till sidan frågorna handlar om, trots
att ägarskapet redan finns i datan (faq-blocken bär sektionens id).
"""

from django.test import Client, TestCase

from apps.faq.models import FAQItem, FAQSection
from apps.website.models import Block, BlockPage


class FaqOwnerLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.section = FAQSection.objects.create(
            slug="faq-testbransch", title="Testbransch", is_active=True
        )
        FAQItem.objects.create(
            section=cls.section, question="En fråga?", answer="Ett svar.", is_active=True
        )
        cls.page = BlockPage.objects.create(
            slug="hemsida-testbransch", title="Hemsida för testbransch", is_published=True
        )
        Block.objects.create(
            page=cls.page,
            block_type="faq",
            # Seedvägen lagrar id:t som sträng - motorn ska tåla båda.
            data={"faq_section_id": str(cls.section.pk)},
            order=1,
            is_visible=True,
        )

    def test_the_section_page_links_back_to_its_owning_page(self):
        html = Client().get(f"/faq/{self.section.slug}/").content.decode()
        self.assertIn('href="/hemsida-testbransch/"', html)
        self.assertIn("gäller", html)

    def test_an_integer_section_id_is_matched_too(self):
        block = self.page.blocks.get(block_type="faq")
        block.data = {"faq_section_id": self.section.pk}
        block.save(update_fields=["data"])
        html = Client().get(f"/faq/{self.section.slug}/").content.decode()
        self.assertIn('href="/hemsida-testbransch/"', html)

    def test_a_draft_page_is_never_linked(self):
        self.page.is_published = False
        self.page.save(update_fields=["is_published"])
        html = Client().get(f"/faq/{self.section.slug}/").content.decode()
        self.assertNotIn('href="/hemsida-testbransch/"', html)
