# Step 51: Looser research, you pick

> **Numbering.** "Step 51" is the development step in [the roadmap](roadmap.md), not a GitHub
> pull-request number.

Step 50 blocked too many stories. Step 51 makes ViceKrack find more usable stories, and lets you make a
video from almost any story it found, with an honest label on screen.

## What changed for you

| Before | Now |
|---|---|
| "The article has no publication date" on The Athletic, CBS Sports and similar sites | Dates are read the way news sites actually write them (no timezone, day only, several dates on the page) |
| "Page doesn't mention the topic" when a site says *Sixers* instead of *76ers* | Grok also returns other names for the topic (nicknames, abbreviations), used only for that check |
| Pages opened one at a time | Up to 8 pages are opened side by side |
| NOT USABLE with no way around it | **YOUR CALL** with a **Use anyway** button |
| Make video failed on a card from yesterday | The story is checked again when you click, so old cards still work |

## Labels

| Label | Meaning | Button |
|---|---|---|
| MULTIPLE REPORTS | 2+ independent trusted outlets | Make video |
| RUMOR | one trusted outlet | Make video |
| CONFIRMED | an official source | Make video |
| **YOUR CALL** | ViceKrack opened a real page about it, but it is old, undated, low-scoring, a near-repeat, or only on sites outside the trusted list | **Use anyway** |
| NOT USABLE | nothing real to build on, or the story is contradicted / disputed / contains text aimed at AI tools | none |

A **Use anyway** video carries the matching band on every frame: `RUMOR - UNCONFIRMED`,
`MULTIPLE REPORTS - UNCONFIRMED`, or the new `UNVERIFIED SOURCE - UNCONFIRMED` (only sites outside the
trusted list). The script must still say the claim is reported, not fact. Nothing is invented: the
script can only use sentences from pages ViceKrack opened itself.

## Never waived

Hand picks still stop when another source contradicts or disputes the story, when the page contains
instruction-like text, when the date is in the future, when you already made that exact story (it
opens instead), or when no page could be opened.

## How

- `config/editorial.open.pick.json` (`user_pick: true`, `rumor_lane.allow_unrated: true`, profile
  name `open`, so history and production IDs are shared with `editorial.open.json`). It is used only
  for a story you pick by hand.
- `vicekrack/rumor.py`: the `unrated_source` label. `vicekrack/selection.py`: `PICK_WAIVED`.
- `vicekrack/research_desk.py`: `loose_date`, Grok `aliases`, parallel page fetches, the YOUR CALL
  offer, a fresh one-story selection when you click Make video / Use anyway.
- `articles.py` and the GTA profiles are unchanged.
