from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from translator_bot.config import Settings


@dataclass(frozen=True)
class MessageAnalysisResult:
    message_english: str
    important: bool
    alerts: dict[str, bool]
    reason: str | None = None


@dataclass(frozen=True)
class SuggestedReply:
    zh: str
    en: str


class OpenAIService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai.api_key)

    async def analyze_message(
        self,
        text: str,
        *,
        chat_title: str,
        sender_name: str,
        protected_placeholders: list[dict[str, str | None]] | None = None,
    ) -> MessageAnalysisResult:
        names = self.settings.alerts.names
        response = await self.client.chat.completions.create(
            model=self.settings.openai.translation_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Analyze one Chinese Telegram message. Return JSON only with keys: "
                        "message_english, important, important_reason, alerts. "
                        "alerts must be an object with booleans: name_mention, question_or_request, urgent. "
                        "Translate the message into natural English. "
                        "The message may contain protected placeholders like XM_PROTECTED_SEGMENT_0. "
                        "Copy placeholders exactly into message_english where they belong. "
                        "Never translate, remove, duplicate, wrap, indent, or modify placeholders. "
                        "Use inline_code placeholder content as context only; do not translate that content. "
                        "Code block placeholder content is intentionally omitted and must not be inferred. "
                        "Mark important true for requests, questions, decisions, deadlines, complaints, "
                        "money/payment, meetings, risks, urgent messages, or watched-name mentions. "
                        "For unimportant messages, important_reason must be null. "
                        "For important messages, important_reason must be under 80 characters. "
                        "Do not include explanations."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "watched_names": names,
                            "chat_title": chat_title,
                            "sender": sender_name,
                            "message": text,
                            "protected_placeholders": protected_placeholders or [],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        data = _json_from_response(response.choices[0].message.content)
        alerts = data.get("alerts") if isinstance(data.get("alerts"), dict) else {}
        compact_alerts = {
            "name_mention": bool(alerts.get("name_mention")) if self.settings.alerts.name_mentions_enabled else False,
            "question_or_request": bool(alerts.get("question_or_request"))
            if self.settings.alerts.question_request_alert
            else False,
            "urgent": bool(alerts.get("urgent")) if self.settings.alerts.urgency_alert else False,
        }
        important = (
            bool(data.get("important"))
            or compact_alerts["name_mention"]
            or compact_alerts["question_or_request"]
            or compact_alerts["urgent"]
        )
        reason = str(data["important_reason"]).strip() if important and data.get("important_reason") else None
        return MessageAnalysisResult(
            message_english=str(data.get("message_english", "")).strip() or text,
            important=important,
            alerts=compact_alerts,
            reason=_short_text(reason, 80),
        )

    async def update_chat_memory(
        self,
        *,
        chat_title: str,
        previous_memory: dict[str, Any] | None,
        recent_messages: list[dict[str, Any]],
        summary: str,
    ) -> dict[str, Any]:
        response = await self.client.chat.completions.create(
            model=self.settings.openai.summary_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Update durable memory for one Telegram chat. Return JSON only with keys: "
                        "chat_title_translation, summary, topics, people, open_items, preferences_or_terms. "
                        "Translate the chat title to concise natural English. Keep memory compact and durable. "
                        "Preserve useful previous context, add new important context, and remove resolved or stale items. "
                        "Do not store small talk or one-off details unless they affect ongoing work. "
                        "people must be an array of objects with name, username, role_or_context. "
                        "topics, open_items, and preferences_or_terms must be arrays of short strings."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "chat_title": chat_title,
                            "previous_memory": previous_memory or {},
                            "recent_messages": recent_messages,
                            "generated_summary": summary,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
        )
        data = _json_from_response(response.choices[0].message.content)
        return {
            "chat_title_translation": str(data.get("chat_title_translation", "")).strip() or chat_title,
            "memory": {
                "summary": str(data.get("summary", "")).strip(),
                "topics": _string_list(data.get("topics")),
                "people": _people_list(data.get("people")),
                "open_items": _string_list(data.get("open_items")),
                "preferences_or_terms": _string_list(data.get("preferences_or_terms")),
            },
        }

    async def translate_batch(self, messages: list[dict[str, Any]]) -> str:
        response = await self.client.chat.completions.create(
            model=self.settings.openai.translation_model,
            temperature=0.1,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Translate these Chinese Telegram messages to English. "
                        "Keep each message on its own bullet with sender and short context."
                    ),
                },
                {"role": "user", "content": json.dumps(messages, ensure_ascii=False, default=str)},
            ],
        )
        return response.choices[0].message.content or ""

    async def suggest_replies(
        self,
        *,
        current_message: dict[str, Any],
        recent_messages: list[dict[str, Any]],
        chat_memory: dict[str, Any] | None,
        profile: dict[str, Any],
        knowledge: str,
        max_count: int,
    ) -> list[SuggestedReply]:
        response = await self.client.chat.completions.create(
            model=self.settings.openai.summary_model,
            temperature=0.4,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Generate reply suggestions for a Telegram work chat. Return JSON only with key replies. "
                        "replies must be an array of objects with zh and en strings. "
                        "Generate up to max_count replies, but if only one strong reply is useful, return one. "
                        "Do not pad with weak or duplicate variations. Do not number or label replies. "
                        "The zh reply should be natural Chinese suitable to send as-is. The en field is the English meaning. "
                        "Use the profile, chat memory, recent chat context, and markdown knowledge when relevant. "
                        "Do not invent project facts. If context is insufficient, suggest asking for missing details. "
                        "Keep replies concise, polite, and practical."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "max_count": max_count,
                            "profile": profile,
                            "chat_memory": chat_memory or {},
                            "recent_messages": recent_messages,
                            "current_message": current_message,
                            "markdown_knowledge": knowledge,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
        )
        data = _json_from_response(response.choices[0].message.content)
        replies = data.get("replies")
        if not isinstance(replies, list):
            return []
        results: list[SuggestedReply] = []
        for item in replies[:max_count]:
            if not isinstance(item, dict):
                continue
            zh = str(item.get("zh", "")).strip()
            en = str(item.get("en", "")).strip()
            if zh and en:
                results.append(SuggestedReply(zh=zh, en=en))
        return results

    async def summarize(self, messages: list[dict[str, Any]], *, title: str) -> str:
        if not messages:
            return "No messages to summarize."

        response = await self.client.chat.completions.create(
            model=self.settings.openai.summary_model,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize Telegram chat messages in concise English. "
                        "Use exactly these emoji section headings when relevant: "
                        "🔑 Key Points, ✅ Decisions, 📌 Action Items, ❓ Questions/Requests, "
                        "📅 Meetings/Events/Deadlines, ⚠️ Urgent or Risky Items. "
                        "Use short bullets under each heading. End every non-empty bullet with "
                        "the source chat display in parentheses, wrapping chat names with backticks. "
                        "Use the provided chat_display field exactly as-is when available, for example: "
                        "'- Backend support can wait until tomorrow. (`技术群` / `Tech Group`)' "
                        "Messages may include an attachment object with type, name, mime_type, and size. "
                        "Mention attachments naturally when they affect the meaning, request, evidence, "
                        "or action item, for example screenshots, error images, PDFs, voice messages, "
                        "or videos. Do not add a separate attachment section just to list files. "
                        "If a section has no useful content, write '- None mentioned.' "
                        "Ignore small talk unless it changes the situation."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"title": title, "messages": messages}, ensure_ascii=False, default=str),
                },
            ],
        )
        return response.choices[0].message.content or "No summary generated."


def _json_from_response(content: str | None) -> dict[str, Any]:
    if not content:
        return {}
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _short_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _people_list(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    people: list[dict[str, str | None]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        people.append(
            {
                "name": name,
                "username": str(item["username"]).strip() if item.get("username") else None,
                "role_or_context": str(item["role_or_context"]).strip() if item.get("role_or_context") else None,
            }
        )
    return people
