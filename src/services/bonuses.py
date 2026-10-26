"""Подтверждённые партнёрские бонусы с возможностью переопределения в настройках."""

from html import escape
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

from services.matches import BOOKMAKERS
from services.partners import FREEBET_LABELS, PARTNER_URLS


class BonusOffer(BaseModel):
    bookmaker: Literal["fonbet", "pari", "marathon", "winline", "leon", "betboom"]
    priority: int = Field(ge=0)
    description: str = Field(min_length=1, max_length=300)
    url: HttpUrl
    enabled: bool = False

    @field_validator("url")
    @classmethod
    def secure_url(cls, value):
        if value.scheme != "https" or value.username or value.password:
            raise ValueError("Нужна HTTPS-ссылка без логина и пароля")
        return value


def ordered_offers(settings):
    offers = {
        name: BonusOffer(
            bookmaker=name, priority=index, description=label.capitalize(), url=PARTNER_URLS[name], enabled=True
        )
        for index, (name, label) in enumerate(FREEBET_LABELS.items())
        if name in PARTNER_URLS
    }
    # Явные настройки, включая отключение предложения, имеют приоритет.
    offers.update({offer.bookmaker: offer for offer in settings.bonus_offers})
    return sorted(
        (o for o in offers.values() if o.enabled and o.bookmaker in PARTNER_URLS),
        key=lambda o: (o.priority, o.bookmaker),
    )


def render_bonus(offers):
    from services.emojis import bookmaker_name

    offers = [o for o in offers if o.enabled and o.bookmaker in PARTNER_URLS]
    lines = ["<b>Бонусы</b>"]
    if not offers:
        lines.append("Сейчас нет опубликованных бонусных предложений. Загляни позже.")
    for offer in offers:
        lines.extend(
            ["", f"<b>{bookmaker_name(offer.bookmaker, BOOKMAKERS[offer.bookmaker])}</b>", escape(offer.description)]
        )
    if offers:
        lines.extend(["", "Условия, ограничения и срок действия — на сайте букмекера."])
    lines.extend(["", "18+ · Играйте ответственно."])
    return "\n".join(lines)
