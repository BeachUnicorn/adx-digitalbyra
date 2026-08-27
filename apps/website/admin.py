"""Django admin registration for the website app."""

from django.contrib import admin
from django.utils.html import format_html

from .models import Block, BlockPage, MediaFile, Menu, MenuItem, SiteSettings


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Singleton admin - prevent add/delete, always edit pk=1."""

    def has_add_permission(self, request):
        # Only allow one instance
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class BlockInline(admin.TabularInline):
    model = Block
    extra = 0
    fields = ("block_type", "order", "is_visible", "data")
    ordering = ("order",)


@admin.register(BlockPage)
class BlockPageAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "is_published", "order")
    list_editable = ("is_published", "order")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [BlockInline]


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ("page", "block_type", "order", "is_visible")
    list_filter = ("block_type", "is_visible", "page")
    list_editable = ("order", "is_visible")


class MenuItemInline(admin.TabularInline):
    model = MenuItem
    extra = 0
    fields = ("label", "page", "url", "open_in_new_tab", "order", "is_visible")
    ordering = ("order",)


@admin.register(Menu)
class MenuAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "heading", "order")
    list_editable = ("heading", "order")
    list_filter = ("location",)
    inlines = [MenuItemInline]


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ("label", "menu", "page", "order", "is_visible")
    list_filter = ("menu",)


@admin.register(MediaFile)
class MediaFileAdmin(admin.ModelAdmin):
    list_display = ("thumbnail", "original_filename", "mime_type", "dimensions", "created_at")
    list_display_links = ("thumbnail", "original_filename")
    list_filter = ("mime_type",)
    readonly_fields = ("preview", "file_size", "width", "height", "created_at")

    @admin.display(description="")
    def thumbnail(self, obj):
        if obj.file and obj.mime_type.startswith("image/"):
            return format_html(
                '<img src="{}" style="height:40px;width:40px;object-fit:cover;'
                'border-radius:4px;" alt="{}">',
                obj.file.url,
                obj.alt_text,
            )
        return "-"

    @admin.display(description="Dimensions")
    def dimensions(self, obj):
        if obj.width and obj.height:
            return f"{obj.width}×{obj.height}"
        return "-"

    @admin.display(description="Preview")
    def preview(self, obj):
        if obj.file and obj.mime_type.startswith("image/"):
            return format_html(
                '<img src="{}" style="max-height:240px;max-width:100%;'
                'border-radius:8px;" alt="{}">',
                obj.file.url,
                obj.alt_text,
            )
        return "-"
