"""Provider-neutral market-data adapters (Step 25). No live feeds.

An adapter turns one source into raw rows (strings plus a row number) and a provenance
record. Validation and the dataset contract are shared (bars.build_dataset), so every
adapter is held to the same rules. Two adapters exist:

- SyntheticFixtureAdapter: labelled JSON fixtures in examples/trading/market/.
- LocalCsvAdapter: a user-supplied local CSV file. It is read once, read-only, within size,
  row and field limits; it is never modified or copied.

Adding a live provider later means adding another adapter; nothing here opens a network
connection.
"""

import csv
import hashlib
import io
import json
import re
from pathlib import Path

from ..contracts import ROOT
from ..errors import TradingError

REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
OPTIONAL_COLUMNS = ("symbol",)
SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
FIXTURE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
FIXTURE_KEYS = {"fixture", "version", "synthetic", "description", "symbol", "interval", "timezone", "currency", "bars"}


def safe_file_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120].lstrip("._-")
    return cleaned or "input.csv"


class MarketDataAdapter:
    """Interface: `read()` returns (rows, source, defaults)."""
    adapter = None
    allowed_labels = ()

    def read(self):  # pragma: no cover - interface
        raise NotImplementedError


class SyntheticFixtureAdapter(MarketDataAdapter):
    adapter = "synthetic_fixture"
    allowed_labels = ("synthetic",)

    def __init__(self, fixture, root=ROOT):
        if not FIXTURE_NAME.match(str(fixture)):
            raise TradingError("unknown_fixture", "Unknown synthetic market fixture.")
        self.path = Path(root) / "examples/trading/market" / f"{fixture}.json"
        self.fixture = fixture

    def read(self):
        try:
            content = self.path.read_bytes()
            fixture = json.loads(content.decode("utf-8"))
        except FileNotFoundError:
            raise TradingError("unknown_fixture", "Unknown synthetic market fixture.") from None
        except (OSError, ValueError, UnicodeError):
            raise TradingError("fixture_invalid", "The synthetic market fixture is unreadable.") from None
        if (not isinstance(fixture, dict) or set(fixture) != FIXTURE_KEYS
                or fixture["fixture"] != "synthetic_market_bars" or fixture["version"] != "1.0"
                or fixture["synthetic"] is not True or not str(fixture["description"]).startswith("SYNTHETIC FIXTURE")
                or not isinstance(fixture["bars"], list)):
            raise TradingError("fixture_invalid", "The synthetic market fixture has an invalid structure.")
        rows = []
        for number, bar in enumerate(fixture["bars"], start=1):
            if not isinstance(bar, dict) or set(bar) != set(REQUIRED_COLUMNS):
                raise TradingError("fixture_invalid", f"Fixture bar {number} has invalid fields.")
            rows.append({"row": number, **bar})
        source = {"adapter": self.adapter, "name": f"synthetic-{self.fixture}", "file_name": self.path.name,
                  "file_sha256": hashlib.sha256(content).hexdigest(), "file_bytes": len(content), "rows": len(rows)}
        defaults = {k: fixture[k] for k in ("symbol", "interval", "timezone", "currency")}
        return rows, source, defaults


class LocalCsvAdapter(MarketDataAdapter):
    adapter = "local_csv"
    allowed_labels = ("synthetic", "historical", "delayed", "unknown")

    def __init__(self, path, *, limits, symbol, source_name="local-csv"):
        self.path = Path(path)
        self.limits = limits
        self.symbol = symbol
        if not SOURCE_NAME.match(str(source_name)):
            raise TradingError("invalid_source_name", "Source names use letters, digits, spaces, dots, dashes or underscores.")
        self.source_name = source_name

    def _read_bytes(self):
        limit = self.limits["max_file_bytes"]
        try:
            if not self.path.is_file():
                raise TradingError("csv_not_found", "The CSV file was not found or is not a regular file.")
            if self.path.stat().st_size > limit:
                raise TradingError("file_too_large", "The CSV file exceeds max_file_bytes in config/market-data.json.")
            with self.path.open("rb") as stream:          # read-only; the source is never modified
                content = stream.read(limit + 1)
        except TradingError:
            raise
        except OSError:
            raise TradingError("csv_unreadable", "The CSV file could not be read.") from None
        if len(content) > limit:
            raise TradingError("file_too_large", "The CSV file exceeds max_file_bytes in config/market-data.json.")
        if not content:
            raise TradingError("csv_malformed", "The CSV file is empty.")
        if b"\x00" in content:
            raise TradingError("csv_malformed", "The CSV file contains NUL bytes.")
        return content

    def read(self):
        content = self._read_bytes()
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise TradingError("csv_encoding", "The CSV file must be UTF-8 text.") from None
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        rows, header = [], None
        max_field, max_rows = self.limits["max_field_length"], self.limits["max_rows"]
        try:
            for record in reader:
                line = reader.line_num
                if header is None:
                    header = [h.strip().lower() for h in record]
                    unknown = set(header) - set(REQUIRED_COLUMNS) - set(OPTIONAL_COLUMNS)
                    if (len(header) != len(set(header)) or unknown or not set(REQUIRED_COLUMNS) <= set(header)):
                        raise TradingError("csv_header_invalid", "Header must contain exactly: timestamp, open, high, low, "
                                                                 "close, volume (and optionally symbol), once each.")
                    continue
                if not record or record == [""]:
                    raise TradingError("csv_malformed", f"Line {line}: blank lines are not allowed.")
                if len(record) != len(header):
                    raise TradingError("csv_malformed", f"Line {line}: expected {len(header)} fields, found {len(record)}.")
                for name, value in zip(header, record):
                    if len(value) > max_field:
                        raise TradingError("field_too_long", f"Line {line}: {name} exceeds max_field_length.")
                    if value != value.strip() or value == "":
                        raise TradingError("csv_malformed", f"Line {line}: {name} is empty or has surrounding spaces.")
                row = dict(zip(header, record))
                if "symbol" in row and row.pop("symbol") != self.symbol:
                    raise TradingError("symbol_mismatch", f"Line {line}: symbol does not match the requested symbol.")
                rows.append({"row": line, **row})
                if len(rows) > max_rows:
                    raise TradingError("too_many_rows", "The CSV file exceeds max_rows in config/market-data.json.")
        except csv.Error:
            raise TradingError("csv_malformed", f"Line {reader.line_num}: the CSV structure is invalid.") from None
        if header is None:
            raise TradingError("csv_malformed", "The CSV file has no header.")
        source = {"adapter": self.adapter, "name": self.source_name, "file_name": safe_file_name(self.path.name),
                  "file_sha256": hashlib.sha256(content).hexdigest(), "file_bytes": len(content), "rows": max(len(rows), 1)}
        return rows, source, {}
