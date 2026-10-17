from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, email=None, **kwargs):
        identifier = email or username or kwargs.get("email")
        if not identifier or not password:
            return None
        user_model = get_user_model()
        users = user_model._default_manager.filter(email__iexact=identifier.strip())
        if users.count() != 1:
            return None
        user = users.first()
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
