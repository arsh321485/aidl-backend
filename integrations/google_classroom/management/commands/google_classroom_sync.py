from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Google Classroom: add whole-school students (daily), enrol AIDL class students, publish their cards, "
            "read answers, issue licences.")

    def handle(self, *args, **options):
        from integrations.google_classroom.poller import sync_all

        self.stdout.write(str(sync_all()))
