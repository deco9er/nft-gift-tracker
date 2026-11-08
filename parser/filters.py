from dataclasses import dataclass
from hashlib import blake2s

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


PRICE_BANDS = {
    "tg": [
        ("Stars", 0, 500), ("Stars", 500, 1000), ("Stars", 1000, 5000), ("Stars", 5000, None),
        ("GRAM", 0, 10), ("GRAM", 10, 50), ("GRAM", 50, 100), ("GRAM", 100, None),
    ],
}
OWNERS = {
    "user": "Есть username",
    "id": "Ссылка по Telegram ID",
    "no_username": "Без username",
    "wallet": "Криптокошелёк",
    "no_wallet": "Без кошелька",
    "hidden": "Владелец не раскрыт",
    "unchecked": "Карточка не проверена",
}
RATINGS = {
    "rated": "Рейтинг получен", "unrated": "Рейтинг неизвестен",
    "0": "Рейтинг ≥ 0", "1": "Рейтинг ≥ 1", "5": "Рейтинг ≥ 5", "10": "Рейтинг ≥ 10",
}
FLOORS = {
    "cheap": ("Дешёвые · до 10 TON", 0, 10),
    "medium": ("Средние · до 50 TON", 10, 50),
    "expensive": ("Дорогие · до 1500 TON", 50, 1500),
}


def floor_category(floor: float | None) -> str:
    if floor is None:
        return ""
    return next((label for label, minimum, maximum in FLOORS.values() if minimum < floor <= maximum), "")


SORTS = {
    "live": "Лайв",
    "title_asc": "Коллекция А–Я", "title_desc": "Коллекция Я–А",
    "number_asc": "Номер ↑", "number_desc": "Номер ↓",
    "stars_asc": "Цена в Stars ↑", "stars_desc": "Цена в Stars ↓",
    "gram_asc": "Цена в GRAM ↑", "gram_desc": "Цена в GRAM ↓",
    "rating_asc": "Рейтинг ↑", "rating_desc": "Рейтинг ↓",
    "floor_asc": "Флор ↑", "floor_desc": "Флор ↓",
}


@dataclass
class Filters:
    collection: str = ""
    band: int | None = None
    owner: str = ""
    rating: str = ""
    sort: str = ""
    floor: str = ""

    def query(self, source: str) -> dict:
        query = {"collection": self.collection, "owner_filter": self.owner,
                 "rating_filter": self.rating, "sort": self.sort}
        if self.band is not None:
            currency, minimum, maximum = PRICE_BANDS[source][self.band]
            query.update(currency=currency, minimum=minimum, maximum=maximum)
        if self.floor:
            _, floor_minimum, floor_maximum = FLOORS[self.floor]
            query["floor_range"] = (floor_minimum, floor_maximum)
        return query

    def labels(self, source: str) -> list[str]:
        return [label for label in (
            self.collection, price_label(*PRICE_BANDS[source][self.band]) if self.band is not None else "",
            OWNERS.get(self.owner, ""),
            RATINGS.get(self.rating, ""), SORTS.get(self.sort, ""),
            FLOORS[self.floor][0] if self.floor else "",
        ) if label]


def price_label(currency: str, minimum: int, maximum: int | None) -> str:
    if maximum is None:
        return f"≥ {minimum} {currency}"
    if minimum == 0:
        return f"< {maximum} {currency}"
    return f"{minimum}–<{maximum} {currency}"


def collection_key(title: str) -> str:
    return blake2s(title.encode(), digest_size=6).hexdigest()


def filter_keyboard(source: str, filters: Filters, screen: str = "menu", titles: list[str] | None = None,
                    page: int = 0) -> InlineKeyboardMarkup:
    def button(text: str, action: str, selected: bool = False) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=("✓ " if selected else "") + text, callback_data=f"f:{source}:{action}")

    back = button("‹ Фильтры", "menu")
    if screen == "types":
        titles = titles or []
        size = 8
        page = min(page, max(0, (len(titles) - 1) // size))
        rows = [[button(title[:45], "c" + collection_key(title), title == filters.collection)]
                for title in titles[page * size:(page + 1) * size]]
        nav = []
        if page:
            nav.append(button("‹", f"t{page - 1}"))
        if (page + 1) * size < len(titles):
            nav.append(button("›", f"t{page + 1}"))
        if nav:
            rows.append(nav)
        rows.extend([[button("Все коллекции", "c0", not filters.collection)], [back]])
    elif screen == "price":
        rows = [[button(price_label(*band), f"p{index}", index == filters.band)]
                for index, band in enumerate(PRICE_BANDS[source])]
        rows.extend([[button("Любая цена", "pall", filters.band is None)], [back]])
    elif screen == "owner":
        rows = [[button("Подтверждённый @username", "o_user", True)], [back]]
    elif screen == "rating":
        rows = [[button(title, f"r_{key}", key == filters.rating)] for key, title in RATINGS.items()]
        rows.extend([[button("Любой рейтинг", "r_all", not filters.rating)], [back]])
    elif screen == "sort":
        rows = [[button(title, f"s_{key}", key == (filters.sort or "title_asc"))]
                for key, title in SORTS.items() if source == "tg" or not key.startswith("stars")]
        rows.append([back])
    elif screen == "floor":
        rows = [[button(label, f"l_{key}", key == filters.floor)] for key, (label, _, _) in FLOORS.items()]
        rows.extend([[button("Любой флор", "l_all", not filters.floor)], [back]])
    else:
        rows = [[button("Коллекция", "t0"), button("Цена лота", "price"), button("Флор", "floor")],
                [button("Владелец", "owner"), button("Сортировка", "sort")],
                [button("Сбросить", "reset")],
                [button("К списку", "done")]]
        rows[2].insert(0, button("Рейтинг", "rating"))
    return InlineKeyboardMarkup(inline_keyboard=rows)
