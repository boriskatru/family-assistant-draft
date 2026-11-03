from unittest.mock import patch

import httpx
import httpcore
import pytest

from assistant.agent import external as e


@pytest.mark.parametrize('url',[
    'file:///etc/passwd','http://127.0.0.1/','http://10.0.0.2/',
    'http://169.254.169.254/latest/meta-data/','http://[::1]/',
    'http://[::ffff:127.0.0.1]/','http://192.168.1.1/',
    'https://localhost/','https://foo.internal/','https://x.example:8443/',
    'https://user:pass@example.com/','https://example.com\\@127.0.0.1/',
    'https://example.com/\nHost: secret','ftp://example.com/',
])
def test_fetch_rejects_unsafe_urls_before_network(url):
    with patch.object(e,'_resolve_public') as resolve, patch.object(e,'_pinned_get') as fetch:
        result=e.read_webpage(url)
    assert not result['ok']
    resolve.assert_not_called();fetch.assert_not_called()


def test_mixed_public_private_dns_is_rejected():
    answers=[(2,1,6,'',('93.184.216.34',443)),(2,1,6,'',('127.0.0.1',443))]
    with patch.object(e.socket,'getaddrinfo',return_value=answers),patch.object(e,'_pinned_get') as fetch:
        assert not e.read_webpage('https://example.com/')['ok']
    fetch.assert_not_called()


def test_redirect_to_metadata_is_not_followed():
    first={'status':302,'headers':{'location':'http://169.254.169.254/latest/'},'body':b''}
    with patch.object(e,'_resolve_public',return_value='93.184.216.34') as resolve,patch.object(e,'_pinned_get',return_value=first) as fetch:
        assert not e.read_webpage('https://example.com/')['ok']
    assert resolve.call_count==1 and fetch.call_count==1


def test_redirect_reresolves_dns_and_blocks_rebinding():
    answers=[[(2,1,6,'',('93.184.216.34',443))],[(2,1,6,'',('127.0.0.1',443))]]
    first={'status':302,'headers':{'location':'/again'},'body':b''}
    with patch.object(e.socket,'getaddrinfo',side_effect=answers),patch.object(e,'_pinned_get',return_value=first) as fetch:
        assert not e.read_webpage('https://example.com/')['ok']
    assert fetch.call_count==1


def test_fetch_connects_to_validated_ip_preserves_host_sni_and_disables_proxy():
    seen={}
    def handler(request):
        seen['request']=request
        return httpx.Response(200,headers={'content-type':'text/plain'},stream=httpx.ByteStream(b'Useful data'))
    real_client=httpx.Client
    def make_client(**kwargs):
        seen['settings']=kwargs
        kwargs.pop('transport',None).close()
        return real_client(transport=httpx.MockTransport(handler),**kwargs)
    with patch.object(e.httpx,'Client',side_effect=make_client):
        result=e._pinned_get('https://example.com/article?q=x','example.com',443,'93.184.216.34')
    assert result['body']==b'Useful data'
    assert seen['request'].url.host=='93.184.216.34'
    assert seen['request'].headers['host']=='example.com'
    assert seen['request'].extensions['sni_hostname']=='example.com'
    assert seen['settings']['trust_env'] is False
    assert seen['settings']['follow_redirects'] is False


def test_fetch_caps_stream_even_without_content_length():
    class Huge(httpx.SyncByteStream):
        def __iter__(self):
            yield b'a'*(e.MAX_PAGE_BYTES//2)
            yield b'b'*e.MAX_PAGE_BYTES
    real_client=httpx.Client
    def make_client(**kwargs):
        kwargs.pop('transport',None).close()
        return real_client(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Huge())),**kwargs)
    with patch.object(e.httpx,'Client',side_effect=make_client),pytest.raises(ValueError):
        e._pinned_get('https://example.com/','example.com',443,'93.184.216.34')


def test_extract_html_marks_untrusted_strips_scripts_and_keeps_source():
    body=b'<title>Opening hours</title><script>leak secrets()</script><h1>Museum</h1><p>10-18</p><a href="/tickets">Tickets</a><a href="http://127.0.0.1/">bad</a>'
    response={'status':200,'headers':{'content-type':'text/html; charset=utf-8'},'body':body}
    with patch.object(e,'_resolve_public',return_value='93.184.216.34'),patch.object(e,'_pinned_get',return_value=response):
        result=e.read_webpage('https://example.com/')
    assert result['ok'] and result['untrusted_data']
    assert '10-18' in result['text'] and 'leak secrets' not in result['text']
    assert result['sources'][0]['url']=='https://example.com/'
    assert result['links']==['https://example.com/tickets']
    assert result['retrieved_at']


def test_no_provider_key_is_actionable(settings):
    settings.LLM_API_KEY='';settings.LLM_MODEL='';settings.BRAVE_SEARCH_API_KEY='';settings.GOOGLE_MAPS_API_KEY=''
    assert e.search_web('Museum')['error_code']=='not_configured'
    assert e.search_places('Museum')['error_code']=='not_configured'
    assert e.route_between('A','B')['error_code']=='not_configured'


def test_brave_search_uses_key_header_and_returns_real_urls(settings):
    settings.BRAVE_SEARCH_API_KEY='test-search-key'
    response={'web':{'results':[{'url':'https://example.com/museum','title':'Museum','description':'Hours 10–18'}, {'url':'http://127.0.0.1/','title':'bad'}]}}
    with patch.object(e,'_json_request',return_value=response) as request:
        result=e.search_web('Museum hours')
    assert result['ok'] and result['provider']=='brave'
    assert len(result['results'])==1
    assert request.call_args.kwargs['headers']['X-Subscription-Token']=='test-search-key'
    assert 'test-search-key' not in str(result)


def test_native_web_search_requires_actual_evidence(settings):
    settings.BRAVE_SEARCH_API_KEY='';settings.LLM_API_KEY='test-model-key';settings.LLM_MODEL='test-model';settings.LLM_BASE_URL='https://api.openai.com/v1'
    unsupported={'output':[{'type':'message','content':[{'type':'output_text','text':'Probably 10–18','annotations':[]}]}]}
    with patch.object(e,'_json_request',return_value=unsupported):
        result=e.search_web('Museum hours')
    assert not result['ok'] and result['error_code']=='no_evidence'
    evidence={'usage':{'input_tokens':120,'output_tokens':80,'total_tokens':200},'output':[{'type':'web_search_call','action':{'sources':[{'url':'https://example.com/hours','title':'Official hours'}]}}, {'type':'message','content':[{'type':'output_text','text':'10–18','annotations':[{'type':'url_citation','url':'https://example.com/hours','title':'Official hours'}]}]}]}
    with patch.object(e,'_json_request',return_value=evidence) as request:
        result=e.search_web('Museum hours')
    assert result['ok'] and result['text']=='10–18' and len(result['sources'])==1
    assert request.call_args.kwargs['json']['tool_choice']=='required'
    assert request.call_args.kwargs['json']['store'] is False
    assert result['usage']['total_tokens']==200


def test_provider_errors_do_not_echo_keys(settings):
    settings.BRAVE_SEARCH_API_KEY='very-private-key'
    with patch.object(e,'_json_request',side_effect=httpx.ConnectError('very-private-key')):
        result=e.search_web('Museum')
    assert not result['ok'] and 'very-private-key' not in str(result)


def test_place_search_uses_new_api_mask_and_attribution(settings):
    settings.GOOGLE_MAPS_API_KEY='test-maps'
    data={'places':[{'id':'place123','displayName':{'text':'Museum'},'formattedAddress':'Tokyo','googleMapsUri':'https://maps.google.com/?cid=123','regularOpeningHours':{'weekdayDescriptions':['Monday 10–18']}}]}
    with patch.object(e,'_json_request',return_value=data) as request:
        result=e.search_places('Museum Tokyo')
    assert result['ok'] and result['attribution']=='Google Maps'
    assert result['results'][0]['opening_hours']==['Monday 10–18']
    assert request.call_args.args[1]=='https://places.googleapis.com/v1/places:searchText'
    assert 'places.location' in request.call_args.kwargs['headers']['X-Goog-FieldMask']


def test_route_keeps_estimate_and_transit_request_distinct(settings):
    settings.GOOGLE_MAPS_API_KEY='test-maps'
    data={'routes':[{'duration':'1801s','distanceMeters':12000,'warnings':['Tolls']}],'fallbackInfo':{'reason':'LATENCY_EXCEEDED'}}
    with patch.object(e,'_json_request',return_value=data) as request:
        result=e.route_between('Tokyo station','Museum','TRANSIT','2027-01-01T09:00:00+09:00')
    assert result['ok'] and result['estimate'] and result['routes'][0]['duration_minutes']==31
    assert 'routingPreference' not in request.call_args.kwargs['json']
    assert request.call_args.kwargs['json']['departureTime']=='2027-01-01T00:00:00Z'
    assert result['fallback']['reason']=='LATENCY_EXCEEDED'
    with patch.object(e,'_json_request',return_value=data) as request:
        e.route_between('A','B','DRIVE')
    assert request.call_args.kwargs['json']['routingPreference']=='TRAFFIC_AWARE'


def test_no_route_is_not_a_fabricated_duration(settings):
    settings.GOOGLE_MAPS_API_KEY='test-maps'
    with patch.object(e,'_json_request',return_value={}):
        result=e.route_between('A','B')
    assert not result['ok'] and result['error_code']=='no_route'
    assert 'routes' not in result


def test_stream_deadline_applies_to_each_socket_read_before_headers_complete():
    from unittest.mock import Mock
    stream=Mock();stream.read.return_value=b'x'
    wrapper=e._DeadlineStream(stream,deadline=110)
    with patch.object(e.time,'monotonic',side_effect=[100,109,111]):
        assert wrapper.read(100,timeout=20)==b'x'
        assert wrapper.read(100,timeout=20)==b'x'
        with pytest.raises(TimeoutError):wrapper.read(100,timeout=20)
    assert [c.kwargs['timeout'] for c in stream.read.call_args_list]==[10,1]


def test_real_transport_adapter_reads_through_deadline_backend_without_network():
    backend=httpcore.MockBackend([b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nHello'])
    with patch.object(e.httpcore,'SyncBackend',return_value=backend):
        result=e._pinned_get('https://example.com/','example.com',443,'93.184.216.34')
    assert result['status']==200 and result['body']==b'Hello'


def test_redirects_share_one_total_deadline():
    response={'status':302,'headers':{'location':'/again'},'body':b''}
    with patch.object(e.time,'monotonic',side_effect=[100,100,126]),patch.object(e,'_resolve_public',return_value='93.184.216.34'),patch.object(e,'_pinned_get',return_value=response) as fetch:
        result=e.read_webpage('https://example.com/')
    assert not result['ok'] and fetch.call_count==1


def test_fetch_pdf_uses_bounded_subprocess_extractor():
    response={'status':200,'headers':{'content-type':'application/pdf'},'body':b'%PDF-test'}
    parsed={'pages':[{'page':1,'text':'Museum ticket'}],'truncated':False}
    with patch.object(e,'_resolve_public',return_value='93.184.216.34'),patch.object(e,'_pinned_get',return_value=response),patch('assistant.integrations.pdf_text.extract_pdf',return_value=parsed) as extract:
        result=e.read_webpage('https://example.com/ticket.pdf')
    assert result['ok'] and 'Museum ticket' in result['text']
    extract.assert_called_once_with(b'%PDF-test',max_pages=20,max_chars=20000,max_page_chars=20000)


def test_pdf_resource_failure_is_safe():
    from assistant.integrations.pdf_text import PDFExtractionError
    response={'status':200,'headers':{'content-type':'application/pdf'},'body':b'%PDF-test'}
    with patch.object(e,'_resolve_public',return_value='93.184.216.34'),patch.object(e,'_pinned_get',return_value=response),patch('assistant.integrations.pdf_text.extract_pdf',side_effect=PDFExtractionError('resource limit')):
        result=e.read_webpage('https://example.com/ticket.pdf')
    assert not result['ok'] and result['error_code']=='unreadable_pdf'
