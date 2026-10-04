"""Run the real manifest methods without requiring a Kodi installation."""
import ast
import html
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlencode, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'resources/lib/youtube_plugin/youtube/client/player_client.py'
SETTINGS = ROOT / 'resources/lib/youtube_plugin/kodion/settings/abstract_settings.py'
METHODS = {'_hdr_transfer', '_process_adaptive_streams', '_generate_mpd_manifest'}
CONSTANTS = {'INTEGER_FPS_SCALE', 'FRACTIONAL_FPS_SCALE', 'QUALITY_FACTOR', 'LANG_ROLE_DETAILS'}


def load_client():
    tree = ast.parse(SOURCE.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'YouTubePlayerClient')
    body = [n for n in node.body if
            (isinstance(n, ast.FunctionDef) and n.name in METHODS) or
            (isinstance(n, ast.Assign) and any(t.id in CONSTANTS for t in n.targets if isinstance(t, ast.Name)))]
    client = ast.ClassDef(name='Client', bases=[], keywords=[], body=body, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[client], type_ignores=[]))
    namespace = dict(re_compile=re.compile, unquote=unquote, entity_escape=html.escape,
                     os_path=os.path, urlunsplit=urlunsplit, urlencode=urlencode,
                     VALUE_TO_STR={True: 'true', False: 'false'}, PATHS=SimpleNamespace(MPD='/mpd'),
                     get_connect_address=lambda *a, **kw: 'localhost:50152')
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace['Client'], namespace


Client, NAMESPACE = load_client()
settings_tree = ast.parse(SETTINGS.read_text())
QUALITY = next(ast.literal_eval(n.value) for cls in settings_tree.body if isinstance(cls, ast.ClassDef)
               for n in cls.body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == '_QUALITY_SELECTIONS' for t in n.targets))
NS = {'d': 'urn:mpeg:dash:schema:mpd:2011'}


def video(itag, width, height, transfer='COLOR_TRANSFER_CHARACTERISTICS_ARIB_STD_B67'):
    return dict(itag=itag, width=width, height=height, fps=30, bitrate=2000000,
                mimeType='video/mp4; codecs="av01.0.08M.10.0.110.09.18.09.0"',
                qualityLabel=str(height)+'p HDR', approxDurationMs='10000',
                colorInfo={'primaries': 'COLOR_PRIMARIES_BT2020', 'transferCharacteristics': transfer},
                url='https://example.invalid/video', indexRange={'start': '100', 'end': '200'},
                initRange={'start': '0', 'end': '99'})


def audio(track_id='ja.4'):
    result = dict(itag=140, bitrate=128000, audioChannels=2, audioSampleRate='48000',
                  mimeType='audio/mp4; codecs="mp4a.40.2"', approxDurationMs='10000',
                  url='https://example.invalid/audio', indexRange={'start': '100', 'end': '200'},
                  initRange={'start': '0', 'end': '99'})
    if track_id:
        result['audioTrack'] = {'id': track_id}
    return result


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.features = {'hdr', 'hfr', 'av01', 'mp4a'}
        self.selection = {'auto', 'list'}
        self.settings = SimpleNamespace(
            mpd_video_qualities=lambda: [q for key, q in sorted(QUALITY.items(), reverse=True) if key <= 6],
            stream_features=lambda: self.features, stream_select=lambda: self.selection)
        self.client = Client()
        self.client.BASE_PATH = self.temp.name
        self.client.video_id = 'fixture'
        self.client._audio_only = False
        self.client._language_base = ''
        self.client._language_prefer_default = False
        self.client.log = SimpleNamespace(debugging=False, debug=lambda *a, **kw: None)
        self.client._context = SimpleNamespace(
            get_settings=lambda: self.settings, inputstream_adaptive_capabilities=lambda: {'av01', 'mp4a'},
            get_language_name=lambda language: {'ja': 'Japanese', 'en-US': 'English & US'}.get(language, language),
            localize=lambda key: 'Automatic' if key == 'stream.automatic' else key)
        self.client._process_url_params = lambda url, **kw: url

        class File:
            def __init__(self, filename, mode):
                self.file = open(filename, mode)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.file.close()
            def write(self, data):
                return self.file.write(data)
        NAMESPACE['xbmcvfs'] = SimpleNamespace(File=File)

    def manifest(self, formats):
        groups = self.client._process_adaptive_streams({'fixture': {'client': {'headers': {}}, 'adaptive_fmts': formats}})
        url, details = self.client._generate_mpd_manifest(*groups, subs_data=None)
        self.assertTrue(url)
        return ET.parse(Path(self.temp.name)/'fixture.mpd').getroot(), groups, details

    def test_hlg_and_pq_remain_separate_with_correct_cicp(self):
        pq = video(700, 1920, 1080, 'COLOR_TRANSFER_CHARACTERISTICS_SMPTE2084')
        root, _, _ = self.manifest([video(699, 1920, 1080), pq, audio()])
        properties = root.findall('.//d:AdaptationSet[@contentType="video"]/d:SupplementalProperty', NS)
        self.assertEqual({'16', '18'}, {p.get('value') for p in properties})
        self.assertTrue(all(p.get('schemeIdUri') == 'urn:mpeg:mpegB:cicp:TransferCharacteristics' for p in properties))

    def test_auto_group_does_not_claim_a_fixed_4k_resolution(self):
        root, _, _ = self.manifest([video(401, 3840, 2160), video(400, 2560, 1440), audio()])
        sets = root.findall('.//d:AdaptationSet[@contentType="video"]', NS)
        automatic = next(s for s in sets if len(s.findall('d:Representation', NS)) == 2)
        self.assertIn('Automatic', automatic.get('name'))
        self.assertNotIn('4K', automatic.get('name'))
        fixed = next(s for s in sets if len(s.findall('d:Representation', NS)) == 1 and s.find('d:Representation', NS).get('height') == '1440')
        self.assertIn('1440', fixed.get('name'))
        self.assertNotIn('4K', fixed.get('name'))

    def test_multiaudio_same_itag_is_retained_and_xml_is_valid(self):
        root, _, details = self.manifest([video(699, 1920, 1080), audio('ja.4'), audio('en-US.10')])
        sets = root.findall('.//d:AdaptationSet[@contentType="audio"]', NS)
        self.assertEqual({'ja', 'en-US'}, {s.get('lang') for s in sets})
        ids = [r.get('id') for s in sets for r in s.findall('d:Representation', NS)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(details['multi_language'])
        self.assertIn('English & US', next(s for s in sets if s.get('lang') == 'en-US').get('name'))

    def test_unknown_audio_role_is_kept_as_alternate(self):
        root, _, _ = self.manifest([video(699, 1920, 1080), audio('en-US.42'), audio('ja.4')])
        self.assertTrue(root.findall('.//d:AdaptationSet[@lang="en-US"]', NS))

    def test_bt2020_sdr_and_empty_color_info_do_not_become_hdr(self):
        self.assertEqual(1, Client._hdr_transfer(video(1, 1920, 1080, 'COLOR_TRANSFER_CHARACTERISTICS_BT709'), 'vp9'))
        self.assertIsNone(Client._hdr_transfer({'colorInfo': {}}, 'vp9'))

    def test_av1_transfer_fallback(self):
        self.assertEqual(18, Client._hdr_transfer({}, 'av01.0.08M.10.0.110.09.18.09.0'))
        self.assertEqual(16, Client._hdr_transfer({}, 'av01.0.08M.10.0.110.09.16.09.0'))

    def test_sdr_does_not_emit_hdr_property(self):
        sdr = video(137, 1920, 1080, 'COLOR_TRANSFER_CHARACTERISTICS_BT709')
        root, _, _ = self.manifest([sdr, audio()])
        self.assertFalse(root.findall('.//d:SupplementalProperty', NS))

    def test_hdr_setting_is_respected(self):
        self.features.remove('hdr')
        sdr = video(137, 1920, 1080, 'COLOR_TRANSFER_CHARACTERISTICS_BT709')
        root, _, _ = self.manifest([video(699, 1920, 1080), sdr, audio()])
        self.assertEqual({'137'}, {r.get('id') for r in root.findall('.//d:AdaptationSet[@contentType="video"]/d:Representation', NS)})


if __name__ == '__main__':
    unittest.main()
