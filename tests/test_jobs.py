"""CURRENT TASKS: the register's laws - a job's life, cooperative cancel declared per job, nothing
alive across a restart, dismiss only what is over, the page's upload tasks, and the routes."""
from __future__ import annotations

import json
from types import SimpleNamespace

from helix.services import jobs as J
from helix.services.jobs import Jobs


def _reg(tmp_path, pushes=None, clock=None):
    return Jobs(tmp_path / "jobs.json", push=(pushes.append if pushes is not None else None), clock=clock)


def test_a_job_lives_start_progress_lines_finish_and_pushes_each_step(tmp_path):
    pushes = []
    t = [0.0]
    r = _reg(tmp_path, pushes, clock=lambda: t[0])
    job = r.start("upload", "Upload song.mp3", progress=0.0, cancel=lambda: None, cancel_note="Stops sending.")
    assert job.state == "running" and job.can_cancel and pushes[-1]["t"] == "job"
    r.line(job, "hello ✓")                       # ASCII-cleaned (rule 7)
    assert pushes[-1] == {"t": "job_line", "id": job.id, "line": "hello ?"}
    r.progress(job, 0.4, "Sending")
    t[0] = 1.0
    r.progress(job, 0.5)
    assert job.progress == 0.5 and job.note == "Sending"
    r.finish(job, True, note="In the catalog")
    assert job.state == "done" and job.progress == 1.0 and job.finished and not job.can_cancel
    assert r.list()[0]["title"] == "Upload song.mp3" and r.log(job.id)["lines"] == ["hello ?"]


def test_progress_pushes_are_throttled_but_state_changes_are_not(tmp_path):
    pushes = []
    t = [0.0]
    r = _reg(tmp_path, pushes, clock=lambda: t[0])
    job = r.start("read", "Reading the fleet")
    n = len(pushes)
    for i in range(10):
        r.progress(job, i / 10)        # same instant: swallowed
    assert len(pushes) == n
    t[0] = 5.0
    r.progress(job, 0.9)
    assert len(pushes) == n + 1
    r.finish(job, False)
    assert pushes[-1]["job"]["state"] == "failed"


def test_cancel_is_declared_per_job_and_a_cancelled_job_is_never_done(tmp_path):
    r = _reg(tmp_path)
    stopped = []
    can = r.start("deploy", "Ship ECHO dev", cancel=lambda: stopped.append(1), cancel_note="Stops the script.")
    cannot = r.start("rollback", "Roll back ECHO dev")
    assert r.cancel(cannot.id) == J.CANNOT_CANCEL and not stopped
    assert r.cancel("nope") == J.NO_SUCH_JOB
    assert r.cancel(can.id) is None and stopped == [1] and can.cancel_asked and not can.can_cancel
    assert r.cancel(can.id) is None and stopped == [1]         # asked twice: the hook runs once
    assert r.cancelled(can)
    r.finish(can, False, rc=1)
    assert can.state == "cancelled"
    r.finish(cannot, True)
    assert r.cancel(cannot.id) == J.ALREADY_OVER


def test_a_failing_hook_never_raises_into_the_caller(tmp_path):
    r = _reg(tmp_path)

    def boom():
        raise RuntimeError("no pid")

    job = r.start("deploy", "Ship", cancel=boom, cancel_note="Stops.")
    assert r.cancel(job.id) is None
    assert any("stop hook failed" in ln for ln in job.lines)


def test_nothing_is_alive_across_a_restart_and_finished_work_is_kept(tmp_path):
    r = _reg(tmp_path)
    running = r.start("upload", "Upload big.mp4", progress=0.3)
    done = r.start("scan", "Scan MES")
    r.finish(done, True)
    again = _reg(tmp_path)
    rows = {j["id"]: j for j in again.list()}
    assert rows[done.id]["state"] == "done"
    assert rows[running.id]["state"] == "failed" and rows[running.id]["note"] == J.LOST
    assert J.LOST in again.log(running.id)["lines"][-1]
    assert not rows[running.id]["can_cancel"]


def test_dismiss_only_what_is_over_and_clear_finished(tmp_path):
    pushes = []
    r = _reg(tmp_path, pushes)
    a = r.start("read", "Reading")
    b = r.start("scan", "Scan")
    c = r.start("merge", "Check")
    assert r.dismiss(a.id) == J.STILL_RUNNING
    r.finish(b, True)
    r.finish(c, False)
    assert r.dismiss(b.id) is None and pushes[-1] == {"t": "job_gone", "id": b.id}
    assert r.dismiss(b.id) == J.NO_SUCH_JOB
    assert r.clear_finished() == 1                  # c goes, a stays
    assert [j["id"] for j in r.list()] == [a.id]
    assert json.loads((tmp_path / "jobs.json").read_text())[0]["id"] == a.id


def test_run_wraps_a_thread_and_an_exception_is_a_failed_task_with_its_words(tmp_path):
    r = _reg(tmp_path)

    def bad(job):
        raise ValueError("bucket said no")

    job = r.start("cache", "Caching the station")
    r.run(job, bad).join(5)
    assert job.state == "failed" and "ValueError: bucket said no" in job.lines[-1]
    ok = r.start("cache", "Caching again")
    r.run(ok, lambda j: True).join(5)
    assert ok.state == "done"


def test_the_register_keeps_the_last_two_hundred(tmp_path):
    r = _reg(tmp_path)
    for i in range(210):
        j = r.start("scan", f"Scan {i}")
        r.finish(j, True)
    assert len(r.list()) == 200 and r.list()[0]["title"] == "Scan 209"


# ------------------------------------------------------------------------------ the routes

def test_routes_list_log_cancel_dismiss_clear_and_the_page_upload(tmp_path):
    from fastapi import FastAPI
    from helix.api import jobs_routes
    from tests.test_fleet_routes import _call
    r = _reg(tmp_path)
    app = FastAPI()
    jobs_routes.mount_jobs(app, SimpleNamespace(jobs=r))
    st, doc = _call(app, "POST", "/api/jobs", {"kind": "upload", "title": "Upload x.mp3"})
    assert st == 200 and doc["state"] == "running" and doc["can_cancel"]
    jid = doc["id"]
    st, doc = _call(app, "PUT", f"/api/jobs/{jid}/progress", {"progress": 0.25, "note": "Sending"})
    assert st == 200 and doc["cancel_asked"] is False
    st, doc = _call(app, "GET", "/api/jobs")
    assert st == 200 and doc["jobs"][0]["progress"] == 0.25 and doc["jobs"][0]["note"] == "Sending"
    assert _call(app, "DELETE", f"/api/jobs/{jid}")[0] == 400            # still running
    st, doc = _call(app, "POST", f"/api/jobs/{jid}/cancel")
    assert st == 200
    st, doc = _call(app, "GET", f"/api/jobs/{jid}")
    assert doc["cancel_asked"] is True and "cancel asked" in doc["lines"][0]
    st, _ = _call(app, "POST", f"/api/jobs/{jid}/finish", {"ok": False, "note": "aborted by the page"})
    assert st == 200 and r.get(jid).state == "cancelled"
    assert _call(app, "GET", "/api/jobs/nope")[0] == 404
    assert _call(app, "POST", "/api/jobs", {"kind": "deploy", "title": "Ship prod"})[0] == 400   # the page opens uploads only
    assert _call(app, "POST", "/api/jobs", {"kind": "upload"})[0] == 400
    st, doc = _call(app, "POST", "/api/jobs/clear")
    assert st == 200 and doc["cleared"] == 1 and r.list() == []
    bare = FastAPI()
    jobs_routes.mount_jobs(bare, SimpleNamespace())
    assert _call(bare, "GET", "/api/jobs")[0] == 503
