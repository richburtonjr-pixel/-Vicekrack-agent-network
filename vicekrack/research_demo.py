"""Step 50: offline stand-ins for the Research Desk (demo mode and tests). Nothing here opens a connection.

- MockGrokResearch: answers the Responses request like Grok with web search would, for ANY topics typed,
  with three clearly labelled DEMO stories per topic: one reported by two trusted outlets, one by a
  single trusted outlet, and one only by an unlisted site. Article URLs point at fixture pages.
- FixturePages: serves those fixture article pages (HTML with a title, a publication date and a few
  sentences) to the Research Desk's page check, in place of the internet.
- MockGrokWriter: answers the Grok script-writer request with a short, hedged, valid script.

The demo shows the real flow and real labels (Verification and Selection run unchanged); its words are
synthetic and say so.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from html import escape

from .creator import MockScriptDrafter


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "topic"


def _envelope(result, input_tokens, output_tokens, searches=0):
    return {"id": "resp_demo", "status": "completed", "model": "demo",
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(result)}]}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "server_side_tool_usage_details": {"web_search_calls": searches}}}


class FixturePages:
    """URL -> fixture page. Shaped like the articles module's fetchers (fetch(url, rule, limits))."""

    mode = "fixture"

    def __init__(self):
        self.pages = {}
        self.calls = []

    def add(self, url, title, sentences, published):
        body = (f"<html><head><title>{escape(title)}</title>"
                f'<meta property="article:published_time" content="{published}"></head>'
                f"<body><article>{''.join(f'<p>{escape(s)}</p>' for s in sentences)}</article></body></html>")
        self.pages[url] = body.encode("utf-8")

    def fetch(self, url, rule, limits):
        from .articles import ArticleError, check_article_url
        self.calls.append(url)
        check_article_url(url, rule)
        if url not in self.pages:
            raise ArticleError("article_unavailable")
        return {"final_url": url, "redirects": [], "content_type": "text/html", "charset": "utf-8",
                "body": self.pages[url]}


class MockGrokResearch:
    def __init__(self, pages, clock):
        self.pages, self.clock, self.calls = pages, clock, []

    def __call__(self, body, *, timeout_seconds):
        self.calls.append(body)
        topics = json.loads(body["input"][1]["content"])["topics"]
        now = datetime.strptime(self.clock(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        published = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        day = published[:10]
        stories = []
        variants = [("schedule change", "could see an unexpected schedule change this week",
                     ("officials will hold a major announcement next month in Atlanta for fans",
                      "A major {} announcement is coming next month in Atlanta, officials told fans")),
                    ("pricing shift", "may announce a pricing shift before the weekend",
                     ("organizers plan a public press event on Friday with surprise guests",
                      "Organizers of {} scheduled a Friday press event and promised surprise guests")),
                    ("partnership", "is reportedly close to a partnership with a streaming service",
                     ("will launch a summer tour across twelve cities starting in June",
                      "A twelve-city summer tour for {} starts in June, the team said"))]
        for number, topic in enumerate(topics):
            slug = _slug(topic)
            change, happening, (one, two) = variants[number % len(variants)]
            first = f"DEMO: {topic} {one}."
            second = "DEMO: " + two.format(topic) + "."
            a = f"https://apnews.com/article/demo-{slug}-announcement"
            b = f"https://www.theverge.com/demo/{slug}-announcement"
            self.pages.add(a, f"DEMO: {topic} {one.split(' for ')[0]} - AP",
                           [first, f"This is demo text written for the ViceKrack demo about {topic}.",
                            "No real reporting is shown here."], published)
            self.pages.add(b, f"DEMO: What we know about {topic} - The Verge",
                           [second, f"The second demo page reports it independently.",
                            "Demo content only."], published)
            c = f"https://www.reuters.com/demo/{slug}-single-report"
            self.pages.add(c, f"DEMO: Exclusive {topic} {change} - Reuters",
                           [f"DEMO: {topic} {happening}.",
                            "Only one outlet carries this demo report."], published)
            d = f"https://fan-rumors.example.net/demo/{slug}"
            self.pages.add(d, f"DEMO: Fan theory about {topic}",
                           [f"DEMO: A fan site claims {topic} has a secret feature nobody has seen.",
                            "This demo site is not on the trusted outlet list."], published)
            stories += [
                {"topic": topic, "headline": f"DEMO: {topic} {one.split(' for ')[0].split(' with ')[0]}",
                 "summary": f"Two outlets report this about {topic}. Demo data, not real news.",
                 "aliases": [], "sources": [{"url": a, "publisher": "AP", "published": day},
                             {"url": b, "publisher": "The Verge", "published": day}]},
                {"topic": topic, "headline": f"DEMO: Exclusive report on a {topic} {change}",
                 "summary": f"A single outlet reports a {topic} {change}. Demo data, not real news.",
                 "aliases": [], "sources": [{"url": c, "publisher": "Reuters", "published": day}]},
                {"topic": topic, "headline": f"DEMO: Fan site theory about {topic}",
                 "summary": f"An unlisted fan site makes a claim about {topic}. Demo data, not real news.",
                 "aliases": [], "sources": [{"url": d, "publisher": "Fan Rumors", "published": day}]},
            ]
        return _envelope({"stories": stories}, 21000 * len(topics), 900 * len(topics), 4 * len(topics))


class MockGrokWriter:
    """Answers the writer request with the offline mock script, wording the hook a little more like a hook."""

    def __init__(self):
        self.calls = []

    def __call__(self, body, *, timeout_seconds):
        self.calls.append(body)
        request = json.loads(body["input"][1]["content"])
        draft = MockScriptDrafter().draft(request=request, model=None)
        draft["beats"][0]["on_screen_text"] = (request["topic"][:57] + "...") if len(request["topic"]) > 60 \
            else request["topic"]
        return _envelope(draft, 2400, 700)
