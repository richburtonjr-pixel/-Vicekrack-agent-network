"""HQ Phase 1: the one department registry (config/departments.json) and the screens that read it."""

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vicekrack import departments
from vicekrack.errors import NetworkError
from vicekrack.hq import api

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(api.STATIC)
PORT = 8765
HOST = {"host": f"127.0.0.1:{PORT}"}


def get(target, method="GET"):
    status, _headers, body = api.respond(method, target, dict(HOST), port=PORT)
    return status, json.loads(body)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.doc = departments.load()

    def test_shipped_registry_is_valid(self):
        self.assertEqual(self.doc["contract"], "department_registry")
        self.assertEqual({d["id"] for d in self.doc["departments"]}, {"headquarters", "content", "trading"})

    def test_departments_keep_their_existing_agents(self):
        ids = lambda dept: [a["id"] for a in departments.agents_in(self.doc, dept)]  # noqa: E731
        self.assertEqual(ids("headquarters"), ["ricky", "kai", "nia"])
        self.assertEqual(ids("content"), ["kiara", "jax", "layla", "ace", "sienna"])
        self.assertEqual(ids("trading"), ["market_scout", "trend_agent", "strategy_agent", "risk_review"])

    def test_trading_is_paper_only(self):
        trading = next(d for d in self.doc["departments"] if d["id"] == "trading")
        self.assertEqual(trading["status"], "paper_only")

    def test_no_permission_or_limit_fields_exist(self):
        text = json.dumps(self.doc).lower()
        for word in ("limit_", "max_", "api_key", "secret", "token", "permission", "allow_"):
            self.assertNotIn(word, text)

    def _invalid(self, change):
        broken = copy.deepcopy(self.doc)
        change(broken)
        with self.assertRaises(NetworkError) as caught:
            departments.validate(broken)
        self.assertEqual(caught.exception.code, "invalid_department_registry")

    def test_bad_registries_are_refused(self):
        self._invalid(lambda d: d["agents"].append(dict(d["agents"][0])))                     # duplicate agent
        self._invalid(lambda d: d["agents"][0].update(department="marketing"))                # unknown department
        self._invalid(lambda d: d["rooms"][1].update(agent="nobody"))                         # unknown agent
        self._invalid(lambda d: d["rooms"][1].update(department="trading"))                   # agent in other dept
        self._invalid(lambda d: d["departments"].append(dict(d["departments"][0])))           # duplicate dept
        self._invalid(lambda d: d["agents"][0].update(id="Bad Id"))                           # id shape
        self._invalid(lambda d: d["agents"][0].update(max_position="100"))                    # unknown field
        self._invalid(lambda d: d.update(version="2.0"))

    def test_unreadable_or_oversized_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for content in (b"{not json", b"\xff\xfe", b" " * (departments.MAX_BYTES + 1)):
                path = Path(tmp) / "departments.json"
                path.write_bytes(content)
                with self.assertRaises(NetworkError):
                    departments.load(path)
            with self.assertRaises(NetworkError):
                departments.load(Path(tmp) / "missing.json")

    def test_a_new_department_needs_only_the_registry(self):
        grown = copy.deepcopy(self.doc)
        grown["departments"].append({"id": "marketing", "name": "Marketing", "kind": "department",
                                     "status": "planned", "purpose": "Future department."})
        grown["agents"].append({"id": "mia", "name": "Mia", "role": "Campaigns", "department": "marketing",
                                "status": "coming_soon", "work": "Coming soon"})
        self.assertIs(departments.validate(grown), grown)


class DepartmentsRouteTests(unittest.TestCase):
    def test_route_serves_the_registry(self):
        status, body = get("/api/departments")
        self.assertEqual(status, 200)
        self.assertEqual(body, departments.load())

    def test_route_is_read_only_and_takes_no_parameters(self):
        self.assertEqual(get("/api/departments?x=1")[0], 400)
        self.assertEqual(get("/api/departments", method="POST")[0], 405)

    def test_route_refuses_other_sites(self):
        status, _h, _b = api.respond("GET", "/api/departments", {**HOST, "origin": "https://evil.example"}, port=PORT)
        self.assertEqual(status, 403)

    def test_broken_registry_gives_a_fixed_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "departments.json"
            bad.write_text("{}", encoding="utf-8")
            with mock.patch.object(departments, "REGISTRY", bad):
                status, body = get("/api/departments")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "invalid_department_registry")


class ScreensReadTheRegistryTests(unittest.TestCase):
    def test_trading_terminal_reads_its_agents_from_the_registry(self):
        js = (STATIC / "terminal.js").read_text(encoding="utf-8")
        self.assertIn('"/api/departments"', js)
        self.assertIn('a.department === "trading"', js)
        for agent_id in ("market_scout", "trend_agent", "strategy_agent", "risk_review"):
            self.assertNotIn(agent_id, js, "the terminal must not keep its own copy of the agent list")

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_3d_headquarters_matches_the_registry(self):
        result = subprocess.run(["node", str(ROOT / "tests/hq3d_registry_dump.mjs"), str(ROOT)],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        hq = json.loads(result.stdout)
        doc = departments.load()
        named = [a for a in doc["agents"] if a["department"] in ("headquarters", "content")]
        room_of = {room["agent"]: room["id"] for room in doc["rooms"] if room["agent"]}
        self.assertEqual(
            sorted((a["id"], a["name"], a["role"], a["work"], room_of[a["id"]]) for a in named),
            sorted((a["id"], a["name"], a["role"], a["work"], a["room"]) for a in hq["agents"]))
        self.assertEqual(sorted((r["id"], r["name"], r["agent"]) for r in doc["rooms"]),
                         sorted((r["id"], r["name"], r["agent"]) for r in hq["rooms"]))


if __name__ == "__main__":
    unittest.main()
