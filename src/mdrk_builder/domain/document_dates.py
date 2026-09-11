"""Document clock times are independent of the clinical source cutoff."""
from datetime import datetime, time


def final_mdrk_datetime(value: datetime | None) -> datetime | None:
    return value.replace(hour=11, minute=0, second=0, microsecond=0) if value else None


def discharge_document_datetime(value: datetime | None) -> datetime | None:
    return value.replace(hour=12, minute=0, second=0, microsecond=0) if value else None


def end_of_day(value: datetime | None) -> datetime | None:
    return datetime.combine(value.date(), time.max) if value else None
