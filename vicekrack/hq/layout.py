"""ViceKrack Living HQ layout (Step 32): rooms, bots and which recorded components drive them.

The mapping is fixed and shown in the interface. A room is driven ONLY by events from its
listed components; a room whose components have no events in the selected timeline shows
"No recorded activity". Nothing here invents work.

Upstairs (trading, teal): the four Step 28 research stages, one per room.
Downstairs (content, violet): content-production stages from saved production state.
  The Step 5 Researcher -> Analyst -> Reviewer workflow emits no Step 31 events, so these
  rooms show the matching production stages instead:
  Researcher <- brief, Creator <- creator, Reviewer <- validate, Analyst <- plan.
Operations lobby (shared): the research controller, the simulator engine and the
  content preview render. Lounge and kitchen are decorative shared spaces.
"""

TRADING_RESEARCH = ("trading.research.market_scout", "trading.research.trend_agent",
                    "trading.research.strategy_agent", "trading.research.risk_review")
CONTENT_PRODUCTION = ("content.production.brief", "content.production.creator", "content.production.validate",
                      "content.production.plan", "content.production.preview")

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
     "components": ["content.production.brief"], "mapping": "Content production stage: story brief",
     "role": "Turns a verified, selected story into a Story Brief.",
     "inputs": "Verified research and a selection report", "decisions": "Brief contents and sources",
     "outputs": "A Story Brief"},
    {"room": "analyst", "label": "Analyst", "floor": "ground", "department": "content", "bot": "analyst",
     "components": ["content.production.plan"], "mapping": "Content production stage: scene plan",
     "role": "Plans the 15-second Short's scenes from the validated script.",
     "inputs": "A validated Short Script", "decisions": "Scene timing and visual needs",
     "outputs": "A scene plan"},
    {"room": "reviewer", "label": "Reviewer", "floor": "ground", "department": "content", "bot": "reviewer",
     "components": ["content.production.validate"], "mapping": "Content production stage: script validation",
     "role": "Validates the drafted script against the Short Script contract.",
     "inputs": "A drafted Short Script", "decisions": "Pass or fail validation",
     "outputs": "A validated script"},
    {"room": "creator", "label": "Creator", "floor": "ground", "department": "content", "bot": "creator",
     "components": ["content.production.creator"], "mapping": "Content production stage: creator draft",
     "role": "Drafts the Short Script from the Story Brief.",
     "inputs": "A Story Brief", "decisions": "Hook, script and captions",
     "outputs": "A drafted Short Script"},
    {"room": "operations", "label": "Operations", "floor": "shared", "department": "shared", "bot": None,
     "components": ["trading.research.controller", "trading.simulation.engine", "content.production.preview"],
     "mapping": "Research controller, simulator engine and content preview render",
     "role": "Workflow consoles: they start and finish workflows; they are not agents.",
     "inputs": "Recorded workflow events", "decisions": "None (display only)", "outputs": "Status lights"},
    {"room": "lounge", "label": "Lounge", "floor": "shared", "department": "shared", "bot": None, "components": [],
     "mapping": "Decorative shared space", "role": "Idle bots may relax here.", "inputs": "-", "decisions": "-",
     "outputs": "-"},
    {"room": "kitchen", "label": "Kitchen", "floor": "shared", "department": "shared", "bot": None, "components": [],
     "mapping": "Decorative shared space", "role": "Idle bots may grab a coffee here.", "inputs": "-",
     "decisions": "-", "outputs": "-"},
)

COMPONENT_ROOM = {component: room["room"] for room in ROOMS for component in room["components"]}

# Fixed workflow orders. "Waiting" and handoffs are derived only inside these sequences.
GROUPS = (
    {"group": "trading_research", "controller": "trading.research.controller", "members": TRADING_RESEARCH},
    {"group": "content_production", "controller": None, "members": CONTENT_PRODUCTION},
)
