import hashlib

import pytest
from round_robin.research.fetcher import (
    FetchAllowance,
    FetchRejected,
    FetchResponse,
    collect_source,
    exact_passages,
)


class FakeTransport:
    def __init__(self, body):
        self.body = body.encode()

    def get(self, target, **kwargs):
        return FetchResponse(200, {'content-type': 'application/xhtml+xml'}, self.body)


def collect(body):
    result = collect_source('candidate', FetchAllowance(registered_urls={'candidate': 'https://example.com/source'}),
                            transport=FakeTransport(body), resolver=lambda _: ['93.184.216.34'])
    assert result.content_hash == hashlib.sha256(result.text.encode()).hexdigest()
    assert ''.join(p['text'] for p in exact_passages(result, size=7)) == result.text
    return result.text


def test_xhtml_namespaces_cdata_and_punctuation_are_preserved_or_filtered():
    body = '''<h:html xmlns:h="http://www.w3.org/1999/xhtml" xmlns:s="http://www.w3.org/2000/svg" xmlns:n="urn:note"><h:body>
    <h:p>Offer <![CDATA[<exact> & valid]]>.</h:p><h:script><![CDATA[</script>HIDDEN]]></h:script>
    <s:svg><s:text>HIDDEN</s:text></s:svg><n:script>PUBLIC NOTE</n:script><n:svg>MORE PUBLIC</n:svg>
    <h:p>After &amp; beyond!</h:p></h:body></h:html>'''
    text = collect(body)
    assert 'Offer <exact> & valid.' in text and 'PUBLIC NOTE' in text and 'MORE PUBLIC' in text
    assert 'After & beyond!' in text and 'HIDDEN' not in text


@pytest.mark.parametrize('body', [
    '<html><p>incomplete',
    '<p>first</p><p>second</p>',
    '<!DOCTYPE html><html>private-sentinel</html>',
    '<!DOCTYPE html [<!ENTITY x "PRIVATE">]><html>&x;</html>',
    '<!DOCTYPE html [<!ENTITY x SYSTEM "file:///private-sentinel">]><html>&x;</html>',
])
def test_malformed_or_dtd_xhtml_fails_without_exposing_input(body):
    with pytest.raises(FetchRejected) as error:
        collect(body)
    assert 'private-sentinel' not in str(error.value)
    assert 'PRIVATE' not in str(error.value)


def test_xhtml_depth_limit_is_inclusive():
    assert collect('<div>' * 256 + 'visible' + '</div>' * 256) == 'visible'
    with pytest.raises(FetchRejected, match='structural limits'):
        collect('<div>' * 257 + 'visible' + '</div>' * 257)


def test_xhtml_node_limit_is_inclusive():
    assert collect('<html>' + '<b/>' * 49999 + 'visible</html>') == 'visible'
    with pytest.raises(FetchRejected, match='structural limits'):
        collect('<html>' + '<b/>' * 50000 + 'visible</html>')


def test_xhtml_element_names_are_case_sensitive():
    body = '<html xmlns="http://www.w3.org/1999/xhtml"><p>BEFORE</p><SCRIPT>PUBLIC UPPER</SCRIPT><STYLE>PUBLIC STYLE</STYLE><SVG>PUBLIC SVG</SVG><p>AFTER</p></html>'
    text = collect(body)
    assert all(value in text for value in ('BEFORE', 'PUBLIC UPPER', 'PUBLIC STYLE', 'PUBLIC SVG', 'AFTER'))


def test_xhtml_template_is_ignored_but_custom_namespace_template_is_not():
    body = '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:n="urn:note"><template>HIDDEN</template><n:template>PUBLIC</n:template></html>'
    assert collect(body) == 'PUBLIC'
