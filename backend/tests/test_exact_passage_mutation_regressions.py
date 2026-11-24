import hashlib

import pytest
from round_robin.research.fetcher import FetchRejected, RetrievedSource, exact_passages


def source(text):
    return RetrievedSource('https://example.invalid/source', text, hashlib.sha256(text.encode()).hexdigest(),
                           '2026-11-17T12:00:00+00:00', 'synthetic_origin')


def test_passage_count_and_size_bounds_are_inclusive():
    value = source('x' * 25)
    assert len(exact_passages(value, maximum=1, size=1)) == 1
    assert len(exact_passages(value, maximum=24, size=1)) == 24
    assert len(exact_passages(source('x' * 2000), maximum=1, size=2000)[0]['text']) == 2000
    for maximum, size in [(0, 1), (25, 1), (1, 0), (1, 2001)]:
        with pytest.raises(FetchRejected):
            exact_passages(value, maximum=maximum, size=size)


def assert_exact_metadata(value, passages):
    intervals = []
    for item in passages:
        start, end = item['selected_start'], item['selected_end']
        assert 0 <= start < end <= len(value.text)
        assert item['text'] == value.text[start:end]
        assert item['sha256'] == hashlib.sha256(item['text'].encode()).hexdigest()
        assert item['source_content_hash'] == value.content_hash
        assert item['source_text_length'] == len(value.text)
        assert item['locator'] == f'extracted_text[{start}:{end}]'
        assert item['verification_status'] == 'not_checked'
        intervals.append((start, end))
    covered = set()
    for start, end in intervals:
        covered.update(range(start, end))
    assert all(item['uninspected_characters'] == len(value.text) - len(covered) for item in passages)
    assert intervals == sorted(intervals)
    assert len(set(intervals)) == len(intervals)


def test_complete_unicode_source_preserves_offsets_hashes_and_full_coverage():
    value = source('Café safety ✓ ' * 31)
    passages = exact_passages(value, maximum=5, size=100)
    assert_exact_metadata(value, passages)
    assert ''.join(item['text'] for item in passages) == value.text
    assert all(item['uninspected_characters'] == 0 for item in passages)


def test_incomplete_selection_discloses_exact_uninspected_source():
    value = source('Header and generic filler. ' * 100)
    passages = exact_passages(value, maximum=2, size=100)
    assert len(passages) == 2
    assert_exact_metadata(value, passages)
    assert all(item['uninspected_characters'] > 0 for item in passages)


def test_case_insensitive_exception_in_long_source_is_not_lost():
    value = source('Generic offer details. ' * 60 + ' INSTALLATION IS NOT INCLUDED. ' + 'Generic offer details. ' * 90)
    passages = exact_passages(value, maximum=1, size=120)
    assert len(passages) == 1
    assert 'NOT INCLUDED' in passages[0]['text']
    assert_exact_metadata(value, passages)


def test_requirement_terms_prioritize_relevant_middle_passage():
    value = source('General introduction. ' * 60 + ' WARRANTY lasts five years. ' + 'General introduction. ' * 90)
    passages = exact_passages(value, maximum=1, size=100, terms=['wArRaNtY'])
    assert len(passages) == 1
    assert 'WARRANTY' in passages[0]['text']
    assert_exact_metadata(value, passages)


def test_fitting_packet_does_not_drop_qualifier_across_chunk_boundary():
    qualifier = ' NOT INCLUDED '
    value = source('x' * 95 + qualifier + 'y' * (200 - 95 - len(qualifier)))
    passages = exact_passages(value, maximum=2, size=100)
    assert len(value.text) == 200
    assert ''.join(item['text'] for item in passages) == value.text
    assert_exact_metadata(value, passages)
    assert all(item['uninspected_characters'] == 0 for item in passages)


def test_unsampled_qualifier_is_anchored_in_large_source():
    value = source('x' * 145 + ' NOT INCLUDED ' + 'x' * 200000)
    passages = exact_passages(value, maximum=1, size=120)
    assert len(passages) == 1 and 'NOT INCLUDED' in passages[0]['text']
    assert_exact_metadata(value, passages)


def test_large_source_uses_bounded_candidate_windows(monkeypatch):
    from round_robin.research import fetcher

    value = source('Header and generic filler. ' * 100)
    original = fetcher.re.findall
    calls = []
    def counted(pattern, text, *args, **kwargs):
        if pattern == r'[\w-]{3,}':
            calls.append(1)
        return original(pattern, text, *args, **kwargs)
    monkeypatch.setattr(fetcher.re, 'findall', counted)
    passages = exact_passages(value, maximum=2, size=100)
    assert len(passages) == 2
    assert len(calls) <= 100
    assert_exact_metadata(value, passages)


@pytest.mark.parametrize('prefix_length', [95, 105])
def test_relevant_term_crossing_window_boundary_is_not_lost(prefix_length):
    value = source('x' * prefix_length + ' WARRANTY ' + 'x' * 600)
    passages = exact_passages(value, maximum=1, size=100, terms=['warranty'])
    assert len(passages) == 1 and 'WARRANTY' in passages[0]['text']
    assert_exact_metadata(value, passages)


def test_relevant_first_word_is_not_skipped():
    value = source('WARRANTY ' + 'x' * 600)
    passages = exact_passages(value, maximum=1, size=100, terms=['warranty'])
    assert len(passages) == 1 and passages[0]['text'].startswith('WARRANTY')
    assert_exact_metadata(value, passages)


def test_sampling_boundary_still_finds_relevant_term():
    prefix = 'x' * 145 + ' WARRANTY '
    value = source(prefix + 'x' * (163840 - len(prefix)))
    passages = exact_passages(value, maximum=1, size=100, terms=['warranty'])
    assert len(passages) == 1 and 'WARRANTY' in passages[0]['text']
    assert_exact_metadata(value, passages)


def test_sampling_above_window_limit_does_not_scan_every_window(monkeypatch):
    from round_robin.research import fetcher

    value = source('x' * 163841)
    original = fetcher.re.findall
    calls = []
    def counted(pattern, text, *args, **kwargs):
        if pattern == r'[\w-]{3,}':
            calls.append(1)
        return original(pattern, text, *args, **kwargs)
    monkeypatch.setattr(fetcher.re, 'findall', counted)
    assert len(exact_passages(value, maximum=1, size=100)) == 1
    assert len(calls) <= 1100


def test_last_qualifier_retains_a_full_bounded_window():
    value = source('x' * 2500 + ' NOT INCLUDED ')
    passages = exact_passages(value, maximum=1, size=100)
    assert len(passages) == 1
    assert 'NOT INCLUDED' in passages[0]['text'] and len(passages[0]['text']) == 100
    assert_exact_metadata(value, passages)


@pytest.mark.parametrize('prefix_length', [79, 82])
def test_selected_passages_do_not_repeat_most_of_a_window(prefix_length):
    value = source('x' * prefix_length + ' NOT INCLUDED ' + 'x' * 400)
    passages = exact_passages(value, maximum=2, size=100)
    assert len(passages) == 2
    first, second = passages
    overlap = max(0, min(first['selected_end'], second['selected_end']) - max(first['selected_start'], second['selected_start']))
    assert overlap < min(len(first['text']), len(second['text'])) / 2
    assert_exact_metadata(value, passages)


@pytest.mark.parametrize('prefix_length', [50, 700])
def test_ranked_passages_are_returned_in_source_order(prefix_length):
    value = source('x' * prefix_length + ' NOT INCLUDED ' + 'x' * 700)
    passages = exact_passages(value, maximum=2, size=100)
    assert len(passages) == 2 and any('NOT INCLUDED' in item['text'] for item in passages)
    assert_exact_metadata(value, passages)


def test_selected_passages_cover_distinct_relevant_terms():
    terms = ['alpha', 'bravo', 'charlie', 'delta']
    heads = ['charlie alpha ', 'delta ', 'charlie delta bravo ', 'charlie ', 'alpha charlie delta ', 'bravo ', ' ']
    value = source(''.join((head + 'z' * 80)[:80] for head in heads))
    passages = exact_passages(value, maximum=2, size=80, terms=terms)
    assert len(passages) == 2
    assert all(any(term in item['text'] for item in passages) for term in terms)
    assert_exact_metadata(value, passages)


def test_coverage_accumulates_across_multiple_passages():
    terms = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot']
    heads = [' ', ' ', 'delta foxtrot ', 'bravo ', 'foxtrot charlie bravo echo ',
             'bravo delta ', 'bravo ', 'charlie alpha ', ' ']
    value = source(''.join((head + 'z' * 80)[:80] for head in heads))
    passages = exact_passages(value, maximum=3, size=80, terms=terms)
    assert len(passages) == 3
    assert all(any(term in item['text'] for item in passages) for term in terms)
    assert_exact_metadata(value, passages)


@pytest.mark.parametrize('common_word', [
    'the', 'and', 'for', 'with', 'this', 'that',
    'must', 'should', 'from', 'have', 'into', 'will',
])
def test_common_requirement_words_do_not_bias_relevance_ties(common_word):
    # Both passages cover one meaningful requirement; the common word in the
    # later passage must not make it outrank the equally relevant first one.
    first = 'warranty '.ljust(100)
    middle = ' ' * 100
    later = f'battery {common_word} '.ljust(100)
    value = source(first + middle + later + middle)

    passages = exact_passages(
        value, maximum=1, size=100,
        terms=['warranty', 'battery', common_word.upper()],
    )

    assert len(passages) == 1
    assert passages[0]['selected_start'] == 0
    assert 'warranty' in passages[0]['text']
    assert_exact_metadata(value, passages)
