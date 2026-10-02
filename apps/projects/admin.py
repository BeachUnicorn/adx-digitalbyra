from django.contrib import admin

from .models import Comment, Customer, Issue, Label, Project, TimeEntry


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "phone", "is_active")
    search_fields = ("name", "org_number", "email")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "customer", "status", "due_on")
    list_filter = ("status",)
    search_fields = ("key", "name", "customer__name")


class TimeEntryInline(admin.TabularInline):
    model = TimeEntry
    extra = 0
    readonly_fields = ("seconds",)


class CommentInline(admin.TabularInline):
    model = Comment
    extra = 0


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("key", "title", "project", "status", "priority", "assignee", "closed_at")
    list_filter = ("status", "issue_type", "priority", "project")
    search_fields = ("title", "description")
    inlines = [TimeEntryInline, CommentInline]


admin.site.register(Label)
admin.site.register(TimeEntry)
