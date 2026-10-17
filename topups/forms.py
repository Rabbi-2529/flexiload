from django import forms
from django.contrib.auth import authenticate

from .models import Operator, Product, SimProfile, pin_validator
from .ussd import USSD_DEFINITIONS


class EmailAuthenticationForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "autofocus": True,
                "placeholder": "name@example.com",
            }
        )
    )
    password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password", "placeholder": "Password"}
        ),
    )

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get("email")
        password = cleaned.get("password")
        if email and password:
            self.user_cache = authenticate(
                self.request,
                email=email,
                password=password,
            )
            if self.user_cache is None:
                raise forms.ValidationError("Invalid email or password.")
        return cleaned

    def get_user(self):
        return self.user_cache


class SimProfileAdminForm(forms.ModelForm):
    pin = forms.CharField(
        label="Flexiload PIN",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank when editing to keep the existing encrypted PIN.",
        validators=[pin_validator],
    )

    class Meta:
        model = SimProfile
        fields = [
            "owner",
            "name",
            "operator",
            "port",
            "pin",
            "is_active",
        ]

    def clean_pin(self):
        pin = self.cleaned_data.get("pin", "")
        if not self.instance.pk and not pin:
            raise forms.ValidationError("A Flexiload PIN is required.")
        return pin

    def save(self, commit=True):
        instance = super().save(commit=False)
        pin = self.cleaned_data.get("pin")
        if pin:
            instance.set_pin(pin)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class ModemConnectForm(forms.Form):
    port = forms.CharField(max_length=100)
    name = forms.CharField(max_length=100, required=False)
    operator = forms.ChoiceField(choices=Operator.choices)
    pin = forms.CharField(
        min_length=4,
        max_length=12,
        validators=[pin_validator],
        widget=forms.PasswordInput(render_value=False),
    )


class AdminTopupForm(forms.Form):
    sim_profile = forms.ModelChoiceField(
        queryset=SimProfile.objects.none(),
        empty_label="Select connected SIM",
    )
    product = forms.ChoiceField(choices=Product.choices)
    phone_number = forms.RegexField(
        r"^(?:\+?88)?01[3-9]\d{8}$",
        help_text="Bangladesh mobile number, for example 01712345678.",
    )
    amount = forms.IntegerField(
        min_value=1,
        max_value=100000,
    )
    pin = forms.CharField(
        label="PIN override",
        required=False,
        validators=[pin_validator],
        widget=forms.PasswordInput(render_value=False),
        help_text="Optional. Leave blank to use the encrypted PIN saved for this SIM.",
    )
    confirm = forms.BooleanField(
        label="I confirm this top-up and understand it will dial the USSD command.",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = SimProfile.objects.filter(
            is_active=True,
            sim_number__gt="",
            sim_verified_at__isnull=False,
        ).select_related("owner")
        if user and not user.is_superuser:
            queryset = queryset.filter(owner=user)
        self.fields["sim_profile"].queryset = queryset

    def clean_phone_number(self):
        number = self.cleaned_data["phone_number"]
        if number.startswith("+88"):
            return number[3:]
        if number.startswith("88"):
            return number[2:]
        return number

    def clean(self):
        cleaned = super().clean()
        profile = cleaned.get("sim_profile")
        product = cleaned.get("product")
        if profile and product and (profile.operator, product) not in USSD_DEFINITIONS:
            self.add_error(
                "product",
                "This product is not supported by the selected SIM operator.",
            )
        return cleaned
