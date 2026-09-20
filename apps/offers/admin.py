from django.contrib import admin

from .models import Product, Quote, QuoteLine


class QuoteLineInline(admin.TabularInline):
    model = QuoteLine
    extra = 0
    # Raden minns vilket ärende den blev; en dropdown över alla ärenden
    # skulle bli ohanterlig.
    raw_id_fields = ("issue",)


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ("customer_name", "project_title", "project", "status", "sent_at", "accepted_at")
    list_filter = ("status",)
    search_fields = ("customer_name", "customer_email", "project_title", "project__key")
    readonly_fields = ("token", "accepted_ip", "accepted_user_agent")
    raw_id_fields = ("project",)
    inlines = [QuoteLineInline]


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "default_price", "default_period", "is_active")
    list_filter = ("is_active", "default_period")
    search_fields = ("name",)
