# Departments and agents (HQ Phase 1)

ViceKrack keeps **one list** of its departments, agents and headquarters rooms:
[`config/departments.json`](../config/departments.json). Think of it as the building directory in
the lobby: every screen reads the same directory instead of keeping its own copy.

## What is in it

| Department | Kind | Status | Agents |
|---|---|---|---|
| Headquarters | shared (serves every department) | active | Ricky (Leadership), Kai (Analytics, coming soon), Nia (Operations) |
| Content Production | department | active | Kiara (Research), Jax (Trends, coming soon), Layla (Strategy), Ace (Risk Review), Sienna (Content Creator) |
| Trading | department | paper only | Market research, Technical analysis, Strategy, Risk review (the Step 28 rule-based research agents) |

`rooms` lists the 3D headquarters rooms and which department and agent each belongs to.

## Who reads it

| Screen | How |
|---|---|
| Trading terminal | Loads its agent list from `GET /api/departments` (trading agents only). |
| 3D headquarters | Still draws from `hq3d/agents.js` and `hq3d/layout.js`, but `tests/test_departments.py` fails if their agents or rooms differ from the registry in any way (id, name, role, job line, room). |
| `GET /api/departments` | Read-only, no parameters, same local-only rules as every other HQ route. A broken registry gives the fixed error `invalid_department_registry`, never a guess. |

The phone app and the classic house will read it in later phases.

## Adding an agent or a department

1. Add it to `config/departments.json`.
2. Run the tests. The registry is checked against
   [`schemas/department_registry.schema.json`](../schemas/department_registry.schema.json) and for
   consistency (unique ids, every agent in a real department, every room's agent in that room's
   department).

## What the registry is not

- **Not permission.** It has no fields for limits, keys or access. Trading limits, the kill switch and
  money calculations stay in `vicekrack/trading/`, enforced by code.
- **Not live status.** Whether an agent is working right now always comes from saved records, never from
  this file. `status` here only says whether an agent exists yet (`active`) or is planned
  (`coming_soon`).
