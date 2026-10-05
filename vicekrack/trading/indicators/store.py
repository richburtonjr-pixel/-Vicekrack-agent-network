"""Indicator result storage (Step 26): runtime/trading/indicators/ (ignored by Git).

Results are written to a temporary file, fsynced and published with an exclusive hard
link: never partial and never overwritten. The result ID is derived from the dataset ID,
bars hash, settings hash and replay window, so saving the same calculation twice is
rejected with `result_exists`. Every stored result is re-validated when read.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from ..contracts import ROOT, reject_trading_secrets, sha256, validate_schema
from ..errors import TradingError
from .engine import validate_result

RESULT_ID = re.compile(r"^ind-[0-9a-f]{24}$")


def load_indicator_config(path="config/indicators.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_indicator_config", "Indicator configuration must remain inside the project.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        validate_schema("indicator_config", config)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        raise TradingError("invalid_indicator_config", "config/indicators.json is invalid.") from None
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_indicator_config", "Cannot read a valid indicator configuration.") from None
    return config, sha256(config)


class IndicatorStore:
    def __init__(self, root=None):
        self.folder = Path(root if root is not None else ROOT) / "runtime/trading/indicators"

    def save(self, result):
        validate_result(result)
        reject_trading_secrets(result)
        temporary = None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.folder, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(result, stream, indent=1, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, self.folder / f"{result['result_id']}.json")
        except FileExistsError:
            raise TradingError("result_exists", "An identical indicator result is already saved; nothing was changed.") from None
        except (OSError, ValueError, TypeError):
            raise TradingError("indicator_write_failed", "Could not save the indicator result; nothing was stored.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return result

    def load(self, result_id):
        if not RESULT_ID.match(str(result_id)):
            raise TradingError("invalid_result_id", "A result ID looks like ind- followed by 24 hex characters.")
        path = self.folder / f"{result_id}.json"
        if not path.is_file():
            raise TradingError("result_not_found", "No saved indicator result with this ID.")
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            validate_result(result)
            if result["result_id"] != result_id:
                raise ValueError
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("result_corrupt", "The saved indicator result failed validation.") from None
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            raise TradingError("result_corrupt", "The saved indicator result is unreadable.") from None
        return result

    def list(self):
        items = []
        for path in sorted(self.folder.glob("ind-*.json")) if self.folder.is_dir() else []:
            try:
                r = self.load(path.stem)
                items.append({"result_id": r["result_id"], "dataset_id": r["dataset"]["dataset_id"],
                              "symbol": r["dataset"]["symbol"], "interval": r["dataset"]["interval"],
                              "indicators": [s["key"] for s in r["series"]], "gap_policy": r["settings"]["gap_policy"],
                              "bars": r["summary"]["bars_processed"], "readable": True})
            except TradingError:
                items.append({"result_id": path.stem, "readable": False})
        return items
