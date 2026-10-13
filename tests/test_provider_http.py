import io
import unittest
import urllib.error
from unittest.mock import patch, MagicMock
from aiusage.provider_http import request_json, NoRedirect


class TransportTests(unittest.TestCase):
    def test_only_official_https_recipients(self):
        for url in ['http://chatgpt.com/backend-api/wham/usage', 'https://chatgpt.com.evil.invalid/x',
                    'https://user:password@cursor.com/api', 'https://cursor.com:8443/api',
                    'https://127.0.0.1/api', 'https://cursor.com/api#fragment']:
            with self.subTest(url=url), patch('aiusage.provider_http.urllib.request.build_opener') as factory:
                with self.assertRaises(ValueError): request_json(url)
                factory.assert_not_called()

    def test_no_redirects_or_cookie_proxy_handlers(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'moved', {}, 'https://evil.invalid/'))
        response=MagicMock();response.__enter__.return_value=io.BytesIO(b'{"quota":1}')
        opener=MagicMock();opener.open.return_value=response
        with patch('aiusage.provider_http.urllib.request.build_opener',return_value=opener) as factory:
            self.assertEqual(request_json('https://cursor.com/api/usage',headers={'Authorization':'synthetic'},body={}),{'quota':1})
        handlers=factory.call_args.args
        self.assertEqual(handlers[0].proxies,{})
        self.assertIsInstance(handlers[1],NoRedirect)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_method(),'POST')
        self.assertEqual(request.data,b'{}')

    def test_size_and_json_shape_boundaries(self):
        for body, limit in [(b'{} ',2),(b'null',10),(b'"scalar"',20),(b'invalid',20)]:
            response=MagicMock();response.__enter__.return_value=io.BytesIO(body)
            opener=MagicMock();opener.open.return_value=response
            with patch('aiusage.provider_http.urllib.request.build_opener',return_value=opener):
                with self.assertRaises(ValueError):request_json('https://api.z.ai/api/monitor/usage/quota/limit',max_bytes=limit)

    def test_invalid_deadline_never_opens_connection(self):
        for timeout in [True,0,-1,float('nan'),float('inf'),61]:
            with self.assertRaises(ValueError):request_json('https://cursor.com/api',timeout=timeout)


if __name__=='__main__':unittest.main()
