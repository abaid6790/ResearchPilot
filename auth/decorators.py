"""Access-control decorators."""

from __future__ import annotations

from functools import wraps

from flask import current_app, flash, redirect, url_for
from flask_login import current_user


def verified_required(view):
    """Require a signed-in user whose email address has been verified."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return current_app.login_manager.unauthorized()
        if not current_user.is_verified:
            flash("Please confirm your email address to unlock your workspace.", "warning")
            return redirect(url_for("auth.verify_pending"))
        return view(*args, **kwargs)

    return wrapped
