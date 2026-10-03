"""
ADX Flamingo i Djangos admin: för felsökning och rättningar i nödfall.

Det vanliga arbetet görs i verktyget (/flamingo/app/) och på byråns sida
(/manage/flamingo/). Inget här mejlar eller sms:ar någon.
"""

from django.contrib import admin

from .models import (
    Campaign,
    ConversionUpload,
    Fact,
    FlamingoAccount,
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
    list_display = ("customer", "is_enabled", "google_status", "scan_status", "notify_sms")
    list_filter = ("is_enabled", "google_status", "scan_status")
    search_fields = ("customer__name", "website_url", "google_ads_customer_id")
    raw_id_fields = ("customer", "enabled_by")
    readonly_fields = ("enabled_at", "scanned_at", "created_at", "updated_at")
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
    list_display = ("name", "account", "status", "daily_budget_kr", "page_slug", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "page_slug", "account__customer__name")
    raw_id_fields = ("account", "service", "approved_by", "created_by")
    readonly_fields = ("created_at", "updated_at")
    inlines = [ReviewInline]


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
    list_filter = ("status", "source")
    search_fields = ("name", "phone", "email", "account__customer__name", "gclid")
    raw_id_fields = ("account", "campaign", "service")
    readonly_fields = ("updated_at",)
    date_hierarchy = "created_at"
    inlines = [ConversionInline]


@admin.register(ConversionUpload)
class ConversionUploadAdmin(admin.ModelAdmin):
    list_display = ("lead", "value_kr", "status", "exported_at", "created_at")
    list_filter = ("status",)
    raw_id_fields = ("lead",)


@admin.register(SmsLog)
class SmsLogAdmin(admin.ModelAdmin):
    list_display = ("account", "kind", "to", "status", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("to", "account__customer__name")
    raw_id_fields = ("account", "lead")


admin.site.register(Fact)
admin.site.register(Service)
