from contextlib import redirect_stdout
import io
from pathlib import Path
import ssl
import traceback
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from premwire.config import load_config
from premwire.cli import main
from premwire.collectors.http import get_text
from premwire.collectors.base import CollectorError
from premwire.collectors.x_rss import parse_x_rss
from premwire.models import Source
from premwire.runtime_paths import resolve_runtime_root
from premwire.telegram_api import TelegramBotApi, TelegramApiError
from premwire.vk_api import VkApiClient, VkTransportError

ROOT=Path(__file__).resolve().parents[1]

class PublicSecurityTests(unittest.TestCase):
    def test_defaults_are_inert(self):
        config=load_config(ROOT)
        self.assertTrue(config.publishing.dry_run)
        self.assertFalse(config.production.enabled)
        self.assertFalse(config.x_publishing.enabled)
        self.assertFalse(config.vk_publishing.enabled)
        self.assertFalse(config.dzen_digest.enabled)
        self.assertFalse(config.moderation.enabled)
        self.assertFalse(config.imagegen_worker.enabled)
        self.assertFalse(any(s.enabled for s in config.sources))

    def test_public_cli_has_no_live_confirm(self):
        with patch('sys.argv',['premwire','daily-digest','--confirm']), patch('sys.stderr',io.StringIO()):
            with self.assertRaises(SystemExit) as exc: main()
        self.assertEqual(exc.exception.code,2)

    def test_cli_status_is_offline(self):
        with patch('sys.argv',['premwire','--root',str(ROOT),'status']), patch('premwire.env.load_secrets',side_effect=AssertionError('no credential reads')), redirect_stdout(io.StringIO()):
            self.assertEqual(main(),0)

    def test_example_env_values_empty(self):
        for line in (ROOT/'.env.example').read_text().splitlines():
            if line and not line.startswith('#'):
                self.assertEqual(line.partition('=')[2],'')

    def test_runtime_does_not_attach_to_operator_profile(self):
        self.assertEqual(resolve_runtime_root({}), (Path.cwd()/'.runtime').resolve())

    def test_unsafe_source_schemes_rejected_before_network(self):
        for url in ['file:///tmp/secret','http://example.test','https://' + 'user:pass' + '@example.test']:
            with patch('premwire.collectors.http.urlopen') as network:
                with self.assertRaises(CollectorError): get_text(url)
                network.assert_not_called()

    def test_telegram_trace_does_not_echo_token(self):
        dummy='synthetic-sensitive-value'
        with patch('premwire.telegram_api.urlopen',side_effect=URLError(dummy)):
            try: TelegramBotApi(dummy).call('getMe')
            except TelegramApiError:
                self.assertNotIn(dummy,traceback.format_exc())
            else: self.fail('transport error expected')

    def test_vk_trace_does_not_echo_transport_parameters(self):
        dummy='synthetic-sensitive-value'
        client=VkApiClient(dummy,opener=lambda *a,**k: (_ for _ in ()).throw(URLError(dummy)))
        try: client.call('wall.get')
        except VkTransportError: self.assertNotIn(dummy,traceback.format_exc())
        else: self.fail('transport error expected')

    def test_telegram_media_errors_do_not_echo_sensitive_values(self):
        dummy='synthetic-sensitive-value'
        with tempfile.TemporaryDirectory() as folder:
            photo=Path(folder)/'synthetic.bin'
            photo.write_bytes(b'synthetic')
            client=TelegramBotApi(dummy)
            for operation in [lambda:client.send_photo('fake-channel',photo,'test'),
                              lambda:client.edit_message_media('fake-channel',1,photo,'test')]:
                with patch('premwire.telegram_api.urlopen',side_effect=URLError(dummy)):
                    try: operation()
                    except TelegramApiError: self.assertNotIn(dummy,traceback.format_exc())
                    else: self.fail('transport error expected')

    def test_telegram_http_error_body_is_not_echoed(self):
        dummy='synthetic-sensitive-value'
        error=HTTPError('https://example.test/'+dummy,401,dummy,{},io.BytesIO(b'{}'))
        with patch('premwire.telegram_api.urlopen',side_effect=error):
            try: TelegramBotApi(dummy).call('getMe')
            except TelegramApiError: self.assertNotIn(dummy,traceback.format_exc())
            else: self.fail('HTTP error expected')

    def test_xml_entities_rejected(self):
        payload='<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY secret SYSTEM "file:///tmp/private">]><rss><channel><item><title>&secret;</title></item></channel></rss>'
        with self.assertRaises(Exception):
            parse_x_rss(payload,Source('demo','x','Demo','demo','https://example.test'))

    def test_source_redirect_is_blocked_without_following(self):
        from premwire.collectors.http import _NoRedirect
        with self.assertRaises(CollectorError):
            _NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.test')

    def test_sql_identifier_is_quoted(self):
        import sqlite3
        from premwire.v2.migration import table_fingerprint
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE "odd""table" (value TEXT)')
            self.assertEqual(table_fingerprint(db, 'odd"table')['count'], 0)
            with self.assertRaises(sqlite3.OperationalError):
                table_fingerprint(db, 'missing; DROP TABLE items;--')

    def test_ollama_endpoint_refuses_private_file_and_remote_http(self):
        from dataclasses import replace
        from premwire.text_editor import OllamaTextEditor, EditorError
        config = load_config(ROOT).editor
        for address in ['file:///private', 'http://example.test', 'https://' + 'user:pass' + '@example.test']:
            with patch('premwire.text_editor.urlopen') as transport:
                with self.assertRaises(EditorError):
                    OllamaTextEditor(replace(config, base_url=address)).available_models()
                transport.assert_not_called()

if __name__=='__main__': unittest.main()
