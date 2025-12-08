from django.core.management.base import BaseCommand
from django.core.management import call_command
from datetime import datetime


class Command(BaseCommand):
    help = "Runs Django's check command and prints the time it was executed."

    def handle(self, *args, **options):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.stdout.write(f"[{now}] Running django check...")

        # Run Django's built-in check command
        call_command("check")

        self.stdout.write(self.style.SUCCESS(f"[{now}] Django check completed!"))
