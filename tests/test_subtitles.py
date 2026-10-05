"""Exercise caption selection with player response shapes used by TV clients."""
import ast
import html
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'resources/lib/youtube_plugin/youtube/client/subtitles.py'
PLAYER = ROOT / 'resources/lib/youtube_plugin/youtube/client/player_client.py'
REQUEST = ROOT / 'resources/lib/youtube_plugin/youtube/client/request_client.py'


def load_subtitles():
    tree = ast.parse(SOURCE.read_text())
    body = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.Assign))]

    class RequestClient:
        def __init__(self, **kwargs):
            self._context = kwargs.get('context')

        @staticmethod
        def _normalize_url(url):
            return url

        @staticmethod
        def _unescape(value):
            return html.unescape(value)

    logger = SimpleNamespace(debug=lambda *a, **kw: None, error_trace=lambda *a, **kw: None)
    namespace = dict(YouTubeRequestClient=RequestClient,
                     logging=SimpleNamespace(getLogger=lambda *args: logger),
                     make_dirs=lambda *args: '/unused', TEMP_PATH='/unused',
                     TRANSLATION_LANGUAGES=[], PLAY_PROMPT_SUBTITLES='subtitles',
                     parse_qs=parse_qs, urlencode=urlencode, urlsplit=urlsplit, os=os)
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace['Subtitles'], namespace['SUBTITLE_SELECTIONS'], namespace


Subtitles, SELECTIONS, NAMESPACE = load_subtitles()


def caption(language, asr=False):
    track = {'languageCode': language, 'baseUrl': 'https://example.invalid/caption',
             'isTranslatable': True, 'name': {'simpleText': language}}
    if asr:
        track['kind'] = 'asr'
    return track


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        settings = SimpleNamespace(subtitle_download=lambda: False,
                                   get_subtitle_selection=lambda: SELECTIONS['preferred_fallback_asr'],
                                   stream_features=lambda: {'vtt'}, get_language=lambda: 'zh-CN',
                                   get_region=lambda: 'CN')
        context = SimpleNamespace(get_settings=lambda: settings,
                                  get_subtitle_language=lambda: None,
                                  get_system_version=lambda: SimpleNamespace(compatible=lambda *args: True),
                                  inputstream_adaptive_capabilities=lambda *args: True,
                                  get_ui=lambda: SimpleNamespace(pop_property=lambda *a, **kw: False))
        self.subtitles = Subtitles(context, 'fixture', use_mpd=True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.subtitles.BASE_PATH = self.temp.name
        self.requests = []

        class File:
            def __init__(self, path, mode):
                self.handle = open(path, 'wb' if mode == 'w' else 'r',
                                   **({} if mode == 'w' else {'encoding': 'utf-8'}))
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def read(self):
                return self.handle.read()
            def write(self, data):
                return self.handle.write(data)

        class Response:
            def __init__(self, text):
                self.text = text
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        self.response_text = 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\ncaption\n'
        def request(url, **kwargs):
            self.requests.append(url)
            return Response(self.response_text)
        self.subtitles.request = request
        NAMESPACE['xbmcvfs'] = SimpleNamespace(File=File, exists=os.path.exists)

    def load(self, tracks, **renderer):
        renderer['captionTracks'] = tracks
        self.subtitles.load({'playerCaptionsTracklistRenderer': renderer}, headers={})

    def test_caption_tracks_without_audio_metadata_enable_preferred_translation(self):
        self.load([caption('ja', asr=True)],
                  translationLanguages=[{'languageCode': 'zh-CN', 'languageName': {'simpleText': 'Chinese'}}])
        subtitles = self.subtitles.get_subtitles()
        self.assertEqual('ja', self.subtitles.get_lang_details()['original'])
        self.assertIn('zh-CN_translation', subtitles)
        self.assertTrue(subtitles['zh-CN_translation']['default'])
        self.assertEqual(['zh-CN'], parse_qs(urlsplit(subtitles['zh-CN_translation']['url']).query)['tlang'])

    def test_missing_default_audio_index_uses_first_audio_track(self):
        self.load([caption('en'), caption('ja')],
                  audioTracks=[{'hasDefaultTrack': True, 'defaultCaptionTrackIndex': 1}])
        self.assertEqual('ja', self.subtitles.get_lang_details()['default'])

    def test_existing_default_audio_and_caption_indices_are_preserved(self):
        self.load([caption('en'), caption('ja')], defaultAudioTrackIndex=1,
                  audioTracks=[{'captionTrackIndices': [0]}, {'captionTrackIndices': [1]}])
        self.assertEqual('ja', self.subtitles.get_lang_details()['default'])

    def test_empty_translation_sources_and_invalid_caption_index_use_available_track(self):
        self.load([caption('ja')], defaultTranslationSourceTrackIndices=[],
                  audioTracks=[{'hasDefaultTrack': True, 'defaultCaptionTrackIndex': 9}])
        self.assertEqual('ja', self.subtitles.get_lang_details()['default'])
        self.assertIsNotNone(self.subtitles.defaults['base'])

    def test_no_caption_tracks_has_no_subtitles(self):
        self.load([])
        self.assertEqual({}, self.subtitles.get_subtitles())

    def test_multilingual_asr_uses_the_selected_caption_language_as_translation_source(self):
        self.load([caption('ar', asr=True), caption('en', asr=True)],
                  defaultAudioTrackIndex=1,
                  audioTracks=[{'captionTrackIndices': [0]}, {'captionTrackIndices': [1]}])
        self.assertEqual('en', self.subtitles.get_lang_details()['original'])
        self.assertEqual('en', self.subtitles.defaults['base_lang'])

    def test_chinese_region_uses_supported_script_translation_tag(self):
        for preferred, supported in (('zh-CN', 'zh-Hans'), ('zh-TW', 'zh-Hant')):
            with self.subTest(preferred=preferred):
                self.subtitles.preferred_lang = (preferred,)
                self.load([caption('en', asr=True)], translationLanguages=[
                    {'languageCode': 'zh', 'languageName': {'simpleText': 'Chinese'}},
                    {'languageCode': supported, 'languageName': {'simpleText': 'Chinese'}}])
                selected = self.subtitles.get_subtitles()
                self.assertIn(supported+'_translation', selected)
                track = selected[supported+'_translation']
                self.assertTrue(track['default'])
                self.assertEqual([supported], parse_qs(urlsplit(track['url']).query)['tlang'])
                self.assertEqual(supported, self.subtitles.get_preferred(selected)['language'])

    def test_html_and_cueless_webvtt_are_not_saved_as_subtitles(self):
        self.subtitles.pre_download = True
        for body in ('<html><title>Sorry...</title></html>', 'WEBVTT\n\n'):
            self.response_text = body
            self.assertEqual((None, None), self.subtitles._get_url(caption('en', asr=True)))
            self.assertFalse(list(Path(self.temp.name).glob('*.vtt')))

    def test_invalid_cached_vtt_is_ignored_and_replaced_after_a_valid_download(self):
        self.subtitles.pre_download = True
        cached = Path(self.temp.name)/'fixture.asr.en.vtt'
        cached.write_text('<html><title>Sorry...</title></html>')
        path, _ = self.subtitles._get_url(caption('en', asr=True))
        self.assertEqual(str(cached), path)
        self.assertEqual(1, len(self.requests))
        self.assertTrue(cached.read_text().startswith('WEBVTT'))
        self.subtitles._get_url(caption('en', asr=True))
        self.assertEqual(1, len(self.requests))

    def test_invalid_chinese_translation_falls_back_to_valid_english_and_correct_filename(self):
        self.subtitles.pre_download = True
        self.subtitles.use_isa = False
        self.load([caption('ar', asr=True), caption('en', asr=True)],
                  defaultAudioTrackIndex=1,
                  audioTracks=[{'captionTrackIndices': [0]}, {'captionTrackIndices': [1]}],
                  translationLanguages=[{'languageCode': 'zh-Hans',
                                         'languageName': {'simpleText': 'Chinese'}}])
        request = self.subtitles.request
        def translated_request(url, **kwargs):
            self.response_text = ('<html><title>Sorry...</title></html>'
                                  if 'tlang=' in url else
                                  'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\ncaption\n')
            return request(url, **kwargs)
        self.subtitles.request = translated_request
        selected = self.subtitles.get_subtitles()
        self.assertEqual({'en_asr'}, set(selected))
        self.assertEqual({'language': 'en', 'filename': 'fixture.asr.en.vtt'},
                         self.subtitles.get_preferred(selected))
        self.assertEqual(2, len(self.requests))

    def test_legacy_tv_captions_are_used_without_an_extra_player_request(self):
        request_tree = ast.parse(REQUEST.read_text())
        clients = next(node.value for cls in request_tree.body if isinstance(cls, ast.ClassDef)
                       for node in cls.body if isinstance(node, ast.Assign)
                       and any(isinstance(name, ast.Name) and name.id == 'CLIENTS' for name in node.targets))
        legacy = next(ast.literal_eval(value) for key, value in zip(clients.keys, clients.values)
                      if isinstance(key, ast.Constant) and key.value == 'tv_legacy')
        self.assertIs(legacy.get('_use_subtitles'), True)
        legacy['headers'] = {}

        method = next(node for cls in ast.parse(PLAYER.read_text()).body if isinstance(cls, ast.ClassDef)
                      for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_process_captions')
        namespace = dict(SUBTITLE_SELECTIONS=SELECTIONS)
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        exec(compile(module, str(PLAYER), 'exec'), namespace)
        client = SimpleNamespace()
        client.build_client = lambda *args: self.fail('Available TV captions must not request another client')
        self.subtitles.sub_selection = SELECTIONS['all']
        details, selected = namespace['_process_captions'](client, self.subtitles, {'tv_legacy': {
            'client': legacy, 'captions': {'playerCaptionsTracklistRenderer': {'captionTracks': [caption('en')]}}
        }})
        self.assertEqual('en', details['default'])
        self.assertIn('en', selected)


if __name__ == '__main__':
    unittest.main()
