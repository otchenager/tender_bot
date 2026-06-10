"""Классификация тендеров по ключевым словам и приоритетам."""

import re

PRIORITY_1 = [
    "охранная сигнализация",
    "охранная система",
    "система охраны",
    "видеонаблюдение",
    "ip-камеры",
    "ip камеры",
    "монтаж камер",
    "cctv",
    "система безопасности",
    "контроль доступа",
    "скуд",
    "пожарная сигнализация",
    "пожарная автоматика",
    "техническое обслуживание охранных",
    "слаботочные системы",
]

PRIORITY_2 = [
    "реновация",
    "снос",
    "капитальный ремонт",
    "капремонт",
    "текущий ремонт",
    "реконструкция",
    "ремонт здания",
    "строительство школы",
    "строительство больницы",
    "фасад",
    "кровля",
    "кровельные работы",
    "благоустройство",
    "асфальтирование",
    "сантехника",
    "вентиляция",
    "теплоснабжение",
    "отопление",
    "штукатурка",
    "стяжка",
    "малярные работы",
    "покраска",
    "плиточные работы",
    "укладка плитки",
    "напольное покрытие",
    "натяжной потолок",
    "отделочные работы",
]

LOW_PRIORITY = [
    "электромонтаж",
    "электромонтажные работы",
    "прокладка кабеля",
]

EXCLUDE = [
    "уборка помещений",
    "клининг",
    "поставка мебели",
    "поставка продуктов",
    "юридические услуги",
    "охрана объекта",
    "технический надзор",
    "выполнение функций заказчика",
    "it-услуги",
    "программное обеспечение",
]


# Аббревиатуры и короткие слова, которые НЕ нужно сопоставлять по основе
# (иначе "скуд" поймает "скудный" и т.п.) - ищутся только как целое слово.
_ABBREVIATIONS = {"скуд", "cctv", "ip", "икц", "окрб", "тру", "сму"}

# Типичные окончания русских существительных/прилагательных, отбрасываемые
# для построения "основы" слова. Проверяются от самых длинных к коротким.
_SUFFIXES = sorted(
    [
        "иями", "иях", "ями", "ами", "его", "ому", "ему", "ыми", "ими",
        "ой", "ей", "ом", "ем", "ах", "ях", "их", "ых", "ие", "ия", "ию", "ием",
        "а", "я", "ы", "и", "е", "о", "у", "ю", "й", "ь", "м", "х",
    ],
    key=len,
    reverse=True,
)

_MIN_STEM_LEN = 4


def _stem(word: str) -> str:
    """Грубо отбрасывает типичное окончание слова, оставляя основу."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= _MIN_STEM_LEN:
            return word[: -len(suf)]
    return word


def _word_pattern(word: str) -> str:
    """Строит regex-фрагмент для одного слова с учётом морфологии."""
    if word.lower() in _ABBREVIATIONS:
        return re.escape(word)

    if "-" in word:
        return "-".join(_word_pattern(part) for part in word.split("-"))

    if not word.isalpha():
        return re.escape(word)

    return re.escape(_stem(word)) + r"\w*"


def _keyword_pattern(keyword: str) -> "re.Pattern":
    """Компилирует ключевую фразу в regex, допускающий любые формы слов."""
    words = keyword.split()
    pattern = r"\b" + r"\s+".join(_word_pattern(w) for w in words) + r"\b"
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


# Дополнительные основы для слов, у которых прилагательная форма
# образуется не от той же основы, что и существительное
# (например "кровля" -> "кровельные работы").
_EXTRA_STEMS = {
    "кровля": ["кровельн"],
    "фасад": ["фасадн"],
    "вентиляция": ["вентиляционн"],
    "отопление": ["отопительн"],
    "сантехника": ["сантехническ"],
    "благоустройство": ["благоустроительн"],
    "штукатурка": ["штукатурн"],
}


def _compile_group(keywords: list) -> list:
    compiled = []
    for kw in keywords:
        compiled.append((kw, _keyword_pattern(kw)))
        for extra in _EXTRA_STEMS.get(kw, []):
            extra_pattern = re.compile(
                r"\b" + re.escape(extra) + r"\w*\b", re.IGNORECASE | re.UNICODE
            )
            compiled.append((kw, extra_pattern))
    return compiled


_PRIORITY_1_PATTERNS = _compile_group(PRIORITY_1)
_PRIORITY_2_PATTERNS = _compile_group(PRIORITY_2)
_LOW_PRIORITY_PATTERNS = _compile_group(LOW_PRIORITY)
_EXCLUDE_PATTERNS = _compile_group(EXCLUDE)


def is_relevant(title: str, description: str = ""):
    """
    Определяет релевантность тендера.

    Возвращает кортеж: (relevant: bool, group: str, priority: int)
    priority: 1 - core-бизнес (охрана/видеонаблюдение),
              2 - строительство/ремонт,
              3 - низкий приоритет (электромонтаж и т.п.)
    """
    text = f"{title} {description}".lower()

    # Автоматический отсев
    for kw, pattern in _EXCLUDE_PATTERNS:
        if pattern.search(text):
            return False, "exclude", 0

    # Приоритет 1 - core-бизнес
    for kw, pattern in _PRIORITY_1_PATTERNS:
        if pattern.search(text):
            return True, kw, 1

    # Приоритет 2 - строительство/ремонт
    for kw, pattern in _PRIORITY_2_PATTERNS:
        if pattern.search(text):
            return True, kw, 2

    # Низкий приоритет - не исключаем, но понижаем
    for kw, pattern in _LOW_PRIORITY_PATTERNS:
        if pattern.search(text):
            return True, kw, 3

    return False, "none", 0
