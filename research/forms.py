"""New-research form. Every value is validated again on the server through
``ResearchSettings`` (the single source of truth for limits)."""

from __future__ import annotations

from flask_wtf import FlaskForm
from pydantic import ValidationError as PydanticValidationError
from wtforms import (
    BooleanField,
    IntegerField,
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField,
    TextAreaField,
    widgets,
)
from wtforms.validators import DataRequired, Length, Optional

from research.constants import DEPTHS, SOURCE_CATEGORIES
from research.schemas import DEPTH_PRESETS, HARD_LIMITS, ResearchSettings

TOPIC_MAX = 4000
OBJECTIVES_MAX = 4000

DEPTH_LABELS = {
    "quick": "Quick: a short overview, fewest sources",
    "standard": "Standard: balanced coverage",
    "deep": "Deep: more sources and research rounds",
    "comprehensive": "Comprehensive: the most thorough, slowest and most API usage",
}
LENGTH_CHOICES = [("short", "Short"), ("medium", "Medium"), ("long", "Long"), ("very_long", "Very long")]
FORMAT_CHOICES = [("pdf", "PDF"), ("docx", "Word (.docx)"), ("markdown", "Markdown"), ("html", "HTML")]
CITATION_CHOICES = [("numeric", "Numeric [1]"), ("apa", "APA"), ("mla", "MLA"), ("chicago", "Chicago")]
LANGUAGE_CHOICES = [
    ("en", "English"), ("ur", "Urdu"), ("ar", "Arabic"), ("es", "Spanish"), ("fr", "French"),
    ("de", "German"), ("pt", "Portuguese"), ("hi", "Hindi"), ("zh", "Chinese"), ("ja", "Japanese"),
]  # fmt: skip
CATEGORY_LABELS = {
    "official_documentation": "Official documentation", "academic": "Academic papers", "news": "News",
    "industry": "Industry reports", "government": "Government", "reference": "Reference works",
    "blog": "Blogs", "forum": "Forums and Q&A", "company": "Company pages", "other": "Other",
}  # fmt: skip


class _CheckboxList(SelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


def _lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").replace(",", "\n").splitlines() if line.strip()]


class NewResearchForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired(), Length(max=200)])
    topic = TextAreaField(
        "Research topic or question", validators=[DataRequired(), Length(min=10, max=TOPIC_MAX)]
    )
    objectives = TextAreaField(
        "Detailed objectives (optional)", validators=[Optional(), Length(max=OBJECTIVES_MAX)]
    )
    depth = SelectField("Research depth", choices=[(d, DEPTH_LABELS[d]) for d in DEPTHS], default="standard")
    output_length = SelectField("Output length", choices=LENGTH_CHOICES, default="medium")
    audience = StringField("Target audience", validators=[Optional(), Length(max=100)], default="general")
    language = SelectField("Language", choices=LANGUAGE_CHOICES, default="en")
    writing_style = StringField("Writing style", validators=[Optional(), Length(max=100)], default="neutral")
    source_categories = _CheckboxList(
        "Preferred source categories", choices=[(c, CATEGORY_LABELS[c]) for c in SOURCE_CATEGORIES]
    )
    prioritize_domains = TextAreaField("Domains to prioritise (one per line)", validators=[Optional()])
    exclude_domains = TextAreaField("Domains to exclude (one per line)", validators=[Optional()])
    date_from = StringField("Sources published from", validators=[Optional()])
    date_to = StringField("Sources published until", validators=[Optional()])
    max_sources = IntegerField("Maximum sources", validators=[Optional()])
    max_research_rounds = IntegerField("Maximum research rounds", validators=[Optional()])
    max_revision_rounds = IntegerField("Maximum revision rounds", validators=[Optional()])
    output_format = SelectField("Output format", choices=FORMAT_CHOICES, default="pdf")
    citation_style = SelectField("Citation style", choices=CITATION_CHOICES, default="numeric")
    enable_critic = BooleanField("Independent critic review", default=True)
    enable_additional_research = BooleanField("Search again when evidence is thin", default=True)
    submit = SubmitField("Save research project")

    def build_settings(self) -> ResearchSettings | None:
        """Validate the settings fields through ``ResearchSettings``; attach errors to the fields."""
        data = {
            "depth": self.depth.data,
            "output_length": self.output_length.data,
            "audience": (self.audience.data or "general").strip() or "general",
            "language": self.language.data,
            "writing_style": (self.writing_style.data or "neutral").strip() or "neutral",
            "source_categories": list(self.source_categories.data or []),
            "prioritize_domains": _lines(self.prioritize_domains.data),
            "exclude_domains": _lines(self.exclude_domains.data),
            "date_from": (self.date_from.data or "").strip() or None,
            "date_to": (self.date_to.data or "").strip() or None,
            "max_sources": self.max_sources.data,
            "max_research_rounds": self.max_research_rounds.data,
            "max_revision_rounds": self.max_revision_rounds.data,
            "output_format": self.output_format.data,
            "citation_style": self.citation_style.data,
            "enable_critic": bool(self.enable_critic.data),
            "enable_additional_research": bool(self.enable_additional_research.data),
        }
        try:
            return ResearchSettings.model_validate(data)
        except PydanticValidationError as exc:
            for error in exc.errors():
                name = str(error["loc"][0]) if error["loc"] else ""
                message = error["msg"].removeprefix("Value error, ")
                target = getattr(self, name, None)
                if target is not None:
                    target.errors = [*target.errors, message]
                else:
                    self.form_errors.append(message)
            return None

    def validate_on_submit(self, extra_validators=None) -> bool:  # type: ignore[override]
        ok = super().validate_on_submit(extra_validators)
        # Run the settings check even when other fields failed so every problem shows at once.
        settings_ok = self.build_settings() is not None if self.is_submitted() else False
        return bool(ok and settings_ok)


def limits_for_template() -> dict:
    """Numbers the page shows (and new.js uses) so the UI matches the server's limits."""
    return {
        "presets": {
            k: {"sources": v[0], "research": v[1], "revision": v[2]} for k, v in DEPTH_PRESETS.items()
        },
        "hard": HARD_LIMITS,
        "topic_max": TOPIC_MAX,
        "objectives_max": OBJECTIVES_MAX,
    }
