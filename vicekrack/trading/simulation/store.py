"""Simulation storage and kill switch (Step 29): runtime/trading/simulation/ (ignored by Git).

runs/<run_id>.json   temp file + fsync + exclusive link: never partial or overwritten
                     (`sim_run_exists`); fully re-validated on read (`sim_run_corrupt`).
kill-switch.json     the simulation's own kill switch, separate from the paper-account
                     switch. Policy engagement always wins; an unreadable file counts as
                     engaged. It blocks new simulated entries only (exits still execute).
"""

import json
import os
import re
import tempfile
from pathlib import Path

from ..contracts import ROOT, reject_trading_secrets, sha256
from ..errors import TradingError
from .engine import validate_policy, validate_run

RUN_ID = re.compile(r"^srun-[0-9a-f]{24}$")


def load_policy(path="config/simulation.paper.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise TradingError("invalid_simulation_policy", "The simulation policy must remain inside the project.")
    try:
        policy = json.loads(target.read_text(encoding="utf-8-sig"))
        validate_policy(policy)
    except TradingError as error:
        if error.code == "sensitive_state":
            raise
        raise TradingError("invalid_simulation_policy", "The simulation policy is invalid.") from None
    except (OSError, ValueError, UnicodeError):
        raise TradingError("invalid_simulation_policy", "Cannot read a valid simulation policy.") from None
    return policy, sha256(policy)


class SimulationStore:
    def __init__(self, root=None):
        self.base = Path(root if root is not None else ROOT) / "runtime/trading/simulation"
        self.runs = self.base / "runs"
        self.switch = self.base / "kill-switch.json"

    # ------------------------------------------------------------------ kill switch
    def kill_switch(self, policy):
        if policy["kill_switch"]["engaged"]:
            return True, "policy"
        if not self.switch.exists():
            return False, None
        try:
            data = json.loads(self.switch.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("engaged"), bool):
                raise ValueError
        except (OSError, ValueError, UnicodeError):
            return True, "switch_file_unreadable"
        return (True, "switch_file") if data["engaged"] else (False, None)

    def set_kill_switch(self, engaged, at):
        self._write(self.switch, {"engaged": bool(engaged), "changed_at": at}, replace=True)

    # ------------------------------------------------------------------ runs
    def _write(self, target, document, replace=False):
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, indent=1, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, target)
                temporary = None
            else:
                os.link(temporary, target)
        except FileExistsError:
            raise TradingError("sim_run_exists", "An identical simulation run is already saved; nothing was changed.") from None
        except (OSError, ValueError, TypeError):
            raise TradingError("sim_write_failed", "Could not save simulation data; nothing was stored.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def save(self, run):
        validate_run(run)
        reject_trading_secrets(run)
        self._write(self.runs / f"{run['run_id']}.json", run)
        return run

    def load(self, run_id):
        if not RUN_ID.match(str(run_id)):
            raise TradingError("invalid_sim_run_id", "A simulation run ID looks like srun- followed by 24 hex characters.")
        path = self.runs / f"{run_id}.json"
        if not path.is_file():
            raise TradingError("sim_run_not_found", "No saved simulation run with this ID.")
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            validate_run(run)
            if run["run_id"] != run_id:
                raise ValueError
        except TradingError as error:
            if error.code == "sensitive_state":
                raise
            raise TradingError("sim_run_corrupt", "The saved simulation run failed validation.") from None
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            raise TradingError("sim_run_corrupt", "The saved simulation run is unreadable.") from None
        return run

    def list(self):
        items = []
        for path in sorted(self.runs.glob("srun-*.json")) if self.runs.is_dir() else []:
            try:
                run = self.load(path.stem)
                items.append({"run_id": run["run_id"], "dataset_id": run["dataset"]["dataset_id"],
                              "symbol": run["dataset"]["symbol"], "fills": run["summary"]["fills"],
                              "ending_equity": run["summary"]["ending_equity"], "simulated": True, "readable": True})
            except TradingError:
                items.append({"run_id": path.stem, "readable": False})
        return items
