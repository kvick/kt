from base64 import b64decode
from base64 import b64encode
from functools import partial
import sys
try:
    from urllib.parse import quote_from_bytes
    from urllib.parse import unquote_to_bytes
    from urllib.parse import urlencode
except ImportError:
    from urllib import quote as quote_from_bytes
    from urllib import unquote as unquote_to_bytes
    from urllib import urlencode

import requests

from ._binary import decode
from ._binary import encode
from .exceptions import ProtocolError
from .exceptions import ServerError


IS_PY2 = sys.version_info[0] == 2

if not IS_PY2:
    unicode = str


quote_b = partial(quote_from_bytes, safe='')
unquote_b = partial(unquote_to_bytes, safe='')


def decode_from_content_type(content_type):
    if content_type.endswith('colenc=B'):
        return b64decode
    elif content_type.endswith('colenc=U'):
        return unquote_b


class HttpProtocol(object):
    _content_type = 'text/tab-separated-values; colenc=B'

    def __init__(self, client):
        self.client = client
        self._prefix = 'http://%s:%s/rpc' % (self.client._host,
                                             self.client._port)
        self._session = None

    def open(self):
        if self._session is not None:
            return False

        self._session = requests.Session()
        self._session.headers['Content-Type'] = self._content_type
        return True

    def close(self):
        if self._session is None:
            return False

        self._session = None
        return True

    def _encode_keys_values(self, data):
        accum = []
        for key, value in data.items():
            bkey = encode(key)
            bvalue = encode(value)
            accum.append(b'%s\t%s' % (b64encode(bkey), b64encode(bvalue)))

        return b'\n'.join(accum)

    def _encode_keys(self, keys):
        accum = []
        for key in keys:
            accum.append(b'%s\t' % b64encode(b'_' + encode(key)))
        return b'\n'.join(accum)

    def _decode_response(self, tsv, content_type):
        decoder = decode_from_content_type(content_type)
        accum = {}
        for line in tsv.split(b'\n'):
            try:
                key, value = line.split(b'\t', 1)
            except ValueError:
                continue

            if decoder is not None:
                key, value = decoder(key), decoder(value)

            if self.client._decode_keys:
                key = decode(key)
            accum[key] = value

        return accum

    def path(self, url):
        return ''.join((self._prefix, url))

    def _post(self, path, body, db):
        if db is not False:
            path += '?DB=%s' % db
        return self._session.post(self.path(path), data=body)

    def request(self, path, data, db=None, allowed_status=None):
        if isinstance(data, dict):
            body = self._encode_keys_values(data)
        elif isinstance(data, list):
            body = self._encode_keys(data)
        else:
            body = data

        r = self._post(path, body, db)
        if r.status_code != 200:
            if allowed_status is None or r.status_code not in allowed_status:
                raise ProtocolError('protocol error [%s]' % r.status_code)

        return (self._decode_response(r.content, r.headers['content-type']),
                r.status_code)

    def status(self, db=None):
        resp, status = self.request('/status', {}, db)
        return resp

    def report(self):
        resp, status = self.request('/report', {}, None)
        return resp

    def clear(self, db=None):
        resp, status = self.request('/clear', {}, db)
        return status == 200

    def play_script(self, name, __data=None, **params):
        if __data is not None:
            params.update(__data)

        accum = {}
        for key, value in params.items():
            accum['_%s' % key] = self.client._encode_value(value)

        resp, status = self.request('/play_script', accum, False, (450,))
        if status == 450:
            return

        accum = {}
        for key, value in resp.items():
            accum[key[1:]] = self.client._decode_value(value)
        return accum

    def get(self, key, db=None):
        resp, status = self.request('/get', {'key': key}, db, (450,))
        if status == 450:
            return
        value = resp['value' if self.client._decode_keys else b'value']
        return self.client._decode_value(value)

    def _simple_write(self, cmd, key, value, db=None, expire_time=None):
        data = {'key': key, 'value': self.client._encode_value(value)}
        if expire_time is not None:
            data['xt'] = str(expire_time)
        resp, status = self.request('/%s' % cmd, data, db, (450,))
        return status != 450

    def set(self, key, value, db=None, expire_time=None):
        return self._simple_write('set', key, value, db, expire_time)

    def add(self, key, value, db=None, expire_time=None):
        return self._simple_write('add', key, value, db, expire_time)

    def replace(self, key, value, db=None, expire_time=None):
        return self._simple_write('replace', key, value, db, expire_time)

    def append(self, key, value, db=None, expire_time=None):
        return self._simple_write('append', key, value, db, expire_time)

    def remove(self, key, db=None):
        resp, status = self.request('/remove', {'key': key}, db, (450,))
        return status != 450

    def check(self, key, db=None):
        resp, status = self.request('/check', {'key': key}, db, (450,))
        return status != 450

    def set_bulk(self, data, db=0, expire_time=None):
        accum = {}
        if expire_time is not None:
            accum['xt'] = str(expire_time)

        # Keys must be prefixed by "_".
        for key, value in data.items():
            accum['_%s' % key] = self.client._encode_value(value)

        resp, status = self.request('/set_bulk', accum, db)
        return resp

    def get_bulk(self, keys, db=None):
        resp, status = self.request('/get_bulk', keys, db)

        n = resp.pop('num' if self.client._decode_keys else b'num', b'0')
        if n == b'0':
            return {}

        accum = {}
        for key, value in resp.items():
            accum[key[1:]] = self.client._decode_value(value)
        return accum

    def remove_bulk(self, keys, db=None):
        resp, status = self.request('/remove_bulk', keys, db)
        return int(resp.pop('num' if self.client._decode_keys else b'num'))

    def seize(self, key, db=None):
        resp, status = self.request('/seize', {'key': key}, db, (450,))
        if status == 450:
            return
        value = resp['value' if self.client._decode_keys else b'value']
        return self.client._decode_value(value)

    def cas(self, key, old_val, new_val, db=None, expire_time=None):
        if old_val is None and new_val is None:
            raise ValueError('old value and/or new value must be specified.')

        data = {'key': key}
        if old_val is not None:
            data['oval'] = self.client._encode_value(old_val)
        if new_val is not None:
            data['nval'] = self.client._encode_value(new_val)
        if expire_time is not None:
            data['xt'] = str(expire_time)

        resp, status = self.request('/cas', data, db, (450,))
        return status != 450