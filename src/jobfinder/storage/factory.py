"""Opening the configured opportunity storage."""

from __future__ import annotations

from jobfinder.config import AppConfig, StorageBackend
from jobfinder.storage.excel_repo import ExcelRepository
from jobfinder.storage.repository import OpportunityRepository
from jobfinder.storage.sql_repo import SqlRepository

WORKBOOK = "opportunities.xlsx"
SQLITE_DB = "opportunities.db"


def open_repository(config: AppConfig, *, read_only: bool = False) -> OpportunityRepository:
    """The configured backend. ``read_only`` (Excel) never writes the workbook."""
    visible = frozenset(config.settings.workbook_decisions)
    if config.secrets.STORAGE_BACKEND is StorageBackend.SQLITE:
        return SqlRepository.sqlite(config.data_dir / SQLITE_DB, visible)
    return ExcelRepository(
        config.data_dir / WORKBOOK, read_only=read_only, visible_decisions=visible
    )
