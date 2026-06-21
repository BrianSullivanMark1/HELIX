"""Agents — goal-driven automations the user designs (§agents).

An agent differs from a Task and an App:
  - an App is a tool you open and use,
  - a Task is a one-shot action you trigger that returns a result,
  - an Agent is a saved GOAL + TRIGGER that runs on its own through the AI tool-loop — it decides and
    takes actions toward the goal, asking for approval before anything that spends or reaches outward.

This package owns the agent registry (persistence) and the run loop. v1 supports manual "Run now";
scheduled/event triggers build on the same definitions.
"""
