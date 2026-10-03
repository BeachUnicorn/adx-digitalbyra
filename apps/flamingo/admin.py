"""
ADX Flamingo i Djangos admin: för felsökning och rättningar i nödfall.

Det vanliga arbetet görs i verktyget (/flamingo/app/) och på byråns sida
(/manage/flamingo/). Inget här mejlar eller sms:ar någon, och inget här
anropar Google. Googles nyckel (GoogleAdsConnection) visas aldrig: raden är
skrivskyddad och kopplas om från byråns sida.
"""

from django.contrib import admin

from .models import (
    Campaign,
    CampaignDayStats,
    ConversionUpload,
    Fact,
    FlamingoAccount,
    GoogleAdsConnection,
    Lead,
    Review,
    Service,
    SmsLog,
)


class FactInline(admin.TabularInline):
    model = Fact
    extra = 0
    fields = ("order", "key", "label", "value", "source", "confirmed")


class ServiceInline(admin.TabularInline):
    model = Service
    extra = 0
    fields = ("order", "name", "sales_mode", "is_active")


@admin.register(FlamingoAccount)
class FlamingoAccountAdmin(admin.ModelAdmin):
    list_display = (
        "customer",
        "is_enabled",
        "is_demo",
        "google_status",
        "google_billing_status",
        "scan_status",
        "notify_sms",
    )
    list_filter = ("is_enabled", "is_demo", "google_status", "scan_status")
    search_fields = ("customer__name", "website_url", "google_ads_customer_id")
    raw_id_fields = ("customer", "enabled_by")
    readonly_fields = (
        "enabled_at",
        "scanned_at",
        "google_billing_status",
        "google_auto_tagging",
        "google_synced_at",
        "google_sync_error",
        "google_link_requested_at",
        "google_link_requested_for",
        "google_conversion_actions",
        "publish_day",
        "publish_count",
        "created_at",
        "updated_at",
    )
    inlines = [ServiceInline, FactInline]


class ReviewInline(admin.StackedInline):
    model = Review
    extra = 0
    fields = (
        "round",
        "state",
        "submitted_at",
        "submitted_by",
        "reviewer",
        "reviewed_at",
        "changes",
        "note",
    )
    raw_id_fields = ("submitted_by", "reviewer")


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "account",
        "status",
        "review_requested",
        "daily_budget_kr",
        "page_slug",
        "updated_at",
    )
    list_filter = ("status", "review_requested")
    search_fields = ("name", "page_slug", "account__customer__name")
    raw_id_fields = ("account", "service", "approved_by", "created_by")
    readonly_fields = (
        "google_resources",
        "google_synced_at",
        "google_publish_sent",
        "google_attempted_at",
        "agency_alerted_at",
        "agency_alert_subject",
        "created_at",
        "updated_at",
    )
    inlines = [ReviewInline]


@admin.register(CampaignDayStats)
class CampaignDayStatsAdmin(admin.ModelAdmin):
    list_display = ("campaign", "date", "cost_kr", "impressions", "clicks", "conversions")
    search_fields = ("campaign__name", "campaign__account__customer__name")
    raw_id_fields = ("campaign",)
    date_hierarchy = "date"
    readonly_fields = ("updated_at",)

    @admin.display(description="Kostnad (kr)")
    def cost_kr(self, obj):
        return obj.cost_kr


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("campaign", "round", "state", "submitted_at", "reviewer", "reviewed_at")
    list_filter = ("state",)
    search_fields = ("campaign__name", "campaign__account__customer__name")
    raw_id_fields = ("campaign", "submitted_by", "reviewer")


class ConversionInline(admin.StackedInline):
    model = ConversionUpload
    extra = 0


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("display_name", "account", "source", "status", "value_kr", "created_at")
    list_filter = ("status", "source", "ad_consent")
    search_fields = (
        "name",
        "phone",
        "email",
        "account__customer__name",
        "gclid",
        "gbraid",
        "wbraid",
    )
    raw_id_fields = ("account", "campaign", "service")
    readonly_fields = ("updated_at",)
    date_hierarchy = "created_at"
    inlines = [ConversionInline]


@admin.register(ConversionUpload)
class ConversionUploadAdmin(admin.ModelAdmin):
    list_display = (
        "lead",
        "kind",
        "value_kr",
        "status",
        "attempts",
        "downloaded_at",
        "exported_at",
        "sent_at",
        "created_at",
    )
    list_filter = ("kind", "status")
    raw_id_fields = ("lead",)


@admin.register(GoogleAdsConnection)
class GoogleAdsConnectionAdmin(admin.ModelAdmin):
    """Bara för att se läget. Nyckeln (refresh_token_encrypted) är inte
    redigerbar och visas inte; kopplingen görs och tas bort på byråns sida."""

    list_display = ("__str__", "google_email", "connected_at", "last_ok_at", "last_error")
    fields = (
        "has_token",
        "google_email",
        "connected_by",
        "connected_at",
        "last_ok_at",
        "last_error",
        "granted_scopes",
        "conversion_upload_blocked_at",
        "conversion_upload_error",
        "updated_at",
    )
    readonly_fields = fields

    @admin.display(boolean=True, description="Nyckel sparad")
    def has_token(self, obj):
        return obj.is_connected

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SmsLog)
class SmsLogAdmin(admin.ModelAdmin):
    list_display = ("account", "kind", "to", "status", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("to", "account__customer__name")
    raw_id_fields = ("account", "lead")


admin.site.register(Fact)
admin.site.register(Service)
