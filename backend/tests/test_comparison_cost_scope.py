"""Exports preserve the saved cost scope and distinguish unknowns from zero."""
import re
from copy import deepcopy
from html import unescape
from io import BytesIO

import pytest
from pypdf import PdfReader
from round_robin.reports.exports import report_markdown, report_pdf
from round_robin.schemas.runtime import RuntimeDecisionReport

pytest_plugins = ('backend.tests.test_artifacts_api',)


def compact(value):
    return ''.join(str(value).split())


@pytest.mark.parametrize('format', ['markdown', 'pdf'])
def test_saved_comparison_costs_keep_currency_unknowns_and_option_caveats(artifacts, format):
    _, _, _, _, saved = artifacts
    report = deepcopy(saved)
    unknown, zero = report['comparison'][:2]
    unknown.update(total=None, known_subtotal=None, currency='EUR', missing_components=['Installation and tax'], caveat='Option A quote excludes installation and tax; scope must be checked.')
    zero.update(total='0.00', known_subtotal='0.00', currency='EUR', missing_components=[], caveat='Option B zero is only the supplied trial period, not later years.')
    for option in (unknown, zero):
        calculation = next(item for item in report['calculations'] if item['id'] == option['calculation_id'])
        calculation.update(output=option['total'], known_subtotal=option['known_subtotal'], currency='EUR', unit='EUR', missing_components=option['missing_components'])
    report = RuntimeDecisionReport.model_validate(report).model_dump(mode='json')
    original = deepcopy(report)
    if format == 'markdown':
        text = report_markdown(report, title='Saved comparison', sources=[])
        text = unescape(re.sub(r'\\([\\`*_{}\[\]()#+!|\-.])', r'\1', text))
        assert compact('All-in total: Unknown EUR over 12 months.') in compact(text)
        assert compact('Known subtotal: Unknown EUR. Missing: Installation and tax.') in compact(text)
        assert compact('All-in total: 0.00 EUR over 12 months.') in compact(text)
        assert compact('Known subtotal: 0.00 EUR. Missing: none recorded.') in compact(text)
    else:
        doc = PdfReader(BytesIO(report_pdf(report, title='Saved comparison', sources=[])))
        text = '\n'.join(page.extract_text() for page in doc.pages)
        comparison = text.split('Comparable costs and hard requirements', 1)[1].split('Option cost caveats', 1)[0]
        assert compact(unknown['label']) in compact(comparison)
        assert compact(zero['label']) in compact(comparison)
        assert compact('Unknown EUR 12 months Known subtotal Unknown EUR Missing: 1 component; see details below') in compact(comparison)
        assert compact('0.00 EUR 12 months Known subtotal 0.00 EUR Missing: none recorded') in compact(comparison)
    assert compact('Installation and tax') in compact(text)
    if format == 'markdown':
        section = text.split('## Option comparison', 1)[1].split('## Decisive tradeoff', 1)[0]
        first = section.split('### ' + unknown['label'], 1)[1].split('### ' + zero['label'], 1)[0]
        second = section.split('### ' + zero['label'], 1)[1]
    else:
        section = text.split('Option cost caveats', 1)[1].split('Requirement assessment provenance', 1)[0]
        first = section.split('Option: ' + unknown['label'], 1)[1].split('Option: ' + zero['label'], 1)[0]
        second = section.split('Option: ' + zero['label'], 1)[1]
    assert compact(unknown['caveat']) in compact(first)
    assert compact(zero['caveat']) in compact(second)
    assert report == original


def test_pdf_preserves_large_valid_cost_gaps_and_caveat_through_page_breaks(artifacts):
    _, _, _, _, saved = artifacts
    report = deepcopy(saved)
    option = report['comparison'][0]
    missing = [f"gap_{index:02d}_" + 'x' * 70 for index in range(35)]
    caveat = 'START_SCOPE ' + 'carefully scoped source statement ' * 12 + 'END_SCOPE'
    option.update(total=None, missing_components=missing, caveat=caveat)
    calculation = next(item for item in report['calculations'] if item['id'] == option['calculation_id'])
    calculation.update(output=None, missing_components=missing)
    report = RuntimeDecisionReport.model_validate(report).model_dump(mode='json')
    doc = PdfReader(BytesIO(report_pdf(report, title='Long saved comparison', sources=[])))
    parts = []
    for number, page in enumerate(doc.pages, 1):
        page_text = page.extract_text()
        page_text = page_text.replace(f"Round Robin | v{report['version']} / cutoff {report['event_cutoff']}\nPage {number}\n", '')
        page_text = page_text.replace('Option\nAll-in total\nRequirement results\n', '')
        parts.append(page_text)
    text = compact(''.join(parts))
    assert len(doc.pages) > 2
    assert all(item in text for item in missing), [(item[:7], text[text.find(item[:7]):text.find(item[:7])+140]) for item in missing if item not in text]
    assert 'START_SCOPE' in text and 'END_SCOPE' in text


@pytest.mark.parametrize('format', ['markdown', 'pdf'])
def test_partial_known_subtotal_is_not_mistaken_for_complete_total(artifacts, format):
    _, _, _, _, saved = artifacts
    report = deepcopy(saved)
    option = report['comparison'][0]
    option.update(total=None, known_subtotal='123.45', currency='EUR', missing_components=['Installation'])
    calculation = next(item for item in report['calculations'] if item['id'] == option['calculation_id'])
    calculation.update(output=None, known_subtotal='123.45', currency='EUR', unit='EUR', missing_components=option['missing_components'])
    report = RuntimeDecisionReport.model_validate(report).model_dump(mode='json')
    if format == 'markdown':
        raw = report_markdown(report, title='Partial cost', sources=[])
        text = unescape(re.sub(r'\\([\\`*_{}\[\]()#+!|\-.])', r'\1', raw))
        assert compact('All-in total: Unknown EUR over 12 months.') in compact(text)
        assert compact('Known subtotal: 123.45 EUR. Missing: Installation.') in compact(text)
    else:
        doc = PdfReader(BytesIO(report_pdf(report, title='Partial cost', sources=[])))
        text = ''.join(page.extract_text() for page in doc.pages)
        assert compact('Unknown EUR 12 months Known subtotal 123.45 EUR Missing: 1 component; see details below') in compact(text)
