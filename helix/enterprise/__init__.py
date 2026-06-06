"""The Enterprise pillar — tools for tracking a person's work/business.

v1 connectors (read-only, summarized by Claude):
- `slack.py`   — Slack activity digest (mentions / DMs / recent channel activity).
- `gitwork.py` — recent git history across associated projects ("what work got done").

Mirrors the Investment pillar's shape: stdlib `urllib`/`subprocess` clients, secrets in the
git-ignored settings, nothing leaves the machine except the explicit Slack/Claude calls.
"""
