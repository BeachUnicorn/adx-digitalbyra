"""
Steg 2 (README, Giovanni 2026-10-28): landningssidornas egna adresser,
källorna och besöken.

- FlamingoAccount.lp_slug (null, unik) och LandingPage.slug (db_default "",
  unik per konto när den finns): /lp/<konto>/<sida>/.
- LandingPageOldSlug: en adress sidan hade förut (skickar vidare).
- PageVisitDay: besöken per sida, svensk dag och källa.
- Lead.landing_page, Lead.page_source (db_default "") och Lead.named_link:
  sidan och källan för förfrågan.

Datamigreringen ger konton och sidor sina adresser (addresses.assign_missing)
och gamla förfrågningar från en kampanjs sida sin sida (kampanjens sida nu,
den enda som går att veta) och sin källa (spårets kanal, annars Annons).
Bara rader som saknar värdet, så den kan köras igen. Baklänges gör den
ingenting (kolumnerna tas bort av schemats baklängessteg).

README för utskick B.0: den förra versionen skriver förfrågningar, sidor och
konton mellan migrate och reload, och för gott efter en tillbakarullning.
Därför är varje nytt fält på en äldre tabell null eller har db_default, och
en rad som den förra versionen skrev läses med samma regler
(sources.lead_source_expr, addresses.page_path). Varje ny främmande nyckel
får sin raderingsregel också i databasen (apps/utskick/dbfk.py), så att den
förra versionen kan ta bort en sida, en kampanj, ett utskick, en länk eller
ett konto.
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models
from django.db.models import Case, OuterRef, Q, Subquery, Value, When

from apps.utskick import dbfk

#: Varje främmande nyckel som migreringen skapar.
FOREIGN_KEYS = [
    ("flamingo", "landingpageoldslug", "account"),
    ("flamingo", "landingpageoldslug", "page"),
    ("flamingo", "pagevisitday", "account"),
    ("flamingo", "pagevisitday", "page"),
    ("flamingo", "pagevisitday", "campaign"),
    ("flamingo", "pagevisitday", "utskick"),
    ("flamingo", "pagevisitday", "link"),
    ("flamingo", "lead", "landing_page"),
    ("flamingo", "lead", "named_link"),
]

SOURCES = [
    ("ad", "Annons"),
    ("sms", "Sms"),
    ("email", "E-post"),
    ("named", "Länk"),
    ("direct", "Direkt"),
]


def on_delete_in_database(apps, schema_editor):
    dbfk.apply(apps, schema_editor, FOREIGN_KEYS)


def forwards(apps, schema_editor):
    from apps.flamingo import addresses

    addresses.assign_missing(apps)

    Campaign = apps.get_model("flamingo", "Campaign")
    Lead = apps.get_model("flamingo", "Lead")
    TrackedLink = apps.get_model("utskick", "TrackedLink")
    page_leads = Lead.objects.filter(source__in=("form", "call_click"))
    page_leads.filter(landing_page__isnull=True, campaign__isnull=False).update(
        landing_page_id=Subquery(
            Campaign.objects.filter(pk=OuterRef("campaign_id")).values("landing_page_id")[:1]
        )
    )
    page_leads.filter(landing_page__isnull=False, page_source="").update(
        page_source=Case(
            When(attribution__channel="named", then=Value("named")),
            When(attribution__channel="sms", then=Value("sms")),
            When(attribution__channel="email", then=Value("email")),
            When(Q(campaign__isnull=False), then=Value("ad")),
            default=Value("direct"),
            output_field=models.CharField(),
        )
    )
    named = page_leads.filter(page_source="named", named_link__isnull=True)
    for pk, attribution in named.values_list("pk", "attribution"):
        link_id = attribution.get("link") if isinstance(attribution, dict) else None
        if not isinstance(link_id, int):
            continue
        link = (
            TrackedLink.objects.filter(pk=link_id, utskick__isnull=True)
            .values_list("pk", "account_id")
            .first()
        )
        if link is None:
            continue
        Lead.objects.filter(pk=pk, account_id=link[1], named_link__isnull=True).update(
            named_link_id=link[0]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("flamingo", "0018_flervalssvar"),
        ("utskick", "0005_gamla_adresser"),
    ]

    operations = [
        migrations.CreateModel(
            name="LandingPageOldSlug",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("slug", models.SlugField(max_length=60, verbose_name="Tidigare adress")),
                ("retired_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "page",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="old_slugs",
                        to="flamingo.landingpage",
                    ),
                ),
            ],
            options={
                "verbose_name": "Tidigare adress för en landningssida",
                "verbose_name_plural": "Tidigare adresser för landningssidor",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("account", "slug"), name="flamingo_page_old_slug"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="PageVisitDay",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("day", models.DateField(verbose_name="Dag")),
                (
                    "source",
                    models.CharField(choices=SOURCES, max_length=8, verbose_name="Källa"),
                ),
                ("key", models.CharField(max_length=40, verbose_name="Nyckel")),
                ("visits", models.PositiveIntegerField(default=0, verbose_name="Besök")),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="flamingo.flamingoaccount",
                    ),
                ),
                (
                    "page",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="visit_days",
                        to="flamingo.landingpage",
                    ),
                ),
                (
                    "campaign",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="flamingo.campaign",
                    ),
                ),
                (
                    "utskick",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="utskick.utskick",
                    ),
                ),
                (
                    "link",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="utskick.trackedlink",
                    ),
                ),
            ],
            options={
                "verbose_name": "Besök på en landningssida",
                "verbose_name_plural": "Besök på landningssidor",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("page", "day", "key"), name="flamingo_visit_day"
                    )
                ],
                "indexes": [
                    models.Index(fields=["account", "day"], name="flamingo_visit_account_day")
                ],
            },
        ),
        migrations.AddField(
            model_name="flamingoaccount",
            name="lp_slug",
            field=models.SlugField(
                blank=True,
                max_length=40,
                null=True,
                unique=True,
                verbose_name="Adress för landningssidorna",
            ),
        ),
        migrations.AddField(
            model_name="landingpage",
            name="slug",
            field=models.SlugField(
                blank=True, db_default="", default="", max_length=60, verbose_name="Adress"
            ),
        ),
        migrations.AddConstraint(
            model_name="landingpage",
            constraint=models.UniqueConstraint(
                condition=models.Q(("slug", ""), _negated=True),
                fields=("account", "slug"),
                name="flamingo_page_slug",
            ),
        ),
        migrations.AddField(
            model_name="lead",
            name="landing_page",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="leads",
                to="flamingo.landingpage",
                verbose_name="Landningssida",
            ),
        ),
        migrations.AddField(
            model_name="lead",
            name="page_source",
            field=models.CharField(
                blank=True,
                choices=SOURCES,
                db_default="",
                default="",
                max_length=8,
                verbose_name="Källa på sidan",
            ),
        ),
        migrations.AddField(
            model_name="lead",
            name="named_link",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="utskick.trackedlink",
                verbose_name="Namngiven länk",
            ),
        ),
        migrations.RunPython(on_delete_in_database, migrations.RunPython.noop),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
