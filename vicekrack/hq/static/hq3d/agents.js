/*
 * ViceKrack HQ 2.0: the eight agents (identities and roles from docs/visual-direction.md, approved
 * 2026-10-13). This is the integration contract for their future 3D characters; Phase 1 draws only a
 * marked, empty workstation for each one ("character pending"), never a stand-in person or face.
 *
 * When a character's model exists it is added here as `model`:
 *   { url: "/static/hq3d/characters/<id>.glb",   // one glTF binary, rigged (humanoid skeleton)
 *     height_m: 1.78,                               // real-world height, used to scale the model
 *     clips: { idle, walk, sit, sit_type, stand_talk } }  // animation clip names inside the file
 * and `reference` points at the approved front / three-quarter / profile images in
 * docs/images/characters/. Nothing here is generated from a photograph automatically.
 *
 * `department` ties the agent to real ViceKrack work; the status shown for an agent always comes from
 * /api/studio/overview (saved records), never from this file.
 */

export const ANIMATION_SLOTS = ["idle", "walk", "sit", "sit_type", "stand_talk"];

export const AGENTS = [
  { id: "ricky", name: "Ricky", role: "Leadership", department: "executive", gender: "man",
    work: "Your approvals and reviews", model: null, reference: null },
  { id: "kiara", name: "Kiara", role: "Research", department: "research", gender: "woman",
    work: "Research runs (Grok web search) and the pages ViceKrack opens", model: null, reference: null },
  { id: "jax", name: "Jax", role: "Trends", department: "trends", gender: "man",
    work: "Coming soon: no trend or audience data yet", model: null, reference: null },
  { id: "layla", name: "Layla", role: "Strategy", department: "strategy", gender: "woman",
    work: "Story selection and angles", model: null, reference: null },
  { id: "ace", name: "Ace", role: "Risk Review", department: "risk", gender: "man",
    work: "Verification, rumor labels and quality checks", model: null, reference: null },
  { id: "sienna", name: "Sienna", role: "Content Creator", department: "studio", gender: "woman",
    work: "Scripts, clips, narration, captions and rendering", model: null, reference: null },
  { id: "kai", name: "Kai", role: "Analytics", department: "analytics", gender: "man",
    work: "Coming soon: no published-video performance data yet", model: null, reference: null },
  { id: "nia", name: "Nia", role: "Operations", department: "operations", gender: "woman",
    work: "Workflow progress and exports (no publishing)", model: null, reference: null },
];

export function agentById(id) {
  return AGENTS.find((agent) => agent.id === id) || null;
}

/* What the HQ may say about a character's appearance: only "pending" until an approved model exists. */
export function characterStatus(agent) {
  if (!agent.model) { return "Character pending: waiting for the approved reference and 3D model."; }
  return "Character model loaded.";
}
