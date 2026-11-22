from django import forms
from django.contrib.auth.forms import BaseUserCreationForm

import labels

from .models import Label, User

# Keeps the label descriptions in the classifier prompt bounded.
LABELS_MAX = 30


class SignupForm(BaseUserCreationForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email


class SettingsForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("agent_enabled", "cleanup_enabled", "work_start", "work_end", "urgent_after_hours")
        widgets = {
            "agent_enabled": forms.CheckboxInput(attrs={"role": "switch"}),
            "cleanup_enabled": forms.CheckboxInput(attrs={"role": "switch"}),
            "work_start": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "work_end": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "urgent_after_hours": forms.CheckboxInput(attrs={"role": "switch"}),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("work_start"), cleaned.get("work_end")
        if start and end and start >= end:
            raise forms.ValidationError("Working hours must end after they start.")
        return cleaned



class IconPicker(forms.TextInput):
    """The emoji text box, plus a button that opens the full emoji picker (labels.html).
    Gmail has no label icons, so the emoji goes in the label name."""

    template_name = "accounts/widgets/icon_picker.html"

    def __init__(self, attrs=None):
        super().__init__({"maxlength": 16, "placeholder": "🏷️", "autocomplete": "off", **(attrs or {})})


class LabelForm(forms.ModelForm):
    class Meta:
        model = Label
        fields = ("emoji", "name", "sublabels", "include", "exclude")
        labels = {
            "emoji": "Icon", "name": "Name", "sublabels": "Sub-labels",
            "include": "Belongs here", "exclude": "Doesn't belong",
        }
        widgets = {
            "emoji": IconPicker,
            "name": forms.TextInput(attrs={"placeholder": "Label name"}),
            "sublabels": forms.Textarea(attrs={"rows": 1, "placeholder": "when useful"}),
            "include": forms.Textarea(attrs={"rows": 2}),
            "exclude": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("name", "").strip()
        # A name typed with its emoji ("✈️ Travel") moves the emoji to its own field.
        emoji, name = labels.split_emoji(name)
        if emoji and not cleaned.get("emoji"):
            cleaned["emoji"] = emoji
        if "/" in name:
            self.add_error("name", "No / in label names; the agent makes the sub-labels.")
        elif name and not labels.key(name):
            self.add_error("name", "Use at least one letter.")
        cleaned["name"] = name
        return cleaned


class BaseLabelFormSet(forms.BaseModelFormSet):
    default_error_messages = {
        "too_many_forms": "You can have at most %(num)d labels. Delete one to add another.",
    }

    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            if self.can_delete and self._should_delete_form(form) or not form.cleaned_data:
                continue
            k = labels.key(form.cleaned_data.get("name", ""))
            if k in seen:
                form.add_error("name", "Another label has this name.")
            seen.add(k)


LabelFormSet = forms.modelformset_factory(
    Label, form=LabelForm, formset=BaseLabelFormSet,
    extra=0, can_delete=True, max_num=LABELS_MAX, validate_max=True,
)
