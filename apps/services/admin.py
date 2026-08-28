"""Django admin for the services app."""

from django.contrib import admin

from .models import Audience, Service, ServiceCategory, ServiceStep


class ServiceStepInline(admin.TabularInline):
    model = ServiceStep
    extra = 0
    fields = ("title", "description", "order")
    ordering = ("order",)


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "order")
    list_editable = ("is_active", "order")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "is_active",
        "is_featured",
        "order",
    )
    list_editable = ("is_active", "is_featured", "order")
    list_filter = ("category", "is_active", "is_featured", "audiences")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("audiences",)
    inlines = [ServiceStepInline]


@admin.register(Audience)
class AudienceAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "order")
    list_editable = ("is_active", "order")
    prepopulated_fields = {"slug": ("name",)}
