"""AI-агент для анализа тендеров через Claude API."""

import os
import re

import httpx

import db
from config import ANTHROPIC_API_KEY
from logger import get_logger

log = get_logger("ai_agent")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

BASE_SYSTEM_PROMPT = """\
Ты - эксперт по строительным тендерам Республики Беларусь, который помогает
небольшой строительно-монтажной компании выбирать самые выгодные тендеры.

ПРОФИЛЬ ЗАКАЗЧИКА:

ПРИОРИТЕТ 1 (core-бизнес, оценивать щедро):
  Охранные системы, видеонаблюдение, СКУД, пожарная сигнализация.

ПРИОРИТЕТ 2 (строительство/ремонт - берём охотно):
  Капремонт, отделка, кровля, фасад, благоустройство, сантехника.

НИЗКИЙ ПРИОРИТЕТ:
  Электромонтаж - понижать оценку на 1-2 балла.

РЫНОЧНЫЙ КОНТЕКСТ РБ (2025):
  Отделочные работы: 20-40 BYN/м²
  Капремонт: 150-400 BYN/м²
  Кровельные работы: 50-120 BYN/м²
  Минимальный комфортный срок подачи: 7 дней (2-3 дня = красный флаг)
  Госзаказчики часто занижают бюджет на 15-30%

КРАСНЫЕ ФЛАГИ (снижать оценку):
  - "технический надзор" (это не строительные работы)
  - "выполнение функций заказчика" (управление, не работы)
  - срок подачи 1-2 дня
  - "согласно заявке" во всех ключевых полях (непрозрачные условия)
  - объект ИКЦ (историко-культурная ценность) - особые требования
  - режимный объект (колония, воинская часть) - сложный доступ
  - закупка из одного источника по основанию №9 при малой сумме
    (вероятно "карманная" закупка под конкретного поставщика)
  - оплата в течение 90+ дней

ЗЕЛЁНЫЕ СИГНАЛЫ (повышать оценку):
  - предусмотрен аванс
  - бюджетные средства (надёжный источник финансирования)
  - чёткое техническое задание с указанием адреса объекта
  - конкурс с ограниченным участием (есть реальный отбор)
  - смешанный тендер (ремонт + охранная сигнализация = идеально для нас)

ТВОЯ ЗАДАЧА:
Напиши анализ тендера как живой эксперт, своими словами, БЕЗ шаблонных фраз
и канцелярита. Структура анализа (без заголовков-нумерации в тексте, пиши
связным текстом по абзацам):

1. Первое впечатление - 1-2 предложения, суть тендера.
2. Что интересно / что настораживает.
3. Финансовая сторона (если есть расчёт маржи - обязательно используй его
   в рассуждении).
4. Риски и подводные камни.
5. Итог и рекомендация.

Тебе будет дан предварительный балл (0-10), рассчитанный программой на основе
суммы, сроков, типа закупки и маржи. Скорректируй его на основе своего анализа
(поправка от -3 до +3 баллов с учётом красных/зелёных флагов), финальный балл
должен быть в диапазоне от 1 до 10.

В САМОМ КОНЦЕ ответа выведи ровно две строки в таком формате (без лишнего
текста после них):
ОЦЕНКА: N/10
ВЕРДИКТ: Перспективный / Сомнительный / Пропустить
"""


# ---------------------------------------------------------------------------
# Предварительная (программная) оценка
# ---------------------------------------------------------------------------

def _amount_score(amount):
    if amount is None:
        return 5
    if amount < 10000:
        return 1
    if amount < 30000:
        return 4
    if amount < 100000:
        return 6
    if amount < 300000:
        return 8
    if amount < 700000:
        return 9
    return 7


def _deadline_score(days_left):
    if days_left is None:
        return 5
    if days_left < 3:
        return 2
    if days_left < 7:
        return 5
    if days_left < 14:
        return 8
    return 10


TYPE_SCORES = {
    "конкурс с ограниченным участием": 9,
    "запрос ценовых предложений": 7,
    "электронный аукцион": 7,
    "закупка из одного источника": 5,
    "заявка о предоставлении сведений": 4,
    "заявка о ценах": 4,
    "переговоры": 6,
    "иной вид": 5,
}


def _type_score(tender_type):
    if not tender_type:
        return 5
    tender_type_lower = tender_type.lower()
    for key, score in TYPE_SCORES.items():
        if key in tender_type_lower:
            return score
    return 5


def _margin_score(margin_pct):
    if margin_pct is None:
        return None
    if margin_pct < 0:
        return 1
    if margin_pct < 5:
        return 4
    if margin_pct < 10:
        return 6
    if margin_pct < 15:
        return 8
    return 10


RISK_KEYWORDS = {
    "технический надзор": -3,
    "выполнение функций заказчика": -3,
    "икц": -2,
    "историко-культурн": -2,
    "колони": -3,
    "воинск": -3,
    "режимн": -2,
    "согласно заявке": -1,
}

GREEN_KEYWORDS = {
    "аванс": 1,
    "бюджетн": 1,
    "ограниченным участием": 1,
}


def _risk_score(tender):
    """Эвристическая оценка рисков 1-10 на основе текста тендера."""
    text_parts = [
        tender.get("title") or "",
        tender.get("description") or "",
        tender.get("financing") or "",
        tender.get("payment_terms") or "",
        tender.get("tender_type") or "",
    ]
    text = " ".join(text_parts).lower()

    score = 6  # нейтральный старт

    for kw, delta in RISK_KEYWORDS.items():
        if kw in text:
            score += delta

    for kw, delta in GREEN_KEYWORDS.items():
        if kw in text:
            score += delta

    payment_terms = (tender.get("payment_terms") or "").lower()
    match = re.search(r"(\d+)\s*дн", payment_terms)
    if match and int(match.group(1)) >= 90:
        score -= 2

    deadline_days = tender.get("_days_left")
    if deadline_days is not None and deadline_days < 3:
        score -= 1

    return max(1, min(10, score))


def _days_left_from_deadline(deadline):
    if not deadline:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(deadline)
    except ValueError:
        return None
    return (dt - datetime.now()).total_seconds() / 86400.0


def calculate_pre_score(tender: dict, margin_result: dict = None) -> float:
    """Рассчитывает предварительную (программную) оценку тендера 1-10."""
    days_left = _days_left_from_deadline(tender.get("deadline"))
    tender["_days_left"] = days_left

    amount_s = _amount_score(tender.get("amount"))
    deadline_s = _deadline_score(days_left)
    type_s = _type_score(tender.get("tender_type"))
    risk_s = _risk_score(tender)

    margin_pct = (margin_result or {}).get("margin_pct")
    margin_s = _margin_score(margin_pct)

    base_weights = {"deadline": 0.20, "type": 0.15, "amount": 0.15, "risk": 0.10}
    base_sum = sum(base_weights.values())  # 0.60

    if margin_s is not None:
        margin_weight = 0.50
    else:
        margin_weight = 0.0

    remaining_weight = 1.0 - margin_weight
    factor = remaining_weight / base_sum if base_sum else 0

    pre_score = (
        deadline_s * base_weights["deadline"] * factor
        + type_s * base_weights["type"] * factor
        + amount_s * base_weights["amount"] * factor
        + risk_s * base_weights["risk"] * factor
    )

    if margin_s is not None:
        pre_score += margin_s * margin_weight

    return round(pre_score, 2)


# ---------------------------------------------------------------------------
# Обучение на обратной связи
# ---------------------------------------------------------------------------

def build_learned_patterns() -> str:
    """
    Анализирует накопленную обратную связь (feedback) и возвращает текстовый
    блок с паттернами, который добавляется в системный промпт.

    Возвращает пустую строку, если обратной связи меньше 10 записей.
    """
    stats = db.get_feedback_stats()
    total = sum(row["count"] for row in stats)

    if total < 10:
        return ""

    lines = ["ВЫУЧЕННЫЕ ПАТТЕРНЫ ИЗ ОБРАТНОЙ СВЯЗИ ПОЛЬЗОВАТЕЛЯ:"]

    negative_reasons = [r for r in stats if r["reaction"] == "👎" and r["reason"]]
    if negative_reasons:
        lines.append("Пользователь чаще всего отклоняет тендеры по причинам:")
        for row in sorted(negative_reasons, key=lambda r: -r["count"])[:5]:
            lines.append(f"  - {row['reason']}: {row['count']} раз")
        lines.append(
            "Учитывай эти причины: если видишь похожие признаки - снижай оценку "
            "и явно упоминай этот риск в анализе."
        )

    positive = [r for r in stats if r["reaction"] == "👍"]
    if positive:
        positive_count = sum(r["count"] for r in positive)
        lines.append(
            f"Пользователь отметил как интересные {positive_count} тендеров - "
            "продолжай искать похожие по профилю."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Вызов Claude API
# ---------------------------------------------------------------------------

def _call_claude(system_prompt: str, user_message: str) -> str:
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1500,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }

    resp = httpx.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    result = resp.json()

    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    return text


def _parse_score_verdict(text: str):
    score = None
    verdict = None

    score_match = re.search(r"ОЦЕНКА:\s*(\d+)\s*/\s*10", text, re.IGNORECASE)
    if score_match:
        score = int(score_match.group(1))
        score = max(1, min(10, score))

    verdict_match = re.search(
        r"ВЕРДИКТ:\s*(Перспективный|Сомнительный|Пропустить)", text, re.IGNORECASE
    )
    if verdict_match:
        verdict = verdict_match.group(1).capitalize()

    return score, verdict


def _build_user_message(tender: dict, smeta_positions: list, margin_result: dict, pre_score: float) -> str:
    days_left = tender.get("_days_left")
    days_left_str = f"{days_left:.1f}" if days_left is not None else "неизвестно"

    lines = [
        f"Название: {tender.get('title')}",
        f"Заказчик: {tender.get('customer')}",
        f"Категория/отрасль: {tender.get('category')}",
        f"Тип закупки: {tender.get('tender_type')}",
        f"Сумма: {tender.get('amount')} BYN",
        f"Срок подачи: {tender.get('deadline')} (осталось дней: {days_left_str})",
        f"Источник финансирования: {tender.get('financing')}",
        f"Условия оплаты: {tender.get('payment_terms')}",
        f"ОКРБ: {tender.get('okrb_code')}",
        f"Группа совпадения: {tender.get('matched_group')} (приоритет {tender.get('priority')})",
    ]

    if margin_result and margin_result.get("margin_byn") is not None:
        lines.append(
            f"Маржа по смете: {margin_result['margin_byn']} BYN "
            f"({margin_result['margin_pct']}%), "
            f"сопоставлено позиций: {margin_result['matched_count']}/{margin_result['total_count']}"
        )
    elif smeta_positions:
        lines.append(
            f"Смета найдена, но позиций сопоставлено недостаточно "
            f"({(margin_result or {}).get('matched_count', 0)}/{(margin_result or {}).get('total_count', len(smeta_positions))})"
        )
    else:
        lines.append("Смета не найдена или не удалось извлечь позиции.")

    lines.append(f"\nПредварительный балл (рассчитан программой): {pre_score}/10")
    lines.append("\nНапиши анализ этого тендера.")

    return "\n".join(lines)


def analyze_tender(tender: dict, smeta_positions: list, margin_result: dict) -> dict:
    """
    Анализирует тендер с помощью Claude API.

    Возвращает словарь:
      {analysis, score, verdict, margin_byn, margin_pct}
    """
    pre_score = calculate_pre_score(tender, margin_result)

    system_prompt = BASE_SYSTEM_PROMPT
    learned = build_learned_patterns()
    if learned:
        system_prompt = f"{BASE_SYSTEM_PROMPT}\n\n{learned}"

    user_message = _build_user_message(tender, smeta_positions, margin_result, pre_score)

    try:
        text = _call_claude(system_prompt, user_message)
    except Exception as e:
        log.error(f"Ошибка вызова Claude API для {tender.get('id')}: {e}")
        text = (
            "Не удалось получить анализ от AI из-за технической ошибки. "
            f"Предварительная программная оценка: {pre_score}/10.\n"
            f"ОЦЕНКА: {round(pre_score)}/10\n"
            "ВЕРДИКТ: Сомнительный"
        )

    score, verdict = _parse_score_verdict(text)
    if score is None:
        score = max(1, min(10, round(pre_score)))
    if verdict is None:
        if score >= 7:
            verdict = "Перспективный"
        elif score >= 4:
            verdict = "Сомнительный"
        else:
            verdict = "Пропустить"

    margin_byn = (margin_result or {}).get("margin_byn")
    margin_pct = (margin_result or {}).get("margin_pct")

    return {
        "analysis": text.strip(),
        "score": score,
        "verdict": verdict,
        "margin_byn": margin_byn,
        "margin_pct": margin_pct,
    }


def analyze_batch(tenders: list) -> None:
    """
    Прогоняет список тендеров через полный цикл анализа:
    смета -> маржа -> AI-анализ -> сохранение в БД.
    """
    from smeta_parser import process_tender_smeta
    from margin_calculator import calculate_margin

    if not tenders:
        return

    price_items = db.get_price_items()
    analyzed = []

    for tender in tenders:
        try:
            log.info(f"Анализ тендера {tender['id']}: {tender.get('title', '')[:60]}")

            smeta_positions = process_tender_smeta(tender)
            margin_result = calculate_margin(smeta_positions, price_items)

            result = analyze_tender(tender, smeta_positions, margin_result)

            db.save_analysis(
                tender_id=tender["id"],
                analysis=result["analysis"],
                score=result["score"],
                verdict=result["verdict"],
                margin_byn=result["margin_byn"],
                margin_pct=result["margin_pct"],
            )

            analyzed.append((tender["id"], result["score"]))

        except Exception as e:
            log.error(f"Ошибка анализа тендера {tender.get('id')}: {e}")
            continue

    analyzed.sort(key=lambda x: x[1], reverse=True)
    log.info(f"Анализ завершён. Топ тендеров: {analyzed[:3]}")
