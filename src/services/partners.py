"""Активные партнёры и единственные публичные ссылки на букмекеров."""

PARTNER_URLS = {
    "marathon": "https://bett1.pro/click?o=354&a=53074&tsource=1015",
    "winline": "https://bett1.pro/click?o=159&a=53074&tsource=1015&link_id=5155",
}


def active_bookmakers():
    return tuple(PARTNER_URLS)


def public_offers(offers):
    """Фильтруем и старые данные; адрес из снимка не используется для перехода."""
    return [
        {**offer, "url": PARTNER_URLS[offer["bookmaker"]]} for offer in offers if offer.get("bookmaker") in PARTNER_URLS
    ]
