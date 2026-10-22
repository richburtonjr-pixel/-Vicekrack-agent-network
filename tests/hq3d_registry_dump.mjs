// Prints the 3D headquarters' agents and rooms as JSON, so tests/test_departments.py can check that
// they match the one department registry (config/departments.json). Run with Node: node <this> <repo>.
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const L = await import(pathToFileURL(resolve(process.argv[2], "vicekrack/hq/static/hq3d/layout.js")).href);
const A = await import(pathToFileURL(resolve(process.argv[2], "vicekrack/hq/static/hq3d/agents.js")).href);

process.stdout.write(JSON.stringify({
  agents: A.AGENTS.map((a) => ({ id: a.id, name: a.name, role: a.role, room: a.department, work: a.work })),
  rooms: L.DEPARTMENTS.map((d) => ({ id: d.id, name: d.name, agent: d.agent })),
}));
