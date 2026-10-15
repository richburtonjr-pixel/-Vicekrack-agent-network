# ViceKrack HQ 2.0: the 3D headquarters

Phase 1 is an explorable, two-level luxury office building. It is a presentation and navigation layer
over the existing app: Research, Video Studio, approvals, costs and saved records are unchanged, and
the HQ only reads them. The approved look and the eight agents are in
[visual-direction.md](visual-direction.md).

## Open it

1. Double-click **ViceKrack** on your Desktop (it starts ViceKrack with the Studio on).
2. The app opens on **Headquarters**. You can also click **Headquarters** at the top of the menu at
   any time. **Classic house** is still there.

| Do this | How |
|---|---|
| Walk | `W` `A` `S` `D` or the arrow keys (`Shift` to run). On a touch screen, hold the arrow pad |
| Look around | Drag with the mouse (or a finger) |
| Zoom | Mouse wheel |
| Go upstairs | Walk up the grand staircase in the middle of the atrium |
| Open a department | Stand in it and press `E`, click its sign or a desk, or pick it from the list on the left |
| Close the panel | `Esc` or the × |
| Jump to a department | Click it in the list on the left (also works with the keyboard: `Tab`, then `Enter`) |
| Slower laptop | **Graphics: high** switches to **low** (no shadows, lower resolution) |

Each department panel has a button that opens the real screen for that work (**Open Research**,
**Open Video Studio**, **Open videos to review**).

## The building

```
 north ─────────────────────────────────────────────────────────────────────────
 │ UPPER FLOOR (balcony, 6 m up): Analytics (Kai) | Executive office (Ricky) | Operations (Nia) │
 │ west wing upstairs: Trends (Jax)            stairs            east wing upstairs: lounge   │
 │ GROUND: Strategy (Layla)                      ▲                Risk Review (Ace)           │
 │ GROUND: Research (Kiara)            Command desk (live)         Video Studio (Sienna)       │
 │                                     lounge, indoor trees                                    │
 south (glass facade, entrance) ────────────────────────────────────────────────────────────────
```

80 m × 56 m, a 15 m atrium with a skylight, floor-to-ceiling glass on three sides, glass balcony
railings, indoor trees, lounges, a cyclorama video studio and a glass executive office upstairs.

## Real data only

- Every number, status and activity line comes from `GET /api/studio/overview`, which counts saved
  research runs, story cards, productions and video workflows. Nothing is estimated or invented.
- A badge always says where the numbers come from: **Real data**, **Practice data (demo)** (the
  `--studio-demo` copy), **Studio off: no real data**, or **Data unavailable**.
- Departments without a real data source say **Coming soon** (Trends, Analytics). Empty departments
  say **Nothing yet**.
- The HQ never sends a write request and never triggers a paid request. It refreshes its numbers when
  you open it, when you click **Refresh numbers**, and every 30 seconds while it is on screen (free,
  local reads).
- Nothing moves on its own: there is no decorative activity yet, so nothing can be mistaken for work.

## Technology

**Audit of the existing app.** The HQ is plain HTML, CSS and JavaScript served by ViceKrack's own Python
server (`vicekrack/hq/api.py`). There is no build step and no CDN: every file is on an allowlist, and a
strict Content-Security-Policy only allows scripts from the app itself. Views switch in `app.js`
(`setView`); Research and Video Studio are separate scripts (`research.js`, `studio.js`) that read and
act through `/api/studio/...`.

**Choice: Three.js r169, vendored, as ES modules. No React Three Fiber.**

| | Three.js (chosen) | React Three Fiber |
|---|---|---|
| Fits the current app | Yes: one more script file, like the others | Needs React, JSX and a build system the app does not have |
| Security policy | One local file on the allowlist (`static/vendor/three.module.min.js`, SHA-256 pinned in tests) | Many more dependencies to vet and pin |
| Performance | Direct control: frames are drawn only when something changes; static shadows drawn once | Same engine underneath, plus React overhead |
| Later: characters | `GLTFLoader` + `AnimationMixer` (Three.js add-ons) load rigged glTF characters | Same |

Files:

| File | What |
|---|---|
| `vicekrack/hq/static/hq3d/layout.js` | The floor plan, departments, workstations and walking rules (pure; tested in Node) |
| `vicekrack/hq/static/hq3d/world.js` | The building, furniture, lighting and in-world screens (placeholder shapes, no downloads) |
| `vicekrack/hq/static/hq3d/agents.js` | The eight agents and the character integration contract |
| `vicekrack/hq/static/hq3d/main.js` | Renderer, third-person camera, movement, panels and real-data loading |
| `vicekrack/hq/studio.py` `overview()` | The read-only, real-data summary per department |

Performance: about 600 simple meshes, a static shadow map, on-demand rendering (an idle HQ draws no
frames) and a **Graphics: low** switch. Rendering pauses whenever another screen is open.

Path to controller and mobile: movement already takes a direction from any source (keys or the touch
pad); a gamepad (the browser Gamepad API) can feed the same input. The department list doubles as a
keyboard- and touch-friendly tour.

## Characters: what a photo can and cannot do

A photograph cannot automatically become a moving 3D person. Each of the eight characters needs:

1. **Reference sheet** (front, three-quarter, profile) per agent, approved by you. Your Content House
   image is a good start for faces, hair and clothing; a second pass per person keeps them consistent.
2. **3D model** (mesh + textures) built from the references. Options: a 3D character artist
   (most faithful, typically several hundred dollars per character), or an AI image-to-3D tool followed
   by an artist's cleanup (faster and cheaper, but faces and hair usually need manual work to stay
   recognizable and respectful).
3. **Rigging**: a humanoid skeleton and skin weights so the body can bend.
4. **Animation**: idle, walk, sit, sit-and-type and stand-and-talk clips (motion libraries can be
   retargeted onto a standard humanoid rig).
5. **Export** as one optimized `.glb` per agent (target: under about 10 MB each, about 30k triangles,
   2K textures) so a laptop can show all eight.

The HQ is ready for them: `agents.js` describes each agent's `model` (`url`, `height_m`, clip names)
and `reference`. Until a model exists, each workstation shows only a marked empty place with a name
plate: **Character pending reference**. No stand-in people or faces are used.

Note for the owner: the role captions printed on the Content House image (for example "Anchor Agent",
"Automation Agent") differ from the approved roles in `visual-direction.md` (Leadership, Research,
Trends, Strategy, Risk Review, Content Creator, Analytics, Operations). Phase 1 keeps the approved
roles; tell us if they should change.

## Next phases (proposal)

1. **Phase 2:** materials and furniture pass (real glTF furniture, better glass and lighting), a
   wide establishing camera, minimap and gamepad.
2. **Phase 3:** first character pipeline end to end with one agent (reference → model → rig → idle and
   sit-and-type), loaded only after your approval.
3. **Phase 4:** all eight characters at their workstations; status cues driven only by
   `/api/studio/overview`. Any decorative walking is labelled as atmosphere.

## Limitations

- Placeholder architecture: clean simple shapes, not photo-real models yet.
- Software rendering (no graphics acceleration) is slow; the HQ falls back to a message and the rest of
  the app keeps working.
- The overview reads the 20 most recent research runs for story-card counts.
