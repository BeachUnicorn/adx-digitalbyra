from django import forms
from django.contrib.auth import get_user_model

from .models import Column, Customer, Issue, Project, validate_attachment


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    """Flera filer i ett fält; varje fil går genom samma validering."""

    widget = MultiFileInput

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        cleaned = []
        for f in files:
            single = super().clean(f, initial)
            if single:
                validate_attachment(single)
                cleaned.append(single)
        return cleaned


class IssueForm(forms.ModelForm):
    class Meta:
        model = Issue
        fields = [
            "title",
            "description",
            "project",
            "customer",
            "column",
            "issue_type",
            "priority",
            "assignee",
            "labels",
            "estimate_minutes",
            "due_on",
            "is_billable",
            "visible_to_customer",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 6})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.exclude(status="archived")
        self.fields["customer"].queryset = Customer.objects.filter(is_active=True)
        self.fields["assignee"].queryset = get_user_model().objects.filter(is_staff=True)
        project = self.instance.project if self.instance and self.instance.project_id else None
        if "project" in self.data and self.data.get("project"):
            project = Project.objects.filter(pk=self.data.get("project")).first()
        self.fields["column"].queryset = (
            Column.objects.filter(project=project) if project else Column.objects.none()
        )
        self.fields["column"].required = False


class PortalIssueForm(forms.Form):
    title = forms.CharField(label="Rubrik", max_length=200)
    description = forms.CharField(
        label="Beskrivning",
        widget=forms.Textarea(attrs={"rows": 7}),
        required=False,
        help_text="Vad vill ni ha gjort? Var gärna konkret: sida, vad som ska ändras, när.",
    )
    files = MultiFileField(
        label="Bilagor", required=False, help_text="Skärmdumpar, dokument. Max 15 MB per fil."
    )


class CommentForm(forms.Form):
    body = forms.CharField(label="Kommentar", widget=forms.Textarea(attrs={"rows": 3}))
    is_internal = forms.BooleanField(
        label="Intern anteckning (syns inte för kunden)", required=False
    )
    files = MultiFileField(label="Bilagor", required=False)


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = [
            "name",
            "key",
            "customer",
            "description",
            "status",
            "hourly_rate",
            "budget_hours",
            "starts_on",
            "due_on",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "starts_on": forms.DateInput(attrs={"type": "date"}),
            "due_on": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["key"].required = False
        self.fields["key"].help_text = "Tomt = skapas ur namnet."

    def clean_key(self):
        key = (self.cleaned_data.get("key") or "").strip().upper()
        if key and not key.isalnum():
            raise forms.ValidationError("Bara bokstäver och siffror.")
        return key

    def save(self, commit=True):
        project = super().save(commit=False)
        if not project.key:
            project.key = Project.make_key(project.name)
        if commit:
            project.save()
        return project


class ColumnForm(forms.ModelForm):
    class Meta:
        model = Column
        fields = ["title", "wip_limit", "is_done"]


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "org_number", "email", "phone", "website", "notes", "is_active"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class InviteForm(forms.Form):
    email = forms.EmailField(label="E-post")
    first_name = forms.CharField(label="Förnamn", max_length=80, required=False)
    last_name = forms.CharField(label="Efternamn", max_length=80, required=False)
