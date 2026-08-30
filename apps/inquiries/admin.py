from django.contrib import admin

from .models import Inquiry, InquiryImage, NewsletterSignup


class InquiryImageInline(admin.TabularInline):
    model = InquiryImage
    extra = 0
    readonly_fields = ("file", "original_filename", "file_size")


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "name",
        "customer_type",
        "status",
        "traffic_source",
        "is_read",
        "created_at",
    )
    list_filter = ("status", "customer_type", "traffic_source", "is_read")
    search_fields = ("reference", "name", "email", "company_name")
    readonly_fields = (
        "reference",
        "created_at",
        "updated_at",
        "analytics_session",
        "traffic_source",
        "traffic_source_detail",
        "traffic_referrer",
    )
    inlines = [InquiryImageInline]


@admin.register(NewsletterSignup)
class NewsletterSignupAdmin(admin.ModelAdmin):
    """Fanns inte alls - adresser samlades in utan att synas någonstans."""

    list_display = ("email", "source_path", "created_at")
    search_fields = ("email",)
    readonly_fields = ("email", "source_path", "created_at")
