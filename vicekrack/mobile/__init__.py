"""ViceKrack Mobile Command Center (a separate program next to the Living HQ).

The HQ keeps running exactly as before, bound to 127.0.0.1. This package is its own small server
(default 127.0.0.1:8770) that the phone reaches through a private Tailscale connection. It talks
to the HQ only through the HQ's existing Studio API, so no HQ code changes. See docs/mobile.md.
"""
