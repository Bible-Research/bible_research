"""Master Cloud Run Job entry point.

Runs the sub-jobs listed in ``JOBS`` sequentially, in listed order.
Each entry is a ``(label, argv, schedule)`` tuple — comment the
whole tuple out to disable that job. This file is the single
source of truth for which jobs the ``all-jobs`` Cloud Run Job
executes.
"""
import calendar
import logging
from datetime import date

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

# label, argv, schedule — runs only when `schedule` matches today (UTC)
JOBS = [
    # Mirrors the intended monthly-3rd schedule of the (currently
    # disabled) audio-generator Cloud Scheduler trigger.
    (
        "generate_chapter_audio",
        ["--fileset-id", "LVSGLU8", "--mode", "both"],
        "dom:3",
    ),
]


def _daily(today: date) -> bool:
    return True


def _dom_odd(today: date) -> bool:
    """Odd day-of-month — mirrors cron ``*/2`` on day-of-month."""
    return today.day % 2 == 1


def _last_day_of_month(today: date) -> bool:
    return today.day == calendar.monthrange(today.year, today.month)[1]


def _manual(today: date) -> bool:
    """Never runs in a scheduled pass — only via ``--only`` or
    ``--force`` (both ignore schedules)."""
    return False


def _resolve_schedule(schedule: str):
    """Map a schedule string to a predicate ``f(date) -> bool``.

    Supported values (all evaluated in UTC):
    - ``daily``
    - ``weekly:<mon..sun>``
    - ``dom:odd`` / ``dom:N``
    - ``last_day_of_month``
    - ``manual``
    """
    if schedule == "daily":
        return _daily
    if schedule.startswith("weekly:"):
        weekday = schedule.split(":", 1)[1].lower()
        weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        if weekday not in weekdays:
            raise CommandError(f"Unknown weekday in schedule: {schedule}")
        target = weekdays.index(weekday)
        return lambda today: today.weekday() == target
    if schedule.startswith("dom:"):
        spec = schedule.split(":", 1)[1]
        if spec == "odd":
            return _dom_odd
        try:
            target = int(spec)
        except ValueError:
            raise CommandError(f"Unknown dom schedule: {schedule}")
        if not 1 <= target <= 31:
            raise CommandError(f"dom day out of range: {schedule}")
        return lambda today: today.day == target
    if schedule == "last_day_of_month":
        return _last_day_of_month
    if schedule == "manual":
        return _manual
    raise CommandError(f"Unknown schedule: {schedule}")


class Command(BaseCommand):
    help = "Run every enabled sub-job in JOBS, sequentially."

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            metavar="LABEL",
            help="Run a single JOBS entry by label, ignoring its "
                 "schedule.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Run all enabled entries, ignoring schedules.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would run today and exit.",
        )

    def handle(self, *args, **options):
        today = date.today()  # container runs in UTC
        entries = self._select(options, today)

        if options["dry_run"]:
            for label, argv, schedule in entries:
                self.stdout.write(
                    f"would run: {label} {' '.join(argv)} "
                    f"(schedule={schedule})"
                )
            if not entries:
                self.stdout.write("nothing scheduled to run")
            return

        failed = []
        for label, argv, _schedule in entries:
            self.stdout.write(f"running: {label} {' '.join(argv)}")
            # Catch SystemExit too — a sub-job that calls
            # sys.exit() must not abort the whole pass. A clean
            # exit (code 0 or None) is not a failure.
            # KeyboardInterrupt still propagates and stops the run.
            try:
                call_command(label, *argv)
            except (Exception, SystemExit) as exc:
                if (
                    isinstance(exc, SystemExit)
                    and exc.code in (None, 0)
                ):
                    continue
                logger.exception("sub-job %s failed", label)
                failed.append(label)

        if failed:
            raise CommandError(
                f"sub-jobs failed: {', '.join(failed)}"
            )

    def _select(self, options, today):
        """Filter JOBS down to the entries to run this pass."""
        if options["only"]:
            selected = [
                job for job in JOBS if job[0] == options["only"]
            ]
            if not selected:
                raise CommandError(
                    f"no JOBS entry labelled {options['only']!r}"
                )
            return selected

        selected = []
        for label, argv, schedule in JOBS:
            if options["force"] or _resolve_schedule(schedule)(today):
                selected.append((label, argv, schedule))
            else:
                self.stdout.write(
                    f"skipping: {label} (schedule={schedule}, "
                    f"today={today.isoformat()})"
                )
        return selected
