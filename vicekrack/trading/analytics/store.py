"""Analytics storage (Step 30): runtime/trading/analytics/reports/ (ignored by Git).

Reports are written with temp file + fsync + exclusive link, so they are never partial or
overwritten (`report_exists`). Every read fully re-validates the schema, the results hash
and the internal consistency checks (`report_corrupt`).
"""

import json
import os
import re
import tempfile
from pathlib import Path

from ..contracts import ROOT, reject_trading_secrets, sha256, validate_schema
from ..errors import TradingError
from .report import validate_report

REPORT_ID = re.compile(r"^sarp-[0-9a-f]{24}$")


def load_analytics_config(path="config/analytics.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_analytics_config", "The analytics config must remain inside the project.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        validate_schema("analytics_config", config)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        raise TradingError("invalid_analytics_config", "The analytics config is invalid.") from None
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_analytics_config", "Cannot read a valid analytics config.") from None
    return config, sha256(config)


class AnalyticsStore:
    def __init__(self, root=None):
        self.reports = Path(root if root is not None else ROOT) / "runtime/trading/analytics/reports"

    def save(self, report):
        validate_report(report)
        reject_trading_secrets(report)
        target = self.reports / f"{report['report_id']}.json"
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(report, stream, indent=1, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        except FileExistsError:
            raise TradingError("report_exists", "An analytics report for this run and config is already saved; nothing was changed.") from None
        except (OSError, ValueError, TypeError):
            raise TradingError("report_write_failed", "Could not save the analytics report; nothing was stored.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return report

    def load(self, report_id):
        if not REPORT_ID.match(str(report_id)):
            raise TradingError("invalid_report_id", "An analytics report ID looks like sarp- followed by 24 hex characters.")
        path = self.reports / f"{report_id}.json"
        if not path.is_file():
            raise TradingError("report_not_found", "No saved analytics report with this ID.")
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            validate_report(report)
            if report["report_id"] != report_id:
                raise ValueError
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("report_corrupt", "The saved analytics report failed validation.") from None
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, IndexError):
            raise TradingError("report_corrupt", "The saved analytics report is unreadable.") from None
        return report

    def list(self, limit):
        items = []
        paths = sorted(self.reports.glob("sarp-*.json")) if self.reports.is_dir() else []
        for path in paths[:limit]:
            try:
                report = self.load(path.stem)
                items.append({"report_id": report["report_id"], "run_id": report["source"]["run_id"],
                              "symbol": report["source"]["symbol"], "closed_trades": report["closed_trades"]["count"],
                              "net_return": report["account"]["net_return"], "simulated": True, "readable": True})
            except TradingError:
                items.append({"report_id": path.stem, "readable": False})
        return items, len(paths)
