import base64
import os
import tempfile

from .common import InfoExtractor
from ..cookies import YoutubeDLCookieJar
from ..utils import (
    ExtractorError,
    encode_base_n,
    get_elements_by_class,
    int_or_none,
    join_nonempty,
    merge_dicts,
    parse_duration,
    str_to_int,
    url_or_none,
)


class EpornerIE(InfoExtractor):
    _VALID_URL = r'https?://(?:www\.)?eporner\.com/(?:(?:hd-porn|embed)/|video-)(?P<id>\w+)(?:/(?P<display_id>[\w-]+))?'
    _TESTS = [{
        'url': 'http://www.eporner.com/hd-porn/95008/Infamous-Tiffany-Teen-Strip-Tease-Video/',
        'md5': '39d486f046212d8e1b911c52ab4691f8',
        'info_dict': {
            'id': 'qlDUmNsj6VS',
            'display_id': 'Infamous-Tiffany-Teen-Strip-Tease-Video',
            'ext': 'mp4',
            'title': 'Infamous Tiffany Teen Strip Tease Video',
            'description': 'md5:764f39abf932daafa37485eb46efa152',
            'timestamp': 1232520922,
            'upload_date': '20090121',
            'duration': 1838,
            'view_count': int,
            'age_limit': 18,
        },
    }, {
        # New (May 2016) URL layout
        'url': 'http://www.eporner.com/hd-porn/3YRUtzMcWn0/Star-Wars-XXX-Parody/',
        'only_matching': True,
    }, {
        'url': 'http://www.eporner.com/hd-porn/3YRUtzMcWn0',
        'only_matching': True,
    }, {
        'url': 'http://www.eporner.com/embed/3YRUtzMcWn0',
        'only_matching': True,
    }, {
        'url': 'https://www.eporner.com/video-FJsA19J3Y3H/one-of-the-greats/',
        'only_matching': True,
    }]

    @staticmethod
    def _first_extractor_arg(args, key):
        values = args.get(key)
        if isinstance(values, (list, tuple)):
            return values[0] if values else None
        return values

    def _eporner_extractor_args(self):
        return (self.get_param('extractor_args') or {}).get('eporner') or {}

    def _eporner_impersonate(self):
        return self._first_extractor_arg(self._eporner_extractor_args(), 'impersonate')

    @staticmethod
    def _looks_like_cookie_data(cookie_source):
        return any(marker in cookie_source for marker in ('\n', '\t')) or cookie_source.lstrip().startswith('# Netscape HTTP Cookie File')

    @staticmethod
    def _normalise_cookie_data(cookie_data):
        if '\\n' in cookie_data and '\n' not in cookie_data:
            cookie_data = cookie_data.replace('\\n', '\n')
        if '\\t' in cookie_data and '\t' not in cookie_data:
            cookie_data = cookie_data.replace('\\t', '\t')
        return cookie_data

    def _eporner_cookie_source(self):
        cookiefile = self._first_extractor_arg(self._eporner_extractor_args(), 'cookiefile')
        if cookiefile:
            return cookiefile, 'extractor-arg cookiefile'

        for env_var in ('YTDLP_EPORNER_COOKIEFILE', 'EPORNER_COOKIEFILE', 'EPORNER_COOKIE_FILE'):
            cookiefile = os.environ.get(env_var)
            if cookiefile:
                return cookiefile, env_var

        for env_var in ('YTDLP_EPORNER_COOKIES_B64', 'EPORNER_COOKIES_B64'):
            cookie_data_b64 = os.environ.get(env_var)
            if cookie_data_b64:
                try:
                    return base64.b64decode(cookie_data_b64).decode(), env_var
                except Exception as error:
                    raise ExtractorError(f'Unable to decode eporner cookies from {env_var}: {error}', expected=True)

        for env_var in ('YTDLP_EPORNER_COOKIES', 'EPORNER_COOKIES'):
            cookie_source = os.environ.get(env_var)
            if cookie_source:
                return cookie_source, env_var

        return None, None

    def _load_eporner_cookiejar(self, cookie_source, source_name):
        if self._looks_like_cookie_data(cookie_source):
            cookie_data = self._normalise_cookie_data(cookie_source)
            tmp_cookiefile = None
            try:
                with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False) as tmp:
                    tmp.write(cookie_data)
                    tmp_cookiefile = tmp.name
                jar = YoutubeDLCookieJar(tmp_cookiefile)
                jar.load(ignore_discard=True, ignore_expires=True)
            except OSError as error:
                raise ExtractorError(f'Unable to load eporner cookies from {source_name}: {error}', expected=True)
            finally:
                if tmp_cookiefile:
                    try:
                        os.remove(tmp_cookiefile)
                    except OSError:
                        pass
            return jar

        cookiefile = os.path.expanduser(cookie_source)
        jar = YoutubeDLCookieJar(cookiefile)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except OSError as error:
            raise ExtractorError(f'Unable to load eporner cookie file {cookiefile!r}: {error}', expected=True)
        return jar

    def _real_initialize(self):
        cookie_source, source_name = self._eporner_cookie_source()
        if not cookie_source:
            return

        jar = self._load_eporner_cookiejar(cookie_source, source_name)
        cookie_count = 0
        for cookie in jar:
            if (cookie.domain or '').lstrip('.').lower().endswith('eporner.com'):
                self.cookiejar.set_cookie(cookie)
                cookie_count += 1

        if cookie_count:
            self.write_debug(f'Loaded {cookie_count} eporner cookies from {source_name}')
        else:
            self.report_warning(f'No eporner.com cookies found in {source_name}')

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        video_id = mobj.group('id')
        display_id = mobj.group('display_id') or video_id
        impersonate = self._eporner_impersonate()

        webpage, urlh = self._download_webpage_handle(url, display_id, impersonate=impersonate)

        video_id = self._match_id(urlh.url)

        vid_hash = self._search_regex(
            r'hash\s*[:=]\s*["\']([\da-f]{32})', webpage, 'hash')

        title = self._og_search_title(webpage, default=None) or self._html_search_regex(
            r'<title>(.+?) - EPORNER', webpage, 'title')

        # Reverse engineered from vjs.js
        def calc_hash(s):
            return ''.join(encode_base_n(int(s[lb:lb + 8], 16), 36) for lb in range(0, 32, 8))

        video = self._download_json(
            f'http://www.eporner.com/xhr/video/{video_id}',
            display_id, note='Downloading video JSON',
            query={
                'hash': calc_hash(vid_hash),
                'device': 'generic',
                'domain': 'www.eporner.com',
                'fallback': 'false',
            }, impersonate=impersonate)

        if video.get('available') is False:
            raise ExtractorError(
                '{} said: {}'.format(self.IE_NAME, video['message']), expected=True)

        sources = video['sources']

        formats = []
        has_av1 = bool(get_elements_by_class('download-av1', webpage))
        for kind, formats_dict in sources.items():
            if not isinstance(formats_dict, dict):
                continue
            for format_id, format_dict in formats_dict.items():
                if not isinstance(format_dict, dict):
                    continue
                src = url_or_none(format_dict.get('src'))
                if not src or not src.startswith('http'):
                    continue
                if kind == 'hls':
                    formats.extend(self._extract_m3u8_formats(
                        src, display_id, 'mp4', entry_protocol='m3u8_native',
                        m3u8_id=kind, fatal=False))
                else:
                    height = int_or_none(self._search_regex(
                        r'(\d+)[pP]', format_id, 'height', default=None))
                    fps = int_or_none(self._search_regex(
                        r'(\d+)fps', format_id, 'fps', default=None))

                    formats.append({
                        'url': src,
                        'format_id': format_id,
                        'height': height,
                        'fps': fps,
                    })
                    if has_av1:
                        formats.append({
                            'url': src.replace('.mp4', '-av1.mp4'),
                            'format_id': join_nonempty('av1', format_id),
                            'height': height,
                            'fps': fps,
                            'vcodec': 'av1',
                        })

        if impersonate:
            for f in formats:
                f.setdefault('impersonate', impersonate)

        json_ld = self._search_json_ld(webpage, display_id, default={})

        duration = parse_duration(self._html_search_meta(
            'duration', webpage, default=None))
        view_count = str_to_int(self._search_regex(
            r'id=["\']cinemaviews1["\'][^>]*>\s*([0-9,]+)',
            webpage, 'view count', default=None))

        return merge_dicts(json_ld, {
            'id': video_id,
            'display_id': display_id,
            'title': title,
            'duration': duration,
            'view_count': view_count,
            'formats': formats,
            'age_limit': 18,
        })
