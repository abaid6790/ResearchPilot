"""Account settings: profile and password change.

The two forms on the settings page post to different URLs, so they need no field
prefixes (a prefix would also rename the CSRF field and break global CSRF checks).
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, session, url_for
from flask_login import current_user, login_required, login_user

from auth import services
from auth.decorators import verified_required
from auth.forms import ChangePasswordForm, ProfileForm
from extensions import db, limiter

bp = Blueprint("settings", __name__, url_prefix="/settings")


def _render(profile_form: ProfileForm, password_form: ChangePasswordForm, status: int = 200):
    return render_template(
        "settings/profile.html", profile_form=profile_form, password_form=password_form
    ), status


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
