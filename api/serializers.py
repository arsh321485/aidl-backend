from rest_framework import serializers

from .models import AIDLUser, Item


class ItemSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = Item
        fields = ["id", "name", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class AIDLUserSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = AIDLUser
        fields = [
            "id",
            "email",
            "full_name",
            "enroll_as",
            "provider",
            "microsoft_id",
            "avatar_url",
            "is_active",
            "last_login_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
