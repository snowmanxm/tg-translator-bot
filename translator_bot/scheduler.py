from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from translator_bot.config import Settings


class SummaryScheduler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scheduler = AsyncIOScheduler(timezone=settings.summary.timezone)

    def configure(self, *, hourly_job, daily_job, prune_job) -> None:
        self.scheduler.remove_all_jobs()
        if self.settings.features.hourly_summary:
            self.scheduler.add_job(
                hourly_job,
                CronTrigger.from_crontab(self.settings.summary.hourly_cron, timezone=self.settings.summary.timezone),
                id="hourly-summary",
                replace_existing=True,
            )
        if self.settings.features.daily_summary:
            self.scheduler.add_job(
                daily_job,
                CronTrigger.from_crontab(self.settings.summary.daily_cron, timezone=self.settings.summary.timezone),
                id="daily-summary",
                replace_existing=True,
            )
        self.scheduler.add_job(prune_job, "cron", hour=3, minute=15, id="prune-old-messages", replace_existing=True)

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
