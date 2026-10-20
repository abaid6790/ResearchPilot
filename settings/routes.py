"""Account settings: profile, password, preferences, report defaults, privacy and data.

The forms on a settings page post to different URLs, so they need no field prefixes (a prefix
would also rename the CSRF field and break global CSRF checks).
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, flash, redirect, render_template, session, url_for
from flask_login import current_user, login_required, login_user
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, ValidationError

from auth import services
from auth.decorators import verified_required
from auth.forms import ChangePasswordForm, ProfileForm
from core.audit import record_event
from core.db_utils import utcnow
from extensions import db, limiter
from research.forms import SettingsFields
from settings import services as account
from settings.services import DELETE_PHRASE

bp = Blueprint("settings", __name__, url_prefix="/settings")

THEME_CHOICES = [("system", "Match my device"), ("light", "Light"), ("dark", "Dark")]


class PreferencesForm(FlaskForm):
    theme_preference = SelectField("Theme", choices=THEME_CHOICES)
    notify_on_finish = BooleanField("Email me when a research run finishes")
    submit = SubmitField("Save preferences")


class DefaultsForm(SettingsFields):
    submit = SubmitField("Save defaults")


class DeleteAccountForm(FlaskForm):
    password = PasswordField("Your password", validators=[DataRequired()])
    confirm = StringField("Type the phrase to confirm", validators=[DataRequired()])
    submit = SubmitField("Delete my account")

    def validate_confirm(self, field) -> None:
        if (field.data or "").strip() != DELETE_PHRASE:
            raise ValidationError(f"Type exactly: {DELETE_PHRASE}")


# ------------------------------------------------------------------------------ profile
def _render(profile_form: ProfileForm, password_form: ChangePasswordForm, status: int = 200):
    return render_template("settings/profile.html", profile_form=profile_form, password_form=password_form,
                           tab="profile"), status  # fmt: skip


@bp.route("/profile", methods=["GET", "POST"])
@verified_required
def profile():
    profile_form = ProfileForm(data={"display_name": current_user.display_name})
    if profile_form.validate_on_submit():
        current_user.display_name = profile_form.display_name.data
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("settings.profile"))
    status = 400 if profile_form.errors else 200
    return _render(profile_form, ChangePasswordForm(formdata=None), status)


@bp.post("/password")
@login_required
@limiter.limit("10 per hour")
def change_password():
    password_form = ChangePasswordForm()
    if password_form.validate_on_submit():
        if services.change_password(
            current_user, password_form.current_password.data, password_form.password.data
        ):
            # The old session is now invalid (auth_version bumped); keep this one signed in.
            session.clear()
            session.permanent = True
            login_user(current_user, remember=False)
            flash("Password changed. Other devices have been signed out.", "success")
            return redirect(url_for("settings.profile"))
        password_form.current_password.errors.append("That is not your current password.")
    profile_form = ProfileForm(formdata=None, data={"display_name": current_user.display_name})
    return _render(profile_form, password_form, 400)


# ------------------------------------------------------------------------------ preferences
@bp.route("/preferences", methods=["GET", "POST"])
@verified_required
def preferences():
    form = PreferencesForm(data={"theme_preference": current_user.theme_preference,
                                 "notify_on_finish": current_user.notify_on_finish})  # fmt: skip
    if form.validate_on_submit():
        current_user.theme_preference = form.theme_preference.data
        current_user.notify_on_finish = bool(form.notify_on_finish.data)
        record_event(
            "preferences_changed",
            actor_user_id=current_user.id,
            target_type="user",
            target_id=current_user.id,
        )
        db.session.commit()
        flash("Preferences saved.", "success")
        return redirect(url_for("settings.preferences"))
    return render_template("settings/preferences.html", form=form, tab="preferences"), (
        400 if form.errors else 200
    )


# ------------------------------------------------------------------------------ report defaults
def defaults_to_form_data(defaults: dict | None) -> dict:
    data = dict(defaults or {})
    for key in ("prioritize_domains", "exclude_domains"):
        data[key] = "\n".join(data.get(key) or [])
    return data


@bp.route("/defaults", methods=["GET", "POST"])
@verified_required
def defaults():
    form = DefaultsForm(data=defaults_to_form_data(current_user.default_settings))
    if form.validate_on_submit():
        settings = form.build_settings()
        if settings is not None and not form.errors and not form.form_errors:
            current_user.default_settings = settings.model_dump(mode="json")
            record_event("preferences_changed", actor_user_id=current_user.id, target_type="user", target_id=current_user.id,
                         what="defaults")  # fmt: skip
            db.session.commit()
            flash("Default report settings saved. New projects start with them.", "success")
            return redirect(url_for("settings.defaults"))
    from research.forms import CATEGORY_LABELS

    status = 400 if form.is_submitted() else 200
    return render_template("settings/defaults.html", form=form, tab="defaults", has_defaults=bool(current_user.default_settings),
                           category_labels=CATEGORY_LABELS), status  # fmt: skip


@bp.post("/defaults/reset")
@verified_required
def reset_defaults():
    form = FlaskForm()
    if form.validate_on_submit():
        current_user.default_settings = None
        db.session.commit()
        flash("Default report settings cleared.", "success")
    return redirect(url_for("settings.defaults"))


# ------------------------------------------------------------------------------ privacy and data
@bp.get("/privacy")
@verified_required
def privacy():
    return render_template(
        "settings/privacy.html", tab="privacy", delete_form=DeleteAccountForm(formdata=None), phrase=DELETE_PHRASE,
        delete_on=account.deletion_date(current_user), grace_days=current_app.config["ACCOUNT_DELETION_GRACE_DAYS"],
    )  # fmt: skip


@bp.post("/privacy/export")
@verified_required
@limiter.limit("5 per day")
def export_data():
    form = FlaskForm()
    if not form.validate_on_submit():
        return redirect(url_for("settings.privacy"))
    payload = account.build_export(current_user)
    record_event(
        "data_export_requested", actor_user_id=current_user.id, target_type="user", target_id=current_user.id
    )
    db.session.commit()
    name = f"researchpilot-export-{utcnow().strftime('%Y%m%d')}.zip"
    return Response(payload, mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"})  # fmt: skip


@bp.post("/privacy/delete")
@verified_required
@limiter.limit("5 per hour")
def delete_account():
    form = DeleteAccountForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.password.data):
            form.password.errors.append("That is not your current password.")
        else:
            account.request_deletion(current_user)
            flash(
                "Your account is scheduled for deletion. You can cancel until the date shown below.",
                "warning",
            )
            return redirect(url_for("settings.privacy"))
    return render_template("settings/privacy.html", tab="privacy", delete_form=form, phrase=DELETE_PHRASE,
                           delete_on=account.deletion_date(current_user),
                           grace_days=current_app.config["ACCOUNT_DELETION_GRACE_DAYS"]), 400  # fmt: skip


@bp.post("/privacy/cancel-deletion")
@verified_required
def cancel_deletion():
    form = FlaskForm()
    if form.validate_on_submit() and account.cancel_deletion(current_user):
        flash("Account deletion cancelled.", "success")
    return redirect(url_for("settings.privacy"))
