"""
Django admin for analytics.

This is the staff-facing surface (no /manage/ stats page). Read-only by
design - analytics rows are written by the tracking pipeline, not by hand.
"""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import Event, PageView, Session, Visitor


class PageViewInline(admin.TabularInline):
    model = PageView
    extra = 0
    can_delete = False
    readonly_fields = ("path", "title", "viewed_at", "engaged_seconds")
    ordering = ("viewed_at",)

    def has_add_permission(self, request, obj=None):
        return False


class EventInline(admin.TabularInline):
    model = Event
    extra = 0
    can_delete = False
    readonly_fields = ("event_type", "label", "placement", "path", "created_at")

    def has_add_permission(self, request, obj=None):
        return False


class SessionInline(admin.TabularInline):
    model = Session
    extra = 0
    can_delete = False
    fields = (
        "started_at",
        "source",
        "source_detail",
        "device_type",
        "pageview_count",
        "engaged_seconds",
    )
    readonly_fields = fields
    ordering = ("-started_at",)
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Visitor)
class VisitorAdmin(admin.ModelAdmin):
    list_display = (
        "uuid",
        "first_source",
        "first_source_detail",
        "session_count",
        "first_seen",
        "last_seen",
    )
    list_filter = ("first_source", "first_seen")
    search_fields = ("uuid", "first_referrer", "first_landing_page")
    date_hierarchy = "first_seen"
    readonly_fields = (
        "uuid",
        "first_seen",
        "first_referrer",
        "first_source",
        "first_source_detail",
        "first_landing_page",
        "last_seen",
        "session_count",
    )
    inlines = [SessionInline]

    def has_add_permission(self, request):
        return False


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = (
        "started_at",
        "source",
        "source_detail",
        "device_type",
        "os",
        "browser",
        "pageview_count",
        "engaged_display",
        "duration_display",
    )
    list_filter = ("source", "device_type", "os", "browser", "started_at")
    search_fields = (
        "uuid",
        "referrer",
        "landing_page",
        "utm_campaign",
        "utm_term",
        "utm_content",
    )
    date_hierarchy = "started_at"
    readonly_fields = (
        "uuid",
        "visitor",
        "started_at",
        "last_activity",
        "duration_display",
        "engaged_display",
        "pageview_count",
        "referrer",
        "source",
        "source_detail",
        "landing_page",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "device_type",
        "os",
        "browser",
        "screen_resolution",
        "viewport_display",
        "user_agent",
        "ip_address",
        "country",
    )
    inlines = [PageViewInline, EventInline]

    @admin.display(description=_("Tidsspann"))
    def duration_display(self, obj):
        return obj.duration_display

    @admin.display(description=_("Engagerad tid"))
    def engaged_display(self, obj):
        return obj.engaged_display

    @admin.display(description=_("Vyport"))
    def viewport_display(self, obj):
        return obj.viewport_display or "\u2014"

    def has_add_permission(self, request):
        return False


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "label", "placement", "path", "created_at")
    list_filter = ("event_type", "placement", "created_at")
    search_fields = ("label", "path")
    date_hierarchy = "created_at"
    readonly_fields = (
        "session",
        "event_type",
        "label",
        "placement",
        "path",
        "created_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ("path", "title", "engaged_seconds", "viewed_at")
    list_filter = ("viewed_at",)
    search_fields = ("path", "title")
    date_hierarchy = "viewed_at"
    readonly_fields = ("session", "path", "title", "engaged_seconds", "viewed_at")

    def has_add_permission(self, request):
        return False
