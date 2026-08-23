import os
import secrets
from datetime import date, timedelta

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from garminworkouts.app import (
    HEART_RATE_PHASES,
    WEEKDAYS,
    _clock,
    _format_heart_rate_targets,
    _pace,
    _parse_clock,
    _parse_heart_rate_target,
    _parse_pace,
)
from garminworkouts.garmin.ratelimit import GarminRateLimitError, has_reusable_tokens
from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.state import AppState, Goal
from garminworkouts.workflows import GarminAuthenticationRequiredError, PlannerWorkflow

GOAL_TYPES = (
    ("complete_distance", "Complete a distance continuously"),
    ("target_time", "Run a distance within a target time"),
    ("sustain_pace", "Sustain a target pace"),
    ("endurance", "Improve general endurance"),
    ("speed", "Improve speed"),
    ("consistency", "Build running consistency"),
)


def create_app(config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SECRET_KEY=os.getenv("GARMIN_WEB_SECRET_KEY") or secrets.token_hex(32),
        DATA_DIR=os.getenv("GARMIN_WORKOUTS_HOME", "~/.garmin-running-workouts"),
        MAX_CONTENT_LENGTH=1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=_environment_bool("GARMIN_WEB_COOKIE_SECURE", True),
    )
    if config:
        app.config.update(config)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.before_request
    def verify_csrf():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected, supplied):
            abort(400, "The form expired or its security token was invalid. Reload the page and try again.")
        return None

    @app.after_request
    def security_headers(response):
        # The versioned Buy Me a Coffee footer widget injects one known style block
        # and loads Bree Serif. Pin that block by hash instead of allowing all inline CSS.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'sha256-Ql5/wMk95FxT6SRSAAPkbhjdpkaD03IFdbKdmFYoeuc=' "
            "https://fonts.googleapis.com; "
            "script-src 'self' https://cdnjs.buymeacoffee.com; "
            "font-src https://fonts.gstatic.com; img-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def shared_template_values():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {
            "csrf_token": session["csrf_token"],
            "weekday_names": WEEKDAYS,
        }

    @app.errorhandler(GarminAuthenticationRequiredError)
    def authentication_error(error):
        return render_template("error.html", title="Garmin connection required", message=str(error)), 401

    @app.errorhandler(GarminRateLimitError)
    def rate_limit_error(error):
        return render_template("error.html", title="Garmin cooldown active", message=str(error)), 429

    @app.errorhandler(ValueError)
    def validation_error(error):
        return render_template("error.html", title="Check the submitted values", message=str(error)), 400

    @app.errorhandler(RuntimeError)
    def workflow_error(error):
        return render_template("error.html", title="The operation could not be completed", message=str(error)), 409

    @app.get("/")
    def dashboard():
        with _state(app) as state:
            goal = state.active_goal()
            plan = state.active_plan()
            summary = state.block_progress_summary(plan.id) if plan else None
            progress = state.progress(plan.id) if plan else []
            next_workout = next(
                (
                    item
                    for item in progress
                    if item["status"] == "scheduled" and date.fromisoformat(item["workout_date"]) >= date.today()
                ),
                None,
            )
            connected = has_reusable_tokens(state.tokens_dir)
            username = state.get_setting("garmin_username", "")
        return render_template(
            "dashboard.html",
            goal=goal,
            goal_summary=_goal_summary(goal),
            heart_rate_summary=_format_heart_rate_targets(goal.heart_rate_targets) if goal else "",
            plan=plan,
            summary=summary,
            next_workout=next_workout,
            connected=connected,
            username=username,
        )

    @app.route("/goal", methods=["GET", "POST"])
    def goal_form():
        with _state(app) as state:
            current = state.active_goal()
            if request.method == "POST":
                goal = _goal_from_form(request.form)
                state.save_goal(goal)
                flash("The goal and training preferences were saved.", "success")
                return redirect(url_for("dashboard"))
            values = _goal_form_values(current)
        return render_template(
            "goal.html",
            goal_types=GOAL_TYPES,
            heart_rate_phases=HEART_RATE_PHASES,
            values=values,
        )

    @app.post("/progress/refresh")
    def refresh_progress():
        with _state(app) as state:
            workflow = PlannerWorkflow(state)
            workflow.refresh()
            summary = state.block_progress_summary(state.active_plan().id)
        flash(
            f"Progress refreshed: {summary['completed']} completed, {summary['missed']} missed, "
            f"{summary['remaining']} remaining.",
            "success",
        )
        return redirect(url_for("dashboard"))

    @app.post("/plans/generate")
    def generate_plan():
        with _state(app) as state:
            record = PlannerWorkflow(state).generate_plan()
        flash("A proposal was generated. Nothing has been changed in Garmin yet.", "success")
        return redirect(url_for("review_plan", plan_id=record.id))

    @app.post("/plans/adapt")
    def adapt_plan():
        with _state(app) as state:
            record, changes = PlannerWorkflow(state).adapt_plan()
        if record is None:
            flash("The new evidence does not justify changing the remaining schedule.", "success")
            return redirect(url_for("dashboard"))
        flash(f"An adaptation proposal with {len(changes)} calendar change(s) is ready for review.", "success")
        return redirect(url_for("review_plan", plan_id=record.id))

    @app.get("/plans/<int:plan_id>")
    def review_plan(plan_id):
        with _state(app) as state:
            record = state.plan(plan_id)
            if not record:
                abort(404)
            preview = None
            if request.args.get("inspected") == "1":
                preview = PlannerWorkflow(state).load_plan_conflicts(plan_id)
            explanation = None
            if request.args.get("explained") == "1":
                explanation = PlannerWorkflow(state).load_plan_explanation(plan_id)
            llm_enabled = state.get_setting("llm_provider", "none") != "none"
            old_record = state.plan(record.supersedes_plan_id) if record.supersedes_plan_id else None
            changes = (
                PlannerWorkflow.calendar_changes(
                    old_record.config,
                    record.config,
                    progress=state.progress(old_record.id),
                )
                if old_record
                else [f"{item['date']}: add {item['name']}" for item in record.config.get("workouts", [])]
            )
        return render_template(
            "plan.html",
            plan=record,
            workouts=record.config.get("workouts", []),
            preview=preview,
            changes=changes,
            explanation=explanation,
            llm_enabled=llm_enabled,
        )

    @app.post("/plans/<int:plan_id>/explain")
    def explain_plan(plan_id):
        with _state(app) as state:
            record = state.plan(plan_id)
            if not record:
                abort(404)
            PlannerWorkflow(state).explain_plan(record)
        return redirect(
            url_for(
                "review_plan",
                plan_id=plan_id,
                inspected=request.form.get("inspected", "0"),
                explained=1,
            )
        )

    @app.post("/plans/<int:plan_id>/inspect")
    def inspect_plan(plan_id):
        with _state(app) as state:
            record = state.plan(plan_id)
            if not record:
                abort(404)
            PlannerWorkflow(state).inspect_plan_conflicts(record)
        return redirect(url_for("review_plan", plan_id=plan_id, inspected=1))

    @app.post("/plans/<int:plan_id>/apply")
    def apply_plan(plan_id):
        with _state(app) as state:
            record = state.plan(plan_id)
            if not record:
                abort(404)
            if record.status not in {"proposed", "apply-failed"}:
                raise ValueError("Only a proposal can be applied")
            workflow = PlannerWorkflow(state)
            preview = workflow.load_plan_conflicts(plan_id)
            workflow.apply_plan(
                record,
                preview,
                remove_conflicts=_checked("remove_conflicts"),
                delete_templates=_checked("delete_templates"),
                allow_duplicates=_checked("allow_duplicates"),
            )
        flash("The plan was scheduled successfully in Garmin Connect.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/calendar")
    def calendar():
        today_value = date.today()
        today = today_value.isoformat()
        with _state(app) as state:
            plan = state.active_plan()
            if not plan:
                return render_template("calendar.html", plan=None, rows=[], today=today)
            progress = {(row["workout_date"], row["workout_name"]): row for row in state.progress(plan.id)}
            rows = []
            for workout in plan.config.get("workouts", []):
                row = progress.get((str(workout["date"]), workout["name"]), {})
                workout_date = date.fromisoformat(str(workout["date"]))
                status = row.get("status", "scheduled")
                inferred_missed = status == "scheduled" and workout_date < today_value
                if inferred_missed:
                    status = "missed"
                execution_score = row.get("execution_score")
                if execution_score is None:
                    execution_score_band = "unavailable"
                elif execution_score >= 67:
                    execution_score_band = "good"
                elif execution_score >= 34:
                    execution_score_band = "average"
                else:
                    execution_score_band = "low"
                rows.append(
                    {
                        **workout,
                        "date": workout_date.isoformat(),
                        "status": status,
                        "status_label": "Missed · needs refresh" if inferred_missed else status,
                        "inferred_missed": inferred_missed,
                        "execution_score": execution_score,
                        "execution_score_checked": bool(row.get("execution_score_checked_at")),
                        "execution_score_band": execution_score_band,
                    }
                )
        return render_template("calendar.html", plan=plan, rows=rows, today=today)

    @app.get("/cleanup")
    def cleanup():
        with _state(app) as state:
            record = state.active_plan()
            preview = None
            if record and request.args.get("inspected") == "1":
                preview = PlannerWorkflow(state).load_active_conflicts(record.id)
        return render_template("cleanup.html", plan=record, preview=preview)

    @app.post("/cleanup/inspect")
    def inspect_cleanup():
        with _state(app) as state:
            PlannerWorkflow(state).inspect_active_conflicts()
        return redirect(url_for("cleanup", inspected=1))

    @app.post("/cleanup/apply")
    def apply_cleanup():
        with _state(app) as state:
            record = state.active_plan()
            if not record:
                raise ValueError("There is no active plan")
            workflow = PlannerWorkflow(state)
            preview = workflow.load_active_conflicts(record.id)
            actions = workflow.clean_active_conflicts(
                record,
                preview,
                delete_templates=_checked("delete_templates"),
            )
        flash(f"Removed {len(actions)} conflicting Garmin item(s).", "success")
        return redirect(url_for("dashboard"))

    @app.route("/workouts/new", methods=["GET", "POST"])
    def one_off_workout():
        if request.method == "POST":
            config = _one_off_from_form(request.form)
            with _state(app) as state:
                draft_id = PlannerWorkflow(state).save_one_off_draft(config)
            flash("The one-off workout is a local draft; Garmin has not been changed.", "success")
            return redirect(url_for("review_one_off", draft_id=draft_id))
        return render_template("one_off.html", default_date=(date.today() + timedelta(days=1)).isoformat())

    @app.get("/workouts/drafts/<draft_id>")
    def review_one_off(draft_id):
        with _state(app) as state:
            workflow = PlannerWorkflow(state)
            config = workflow.load_one_off_draft(draft_id)
            preview = workflow.load_one_off_conflicts(draft_id) if request.args.get("inspected") == "1" else None
        return render_template(
            "one_off_review.html",
            draft_id=draft_id,
            workout=config["workouts"][0],
            preview=preview,
        )

    @app.post("/workouts/drafts/<draft_id>/inspect")
    def inspect_one_off(draft_id):
        with _state(app) as state:
            PlannerWorkflow(state).inspect_one_off_conflicts(draft_id)
        return redirect(url_for("review_one_off", draft_id=draft_id, inspected=1))

    @app.post("/workouts/drafts/<draft_id>/apply")
    def apply_one_off(draft_id):
        with _state(app) as state:
            workflow = PlannerWorkflow(state)
            preview = workflow.load_one_off_conflicts(draft_id)
            workflow.apply_one_off(
                draft_id,
                preview,
                remove_conflicts=_checked("remove_conflicts"),
                delete_templates=_checked("delete_templates"),
                allow_duplicates=_checked("allow_duplicates"),
            )
        flash("The one-off workout was scheduled successfully in Garmin Connect.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/settings")
    def settings():
        with _state(app) as state:
            values = {
                "garmin_username": state.get_setting("garmin_username", ""),
                "connected": has_reusable_tokens(state.tokens_dir),
                "llm_provider": state.get_setting("llm_provider", "none"),
                "llm_base_url": state.get_setting("llm_base_url", "https://api.openai.com/v1"),
                "llm_model": state.get_setting("llm_model", ""),
                "llm_api_key_env": state.get_setting("llm_api_key_env", "RUNNING_PLANNER_LLM_API_KEY"),
            }
        return render_template("settings.html", values=values)

    @app.post("/settings/garmin")
    def connect_garmin():
        with _state(app) as state:
            PlannerWorkflow(state).connect_garmin(
                request.form.get("garmin_username"),
                request.form.get("garmin_password"),
            )
        flash("Garmin Connect authentication succeeded; reusable tokens were stored privately.", "success")
        return redirect(url_for("settings"))

    @app.post("/settings/llm")
    def save_llm_settings():
        provider = request.form.get("llm_provider", "none")
        if provider not in {"none", "openai-compatible"}:
            raise ValueError("Unsupported LLM provider")
        model = request.form.get("llm_model", "").strip()
        if provider != "none" and not model:
            raise ValueError("A model name is required when LLM explanations are enabled")
        with _state(app) as state:
            state.set_setting("llm_provider", provider)
            state.set_setting("llm_base_url", request.form.get("llm_base_url", "").strip())
            state.set_setting("llm_model", model)
            state.set_setting("llm_api_key_env", request.form.get("llm_api_key_env", "").strip())
        flash("Optional LLM settings were saved; no API key was stored.", "success")
        return redirect(url_for("settings"))

    return app


def _state(app):
    return AppState(app.config["DATA_DIR"])


def _checked(name):
    return request.form.get(name) in {"1", "true", "yes", "on"}


def _goal_from_form(form):
    goal_type = form.get("goal_type", "")
    available_days = tuple(int(item) for item in form.getlist("available_days"))
    target_distance = _optional_float(form.get("target_distance_km"))
    target_time = _parse_clock(form["target_time"]) if goal_type == "target_time" else None
    target_pace = (
        _parse_pace(form["target_pace"])
        if goal_type in {"sustain_pace", "speed"} and form.get("target_pace", "").strip()
        else None
    )
    target_duration = _optional_int(form.get("target_duration_minutes")) if goal_type == "sustain_pace" else None
    heart_rate_targets = {}
    for phase, _label in HEART_RATE_PHASES:
        target_type = form.get(f"hr_{phase}_type", "none")
        value = form.get(f"hr_{phase}_value", "").strip()
        if target_type == "none":
            continue
        if not value:
            raise ValueError(f"Enter a heart-rate value for {phase}")
        key = {
            "max": "heart_rate_max",
            "range": "heart_rate",
            "zone": "heart_rate_zone",
        }.get(target_type)
        if not key:
            raise ValueError(f"Unsupported heart-rate target type for {phase}")
        heart_rate_targets[phase] = _parse_heart_rate_target(key, value)

    return Goal(
        goal_type=goal_type,
        description=form.get("description", "").strip(),
        start_date=date.fromisoformat(form.get("start_date", "")),
        plan_weeks=int(form.get("plan_weeks", 4)),
        available_days=available_days,
        long_run_day=int(form.get("long_run_day", 6)),
        target_date=date.fromisoformat(form["target_date"]) if form.get("target_date") else None,
        target_distance_km=target_distance if goal_type in {"complete_distance", "target_time"} else None,
        target_time_seconds=target_time,
        target_pace_seconds_per_km=target_pace,
        target_duration_minutes=target_duration,
        runs_per_week=int(form.get("runs_per_week", 3)),
        max_session_minutes=int(form.get("max_session_minutes", 90)),
        baseline_long_run_km=_optional_float(form.get("baseline_long_run_km")),
        heart_rate_targets=heart_rate_targets,
        quality_target_preference=form.get("quality_target_preference", "pace"),
        constraints=form.get("constraints", "").strip(),
    )


def _goal_form_values(goal):
    if not goal:
        values = {
            "goal_type": "complete_distance",
            "description": "Complete a distance continuously",
            "start_date": (date.today() + timedelta(days=1)).isoformat(),
            "plan_weeks": 4,
            "available_days": (1, 3, 6),
            "long_run_day": 6,
            "runs_per_week": 3,
            "max_session_minutes": 90,
            "target_distance_km": 10,
            "quality_target_preference": "pace",
            "heart_rate": {},
        }
        return values
    values = goal.to_dict()
    values.update(
        {
            "target_time": _clock(goal.target_time_seconds) if goal.target_time_seconds else "",
            "target_pace": _pace(goal.target_pace_seconds_per_km) if goal.target_pace_seconds_per_km else "",
            "heart_rate": {phase: _heart_rate_form_value(target) for phase, target in goal.heart_rate_targets.items()},
        }
    )
    return values


def _heart_rate_form_value(target):
    if "heart_rate_max" in target:
        return {"type": "max", "value": str(target["heart_rate_max"])}
    if "heart_rate" in target:
        return {"type": "range", "value": "-".join(str(item) for item in target["heart_rate"])}
    if "heart_rate_zone" in target:
        return {"type": "zone", "value": str(target["heart_rate_zone"])}
    return {"type": "none", "value": ""}


def _one_off_from_form(form):
    workout_date = date.fromisoformat(form.get("workout_date", ""))
    if workout_date < date.today():
        raise ValueError("A new one-off workout cannot be scheduled in the past")
    step_types = form.getlist("step_type")
    durations = form.getlist("duration")
    target_types = form.getlist("target_type")
    target_values = form.getlist("target_value")
    lengths = {len(step_types), len(durations), len(target_types), len(target_values)}
    if len(lengths) != 1 or not 1 <= len(step_types) <= 20:
        raise ValueError("Provide between 1 and 20 complete workout steps")
    steps = []
    targeted = 0
    for index, step_type in enumerate(step_types):
        if step_type not in {"warmup", "interval", "recovery", "cooldown"}:
            raise ValueError("Unsupported workout step type")
        step = {"type": step_type, "duration": _clock(_parse_clock(durations[index]))}
        target_type = target_types[index]
        target_value = target_values[index].strip()
        if target_type != "none":
            key = {
                "max": "heart_rate_max",
                "range": "heart_rate",
                "zone": "heart_rate_zone",
            }.get(target_type)
            if not key or not target_value:
                raise ValueError(f"Step {index + 1} needs a valid heart-rate target")
            step.update(_parse_heart_rate_target(key, target_value))
            targeted += 1
        steps.append(step)
    if not targeted:
        raise ValueError("A heart-rate workout must contain at least one HR-targeted step")
    config = {
        "name": f"One-off HR workout {workout_date.isoformat()}",
        "workouts": [
            {
                "date": workout_date.isoformat(),
                "sport": "running",
                "name": form.get("name", "").strip(),
                "description": form.get("description", "").strip(),
                "steps": steps,
            }
        ],
    }
    TrainingPlan(config)
    return config


def _goal_summary(goal):
    if not goal:
        return "No active goal"
    details = []
    if goal.goal_type == "complete_distance" and goal.target_distance_km:
        details.append(f"{goal.target_distance_km:g} km")
    elif goal.goal_type == "target_time" and goal.target_distance_km and goal.target_time_seconds:
        details.append(f"{goal.target_distance_km:g} km in {_clock(goal.target_time_seconds)}")
    elif goal.goal_type == "sustain_pace":
        details.append(f"{goal.target_duration_minutes} min at {_pace(goal.target_pace_seconds_per_km)}/km")
    elif goal.goal_type == "speed" and goal.target_pace_seconds_per_km:
        details.append(f"target pace {_pace(goal.target_pace_seconds_per_km)}/km")
    return " · ".join([goal.description, *details])


def _optional_float(value):
    return float(value) if str(value or "").strip() else None


def _optional_int(value):
    return int(value) if str(value or "").strip() else None


def _environment_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}
