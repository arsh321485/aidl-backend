from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print a new RSA private key for LTI_PRIVATE_KEY (one line, ready for .env)."

    def handle(self, *args, **options):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        self.stdout.write('LTI_PRIVATE_KEY="' + pem.strip().replace("\n", "\\n") + '"')
