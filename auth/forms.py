"""WTForms definitions with server-side validation (CSRF is automatic)."""

from __future__ import annotations

from flask import current_app
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, ValidationError

# A deliberately small list; length is the main control (NIST SP 800-63B style).
COMMON_PASSWORDS = {
    "password",
    "password1",
    "password12",
    "password123",
    "passw0rd123",
    "1234567890",
    "12345678910",
    "0123456789",
    "qwertyuiop",
    "qwerty12345",
    "iloveyou123",
    "letmein1234",
    "welcome1234",
    "administrator",
    "changeme123",
}


def _normalise_email(value: str | None) -> str | None:
    return value.strip().lower() if isinstance(value, str) else value


def _strip(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else value


def password_policy(form, field) -> None:
    password = field.data or ""
    min_len = current_app.config["PASSWORD_MIN_LENGTH"]
    max_len = current_app.config["PASSWORD_MAX_LENGTH"]
    if len(password) < min_len:
        raise ValidationError(f"Use at least {min_len} characters.")
    if len(password) > max_len:
        raise ValidationError(f"Use at most {max_len} characters.")
    if password.lower() in COMMON_PASSWORDS or password.isdigit() or len(set(password)) < 5:
        raise ValidationError("That password is too easy to guess. Try a longer passphrase.")
    email = getattr(getattr(form, "email", None), "data", None)
    if email and password.lower() == email.lower():
        raise ValidationError("Your password cannot be the same as your email address.")


class RegisterForm(FlaskForm):
    display_name = StringField(
        "Your name",
        filters=[_strip],
        validators=[DataRequired("Enter your name."), Length(max=80)],
    )
    email = StringField(
        "Email address",
        filters=[_normalise_email],
        validators=[
            DataRequired("Enter your email address."),
            Email(check_deliverability=False, message="Enter a valid email address."),
            Length(max=320),
        ],
    )
    password = PasswordField("Password", validators=[DataRequired("Choose a password."), password_policy])
    confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired("Confirm your password."), EqualTo("password", "Passwords do not match.")],
    )
    submit = SubmitField("Create account")


class LoginForm(FlaskForm):
    email = StringField(
        "Email address",
        filters=[_normalise_email],
        validators=[DataRequired("Enter your email address."), Length(max=320)],
    )
    password = PasswordField("Password", validators=[DataRequired("Enter your password.")])
    submit = SubmitField("Sign in")


class ForgotPasswordForm(FlaskForm):
    email = StringField(
        "Email address",
        filters=[_normalise_email],
        validators=[
            DataRequired("Enter your email address."),
            Email(check_deliverability=False, message="Enter a valid email address."),
            Length(max=320),
        ],
    )
    submit = SubmitField("Send reset link")


class ResetPasswordForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired("Choose a password."), password_policy])
    confirm_password = PasswordField(
        "Confirm new password",
        validators=[DataRequired("Confirm your password."), EqualTo("password", "Passwords do not match.")],
    )
    submit = SubmitField("Set new password")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField(
        "Current password", validators=[DataRequired("Enter your current password.")]
    )
    password = PasswordField(
        "New password", validators=[DataRequired("Choose a new password."), password_policy]
    )
    confirm_password = PasswordField(
        "Confirm new password",
        validators=[DataRequired("Confirm your password."), EqualTo("password", "Passwords do not match.")],
    )
    submit = SubmitField("Change password")


class ProfileForm(FlaskForm):
    display_name = StringField(
        "Your name",
        filters=[_strip],
        validators=[DataRequired("Enter your name."), Length(max=80)],
    )
    submit = SubmitField("Save changes")


class ActionForm(FlaskForm):
    """An empty, CSRF-protected form for single-button POST actions."""

    submit = SubmitField("Continue")
