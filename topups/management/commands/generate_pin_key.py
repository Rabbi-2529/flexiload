from cryptography.fernet import Fernet
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate a Fernet key for PIN_ENCRYPTION_KEY."

    def handle(self, *args, **options):
        self.stdout.write(Fernet.generate_key().decode("ascii"))
