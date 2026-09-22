"""Ärendebeskrivningen som HTML (apps/projects/richtext.py)."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.projects.models import Customer, Issue
from apps.projects.richtext import html_to_text, sanitize_issue_html, text_to_html


class SanitizeTests(TestCase):
    def test_keeps_exactly_what_the_toolbar_can_make(self):
        html = (
            '<p style="text-align: center"><strong>Fet</strong> <s>struken</s> '
            "<mark>markerad</mark> "
            '<a href="https://adx.se/">länk</a></p><ul><li>a</li></ul><ol><li>b</li></ol>'
            "<table><tbody><tr><th>K</th><td>v</td></tr></tbody></table>"
        )
        out = sanitize_issue_html(html)
        for needle in (
            "<strong>",
            "<s>",
            "<mark>",
            'href="https://adx.se/"',
            "<ul>",
            "<ol>",
            "<table>",
            "<th>",
            'style="text-align: center"',
        ):
            self.assertIn(needle, out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_strips_everything_dangerous(self):
        html = (
            '<p onclick="x()">hej</p><script>alert(1)</script><img src=x onerror=alert(1)>'
            '<a href="javascript:alert(1)">j</a><p style="color:red;position:fixed">s</p>'
            '<p style="text-align: right; color: red">t</p><h1>rubrik</h1>'
            '<iframe src="//x"></iframe>'
        )
        out = sanitize_issue_html(html)
        for bad in (
            "<script",
            "onclick",
            "onerror",
            "<img",
            "javascript:",
            "color",
            "position",
            "<h1",
            "<iframe",
        ):
            self.assertNotIn(bad, out)
        self.assertIn("hej", out)
        self.assertIn("rubrik", out)  # texten kvar, taggen borta
        self.assertNotIn('style="text-align: right; color: red"', out)
        self.assertNotIn("text-align: right", out)  # blandad style förkastas helt

    def test_plain_text_becomes_paragraphs(self):
        self.assertEqual(
            sanitize_issue_html("Rad ett\nrad två\n\nNytt stycke"),
            "<p>Rad ett<br>rad två</p><p>Nytt stycke</p>",
        )
        self.assertEqual(text_to_html("a < b & c"), "<p>a &lt; b &amp; c</p>")

    def test_empty_editor_output_is_stored_as_empty(self):
        self.assertEqual(sanitize_issue_html("<p></p>"), "")
        self.assertEqual(sanitize_issue_html("<p><br></p>"), "")
        self.assertEqual(sanitize_issue_html(""), "")

    def test_html_to_text_reads_well(self):
        html = (
            "<p>Hej <strong>du</strong></p><ul><li>ett</li><li>två</li></ul>"
            "<table><tr><th>K</th><td>V</td></tr></table>"
        )
        self.assertEqual(html_to_text(html), "Hej du\n\n- ett\n- två\n\nK\tV")


class IssueTests(TestCase):
    def test_description_is_sanitized_on_every_save_and_has_a_text_version(self):
        issue = Issue.objects.create(title="x", description="<p>ok</p><script>x</script>")
        self.assertEqual(issue.description, "<p>ok</p>")
        self.assertEqual(issue.description_text, "ok")

    def test_portal_form_stores_html_and_the_page_renders_it(self):
        User = get_user_model()
        contact = User.objects.create_user("nina", email="nina@acme.se")
        customer = Customer.objects.create(name="Acme AB")
        customer.users.add(contact)
        self.client.force_login(contact)
        self.client.post(
            "/kund/arenden/nytt/",
            {
                "title": "Formaterat",
                "description": "<p><strong>Fet</strong> text</p><img src=x onerror=alert(1)>",
            },
        )
        issue = Issue.objects.get(title="Formaterat")
        self.assertEqual(issue.description, "<p><strong>Fet</strong> text</p>")
        page = self.client.get(f"/kund/arenden/{issue.pk}/")
        self.assertContains(page, "<strong>Fet</strong> text")
        form_page = self.client.get("/kund/arenden/nytt/")
        self.assertContains(form_page, 'data-tiptap="issue"')
        self.assertContains(form_page, "dist/tiptap-editor.js")
