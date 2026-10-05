"""Exercise manifest preference propagation and the real playback callback."""
import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlunsplit

import test_adaptive_manifest as manifests

ROOT = Path(__file__).resolve().parents[1]
MONITOR = ROOT / 'resources/lib/youtube_plugin/kodion/monitors/player_monitor.py'
PLAY = ROOT / 'resources/lib/youtube_plugin/youtube/helper/yt_play.py'
CONSTANTS = ('BUSY_FLAG', 'CHANNEL_ID', 'INCOGNITO', 'PLAYBACK_INIT', 'PLAYER_DATA',
             'PLAY_FORCE_AUDIO', 'PLAY_PROMPT_QUALITY', 'PLAY_USING', 'SCREENSAVER',
             'SERVER_WAKEUP', 'TRAKT_PAUSE_FLAG', 'VIDEO_ID')


def load_playback():
    tree = ast.parse(MONITOR.read_text())
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PlayerMonitor')
    methods = {'_audio_language_family', '_apply_preferred_audio', '_apply_preferred_subtitle',
               'onAVStarted', 'onAVChange'}
    body = [n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    client = ast.ClassDef(name='Monitor', bases=[], keywords=[], body=body, decorator_list=[])
    play = next(n for n in ast.parse(PLAY.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == '_play_stream')
    module = ast.fix_missing_locations(ast.Module(body=[client, play], type_ignores=[]))
    namespace = dict(json=json, urlunsplit=urlunsplit, urlencode=urlencode,
                     PATHS=SimpleNamespace(PLAY='/play/'),
                     utils=SimpleNamespace(update_play_info=lambda *args: None), v3=None,
                     _select_stream=lambda context, streams, **kwargs: streams[0])
    namespace.update({name: name.lower() for name in CONSTANTS})
    exec(compile(module, str(MONITOR), 'exec'), namespace)
    return namespace


class AudioStartupTests(unittest.TestCase):
    def setUp(self):
        self.manifest = manifests.ManifestTests()
        self.manifest.setUp()
        self.addCleanup(self.manifest.doCleanups)
        self.namespace = load_playback()
        self.properties = {}
        self.rpc_calls = []
        self.selected = []

        def pop_property(key, process=None, **kwargs):
            value = self.properties.pop(key, None)
            return process(value) if process and value is not None else value

        def set_property(key, value=None, process=None, **kwargs):
            self.properties[key] = process(value) if process else value

        self.ui = SimpleNamespace(
            busy_dialog_active=lambda: False,
            get_property=lambda *a, **kw: False,
            pop_property=pop_property, set_property=set_property,
            clear_property=lambda *a, **kw: None,
        )
        self.playing_file = ''
        self.actual_streams = []
        self.current = -1
        self.actual_subtitles = []
        self.current_subtitle = -1
        self.subtitle_enabled = False
        self.selected_subtitles = []

        def rpc(**kwargs):
            self.rpc_calls.append(kwargs)
            if kwargs['method'] == 'Player.GetActivePlayers':
                return {'result': [{'playerid': 1, 'type': 'video'}]}
            return {'result': {'audiostreams': self.actual_streams,
                               'currentaudiostream': {'index': self.current},
                               'subtitles': self.actual_subtitles,
                               'currentsubtitle': {'index': self.current_subtitle},
                               'subtitleenabled': self.subtitle_enabled}}

        self.namespace['jsonrpc'] = rpc
        self.namespace['PlayerMonitorThread'] = lambda *args: SimpleNamespace()
        self.monitor = self.namespace['Monitor']()
        self.monitor._ui = self.ui
        self.monitor._provider = self.monitor._monitor = None
        self.monitor._context = SimpleNamespace(is_plugin_path=lambda url, path:
                                               url.startswith('plugin://plugin.video.youtube'+path))
        self.monitor.threads = []
        self.monitor.cleanup_threads = lambda: None
        self.monitor.getPlayingFile = lambda: self.playing_file
        self.monitor.getTime = lambda: 0
        self.monitor.getTotalTime = lambda: 10
        self.monitor.setAudioStream = self.selected.append
        self.monitor.setSubtitleStream = self.selected_subtitles.append
        self.monitor.log = SimpleNamespace(debug=lambda *a, **kw: None)

    def start_data(self, tracks, kodi_languages=None, preferred_subtitle=None):
        root, _, info = self.manifest.manifest([manifests.video(699, 1920, 1080)] + tracks)
        client = self.manifest.client
        client.FORMAT = {'9999': {'container': 'MPD', 'title': '', 'video': {}, 'audio': {}}}
        stream = client._get_stream_format('9999', info=info, url='http://localhost/fixture.mpd')
        stream.setdefault('meta', {})['preferred_subtitle'] = preferred_subtitle
        self.playing_file = stream['url']
        self.actual_streams = [
            {'index': i, 'language': (kodi_languages or {}).get(s.get('lang'), s.get('lang')),
             'isdefault': s.get('default') == 'true', 'isoriginal': s.get('original') == 'true'}
            for i, s in enumerate(root.findall('.//d:AdaptationSet[@contentType="audio"]', manifests.NS))
        ]

        class Item:
            def __init__(self, **kwargs):
                self.uri = kwargs['uri']
            def get_uri(self):
                return self.uri

        self.namespace['VideoItem'] = self.namespace['AudioItem'] = Item
        settings = SimpleNamespace(default_player_web_urls=lambda: False,
                                   ask_for_video_quality=lambda: False, audio_only=lambda: False,
                                   use_mpd_videos=lambda: True, PLAY_SUGGESTED='suggested',
                                   get_bool=lambda *a: False, use_remote_history=lambda: False,
                                   use_local_history=lambda: False)
        context = SimpleNamespace(get_ui=lambda: self.ui,
                                  get_params=lambda: {'video_id': 'fixture'},
                                  get_settings=lambda: settings, ipc_exec=lambda *a, **kw: True,
                                  send_notification=lambda *a: None)
        provider = SimpleNamespace(get_client=lambda context: SimpleNamespace(
            load_stream_info=lambda **kw: ([stream], None)))
        self.namespace['_play_stream'](provider, context)
        return json.loads(self.properties['player_data'])

    def test_real_chain_selects_english_dub_once_then_preserves_manual_changes(self):
        data = self.start_data([manifests.audio('ja.4'), manifests.audio('en-US.10'), manifests.audio('en.3')],
                               {'ja': 'jpn', 'en': 'eng'})
        self.assertEqual({'language': 'en', 'role': '3'}, data['preferred_audio'])
        preferred = next(s['index'] for s in self.actual_streams if s['isdefault'])
        original = next(s['index'] for s in self.actual_streams if s['language'] == 'jpn')
        self.current = original
        self.monitor.onAVStarted()
        self.assertEqual([preferred], self.selected)
        self.assertEqual(1, len(self.monitor.threads))
        self.assertNotIn('player_data', self.properties)

        # A user switches back to Japanese; AV changes and repeated startup
        # callbacks must not undo it. A new playback can apply the rule again.
        self.current = original
        self.monitor.onAVChange()
        self.monitor.onAVStarted()
        self.assertEqual([preferred], self.selected)
        self.properties['player_data'] = json.dumps(data)
        self.monitor.onAVStarted()
        self.assertEqual([preferred, preferred], self.selected)

    def test_chinese_default_retains_auto_dub_role_with_iso_language_variants(self):
        self.manifest.features.add('prefer_auto_dub')
        for actual_language in ('chi', 'zho-Hant', 'ZH_tw'):
            with self.subTest(actual_language=actual_language):
                data = self.start_data([manifests.audio('zh-Hant.4'), manifests.audio('zh-Hant.10'),
                                        manifests.audio('ja.4')], {'zh-Hant': actual_language})
                self.assertEqual({'language': 'zh-Hant', 'role': '10'}, data['preferred_audio'])
                preferred = next(s['index'] for s in self.actual_streams if s['isdefault'])
                self.current = next(s['index'] for s in self.actual_streams if not s['isdefault'])
                self.monitor.onAVStarted()
                self.assertEqual(preferred, self.selected[-1])

    def test_unrelated_playback_and_other_languages_do_not_change_audio(self):
        self.start_data([manifests.audio('ja.4'), manifests.audio('en.3')])
        self.playing_file = 'http://localhost/unrelated.mpd'
        self.monitor.onAVStarted()
        self.assertFalse(self.rpc_calls)
        self.assertFalse(self.selected)
        data = self.start_data([manifests.audio('fr.4'), manifests.audio('en.3')])
        self.assertIsNone(data['preferred_audio'])
        self.monitor.onAVStarted()
        self.assertFalse(self.rpc_calls)
        self.assertFalse(self.selected)

    def test_resolved_plugin_url_matches_the_youtube_playback_request(self):
        self.start_data([manifests.audio('ja.4'), manifests.audio('en.3')])
        self.playing_file = 'plugin://plugin.video.youtube/play/?video_id=fixture'
        preferred = next(s['index'] for s in self.actual_streams if s['isdefault'])
        self.monitor.onAVStarted()
        self.assertEqual([preferred], self.selected)

    def test_already_selected_or_ambiguous_default_is_not_changed(self):
        self.start_data([manifests.audio('ja.4'), manifests.audio('en.3')])
        self.current = next(s['index'] for s in self.actual_streams if s['isdefault'])
        self.monitor.onAVStarted()
        self.assertFalse(self.selected)
        self.start_data([manifests.audio('zh.4'), manifests.audio('zh.3')])
        for stream in self.actual_streams:
            stream['isdefault'] = True
        self.monitor.onAVStarted()
        self.assertFalse(self.selected)

    def test_jsonrpc_failure_does_not_interrupt_playback_start(self):
        self.start_data([manifests.audio('ja.4'), manifests.audio('en.3')])
        def failing_rpc(**kwargs):
            raise RuntimeError('player unavailable')
        self.namespace['jsonrpc'] = failing_rpc
        self.monitor.onAVStarted()
        self.assertFalse(self.selected)
        self.assertEqual(1, len(self.monitor.threads))

    def test_selected_external_subtitle_is_enabled_once_and_manual_off_is_preserved(self):
        data = self.start_data([manifests.audio('fr.4')], preferred_subtitle={
            'language': 'en', 'filename': 'fixture.asr.en.vtt'})
        self.assertEqual('en', data['preferred_subtitle']['language'])
        self.actual_subtitles = [
            {'index': 0, 'language': 'ar', 'name': 'fixture asr (External)'},
            {'index': 1, 'language': 'en', 'name': 'fixture asr (External)'},
        ]
        self.current_subtitle = 0
        self.monitor.onAVStarted()
        self.assertEqual([1], self.selected_subtitles)
        self.subtitle_enabled = False
        self.monitor.onAVChange()
        self.monitor.onAVStarted()
        self.assertEqual([1], self.selected_subtitles)

    def test_current_subtitle_is_enabled_when_forced_only_initially_hides_it(self):
        self.start_data([manifests.audio('fr.4')], preferred_subtitle={'language': 'zh-Hans'})
        self.actual_subtitles = [{'index': 2, 'language': 'zho', 'name': 'Chinese'}]
        self.current_subtitle = 2
        self.monitor.onAVStarted()
        self.assertEqual([2], self.selected_subtitles)

    def test_no_successful_subtitle_selection_does_not_enable_other_tracks(self):
        self.start_data([manifests.audio('fr.4')])
        self.actual_subtitles = [{'index': 0, 'language': 'ar', 'name': 'Arabic'}]
        self.monitor.onAVStarted()
        self.assertFalse(self.selected_subtitles)

    def test_subtitle_preference_does_not_apply_to_another_playback(self):
        self.start_data([manifests.audio('fr.4')], preferred_subtitle={'language': 'en'})
        self.actual_subtitles = [{'index': 1, 'language': 'en', 'name': 'English'}]
        self.playing_file = 'http://localhost/unrelated.mpd'
        self.monitor.onAVStarted()
        self.assertFalse(self.selected_subtitles)


if __name__ == '__main__':
    unittest.main()
