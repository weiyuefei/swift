# Copyright (c) 2015 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
The simple scheme for key derivation is as follows:
every path is associated with a key, where the key is derived from the
path itself in a deterministic fashion such that the key does not need to be
stored. Specifically, the key for any path is an HMAC of a root key and the
path itself, calculated using an SHA256 hash function::

  <path_key> = HMAC_SHA256(<root_secret>, <path>)
"""

import base64
import hashlib
import hmac
import os

from swift.common.middleware.crypto_utils import CRYPTO_KEY_CALLBACK
from swift.common.swob import Request, HTTPException
from swift.common.wsgi import WSGIContext


class KeyMasterContext(WSGIContext):
    def __init__(self, keymaster, account, container, obj):
        """
        :param keymaster: a Keymaster instance
        :param account: account name
        :param container: container name
        :param obj: object name
        """
        super(KeyMasterContext, self).__init__(keymaster.app)
        self.keymaster = keymaster
        self.account = account
        self.container = container
        self.obj = obj
        self._keys = None

    def fetch_crypto_keys(self):
        """
        Setup container and object keys based on the request path.

        Keys are derived from request path. The 'id' entry in the results dict
        includes the part of the path used to derived keys. Other keymaster
        implementations may use a different strategy to generate keys and may
        include a different type of 'id', so callers should treat the 'id' as
        opaque keymaster-specific data.

        :returns: A dict containing encryption keys for 'object' and
                  'container' and a key 'id'.
        """
        if self._keys:
            return self._keys

        self._keys = {}
        account_path = os.path.join(os.sep, self.account)

        if self.container:
            path = os.path.join(account_path, self.container)
            self._keys['container'] = self.keymaster.create_key(path)

            if self.obj:
                path = os.path.join(path, self.obj)
                self._keys['object'] = self.keymaster.create_key(path)

            # For future-proofing include a keymaster version number and the
            # path used to derive keys in the 'id' entry of the results. The
            # encrypter will persist this as part of the crypto-meta for
            # encrypted data and metadata. If we ever change the way keys are
            # generated then the decrypter could pass the persisted 'id' value
            # when it calls fetch_crypto_keys to inform the keymaster as to how
            # that particular data or metadata had its keys generated.
            # Currently we have no need to do that, so we are simply persisting
            # this information for future use.
            self._keys['id'] = {'v': '1', 'path': base64.b64encode(path)}

        return self._keys

    def _handle_request(self, req, start_response):
        req.environ[CRYPTO_KEY_CALLBACK] = self.fetch_crypto_keys
        resp = self._app_call(req.environ)
        start_response(self._response_status, self._response_headers,
                       self._response_exc_info)
        return resp

    def PUT(self, req, start_response):
        return self._handle_request(req, start_response)

    def POST(self, req, start_response):
        return self._handle_request(req, start_response)

    def GET(self, req, start_response):
        return self._handle_request(req, start_response)

    def HEAD(self, req, start_response):
        return self._handle_request(req, start_response)


class KeyMaster(object):

    def __init__(self, app, conf):
        self.app = app
        self.root_secret = conf.get('encryption_root_secret')
        try:
            self.root_secret = base64.b64decode(self.root_secret)
            if len(self.root_secret) < 32:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(
                'encryption_root_secret option in proxy-server.conf must be '
                'a base64 encoding of at least 32 raw bytes')

    def __call__(self, env, start_response):
        req = Request(env)

        try:
            parts = req.split_path(2, 4, True)
        except ValueError:
            return self.app(env, start_response)

        if hasattr(KeyMasterContext, req.method):
            # handle only those request methods that may require keys
            km_context = KeyMasterContext(self, *parts[1:])
            try:
                return getattr(km_context, req.method)(req, start_response)
            except HTTPException as err_resp:
                return err_resp(env, start_response)

        # anything else
        return self.app(env, start_response)

    def create_key(self, key_id):
        return hmac.new(self.root_secret, key_id,
                        digestmod=hashlib.sha256).digest()


def filter_factory(global_conf, **local_conf):
    conf = global_conf.copy()
    conf.update(local_conf)

    def keymaster_filter(app):
        return KeyMaster(app, conf)

    return keymaster_filter
