"""JLPT levels: exam structure, scoring and question-writing hints.

Shared by the web server (mock exams, scores) and the worker (prompts for
Claude). Question counts follow the current JLPT format; the official numbers
vary a little between sessions, so treat them as approximate. 発話表現 (the
listening task with pictures) is left out at every level.
"""
import unicodedata

LEVELS = ["N5", "N4", "N3", "N2", "N1"]
DEFAULT_LEVEL = "N3"
CATEGORIES = ["vocab", "grammar", "order", "textgrammar", "reading", "listening"]

# (category, subtype, sets in a full mock, sets in a half mock, items per set)
_V, _G, _O, _T, _R, _L = CATEGORIES
STRUCTURE = {
    "N5": [(_V, "漢字読み", 7, 4, 1), (_V, "表記", 5, 3, 1), (_V, "文脈規定", 6, 3, 1),
           (_V, "言い換え類義", 3, 2, 1),
           (_G, "文法形式の判断", 9, 5, 1), (_O, "文の組み立て", 4, 2, 1), (_T, "文章の文法", 1, 1, 4),
           (_R, "内容理解（短文）", 2, 1, 1), (_R, "内容理解（中文）", 1, 1, 2), (_R, "情報検索", 1, 1, 1),
           (_L, "課題理解", 7, 4, 1), (_L, "ポイント理解", 6, 3, 1), (_L, "即時応答", 6, 3, 1)],
    "N4": [(_V, "漢字読み", 7, 4, 1), (_V, "表記", 5, 3, 1), (_V, "文脈規定", 8, 4, 1),
           (_V, "言い換え類義", 4, 2, 1), (_V, "用法", 4, 2, 1),
           (_G, "文法形式の判断", 13, 7, 1), (_O, "文の組み立て", 4, 2, 1), (_T, "文章の文法", 1, 1, 4),
           (_R, "内容理解（短文）", 3, 2, 1), (_R, "内容理解（中文）", 1, 1, 3), (_R, "情報検索", 1, 1, 2),
           (_L, "課題理解", 8, 4, 1), (_L, "ポイント理解", 7, 4, 1), (_L, "即時応答", 8, 4, 1)],
    "N3": [(_V, "漢字読み", 8, 4, 1), (_V, "表記", 6, 3, 1), (_V, "文脈規定", 11, 6, 1),
           (_V, "言い換え類義", 5, 3, 1), (_V, "用法", 5, 3, 1),
           (_G, "文法形式の判断", 13, 7, 1), (_O, "文の組み立て", 5, 3, 1), (_T, "文章の文法", 1, 1, 5),
           (_R, "内容理解（短文）", 4, 2, 1), (_R, "内容理解（中文）", 2, 1, 3), (_R, "内容理解（長文）", 1, 1, 4),
           (_R, "情報検索", 1, 1, 2),
           (_L, "課題理解", 6, 3, 1), (_L, "ポイント理解", 6, 3, 1), (_L, "概要理解", 3, 2, 1),
           (_L, "即時応答", 9, 5, 1)],
    "N2": [(_V, "漢字読み", 5, 3, 1), (_V, "表記", 5, 3, 1), (_V, "語形成", 3, 2, 1), (_V, "文脈規定", 7, 4, 1),
           (_V, "言い換え類義", 5, 3, 1), (_V, "用法", 5, 3, 1),
           (_G, "文法形式の判断", 12, 6, 1), (_O, "文の組み立て", 5, 3, 1), (_T, "文章の文法", 1, 1, 5),
           (_R, "内容理解（短文）", 5, 3, 1), (_R, "内容理解（中文）", 3, 2, 3), (_R, "統合理解", 1, 1, 2),
           (_R, "主張理解（長文）", 1, 1, 3), (_R, "情報検索", 1, 1, 2),
           (_L, "課題理解", 5, 3, 1), (_L, "ポイント理解", 6, 3, 1), (_L, "概要理解", 5, 3, 1),
           (_L, "即時応答", 12, 6, 1), (_L, "統合理解", 3, 2, 1)],
    "N1": [(_V, "漢字読み", 6, 3, 1), (_V, "文脈規定", 7, 4, 1), (_V, "言い換え類義", 6, 3, 1),
           (_V, "用法", 6, 3, 1),
           (_G, "文法形式の判断", 10, 5, 1), (_O, "文の組み立て", 5, 3, 1), (_T, "文章の文法", 1, 1, 4),
           (_R, "内容理解（短文）", 4, 2, 1), (_R, "内容理解（中文）", 3, 2, 3), (_R, "内容理解（長文）", 1, 1, 3),
           (_R, "統合理解", 1, 1, 2), (_R, "主張理解（長文）", 1, 1, 4), (_R, "情報検索", 1, 1, 2),
           (_L, "課題理解", 6, 3, 1), (_L, "ポイント理解", 7, 4, 1), (_L, "概要理解", 6, 3, 1),
           (_L, "即時応答", 14, 7, 1), (_L, "統合理解", 3, 2, 1)],
}

# Scored sections (each 0–60, N4/N5 言語知識・読解 0–120) and official pass marks.
_KNOW = ["vocab", "grammar", "order", "textgrammar"]
SCORING = {
    lv: {"sections": {"言語知識": _KNOW, "読解": ["reading"], "聴解": ["listening"]},
         "max": {"言語知識": 60, "読解": 60, "聴解": 60}, "pass_total": total,
         "pass_section": {"言語知識": 19, "読解": 19, "聴解": 19}}
    for lv, total in (("N3", 95), ("N2", 90), ("N1", 100))
}
SCORING.update({
    lv: {"sections": {"言語知識・読解": _KNOW + ["reading"], "聴解": ["listening"]},
         "max": {"言語知識・読解": 120, "聴解": 60}, "pass_total": total,
         "pass_section": {"言語知識・読解": 38, "聴解": 19}}
    for lv, total in (("N5", 80), ("N4", 90))
})

# What each level covers (given to Claude with every batch).
SCOPE = {
    "N5": """JLPT N5: about 800 words and 100 kanji. Basic grammar: です/ます, adjectives, て-form, ない-form,
～たい, ～ている, ～てもいい/～てはいけない, existence (ある/いる), counters, simple particles, ～から/～まで,
～前に/～後で. Very simple, polite sentences about daily life (school, family, shopping, time, weather).
Write the way real N5 papers do: mostly kana with only N5 kanji.""",
    "N4": """JLPT N4: about 1,500 words and 300 kanji. Grammar such as potential and volitional forms,
conditionals (～ば/～たら/～と/～なら), giving and receiving (あげる/くれる/もらう), passive, causative,
～そうだ/～ようだ/～らしい (basic), ～ことができる, ～てしまう, ～ておく, simple honorifics. Everyday topics;
only N4/N5 kanji in the text.""",
    "N3": """JLPT N3: about 3,700 words and 650 kanji (N4 allowed as support; avoid N2+ unless it is the kind of
word N3 papers actually use). Grammar such as ～わけ/はず/べき, ～ように/ために, ～ばかり, ～ところ, ～うちに,
～おかげで/せいで, honorifics (尊敬語・謙譲語), passive/causative/causative-passive, ～ことにする/ことになる,
～っぽい, ～らしい/ようだ/そうだ. Everyday and work topics, news-like notices, simple essays.""",
    "N2": """JLPT N2: about 6,000 words and 1,000 kanji. Formal and written grammar such as ～にもかかわらず,
～ものの, ～に際して, ～をめぐって, ～に基づいて, ～わけにはいかない, ～ざるを得ない, ～かねない, ～つつある.
Newspaper-style articles, explanatory and opinion essays, business situations, abstract vocabulary.""",
    "N1": """JLPT N1: about 10,000 words and 2,000 kanji. Advanced and literary grammar such as ～をもって,
～ならでは, ～といえども, ～んばかりに, ～をよそに, ～にかたくない, ～ずにはおかない, ～たるもの.
Editorials, academic and philosophical essays, business and formal speech, idioms and fine nuance.""",
}

_LEN = {  # approximate passage lengths in characters, per level
    "短文": {"N5": "80–120", "N4": "100–200", "N3": "150–220", "N2": "200–250", "N1": "200–250"},
    "中文": {"N5": "200–300", "N4": "350–450", "N3": "350–450", "N2": "450–550", "N1": "450–550"},
    "長文": {"N3": "550–650", "N1": "900–1,100"},
    "主張": {"N2": "850–950", "N1": "950–1,100"},
    "統合": {"N2": "250–300 each", "N1": "300–400 each"},
    "情報": {"N5": "200–300", "N4": "300–400", "N3": "400–600", "N2": "600–700", "N1": "600–700"},
    "文章": {"N5": "200–300", "N4": "250–350", "N3": "300–450", "N2": "400–500", "N1": "500–600"},
    "会話": {"N5": "4–6", "N4": "5–8", "N3": "6–10", "N2": "8–12", "N1": "10–14"},
}


def subtype_desc(level, name, items):
    L = {k: v.get(level, "") for k, v in _LEN.items()}
    d = {
        "漢字読み": "a sentence with one kanji word in 【 】 (no furigana on it); 4 hiragana readings as options "
                "(distractors: wrong voicing, long/short vowels, small っ, on/kun mix-ups).",
        "表記": "a sentence with one word in hiragana in 【 】; 4 kanji spellings as options (no furigana on "
              "options; distractors: look-alike or same-reading kanji).",
        "語形成": "a sentence with （　） where a prefix or suffix must be chosen (e.g. 非～, 未～, 不～, ～的, ～化, "
               "～感, ～性, ～率, ～式); 4 options.",
        "文脈規定": "a sentence with （　）; 4 words, only one fits the context.",
        "言い換え類義": "a sentence with a word or phrase in 【 】; 4 options, pick the closest meaning.",
        "用法": "the question is just the target word; 4 sentences using it, only one uses it correctly.",
        "文法形式の判断": "a sentence (or short A:/B: exchange) with （　）; 4 grammar forms, only one fits.",
        "文の組み立て": "see the format rules above.",
        "文章の文法": f"a passage of {L['文章']} characters with {items} blanks.",
        "内容理解（短文）": f"{L['短文']} characters (notice, email, memo, short essay).",
        "内容理解（中文）": f"{L['中文']} characters (essay or explanation); items such as the reason (なぜ), "
                     "the meaning of a 【 】-marked phrase, and the author's opinion.",
        "内容理解（長文）": f"{L['長文']} characters (essay with a clear argument); items on reasons, what a "
                     "【 】-marked phrase or そう/それ refers to, details, and the author's main point.",
        "主張理解（長文）": f"one opinion essay of {L['主張']} characters; items on the author's claims and the "
                     "reasoning behind them, ending with the author's main point.",
        "情報検索": f"an information sheet ({L['情報']} characters with line breaks: notice, timetable, price "
                "list, event guide, rules) plus a short situation; items need searching and combining conditions.",
        "課題理解": f"N introduces the situation and asks what someone will do (e.g. まず何をしなければなりませんか), "
                f"a dialogue of {L['会話']} lines, then N repeats the question. 4 text options, audio_options false.",
        "ポイント理解": f"like 課題理解, but the question asks for a reason, feeling or specific point (なぜ, どうして, "
                  f"何が一番…); {L['会話']} lines. 4 text options, audio_options false.",
        "概要理解": f"N introduces the situation (no question yet), a monologue or dialogue of {L['会話']} lines, "
                "then N asks the gist question (speaker's opinion, topic, main point). 4 options, audio_options TRUE.",
        "即時応答": "ONE short line by M or F (no narrator); 3 options = possible replies, audio_options TRUE. "
                "Distractors: replies that misunderstand who does what (～てもらう/～てあげる), the tense, "
                "or answer a different question.",
    }
    return d.get(name, "")


def _integrated(category, level):
    """統合理解 exists in reading and listening (N2/N1) with different formats."""
    L = {k: v.get(level, "") for k, v in _LEN.items()}
    if category == "listening":
        return (f"(listening) N introduces the situation; a discussion of 3 people, or a short talk followed by "
                f"two people discussing it ({L['会話']} lines); then N asks which option someone chose or what "
                f"they agree on. 4 text options, audio_options false.")
    return (f"two short texts A and B ({L['統合']}) on the same topic with different views, both in `passage` "
            f"(label them Ａ and Ｂ); items compare the two views.")


def subtypes(level, category=None):
    """[(category, subtype, full, half, items)] for a level (optionally one category)."""
    return [s for s in STRUCTURE[level] if category is None or s[0] == category]


def subtype_lines(level, category):
    """Prompt lines: each subtype of the category with its share and description."""
    subs = subtypes(level, category)
    total = sum(full for _, _, full, _, _ in subs)
    lines = []
    for _, name, full, _, items in subs:
        desc = (_integrated(category, level) if name == "統合理解"
                else subtype_desc(level, name, items))
        share = round(100 * full / total)
        lines.append(f"- {name} (about {share}% of sets; {items} item{'s' if items > 1 else ''} per set): {desc}")
    return "\n".join(lines)


def _norm(s):
    return unicodedata.normalize("NFKC", s or "").replace(" ", "")


def canonical_subtype(level, category, subtype):
    """Map Claude's subtype text to the exact name used by the level structure
    (needed for mock exams); returns the text unchanged if nothing matches."""
    names = [name for _, name, *_ in subtypes(level, category)]
    if len(names) == 1:
        return names[0]
    n = _norm(subtype)
    for name in names:
        if _norm(name) == n:
            return name
    hits = [name for name in names if _norm(name) in n or n in _norm(name)]
    return max(hits, key=len) if len(hits) == 1 else subtype


def mock_plan(level, size):
    """Slots in exam order for a mock exam."""
    return [{"category": c, "subtype": name, "count": full if size == "full" else half, "items": items}
            for c, name, full, half, items in STRUCTURE[level]]


def mock_items(level, size):
    return sum(s["count"] * s["items"] for s in mock_plan(level, size))


def section_of(level, category):
    for sec, cats in SCORING[level]["sections"].items():
        if category in cats:
            return sec
