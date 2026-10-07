"""ViceKrack Living HQ layout: rooms, bots, operations stations and what drives them.

Step 33 attribution (shown in the interface): a room is driven ONLY by events from its own
role. A room whose role has no events in the selected timeline shows "No recorded activity".

Upstairs (trading, teal): the four Step 28 research stages, one per room.
Downstairs (content, violet): the actual content roles.
  Researcher, Analyst, Reviewer <- the Step 5 research_review workflow roles
  Creator                       <- the production pipeline's Creator stage (the Creator role)
Operations lobby (shared): automated, non-agent stations, each clearly labelled:
  research controller, simulator, workflow orchestrator, production pipeline,
  brief builder, script validator, scene planner, preview renderer, quality checker.
Lounge and kitchen are decorative shared spaces.

Until Step 32 the brief, plan and validate stages were drawn in the Researcher, Analyst and
Reviewer rooms. They are automated stages, not those roles, so they now appear only as
operations stations (historical timelines keep their honest stage labels).
"""

TRADING_RESEARCH = ("trading.research.market_scout", "trading.research.trend_agent",
                    "trading.research.strategy_agent", "trading.research.risk_review")
CONTENT_WORKFLOW = ("content.workflow.researcher", "content.workflow.analyst", "content.workflow.reviewer")
CONTENT_PRODUCTION = ("content.production.brief", "content.production.creator", "content.production.validate",
                      "content.production.plan", "content.production.preview")

STATIONS = (
    {"station": "research_controller", "label": "Research controller", "short": "Research ctrl", "department": "trading",
     "component": "trading.research.controller", "role": "Starts and finishes the Step 28 research workflow."},
    {"station": "simulator", "label": "Simulator", "short": "Simulator", "department": "trading",
     "component": "trading.simulation.engine", "role": "Offline Step 29 simulation: simulated order decisions and fills."},
    {"station": "workflow_orchestrator", "label": "Workflow orchestrator", "short": "Orchestrator", "department": "content",
     "component": "content.workflow.orchestrator", "role": "Starts and finishes the Researcher, Analyst, Reviewer workflow."},
    {"station": "production_pipeline", "label": "Production pipeline", "short": "Pipeline", "department": "content",
     "component": "content.production.pipeline", "role": "Runs the fixed production stages in order and checkpoints them."},
    {"station": "brief_builder", "label": "Brief builder", "short": "Brief builder", "department": "content",
     "component": "content.production.brief", "role": "Automated: builds the Story Brief from a verified, selected story."},
    {"station": "script_validator", "label": "Script validator", "short": "Validator", "department": "content",
     "component": "content.production.validate", "role": "Automated: checks the draft against the Short Script contract."},
    {"station": "scene_planner", "label": "Scene planner", "short": "Scene planner", "department": "content",
     "component": "content.production.plan", "role": "Automated: plans the 15-second scenes from the validated script."},
    {"station": "preview_renderer", "label": "Preview renderer", "short": "Renderer", "department": "content",
     "component": "content.production.preview", "role": "Automated: renders the local, watermarked preview."},
    {"station": "quality_checker", "label": "Quality checker", "short": "Quality", "department": "content",
     "component": "content.production.quality", "role": "Automated: technical quality report (never publishes)."},
)

ROOMS = (
    {"room": "market_scout", "label": "Market Scout", "floor": "upper", "department": "trading", "bot": "market_scout",
     "components": ["trading.research.market_scout"], "mapping": "Research workflow stage 1 (Step 28): market scan",
     "role": "Checks the dataset at a simulated time: coverage, gaps and freshness.",
     "inputs": "Stored historical bars at a simulated time", "decisions": "Is the evidence usable?",
     "outputs": "A validated market-scout handoff"},
    {"room": "trend_agent", "label": "Trend Agent", "floor": "upper", "department": "trading", "bot": "trend_agent",
     "components": ["trading.research.trend_agent"], "mapping": "Research workflow stage 2 (Step 28): trend",
     "role": "Describes the trend from closed-bar indicators.",
     "inputs": "EMA and related indicator values", "decisions": "Trend direction and readiness",
     "outputs": "A validated trend handoff"},
    {"room": "strategy_agent", "label": "Strategy Agent", "floor": "upper", "department": "trading",
     "bot": "strategy_agent", "components": ["trading.research.strategy_agent"],
     "mapping": "Research workflow stage 3 (Step 28): strategy signals",
     "role": "Reviews rule-based research signals; never authorizes orders.",
     "inputs": "Research signals and the trend handoff", "decisions": "Which research signals apply",
     "outputs": "A validated strategy handoff"},
    {"room": "risk_review", "label": "Risk Review", "floor": "upper", "department": "trading", "bot": "risk_review",
     "components": ["trading.research.risk_review"], "mapping": "Research workflow stage 4 (Step 28): research review",
     "role": "Checks research completeness. It is not the paper risk engine and authorizes nothing.",
     "inputs": "All earlier handoffs", "decisions": "Research verdict (research only)",
     "outputs": "The workflow verdict"},
    {"room": "researcher", "label": "Researcher", "floor": "ground", "department": "content", "bot": "researcher",
     "components": ["content.workflow.researcher"], "mapping": "Researcher role (Step 5 research_review workflow)",
     "role": "Researches the request using only the supplied context.",
     "inputs": "The task instructions and design notes", "decisions": "What the notes support",
     "outputs": "A research handoff for the Analyst"},
    {"room": "analyst", "label": "Analyst", "floor": "ground", "department": "content", "bot": "analyst",
     "components": ["content.workflow.analyst"], "mapping": "Analyst role (Step 5 research_review workflow)",
     "role": "Analyses the Researcher's handoff.", "inputs": "The research handoff",
     "decisions": "What the research means", "outputs": "An analysis handoff for the Reviewer"},
    {"room": "reviewer", "label": "Reviewer", "floor": "ground", "department": "content", "bot": "reviewer",
     "components": ["content.workflow.reviewer"], "mapping": "Reviewer role (Step 5 research_review workflow)",
     "role": "Reviews the analysis before the workflow finishes.", "inputs": "The analysis handoff",
     "decisions": "Whether the result holds up", "outputs": "The reviewed result"},
    {"room": "creator", "label": "Creator", "floor": "ground", "department": "content", "bot": "creator",
     "components": ["content.production.creator"], "mapping": "Creator role (production pipeline Creator stage)",
     "role": "Drafts the Short Script from the Story Brief.",
     "inputs": "A Story Brief", "decisions": "Hook, script and captions",
     "outputs": "A drafted Short Script"},
    {"room": "operations", "label": "Operations", "floor": "shared", "department": "shared", "bot": None,
     "components": [s["component"] for s in STATIONS], "mapping": "Automated stations and workflow controllers (not agents)",
     "role": "Labelled operations stations: controllers and automated production stages.",
     "inputs": "Recorded workflow events", "decisions": "None (display only)", "outputs": "Station status lights"},
    {"room": "lounge", "label": "Lounge", "floor": "shared", "department": "shared", "bot": None, "components": [],
     "mapping": "Decorative shared space", "role": "Idle bots may relax here.", "inputs": "-", "decisions": "-",
     "outputs": "-"},
    {"room": "kitchen", "label": "Kitchen", "floor": "shared", "department": "shared", "bot": None, "components": [],
     "mapping": "Decorative shared space", "role": "Idle bots may grab a coffee here.", "inputs": "-",
     "decisions": "-", "outputs": "-"},
)

COMPONENT_ROOM = {component: room["room"] for room in ROOMS for component in room["components"]}
COMPONENT_STATION = {station["component"]: station["station"] for station in STATIONS}

# Fixed workflow orders. "Waiting" and handoffs are derived only inside these sequences.
GROUPS = (
    {"group": "trading_research", "controller": "trading.research.controller", "members": TRADING_RESEARCH},
    {"group": "content_workflow", "controller": "content.workflow.orchestrator", "members": CONTENT_WORKFLOW},
    {"group": "content_production", "controller": "content.production.pipeline", "members": CONTENT_PRODUCTION},
)
