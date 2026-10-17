"""Authentication routes."""

from __future__ import annotations

import logging

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from auth import services
from auth.forms import (
    ActionForm,
    ForgotPasswordForm,
    LoginForm,
    RegisterForm,
    ResetPasswordForm,
)
from auth.services import TokenStatus
from core.security import is_safe_redirect_target
from extensions import limiter

logger = logging.getLogger(__name__)
bp = Blueprint("auth", __name__, url_prefix="/auth")

GENERIC_REGISTER_MESSAGE = (
    "If that email address can be registered, we have sent a confirmation link to it. "
    "If you already have an account, sign in or reset your password instead."
)


def _after_login_redirect():
    target = request.args.get("next")
    if is_safe_redirect_target(target):
        return redirect(target)
    return redirect(url_for("dashboard.index"))


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    form = RegisterForm()
    if form.validate_on_submit():
        user = services.register_user(form.email.data, form.password.data, form.display_name.data)
        if user is not None:
            services.send_verification_email(user, background=True)
            logger.info("New account registered (user id %s)", user.id)
        else:
            logger.info("Registration attempted for an already-registered address")
        # Identical response either way, so the form cannot be used to probe for accounts.
        flash(GENERIC_REGISTER_MESSAGE, "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute;60 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    form = LoginForm()
    if form.validate_on_submit():
        user = services.authenticate(form.email.data, form.password.data)
        if user is None:
            flash("Incorrect email address or password.", "error")
            return render_template("auth/login.html", form=form), 401
        session.clear()
        session.permanent = True
        login_user(user, remember=False)
        if not user.is_verified:
            return redirect(url_for("auth.verify_pending"))
        return _after_login_redirect()
    return render_template("auth/login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    form = ActionForm()
    if form.validate_on_submit():
        logout_user()
        session.clear()
        flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.get("/verify-pending")
@login_required
def verify_pending():
    if current_user.is_verified:
        return redirect(url_for("dashboard.index"))
    return render_template("auth/verify_pending.html", form=ActionForm())


@bp.post("/resend-verification")
@login_required
@limiter.limit("5 per hour")
def resend_verification():
    form = ActionForm()
    if not form.validate_on_submit():
        return redirect(url_for("auth.verify_pending"))
    outcome = services.resend_verification_email(current_user)
    if outcome == "already_verified":
        flash("Your email address is already confirmed.", "info")
        return redirect(url_for("dashboard.index"))
    messages = {
        "sent": ("We sent a new confirmation link. Check your inbox and spam folder.", "success"),
        "too_soon": (
            "A link was sent a moment ago. Please wait a minute before asking for another.",
            "warning",
        ),
        "failed": (
            "We could not send the email right now. Please try again shortly; "
            "if this keeps happening, contact the site operator.",
            "error",
        ),
    }
    text, category = messages[outcome]
    flash(text, category)
    return redirect(url_for("auth.verify_pending"))


@bp.route("/verify/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def verify_email(token: str):
    """Show a confirmation page; the token is only consumed on POST.

    Requiring a POST stops mail scanners and link previewers (which issue GET
    requests) from silently using up the one-time link.
    """
    form = ActionForm()
    if request.method == "POST" and form.validate_on_submit():
        status, user = services.verify_email(token)
        if status is TokenStatus.VALID:
            flash(
                "Email confirmed. You can now sign in."
                if not current_user.is_authenticated
                else "Email confirmed. Welcome aboard.",
                "success",
            )
            if current_user.is_authenticated:
                return redirect(url_for("dashboard.index"))
            return redirect(url_for("auth.login"))
    else:
        status, _ = services.lookup_token(token, "verify_email")
    return render_template("auth/verify_email.html", status=status, form=form), (
        200 if status is TokenStatus.VALID else 400
    )


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("settings.profile"))
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        services.request_password_reset(form.email.data)
        flash(
            "If an account exists for that address, we have sent a password reset link. "
            "It expires soon and can only be used once.",
            "info",
        )
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html", form=form)


@bp.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def reset_password(token: str):
    status, _ = services.lookup_token(token, "reset_password")
    form = ResetPasswordForm()
    if status is TokenStatus.VALID and form.validate_on_submit():
        result = services.reset_password(token, form.password.data)
        if result is TokenStatus.VALID:
            logout_user()
            flash("Your password has been updated. Sign in with your new password.", "success")
            return redirect(url_for("auth.login"))
        status = result
    code = 200 if status is TokenStatus.VALID else 400
    return render_template("auth/reset_password.html", form=form, status=status), code
