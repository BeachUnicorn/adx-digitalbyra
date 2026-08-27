from django.contrib import admin

from .models import FAQItem, FAQSection


class FAQItemInline(admin.TabularInline):
    model = FAQItem
    extra = 1
    fields = ("question", "answer", "order", "is_active")
    ordering = ("order",)


@admin.register(FAQSection)
class FAQSectionAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "is_active", "item_count")
    list_filter = ("is_active",)
    prepopulated_fields = {"slug": ("title",)}
    inlines = [FAQItemInline]

    def item_count(self, obj):
        return obj.items.count()

    item_count.short_description = "Frågor"
