import re

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from rest_framework import serializers

from .modem import extract_carrier_message, extract_carrier_transaction_id
from .models import Operator, Product, SimProfile, TopupTransaction
from .ussd import USSD_DEFINITIONS


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "is_staff"]
        read_only_fields = fields


class EmailLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        user = authenticate(
            self.context.get("request"),
            email=attrs["email"],
            password=attrs["password"],
        )
        if user is None:
            raise serializers.ValidationError("Invalid email or password.")
        attrs["user"] = user
        return attrs


class SimProfileSerializer(serializers.ModelSerializer):
    pin = serializers.RegexField(
        r"^\d{4,12}$",
        write_only=True,
        required=False,
    )

    class Meta:
        model = SimProfile
        fields = [
            "id",
            "name",
            "operator",
            "port",
            "sim_number",
            "sim_verified_at",
            "pin",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "sim_number",
            "sim_verified_at",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        if self.instance is None and not attrs.get("pin"):
            raise serializers.ValidationError({"pin": "This field is required."})
        return attrs

    def create(self, validated_data):
        pin = validated_data.pop("pin")
        profile = SimProfile(owner=self.context["request"].user, **validated_data)
        profile.set_pin(pin)
        profile.save()
        return profile

    def update(self, instance, validated_data):
        pin = validated_data.pop("pin", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if pin:
            instance.set_pin(pin)
        instance.save()
        return instance


class TopupRequestSerializer(serializers.Serializer):
    sim_profile_id = serializers.IntegerField(min_value=1)
    product = serializers.ChoiceField(choices=Product.choices)
    phone_number = serializers.RegexField(r"^(?:\+?88)?01[3-9]\d{8}$")
    amount = serializers.IntegerField(
        min_value=1,
        max_value=100000,
    )
    pin = serializers.RegexField(
        r"^\d{4,12}$",
        write_only=True,
        required=False,
        help_text="Optional replacement PIN; encrypted before storage.",
    )
    idempotency_key = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=False,
    )
    retry_failed = serializers.BooleanField(
        required=False,
        default=False,
        write_only=True,
        help_text=(
            "Set true to execute an existing failed idempotency key again. "
            "Processing and successful transactions cannot be retried."
        ),
    )

    def validate_phone_number(self, value):
        normalized = re.sub(r"^\+?88", "", value)
        return normalized

    def validate(self, attrs):
        request = self.context["request"]
        queryset = SimProfile.objects.filter(is_active=True)
        if not request.user.is_staff:
            queryset = queryset.filter(owner=request.user)
        try:
            profile = queryset.get(pk=attrs["sim_profile_id"])
        except SimProfile.DoesNotExist as exc:
            raise serializers.ValidationError(
                {"sim_profile_id": "Active SIM profile not found."}
            ) from exc

        if (profile.operator, attrs["product"]) not in USSD_DEFINITIONS:
            raise serializers.ValidationError(
                {"product": "This product is not supported by the selected operator."}
            )
        attrs["sim_profile"] = profile
        return attrs


class TopupTransactionSerializer(serializers.ModelSerializer):
    sim_profile_name = serializers.CharField(source="sim_profile.name", read_only=True)
    source_sim_number = serializers.CharField(
        source="sim_profile.sim_number",
        read_only=True,
    )
    username = serializers.CharField(source="user.username", read_only=True)
    carrier_transaction_id = serializers.SerializerMethodField()
    message = serializers.SerializerMethodField()
    targeted_number = serializers.CharField(source="phone_number", read_only=True)

    def get_carrier_transaction_id(self, obj):
        return extract_carrier_transaction_id(obj.modem_response)

    def get_message(self, obj):
        return extract_carrier_message(obj.modem_response)

    class Meta:
        model = TopupTransaction
        fields = [
            "id",
            "username",
            "sim_profile",
            "sim_profile_name",
            "source_sim_number",
            "operator",
            "product",
            "phone_number",
            "targeted_number",
            "amount",
            "status",
            "carrier_transaction_id",
            "message",
            "modem_response",
            "error_message",
            "idempotency_key",
            "retry_count",
            "last_retried_at",
            "created_at",
            "completed_at",
        ]
        read_only_fields = fields
