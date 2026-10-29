from main.player.book import Book, chapter

PROLOGUE = [
    "When Zarathustra was thirty years old, he left his home. He went into the mountains.",
    "Thou great star! What would be thy happiness?",
]


def test_offsets_index_the_chapter_text():
    c = chapter(0, "Prologue", PROLOGUE)
    assert c.text == PROLOGUE[0] + "\n\n" + PROLOGUE[1]
    (a0, a1), (b0, b1) = c.paragraphs
    assert c.text[a0:a1] == PROLOGUE[0]
    assert c.text[b0:b1] == PROLOGUE[1]


def test_sentences_split_at_end_punctuation():
    c = chapter(0, "Prologue", PROLOGUE)
    assert [c.text[s:e] for s, e in c.sentences] == [
        "When Zarathustra was thirty years old, he left his home.",
        "He went into the mountains.",
        "Thou great star!",
        "What would be thy happiness?",
    ]


def test_closing_quotes_stay_with_their_sentence():
    c = chapter(0, "x", ["He said: “Go.” And he went."])
    assert [c.text[s:e] for s, e in c.sentences] == ["He said: “Go.”", "And he went."]


def test_a_position_maps_to_the_sentence_and_paragraph_around_it():
    c = chapter(0, "Prologue", PROLOGUE)
    mid = c.text.index("mountains")
    s, e = c.sentence_at(mid)
    assert c.text[s:e] == "He went into the mountains."
    assert c.paragraph_at(mid) == c.paragraphs[0]
    assert c.paragraph_index(c.text.index("star")) == 1


def test_out_of_range_positions_clamp_to_the_first_sentence():
    c = chapter(0, "Prologue", PROLOGUE)
    assert c.sentence_at(-5) == c.sentences[0]


def test_find_reports_chapter_and_offset_in_reading_order():
    book = Book("Z", [chapter(0, "a", PROLOGUE), chapter(1, "b", ["The star rose."])])
    hits = book.find("STAR", limit=10)
    assert hits == [(0, book[0].text.index("star")), (1, book[1].text.index("star"))]
    assert book.find("star", limit=1) == hits[:1]


def test_the_chapters_that_ship_with_the_repo_load():
    book = Book.load()
    assert book.title == "Thus Spake Zarathustra"
    assert [c.id for c in book.chapters] == list(range(len(book.chapters)))
    assert book[0].title == "Zarathustra’s Prologue"
    assert book[0].text.startswith("When Zarathustra was thirty years old")
