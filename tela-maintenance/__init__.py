"""Weekly maintenance for the tela memory provider, offered as an Automation Blueprint.

Memory providers are not loaded by Hermes' general plugin discovery, so the blueprint lives in this
small standalone plugin. It is only offered: the user schedules it (day, time, delivery), since
every run costs model tokens.
"""

from __future__ import annotations

PROMPT = (
    "Weekly memory maintenance. Read every page of your TELA MEMORY with `tela_memory` "
    "action=read (the snapshot in your prompt may be outdated). On each page: remove finished or "
    "stale items, merge duplicates, resolve contradictions in favour of the newest information, "
    "and tighten wordy lines. Move long details to a fitting tela page and leave a one-line "
    "pointer. Keep each page's language, headings and structure; never invent facts or delete "
    "something you are unsure about. Make the edits with `tela_memory`. Finally reply with a short "
    "summary in the user's language: what was added, updated and removed, or that nothing needed "
    "changing."
)

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def register(ctx) -> None:
    ctx.register_automation_blueprint(
        "weekly",
        title="Weekly memory maintenance",
        description="Tidies the tela memory pages once a week and sends a short summary of what changed.",
        category="weekly",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=PROMPT,
        slots=[
            {"name": "time", "type": "time", "label": "What time?", "default": "09:00",
             "help": "24h local time, e.g. 09:00"},
            {"name": "day", "type": "enum", "label": "Which day?", "default": "monday", "options": DAYS},
            {"name": "deliver", "type": "enum", "label": "Where to send the summary?", "default": "origin",
             "options": ("origin", "local", "telegram", "mattermost", "discord", "email"), "strict": False,
             "help": "origin = the chat you set this up from; local = save only; or any connected platform"},
        ],
        tags=("memory", "tela"),
    )
