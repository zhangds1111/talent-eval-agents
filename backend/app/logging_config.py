import logging
import re
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class DailyFileHandler(logging.Handler):
    def __init__(
        self,
        log_dir: Path,
        filename_prefix: str,
        date_provider: Callable[[], date] = date.today,
    ) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.filename_prefix = filename_prefix
        self.date_provider = date_provider
        self._active_date: date | None = None
        self._file_handler: logging.FileHandler | None = None
        self.product_logging_handler = True

    def _ensure_file_handler(self) -> logging.FileHandler:
        current_date = self.date_provider()
        if self._file_handler is not None and current_date == self._active_date:
            return self._file_handler
        if self._file_handler is not None:
            self._file_handler.close()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{self.filename_prefix}-{current_date.isoformat()}.log"
        self._file_handler = logging.FileHandler(path, encoding="utf-8")
        self._file_handler.setFormatter(self.formatter)
        self._active_date = current_date
        return self._file_handler

    def setFormatter(self, formatter: logging.Formatter | None) -> None:
        super().setFormatter(formatter)
        if self._file_handler is not None:
            self._file_handler.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_file_handler().emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._file_handler is not None:
            self._file_handler.close()
            self._file_handler = None
        super().close()


def configure_logging(
    log_dir: str | Path,
    service_name: str,
    *,
    current_date: date | None = None,
) -> None:
    directory = Path(log_dir)
    safe_service_name = re.sub(r"[^a-zA-Z0-9_-]+", "-", service_name).strip("-") or "service"
    date_provider = (lambda: current_date) if current_date is not None else date.today
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in list(root_logger.handlers):
        if getattr(handler, "product_logging_handler", False):
            root_logger.removeHandler(handler)
            handler.close()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    console_handler.product_logging_handler = True

    service_handler = DailyFileHandler(directory, safe_service_name, date_provider)
    service_handler.setLevel(logging.INFO)
    service_handler.setFormatter(formatter)

    error_handler = DailyFileHandler(directory, f"{safe_service_name}-error", date_provider)
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(service_handler)
    root_logger.addHandler(error_handler)
