"""
Pure functions, no I/O, no DB -- the one module worth unit-testing on its
own, because this is where "1,500,000 EGP" / "1.5M" / مليون ونصف all
collapse to the same 1500000, and تشطيب سوبر لوكس / "super lux finishing"
collapse to the same enum value.

Called from extract.py (to canonicalize whatever the LLM / regex layer
produced) and from parse.py (language detection, digit cleanup before the
description ever reaches a prompt).
"""

import re
import unicodedata

# ------------------------------------------------------------- characters

_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_EXT_ARABIC_INDIC = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_TASHKEEL = re.compile(r"[ً-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"


def clean_arabic(text: str) -> str:
    """NFKC + strip tatweel/tashkeel + unify alef/hamza/ta-marbuta variants.
    For MATCHING purposes only (glossary lookups, enum detection) -- never
    apply this to description_raw itself, which must stay unedited."""
    if not text:
        return text
    t = unicodedata.normalize("NFKC", text)
    t = t.translate(_ARABIC_INDIC).translate(_EXT_ARABIC_INDIC)
    t = t.replace(_TATWEEL, "")
    t = _TASHKEEL.sub("", t)
    t = re.sub(r"[إأآا]", "ا", t)
    t = t.replace("ة", "ه").replace("ى", "ي")
    return t.strip()


def to_ascii_digits(text: str) -> str:
    if not text:
        return text
    return unicodedata.normalize("NFKC", text).translate(_ARABIC_INDIC).translate(_EXT_ARABIC_INDIC)


def detect_language(text: str) -> str:
    """ar / en / mixed. Naive ratio-based detectors misfire on short
    strings dominated by digits/punctuation, so this only counts
    alphabetic characters and requires a real minority share (>15%) before
    calling something "mixed" rather than flipping on a single stray word."""
    if not text:
        return None
    arabic = len(re.findall(r"[؀-ۿ]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    total = arabic + latin
    if total == 0:
        return None
    if arabic and latin:
        minority = min(arabic, latin) / total
        if minority > 0.15:
            return "mixed"
    return "ar" if arabic >= latin else "en"


# --------------------------------------------------------------- numbers

_MULTIPLIER_WORDS = {
    "مليون": 1_000_000, "مليار": 1_000_000_000, "الف": 1_000, "ألف": 1_000,
    "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
    "k": 1_000, "thousand": 1_000,
}
_SMALL_ARABIC_NUMBERS = {
    "نص": 0.5, "نصف": 0.5, "ربع": 0.25,
    "واحد": 1, "اثنين": 2, "اتنين": 2, "تلاتة": 3, "ثلاثة": 3, "اربعة": 4,
    "أربعة": 4, "خمسة": 5, "ستة": 6, "سبعة": 7, "تمانية": 8, "ثمانية": 8,
    "تسعة": 9, "عشرة": 10,
}

_NUM_SUFFIX_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*(?P<suf>مليون|مليار|الف|ألف|m|mn|million|k|thousand)?",
    re.IGNORECASE)


def parse_number(text: str) -> float | None:
    """"1,500,000 EGP" / "1.5M" / مليون ونصف -> 1500000.0. Returns None
    (never 0, never a guess) when nothing numeric is present -- a missing
    number must stay null, not become a fabricated 0."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = to_ascii_digits(str(text)).strip()
    if not t:
        return None
    t_clean = clean_arabic(t)

    # "X مليون ونص/ربع" -- word-multiplier plus a fractional word addend.
    m = re.search(
        r"(\d+(?:\.\d+)?)?\s*(مليون|الف|ألف)\s*(?:و)?\s*(نص|نصف|ربع)?", t_clean)
    if m and (m.group(1) or m.group(3)):
        base = float(m.group(1)) if m.group(1) else 1.0
        mult = _MULTIPLIER_WORDS.get(m.group(2), 1)
        value = base * mult
        if m.group(3):
            value += _SMALL_ARABIC_NUMBERS[m.group(3)] * mult
        return value

    # spelled-out small number + مليون/الف, e.g. "تلاتة مليون"
    for word, val in _SMALL_ARABIC_NUMBERS.items():
        m2 = re.search(rf"{word}\s*(مليون|الف|ألف)", t_clean)
        if m2:
            return val * _MULTIPLIER_WORDS[m2.group(1)]

    # digit + optional suffix multiplier: "1.5M", "250 الف", "1,500,000"
    t_nosep = t.replace(",", "")
    m3 = _NUM_SUFFIX_RE.search(t_nosep)
    if m3 and m3.group("num"):
        value = float(m3.group("num"))
        if m3.group("suf"):
            value *= _MULTIPLIER_WORDS.get(m3.group("suf").lower(), 1)
        return value

    return None


def parse_percentage(text) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = to_ascii_digits(str(text))
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return float(m.group(1))
    return parse_number(text)


_YEAR_RE = re.compile(r"(20\d{2})")
_QUARTER_RE = re.compile(r"[Qq]\s*([1-4])|(?:ربع|الربع)\D{0,10}(الاول|الأول|الثاني|الثالث|الرابع|١|٢|٣|٤|[1-4])")
_QUARTER_WORD_TO_NUM = {"الاول": 1, "الأول": 1, "الثاني": 2, "الثالث": 3, "الرابع": 4}


def parse_delivery_date(text: str) -> str | None:
    """Year, or year-quarter (e.g. "2027" or "2027-Q1"). Never invents a
    year that isn't literally present in the text."""
    if not text:
        return None
    t = to_ascii_digits(text)
    year_m = _YEAR_RE.search(t)
    if not year_m:
        return None
    year = year_m.group(1)
    q_m = _QUARTER_RE.search(t)
    if q_m:
        q = q_m.group(1) or _QUARTER_WORD_TO_NUM.get(q_m.group(2), q_m.group(2))
        if q and str(q).isdigit():
            return f"{year}-Q{q}"
    return year


# --------------------------------------------------------------- enums

FINISHING_LEVELS = {
    "core & shell": ["core & shell", "core and shell", "على المحارة", "محارة", "كور اند شل"],
    "semi-finished": ["semi finished", "semi-finished", "نص تشطيب", "نصف تشطيب"],
    "fully finished": ["fully finished", "متشطب بالكامل", "تشطيب كامل", "فوق التشطيب", "تشطيب كامل بالكامل"],
    "super lux": ["super lux", "سوبر لوكس", "سوبرلوكس"],
    "furnished": ["furnished", "مفروش", "مفروشة"],
}

DELIVERY_STATUS = {
    "ready": ["ready", "استلام فوري", "جاهز للاستلام", "تم التسليم", "جاهزة للاستلام", "فوري"],
    "off-plan": ["off plan", "off-plan", "تحت الانشاء", "تحت الإنشاء", "تسليم 20", "على الخريطة"],
}

SALE_TYPES = {
    "primary": ["primary", "من المطور", "developer", "مباشر من الشركة"],
    "resale": ["resale", "إعادة بيع", "اعادة بيع", "من المالك"],
}

PAYMENT_TYPES = {
    "cash": ["cash", "كاش", "نقدا", "نقداً"],
    "installments": ["installment", "installments", "قسط", "أقساط", "اقساط", "تقسيط"],
}

INSTALLMENT_FREQUENCIES = {
    "monthly": ["monthly", "شهري", "شهريا", "شهرياً"],
    "quarterly": ["quarterly", "ربع سنوي", "كل 3 شهور", "كل ثلاث شهور"],
    "annual": ["annual", "yearly", "سنوي", "سنويا", "سنوياً"],
}


def _match_enum(text: str, table: dict) -> str | None:
    if not text:
        return None
    t = clean_arabic(str(text).lower())
    for canon, variants in table.items():
        for v in variants:
            if clean_arabic(v.lower()) in t:
                return canon
    return None


def canon_finishing_level(text): return _match_enum(text, FINISHING_LEVELS)
def canon_delivery_status(text): return _match_enum(text, DELIVERY_STATUS)
def canon_sale_type(text): return _match_enum(text, SALE_TYPES)
def canon_payment_type(text): return _match_enum(text, PAYMENT_TYPES)
def canon_installment_frequency(text): return _match_enum(text, INSTALLMENT_FREQUENCIES)


_FREQ_PER_YEAR = {"monthly": 12, "quarterly": 4, "annual": 1}


# ------------------------------------------------------------- derived

def derive_price_per_sqm(price, area_sqm):
    """null when area is unavailable -- not 0, not price itself. The brief
    calls hidden-area listings out explicitly as a trap."""
    if price is None or area_sqm in (None, 0):
        return None
    try:
        return round(float(price) / float(area_sqm), 2)
    except (TypeError, ZeroDivisionError):
        return None


def derive_total_installment_cost(down_payment_amount, installment_amount,
                                    installment_frequency, installment_years):
    """down_payment + (installment_amount x payments/year x years).
    null unless every one of the four inputs is present -- a partially
    described plan does not get a guessed completion."""
    if None in (down_payment_amount, installment_amount, installment_years):
        return None
    per_year = _FREQ_PER_YEAR.get(installment_frequency)
    if per_year is None:
        return None
    try:
        return round(
            float(down_payment_amount)
            + float(installment_amount) * per_year * float(installment_years), 2)
    except TypeError:
        return None
