"""
Hjälp för testerna: kampanjer som skapas med det gamla Campaign.page får
sin LandingPage precis som migreringen 0011 gav de befintliga kampanjerna
(samma frysta mappning), så att testerna prövar samma väg som riktig data.

    pages_from_campaigns(*campaigns)

Live och pausade kampanjer får sidan publicerad. Kampanjerna som skickas
med läses om, så att campaign.landing_page är satt i testet.
"""

from importlib import import_module

MIGRATION = "apps.flamingo.migrations.0011_sidor_fran_kampanjerna"


def migration():
    return import_module(MIGRATION)


def pages_from_campaigns(*campaigns):
    from django.apps import apps

    migration().forwards(apps, None)
    for campaign in campaigns:
        campaign.refresh_from_db(fields=["landing_page"])
    return campaigns
