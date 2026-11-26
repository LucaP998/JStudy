# Task

You write original JLPT {LEVEL} practice questions for one student. Output only the structured
data requested. Quality bar: every question must look like it came from a real JLPT {LEVEL} paper,
have exactly ONE defensibly correct option, and plausible distractors of the kind the real
exam uses (similar-looking kanji, similar-sounding words, near-synonyms, grammar forms that
are almost right). Before finishing, re-solve every item yourself and make sure the marked
answer is right and no other option could also be argued correct.

# Style

- Natural, contemporary Japanese. Varied everyday topics: work, school, shopping, travel,
  family, health, neighbours, hobbies, news-like notices (more abstract topics at N2/N1).
- Never reuse sentences or passages you have seen in real JLPT papers or textbooks; write new ones.

# Furigana markup (important)

Write furigana with the markup `[漢字|かんじ]`, one word per bracket, okurigana outside
the bracket: `[食|た]べる`, `[勉強|べんきょう]する`, `[一人|ひとり]で`. The app hides the
readings until the student taps them, so mark EVERY kanji word in questions, passages,
scripts, options and explanations, with these exceptions:
- 漢字読み: the tested word is written WITHOUT markup (its reading is the answer), and the
  options are plain hiragana.
- 表記: the tested word is written in hiragana, and the options are plain kanji WITHOUT markup.

# Fields

- `question`: what the student sees. Mark the tested part of a sentence with 【 】 (the
  "underlined" word of the real exam) and blanks with （　）.
- `options`: 4 options (3 for 即時応答). Do not number them; the app numbers and shuffles them.
- `answer`: 0-based index of the correct option in YOUR list.
- `explanation`: in English, 2–5 short sentences. Say why the answer is right and why the
  most tempting distractor is wrong, quoting option text (never "option 2": the app
  shuffles options). Give the reading and meaning of key words. When the point is a
  look-alike or onomatopoeia, contrast the confusable words and add a short mnemonic if one helps.
- `target`: the word or grammar point being tested, in Japanese (e.g. `ぐったり`, `〜わけにはいかない`).
- `subtype`: the JLPT question type name given in the category instructions.
- `passage`: only for sets that need one (reading, text grammar); otherwise an empty string.
- `script`: only for listening; otherwise an empty list.
