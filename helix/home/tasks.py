from __future__ import annotations

from datetime import datetime
from typing import Any

# Where the Home checklist is persisted (settings key). One source of truth for GUI + headless notifier.
HOME_TASKS_SETTING = "home_tasks"

# Frequency label -> cadence in days, used to compute due/overdue. Unknown labels fall back to weekly.
FREQ_DAYS = {
    "daily": 1, "every day": 1,
    "twice a week": 3, "2x week": 3, "twice weekly": 3,
    "weekly": 7, "every week": 7,
    "biweekly": 14, "every two weeks": 14, "twice a month": 15,
    "monthly": 30, "every month": 30,
    "quarterly": 91, "yearly": 365, "annually": 365,
}
DEFAULT_FREQ_DAYS = 7


def freq_to_days(freq: Any) -> int:
    return FREQ_DAYS.get(str(freq).strip().lower(), DEFAULT_FREQ_DAYS)


def task_status(freq: Any, last_done: Any) -> str:
    """Return one of: Overdue, Due now, Due soon, On track — from frequency vs. last-done date."""
    days = freq_to_days(freq)
    last_done = str(last_done or "").strip()
    if not last_done:
        return "Due now"
    try:
        last = datetime.strptime(last_done[:10], "%Y-%m-%d")
    except ValueError:
        return "Due now"
    elapsed = (datetime.now() - last).days
    if elapsed >= days * 1.5:
        return "Overdue"
    if elapsed >= days:
        return "Due now"
    if elapsed >= days * 0.75:
        return "Due soon"
    return "On track"


def normalize_task(task: Any) -> list:
    """Pad/truncate a stored task to [action, item, frequency, last_done] of strings."""
    if isinstance(task, (list, tuple)):
        cells = (list(task) + ["", "", "", ""])[:4]
    else:
        cells = ["", "", "", ""]
    return [str(cell) for cell in cells]


def due_tasks(tasks: list) -> list[dict[str, Any]]:
    """Tasks that are Due now / Overdue (worst first)."""
    out: list[dict[str, Any]] = []
    for task in tasks or []:
        action, item, freq, last_done = normalize_task(task)
        if not (action or item):
            continue
        status = task_status(freq, last_done)
        if status in ("Due now", "Overdue"):
            out.append({"status": status, "action": action, "item": item, "freq": freq})
    out.sort(key=lambda entry: 0 if entry["status"] == "Overdue" else 1)
    return out


def reminder_message(tasks: list) -> str:
    """The SMS body: a short list of what's due/overdue, or an all-clear."""
    due = due_tasks(tasks)
    if not due:
        return "HELIX: you're all caught up on household tasks."
    lines = ["HELIX home reminders:"]
    for entry in due:
        name = f"{entry['action']} {entry['item']}".strip()
        lines.append(f"- {name} ({'overdue' if entry['status'] == 'Overdue' else 'due'})")
    return "\n".join(lines)
