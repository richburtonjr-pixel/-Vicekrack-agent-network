"""Research-agent run storage (Step 28): runtime/trading/agents/ (ignored by Git).

Runs are written to a temporary file, fsynced and published with an exclusive hard link:
never partial, never overwritten (`agent_run_exists`). Every stored run is re-validated
(schema, hash, fixed stage sequence, failure consistency, credential check) when read.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from ..contracts import ROOT, reject_trading_secrets, sha256, validate_schema
from ..errors import TradingError
from .controller import validate_run

RUN_ID = re.compile(r"^rar-[0-9a-f]{24}$")


def load_agent_config(path="config/research-agents.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_agent_config", "Agent configuration must remain inside the project.")
    try:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        validate_schema("research_agent_config", config)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        raise TradingError("invalid_agent_config", "config/research-agents.json is invalid.") from None
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_agent_config", "Cannot read a valid research-agent configuration.") from None
    return config, sha256(config)


class AgentRunStore:
    def __init__(self, root=None):
        self.folder = Path(root if root is not None else ROOT) / "runtime/trading/agents"

    def save(self, run):
        validate_run(run)
        reject_trading_secrets(run)
        temporary = None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.folder, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(run, stream, indent=1, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, self.folder / f"{run['run_id']}.json")
        except FileExistsError:
            raise TradingError("agent_run_exists", "An identical research-agent run is already saved; nothing was changed.") from None
        except (OSError, ValueError, TypeError):
            raise TradingError("agent_write_failed", "Could not save the research-agent run; nothing was stored.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return run

    def load(self, run_id):
        if not RUN_ID.match(str(run_id)):
            raise TradingError("invalid_agent_run_id", "A research-agent run ID looks like rar- followed by 24 hex characters.")
        path = self.folder / f"{run_id}.json"
        if not path.is_file():
            raise TradingError("agent_run_not_found", "No saved research-agent run with this ID.")
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            validate_run(run)
            if run["run_id"] != run_id:
                raise ValueError
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("agent_run_corrupt", "The saved research-agent run failed validation.") from None
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            raise TradingError("agent_run_corrupt", "The saved research-agent run is unreadable.") from None
        return run

    def list(self):
        items = []
        for path in sorted(self.folder.glob("rar-*.json")) if self.folder.is_dir() else []:
            try:
                run = self.load(path.stem)
                items.append({"run_id": run["run_id"], "dataset_id": run["dataset"]["dataset_id"], "symbol": run["dataset"]["symbol"],
                              "sim_time_utc": run["sim_time_utc"], "status": run["status"],
                              "verdict": run["final"]["verdict"], "readable": True})
            except TradingError:
                items.append({"run_id": path.stem, "readable": False})
        return items
