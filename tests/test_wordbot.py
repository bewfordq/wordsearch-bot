"""Reading, solving and screen recognition on real screenshots (no phone needed).

    .venv/bin/python -m pytest tests
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wordbot.glyphs import GlyphReader  # noqa: E402
from wordbot.screens import classify  # noqa: E402
from wordbot.solver import placements, solve  # noqa: E402
from wordbot.vision import find_layout, read_puzzle, word_is_found  # noqa: E402

SCREENS = Path(__file__).resolve().parent / "screens"

MAGIC = ["RABBIT", "VANISH", "SPIRIT", "HWOHSP", "WCURSE", "TKNARP"]
MAGIC_WORDS = ["CURSE", "PRANK", "RABBIT", "SHOW", "SPIRIT", "VANISH"]


def img(name: str):
    im = cv2.imread(str(SCREENS / name))
    assert im is not None, name
    return im


@pytest.fixture()
def fresh_reader(tmp_path):
    """No templates: every letter goes through Tesseract."""
    return GlyphReader(tmp_path / "templates")


# --- solver ------------------------------------------------------------------------------

def test_solver_all_directions():
    grid = [list(r) for r in MAGIC]
    found, missing = solve(grid, MAGIC_WORDS)
    assert not missing
    assert found["RABBIT"].start == (0, 0) and found["RABBIT"].end == (0, 5)
    assert found["PRANK"].start == (5, 5) and found["PRANK"].end == (5, 1)   # backwards
    assert found["SHOW"].start == (3, 4) and found["SHOW"].end == (3, 1)


def test_solver_diagonal_and_vertical():
    grid = [list(r) for r in ["CRAYON", "GYDIGR", "RLUCOE", "YTAELT", "TTRSDT", "SNOWSU", "LNORIB"]]
    found, missing = solve(grid, ["BUTTER", "GLASS", "GOLD", "ICE", "IRON", "SNOW", "CRAYON"])
    assert not missing
    assert (found["GLASS"].start, found["GLASS"].end) == ((1, 0), (5, 4))   # diagonal
    assert (found["BUTTER"].start, found["BUTTER"].end) == ((6, 5), (1, 5))  # upwards


def test_solver_fuzzy_fixes_one_misread():
    grid = [list(r) for r in MAGIC]
    grid[2][3] = "B"                       # SPIRIT's R misread as B
    found, missing = solve(grid, ["SPIRIT"], fuzzy=1)
    assert not missing and found["SPIRIT"].mismatches == 1
    found, missing = solve(grid, ["SPIRIT"], fuzzy=0)
    assert missing == ["SPIRIT"]


def test_solver_short_words_never_fuzzy():
    grid = [list(r) for r in MAGIC]
    assert solve(grid, ["XYZ"], fuzzy=1)[1] == ["XYZ"]


def test_single_letter_placements_cover_every_cell():
    grid = [list(r) for r in MAGIC]
    assert len(placements(grid, "R")) == sum(r.count("R") for r in MAGIC)


# --- reading -----------------------------------------------------------------------------

def test_read_fresh_puzzle(fresh_reader):
    p = read_puzzle(img("puzzle_magic.png"), fresh_reader)
    assert p.title == "MAGIC"
    assert p.grid.text() == MAGIC
    assert [w.text for w in p.words] == MAGIC_WORDS
    assert not any(w.found for w in p.words)


def test_read_half_solved_puzzle_knows_found_words(fresh_reader):
    p = read_puzzle(img("puzzle_magic_half.png"), fresh_reader)
    assert p.grid.text() == MAGIC                      # letters on highlight capsules still read
    found = {w.text for w in p.words if w.found}
    assert found == {"PRANK", "RABBIT", "SHOW"}


def test_read_non_square_grid(fresh_reader):
    p = read_puzzle(img("puzzle_melt_partial.png"), fresh_reader)
    assert (p.grid.rows, p.grid.cols) == (7, 6)
    assert {w.text for w in p.words if not w.found} == {"GLASS", "SNOW"}


def test_read_burst_hint(fresh_reader):
    """Hinted first letters are white on coloured circles; hinted words sit in coloured pills."""
    p = read_puzzle(img("puzzle_burst_hint.png"), fresh_reader)
    assert p.grid.text() == ["KPARKG", "WLELHA", "BIAFCR", "POOWND", "KROREE", "KIAKBN"]
    assert [w.text for w in p.words] == ["BENCH", "BOOK", "FORK", "GARDEN", "PARK", "PIE", "WALK"]
    assert not any(w.found for w in p.words)           # a pill is not "found"
    assert not solve(p.grid.letters, [w.text for w in p.words])[1]


def test_templates_learned_then_used(tmp_path):
    reader = GlyphReader(tmp_path / "t")
    p = read_puzzle(img("puzzle_magic.png"), reader)
    found, _ = solve(p.grid.letters, [w.text for w in p.words])
    for word, pl in found.items():
        for (r, c), ch in zip(pl.cells, word):
            reader.learn(p.grid.glyphs[r][c], ch)
    calls = reader.tesseract_calls
    again = read_puzzle(img("puzzle_magic.png"), reader, with_title=False)
    assert again.grid.text() == MAGIC
    assert reader.tesseract_calls == calls             # every letter came from a template
    assert all(s == "template" for row in again.grid.source for s in row)


def test_word_found_check(fresh_reader):
    im = img("puzzle_magic_half.png")
    p = read_puzzle(im, fresh_reader)
    states = {w.text: word_is_found(im, w.box) for w in p.words}
    assert states["RABBIT"] and not states["CURSE"]


# --- screens -----------------------------------------------------------------------------

@pytest.mark.parametrize("name,kind,detail", [
    ("puzzle_magic.png", "puzzle", ""),
    ("puzzle_burst_hint.png", "puzzle", ""),
    ("next_level.png", "button", "NEXT LEVEL"),
    ("daily_wisdom_continue.png", "button", "CONTINUE"),
    ("achievement.png", "button", "SEE ALL BADGES"),
    ("tutorial_up.png", "tutorial", "tutorial arrow"),
    ("tutorial_down.png", "tutorial", "tutorial arrow"),
    ("level_complete_anim.png", "unknown", ""),
])
def test_classify(name, kind, detail):
    s = classify(img(name))
    assert s.kind == kind
    assert s.detail == detail


def test_tutorial_targets():
    assert classify(img("tutorial_up.png")).target == (540, 1347)      # the Daily Wisdom card above
    assert classify(img("tutorial_down.png")).target == (621, 1937)    # the Burst Hint button below
    assert classify(img("tutorial_left.png")).target == (132, 567)     # the River Race trophy to the left


def test_flying_word_frame_is_not_read_as_puzzle():
    assert find_layout(img("puzzle_word_flying.png")) is None


# --- Picture Puzzle ----------------------------------------------------------------------

SKY = ["ETIKQW", "PJSUNO", "LMKZDB", "AVOUNN", "NFOOEI", "ELVZNA", "CRATSR"]
SKY_ANSWERS = {"CLOUD", "MOON", "RAINBOW", "PLANE", "STAR", "KITE", "SUN"}


def test_picture_puzzle_read(fresh_reader):
    p = read_puzzle(img("picture_puzzle.png"), fresh_reader)
    assert p.kind == "pictures" and len(p.tiles) == 7
    assert p.title == "IN THE SKY"
    assert p.grid.text() == SKY


def test_picture_intro_popup_is_not_a_puzzle():
    im = img("picture_intro_popup.png")
    assert find_layout(im) is None
    s = classify(im)
    assert s.kind == "popup" and s.target == (1008, 857)


def test_dictionary_ranks_picture_answers_high():
    from wordbot.dictionary import Dictionary
    cands = Dictionary().candidates([list(r) for r in SKY])
    top = {c.word for c in cands[:20]}
    assert SKY_ANSWERS <= top


def test_capsule_check(fresh_reader):
    from wordbot.vision import line_marked
    before, sun, wrong = img("picture_puzzle.png"), img("picture_sun_found.png"), img("picture_wrong_swipe.png")
    g = read_puzzle(before, fresh_reader, with_title=False).grid
    at = lambda cells: [g.centers[r][c] for r, c in cells]
    assert line_marked(before, sun, at([(1, 2), (1, 3), (1, 4)]))            # SUN accepted
    assert not line_marked(sun, wrong, at([(5, 1), (5, 2), (5, 3)]))         # LVZ refused
    assert not line_marked(before, sun, at([(6, 4), (6, 3), (6, 2), (6, 1)]))  # STAR not swiped yet


def test_faded_picture(fresh_reader):
    from wordbot.vision import tile_vividness
    p = read_puzzle(img("picture_puzzle.png"), fresh_reader, with_title=False)
    after = img("picture_wrong_swipe.png")
    viv_before = [tile_vividness(img("picture_puzzle.png"), t) for t in p.tiles]
    viv_after = [tile_vividness(after, t) for t in p.tiles]
    assert min(viv_before) > 0.04
    assert viv_after[6] < 0.04                                                # the sun faded


# --- quotes, bonus words -----------------------------------------------------------------

def test_quote_blanks(fresh_reader):
    p = read_puzzle(img("quote_puzzle.png"), fresh_reader, with_title=False)
    assert p.kind == "quote"
    assert p.blanks == [4, 6, 4, 5, 4, 4]              # ONLY, HEALTH, WANT, DRINK, LIKE, WHAT


def test_bonus_words_popup_claims():
    s = classify(img("bonus_words_popup.png"))
    assert s.kind == "button" and s.detail == "CLAIM"


def test_word_inside_found_word_is_skipped(fresh_reader):
    from wordbot.vision import line_covered
    im = img("picture_bonus_toast.png")
    g = read_puzzle(img("picture_puzzle.png"), fresh_reader, with_title=False).grid  # same board position
    # (the "Claim bonus word reward" toast covers row 5 in this shot, so test U-S-K inside HUSKY)
    assert line_covered(im, [g.centers[r][4] for r in (2, 3, 4)])
    assert not line_covered(im, [g.centers[1][c] for c in (3, 2, 1)])      # PUG is still free


def test_hidden_words_and_race_tokens(fresh_reader):
    p = read_puzzle(img("hidden_words.png"), fresh_reader, with_title=False)
    assert [w.text for w in p.words] == ["ALBATROSS", "CHICKEN", "EAGLE", "OWL", "SPARROW", "SWAN", "TURKEY"]
    assert p.hidden == [4, 4, 6, 6]


def test_hidden_word_tutorial_taps_got_it():
    s = classify(img("hidden_word_tutorial.png"))
    assert s.kind == "button" and s.detail == "GOT IT"


def test_event_page_is_not_a_tutorial():
    assert classify(img("river_race.png")).kind != "tutorial"


def test_coins_next_to_a_race_token(fresh_reader):
    p = read_puzzle(img("hidden_words_token_touching.png"), fresh_reader)
    assert p.title == "FRUITS"                         # not "0 FRUITS" (race counter)
    assert p.hidden == [6, 4, 6]                       # BANANA, PLUM, TOMATO


def test_race_token_touching_words(fresh_reader):
    p = read_puzzle(img("race_tokens_touching.png"), fresh_reader, with_title=False)
    words = [w.text for w in p.words]
    assert "CHARGER" in words and "MEMORY" in words and "RINGTONE" in words


def test_board_visible(fresh_reader):
    from wordbot.vision import board_visible
    p = read_puzzle(img("picture_puzzle.png"), fresh_reader, with_title=False)
    assert board_visible(img("picture_puzzle.png"), p.grid)
    assert not board_visible(img("picture_bonus_toast.png"), p.grid)     # toast over rows 5-6


def test_hint_button():
    from wordbot.screens import hint_button
    assert hint_button(img("puzzle_magic.png")) == (782, 1921)
    assert hint_button(img("picture_puzzle.png")) is not None
    assert hint_button(img("next_level.png")) is None


def test_opposite_words_are_found_by_searching(fresh_reader):
    """'Find the opposite words!': the listed words aren't in the grid; their opposites are."""
    from wordbot.dictionary import Dictionary
    p = read_puzzle(img("opposite_words.png"), fresh_reader, with_title=False)
    found, missing = solve(p.grid.letters, [w.text for w in p.words if w.text])
    assert len(missing) > len(p.words) / 2
    top = {c.word for c in Dictionary().candidates(p.grid.letters)[:25]}
    assert {"HAPPY", "LIGHT", "CLEAN", "FAST", "SWEET"} <= top


def test_sequence_level(fresh_reader):
    """'Solve the sequence!': SEED → ______ → ____ → ____ → ____ → ___ (some underscores run together)."""
    p = read_puzzle(img("sequence_puzzle.png"), fresh_reader)
    assert p.kind == "quote" and p.title == "SOLVE THE SEQUENCE"
    assert p.blanks == [6, 4, 4, 4, 3]                 # SPROUT, ROOT, STEM, LEAF, BUD
    assert [w.text for w in p.words] == ["SEED"]       # the arrows aren't words


def test_quote_words_are_read_in_mixed_case(fresh_reader):
    p = read_puzzle(img("quote_puzzle.png"), fresh_reader, with_title=False)
    assert {"WAY", "KEEP", "YOUR", "WHAT", "RATHER"} <= {w.text for w in p.words}
