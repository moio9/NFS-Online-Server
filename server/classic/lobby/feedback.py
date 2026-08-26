"""Classic Underground 2/Most Wanted player-feedback handling."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


log = logging.getLogger("classic.protocols.prelogin")


def _ea_frame():
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


def _clean_report_value(value: object, *, limit: int = 256) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return " ".join(text.replace("\x00", " ").split())[:limit]


def _first(fields: dict[str, str], names: Iterable[str]) -> str:
    for name in names:
        value = _clean_report_value(fields.get(name, ""))
        if value:
            return value
    return ""


class ClassicFeedbackMixin:
    """Acknowledge and audit stock Classic ``rept`` feedback frames."""

    @staticmethod
    def _u2_feedback_ack() -> bytes:
        ClassicEAFrame = _ea_frame()
        return ClassicEAFrame.from_fields(
            "rept",
            (("TEXT", "Report complete"),),
            separator="\t",
            final_separator=False,
        ).encode()

    def _dispatch_u2_feedback(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        return self._dispatch_feedback(
            context,
            fields,
            title="U2",
            source="underground2_lobby",
        )

    def _dispatch_mw_feedback(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        return self._dispatch_feedback(
            context,
            fields,
            title="MW",
            source="most_wanted_lobby",
        )

    def _dispatch_feedback(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
        *,
        title: str,
        source: str,
    ) -> ClassicPreloginReply:
        ack = self._u2_feedback_ack()
        identity = context.auth.identity
        reporter = _clean_report_value(context.auth.persona)
        if identity is None or not reporter:
            return ClassicPreloginReply((ack,), "feedback_not_authenticated")

        target = _first(fields, ("PERS", "USER", "NAME"))
        report_type = _first(fields, ("TYPE", "TEXT", "REASON"))
        language = _first(fields, ("LANG",))
        if self.social is not None:
            self.social.record_report(
                reporter,
                target,
                report_type,
                language=language,
                source=source,
            )
        log.info(
            "%s feedback %s: reporter=%s target=%s type=%s lang=%s",
            title,
            "report" if report_type else "probe",
            reporter,
            target or "-",
            report_type or "-",
            language or "-",
        )
        return ClassicPreloginReply(
            (ack,),
            "feedback_reported" if report_type else "feedback_probe",
        )


__all__ = ["ClassicFeedbackMixin"]
