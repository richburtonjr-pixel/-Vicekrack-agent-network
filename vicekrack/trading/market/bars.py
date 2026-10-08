"""OHLCV bar validation, timezones, gaps and the market_dataset contract (Step 25).

Rules:
- Timestamps are the bar START and must carry a timezone offset, unless the importer
  explicitly names `naive_timezone`. Then local times that do not exist or happen twice at a
  DST change are rejected.
- Bars must align to the interval in the dataset timezone (`1d` = local midnight) and be in
  strictly increasing time order. A duplicate start is rejected; so is any bar out of order.
- Prices are exact decimal strings > 0 with high >= open, close, low and low <= open, close.
  Volume is a decimal >= 0.
- Missing intervals between bars are reported as gaps. Nothing is filled or invented. No
  market calendar is applied, so nights, weekends and holidays appear as gaps.
- A bar becomes available at `available_at_utc` (its close: start + interval, or the next
  local midnight for `1d`).
- Step 40: datasets fetched from Alpaca (`alpaca_historical`) must also pass the provider
  provenance checks in `providers/alpaca.validate_provider`.

Error messages name the row and column, never the value.
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..contracts import reject_trading_secrets, sha256, validate_schema
from ..errors import TradingError
from ..money import parse

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": None}
TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(Z|[+-]\d{2}:\d{2})?$")
PRICE_FIELDS = ("open", "high", "low", "close")
VERIFICATION = {
    "authentic": "not_verified", "current": "not_verified", "licensed": "not_verified",
    "statement": ("A successful import only means the file passed format and consistency checks. It does not show "
                  "that the data is authentic, current, complete or licensed for any use."),
}


def fail(code, message):
    raise TradingError(code, message)


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        fail("invalid_timezone", "The timezone is not a known IANA timezone.")


def utc_text(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_text(moment, tz):
    local = moment.astimezone(zone(tz))
    offset = local.strftime("%z")
    return local.strftime("%Y-%m-%dT%H:%M:%S") + offset[:3] + ":" + offset[3:]


def from_utc_text(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_timestamp(text, naive_timezone=None, where="timestamp"):
    """Return an aware UTC datetime. Naive input needs an explicit naive_timezone."""
    match = TIMESTAMP.match(text) if isinstance(text, str) else None
    if not match:
        fail("timestamp_invalid", f"{where}: timestamp must be ISO 8601 like 2026-01-15T09:30:00-05:00.")
    try:
        naive = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")
    except ValueError:
        fail("timestamp_invalid", f"{where}: timestamp is not a real date and time.")
    offset = match.group(3)
    if offset:
        if offset == "Z":
            return naive.replace(tzinfo=timezone.utc)
        sign = 1 if offset[0] == "+" else -1
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        if hours > 18 or minutes > 59:
            fail("timestamp_invalid", f"{where}: timestamp has an invalid UTC offset.")
        return (naive.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
                .astimezone(timezone.utc))
    if naive_timezone is None:
        fail("timestamp_without_timezone", f"{where}: timestamp has no timezone; add an offset or pass --naive-timezone.")
    tz = zone(naive_timezone)
    early, late = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if early.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
        fail("timestamp_nonexistent", f"{where}: this local time does not exist (daylight-saving gap).")
    if early.utcoffset() != late.utcoffset():
        fail("timestamp_ambiguous", f"{where}: this local time happens twice at a daylight-saving change.")
    return early.astimezone(timezone.utc)


def available_at(start, interval, tz):
    seconds = INTERVAL_SECONDS[interval]
    if seconds is not None:
        return start + timedelta(seconds=seconds)
    local = start.astimezone(zone(tz))
    next_day = (local.date() + timedelta(days=1))
    return datetime(next_day.year, next_day.month, next_day.day, tzinfo=zone(tz)).astimezone(timezone.utc)


def check_alignment(start, interval, tz, where):
    local = start.astimezone(zone(tz))
    if local.second != 0:
        fail("interval_misaligned", f"{where}: bar start must be on a whole minute.")
    if interval == "1d":
        if (local.hour, local.minute) != (0, 0):
            fail("interval_misaligned", f"{where}: daily bars must start at local midnight.")
    elif (local.hour * 60 + local.minute) % (INTERVAL_SECONDS[interval] // 60) != 0:
        fail("interval_misaligned", f"{where}: bar start is not aligned to the {interval} interval.")


def missing_between(previous, current, interval, tz, where):
    """Number of whole intervals missing between two consecutive bar starts."""
    seconds = INTERVAL_SECONDS[interval]
    if seconds is None:
        days = (current.astimezone(zone(tz)).date() - previous.astimezone(zone(tz)).date()).days
        return days - 1
    difference = int((current - previous).total_seconds())
    if difference % seconds != 0:
        fail("interval_inconsistent", f"{where}: bar spacing is not a whole number of {interval} intervals.")
    return difference // seconds - 1


def parse_values(row, where):
    values = {}
    for field in PRICE_FIELDS:
        try:
            values[field] = parse(row.get(field), "decimal", "invalid_price")
        except TradingError:
            fail("invalid_price", f"{where}: {field} must be a plain decimal (up to 8 places), not a float or exponent.")
        if values[field] <= 0:
            fail("invalid_price", f"{where}: {field} must be greater than zero.")
    try:
        values["volume"] = parse(row.get("volume"), "decimal", "invalid_volume")
    except TradingError:
        fail("invalid_volume", f"{where}: volume must be a non-negative plain decimal.")
    o, h, low, c = (values[f] for f in PRICE_FIELDS)
    if h < max(o, c, low) or low > min(o, c):
        fail("invalid_ohlc", f"{where}: high must be >= open, close and low, and low must be <= open and close.")
    return values


def build_dataset(rows, *, symbol, interval, tz, naive_timezone, currency, label, source, config, config_sha256, imported_at):
    """Validate raw rows (dicts of strings with a 'row' number) into a market_dataset."""
    if interval not in config["allowed_intervals"]:
        fail("interval_not_allowed", "This interval is not allowed by config/market-data.json.")
    zone(tz)
    if not rows:
        fail("dataset_empty", "The input contains no bars.")
    bars, gaps, gap_count, missing_total, previous = [], [], 0, 0, None
    for row in rows:
        where = f"Row {row['row']}"
        start = parse_timestamp(row.get("timestamp"), naive_timezone, where)
        check_alignment(start, interval, tz, where)
        parse_values(row, where)
        if previous is not None:
            if start == previous:
                fail("duplicate_bar", f"{where}: a bar with this start time already appeared.")
            if start < previous:
                fail("bars_out_of_order", f"{where}: bars must be in increasing time order.")
            missing = missing_between(previous, start, interval, tz, where)
            if missing > 0:
                gap_count += 1
                missing_total += missing
                if len(gaps) < config["limits"]["max_gap_entries"]:
                    gaps.append({"after_start_utc": utc_text(previous), "before_start_utc": utc_text(start),
                                 "missing_intervals": missing})
        bars.append({"start_utc": utc_text(start), **{k: row[k] for k in (*PRICE_FIELDS, "volume")}})
        previous = start
    dataset_id = "mds-" + sha256({"adapter": source["adapter"], "file_sha256": source["file_sha256"],
                                  "symbol": symbol, "interval": interval})[:24]
    dataset = {
        "contract": "market_dataset", "version": "1.0", "dataset_id": dataset_id, "symbol": symbol, "interval": interval,
        "timezone": tz, "currency": currency, "data_label": label, "source": source,
        "import_settings": {"adapter": source["adapter"], "symbol": symbol, "interval": interval, "timezone": tz,
                            "naive_timezone": naive_timezone, "currency": currency, "data_label": label,
                            "config_sha256": config_sha256},
        "imported_at": imported_at, "bar_count": len(bars), "first_start_utc": bars[0]["start_utc"],
        "last_start_utc": bars[-1]["start_utc"],
        "last_available_utc": utc_text(available_at(from_utc_text(bars[-1]["start_utc"]), interval, tz)),
        "gaps": {"gap_count": gap_count, "missing_intervals": missing_total,
                 "truncated": gap_count > len(gaps), "calendar": "none", "entries": gaps},
        "verification": dict(VERIFICATION), "bars": bars, "bars_sha256": sha256(bars),
    }
    validate_dataset(dataset)
    return dataset


def _gap_pairs(bars, interval, tz):
    for before, after in zip(bars, bars[1:]):
        missing = missing_between(from_utc_text(before["start_utc"]), from_utc_text(after["start_utc"]), interval, tz, "stored")
        if missing > 0:
            yield before, after, missing


def validate_dataset(dataset):
    """Schema + full re-check of every stored bar, gap list, hash and identity."""
    reject_trading_secrets(dataset)
    try:
        validate_schema("market_dataset", dataset)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        fail("dataset_invalid", "The market dataset does not match its contract.")
    bars, interval, tz = dataset["bars"], dataset["interval"], dataset["timezone"]
    zone(tz)
    if sha256(bars) != dataset["bars_sha256"] or len(bars) != dataset["bar_count"]:
        fail("dataset_invalid", "The market dataset bars do not match their hash or count.")
    previous = None
    for number, bar in enumerate(bars, start=1):
        where = f"Bar {number}"
        start = from_utc_text(bar["start_utc"])
        check_alignment(start, interval, tz, where)
        parse_values(bar, where)
        if previous is not None and start <= previous:
            fail("dataset_invalid", f"{where}: stored bars are not in strictly increasing order.")
        previous = start
    gaps = list(_gap_pairs(bars, interval, tz))
    entries = [{"after_start_utc": b["start_utc"], "before_start_utc": a["start_utc"], "missing_intervals": m}
               for b, a, m in gaps]
    recorded = dataset["gaps"]
    if (recorded["gap_count"] != len(gaps) or recorded["missing_intervals"] != sum(m for _, _, m in gaps)
            or recorded["entries"] != entries[:len(recorded["entries"])]
            or recorded["truncated"] != (len(recorded["entries"]) < len(gaps))):
        fail("dataset_invalid", "The market dataset gap report does not match its bars.")
    if (dataset["first_start_utc"], dataset["last_start_utc"]) != (bars[0]["start_utc"], bars[-1]["start_utc"]):
        fail("dataset_invalid", "The market dataset time range does not match its bars.")
    if dataset["last_available_utc"] != utc_text(available_at(previous, interval, tz)):
        fail("dataset_invalid", "The market dataset availability time does not match its bars.")
    expected_id = "mds-" + sha256({"adapter": dataset["source"]["adapter"], "file_sha256": dataset["source"]["file_sha256"],
                                   "symbol": dataset["symbol"], "interval": interval})[:24]
    settings = dataset["import_settings"]
    if (dataset["dataset_id"] != expected_id or settings["adapter"] != dataset["source"]["adapter"]
            or (settings["symbol"], settings["interval"], settings["timezone"], settings["currency"], settings["data_label"])
            != (dataset["symbol"], interval, tz, dataset["currency"], dataset["data_label"])):
        fail("dataset_invalid", "The market dataset identity or settings are inconsistent.")
    if dataset["source"]["adapter"] == "synthetic_fixture" and dataset["data_label"] != "synthetic":
        fail("dataset_invalid", "Synthetic fixtures must be labelled synthetic.")
    if dataset["source"]["adapter"] == "alpaca_historical":
        from ..providers.alpaca import validate_provider       # pure checks; never opens a connection
        validate_provider(dataset)
    elif "provider" in dataset["source"]:
        fail("dataset_invalid", "Only provider-fetched datasets carry provider provenance.")


def expand_bar(dataset, index):
    """The full ohlcv_bar 1.0 contract for one stored bar (validated)."""
    stored = dataset["bars"][index]
    start = from_utc_text(stored["start_utc"])
    bar = {
        "contract": "ohlcv_bar", "version": "1.0", "dataset_id": dataset["dataset_id"], "sequence": index + 1,
        "symbol": dataset["symbol"], "interval": dataset["interval"], "timezone": dataset["timezone"],
        "timestamp": local_text(start, dataset["timezone"]), "timestamp_utc": stored["start_utc"],
        "available_at_utc": utc_text(available_at(start, dataset["interval"], dataset["timezone"])),
        **{k: stored[k] for k in (*PRICE_FIELDS, "volume")},
        "currency": dataset["currency"],
        "source": {"adapter": dataset["source"]["adapter"], "name": dataset["source"]["name"]},
        "data_label": dataset["data_label"],
    }
    validate_bar(bar)
    return bar


def validate_bar(bar):
    validate_schema("ohlcv_bar", bar)
    start = parse_timestamp(bar["timestamp"], where="bar")
    if utc_text(start) != bar["timestamp_utc"] or local_text(start, bar["timezone"]) != bar["timestamp"]:
        fail("invalid_ohlcv_bar", "Bar local and UTC timestamps disagree with its timezone.")
    if bar["available_at_utc"] != utc_text(available_at(start, bar["interval"], bar["timezone"])):
        fail("invalid_ohlcv_bar", "Bar availability time does not match its interval.")
    parse_values(bar, "bar")
